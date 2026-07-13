import datetime
from dateutil.tz import tzutc

import pytest

from brightsky.export import DBExporter, SYNOPExporter


SOURCES = [
    {
        'observation_type': 'historical',
        'lat': 10.1,
        'lon': 20.2,
        'height': 30.3,
        'wmo_station_id': '10001',
        'dwd_station_id': 'XYZ',
        'station_name': 'Münster',
    },
    {
        'observation_type': 'historical',
        'lat': 40.4,
        'lon': 50.5,
        'height': 60.6,
        'wmo_station_id': '10002',
        'dwd_station_id': None,
        'station_name': 'Aurich',
    },
    {
        'observation_type': 'historical',
        'lat': 60.6,
        'lon': 70.7,
        'height': 80.8,
        'wmo_station_id': '10003',
        'dwd_station_id': None,
        'station_name': 'Göttingen',
    },
]
RECORDS = [
    {
        'timestamp': datetime.datetime(2020, 8, 18, 18, tzinfo=tzutc()),
        'temperature': 291.25,
        'precipitation': 0.3,
    },
    {
        'timestamp': datetime.datetime(2020, 8, 18, 19, tzinfo=tzutc()),
        'temperature': 290.25,
        'precipitation': 0.2,
    },
    {
        'timestamp': datetime.datetime(2020, 8, 18, 20, tzinfo=tzutc()),
        'temperature': 289.25,
        'precipitation': 0.1,
    },
]
FINGERPRINT = {
    'url': 'https://example.com/source.zip',
    'last_modified': datetime.datetime(2020, 8, 19, 12, 34, tzinfo=tzutc()),
    'file_size': 12345
}


@pytest.fixture
def exporter():
    exporter = DBExporter()
    exporter.export(
        [
            {**SOURCES[0], **RECORDS[0]},
            {**SOURCES[1], **RECORDS[1]},
        ],
        fingerprint=FINGERPRINT)
    return exporter


def _query_sources(db):
    return db.fetch("SELECT * FROM sources ORDER BY id")


def _query_records(db, table='weather'):
    return db.fetch(
        f"""
        SELECT * FROM {table}
        JOIN sources ON {table}.source_id = sources.id
        ORDER BY sources.id, timestamp
        """)


def test_db_exporter_creates_new_sources(db, exporter):
    db_sources = _query_sources(db)
    assert len(db_sources) == 2
    for source, row in zip(SOURCES[:2], db_sources):
        for k, v in source.items():
            assert row[k] == v


def test_db_exporter_reuses_existing_sources(db, exporter):
    exporter.export([{**SOURCES[0], **RECORDS[2]}])
    db_sources = _query_sources(db)
    assert len(db_sources) == len(SOURCES[:2])
    # Exports with only known sources should also not increase the sources_id
    # sequence
    exporter.export([{**SOURCES[2], **RECORDS[2]}])
    db_sources = _query_sources(db)
    assert db_sources[2]['id'] == db_sources[0]['id'] + 2


def test_db_exporter_creates_new_records(db, exporter):
    db_records = _query_records(db)
    for record, source, row in zip(RECORDS[:2], SOURCES[:2], db_records):
        for k, v in source.items():
            assert row[k] == v
        for k, v in record.items():
            assert row[k] == v


def test_db_exporter_updates_existing_records(db, exporter):
    record = RECORDS[0].copy()
    record['precipitation'] = 10.
    record['cloud_cover'] = 50
    exporter.export([{**SOURCES[0], **record}])
    db_records = _query_records(db)
    for k, v in record.items():
        assert db_records[0][k] == v


def test_db_exporter_updates_parsed_files(db, exporter):
    parsed_files = db.fetch("SELECT * FROM parsed_files")
    assert len(parsed_files) == 1
    for k, v in FINGERPRINT.items():
        assert parsed_files[0][k] == v


def test_db_exporter_updates_source_first_last_record(db, exporter):
    db_sources = _query_sources(db)
    assert db_sources[0]['first_record'] == RECORDS[0]['timestamp']
    assert db_sources[0]['last_record'] == RECORDS[0]['timestamp']
    exporter.export([{**SOURCES[0], **RECORDS[2]}])
    db_sources = _query_sources(db)
    assert db_sources[0]['first_record'] == RECORDS[0]['timestamp']
    assert db_sources[0]['last_record'] == RECORDS[2]['timestamp']


def test_synop_exporter(db):
    exporter = SYNOPExporter()
    assert len(_query_records(db, table='current_weather')) == 0
    # Exporter needs to merge separate records for the same source and time
    record = RECORDS[0].copy()
    record['timestamp'] = datetime.datetime.now(datetime.UTC).replace(
        minute=0, second=0, microsecond=0, tzinfo=tzutc())
    extra_record = {
        'timestamp': record['timestamp'],
        'pressure_msl': 101010,
    }
    previous_record = RECORDS[1].copy()
    previous_record['timestamp'] = (
        record['timestamp'] - datetime.timedelta(minutes=30))
    exporter.export([
        {**SOURCES[0], **record},
        {**SOURCES[0], **extra_record},
        {**SOURCES[0], **previous_record},
    ])
    # Merges records for the same source and timestamp
    synop_records = _query_records(db, table='synop')
    assert len(synop_records) == 2
    assert synop_records[-1]['timestamp'] == record['timestamp']
    assert synop_records[-1]['temperature'] == record['temperature']
    assert synop_records[-1]['pressure_msl'] == extra_record['pressure_msl']
    # Updates current_weather
    # XXX: This test may be flaky as the concurrent refresh may not have
    #      finished yet. Can we somehow wait until the lock is released?
    current_weather_records = _query_records(db, table='current_weather')
    assert len(current_weather_records) == 1


def test_pollen_exporter(db, data_dir):
    from brightsky.parsers import PollenParser
    p = PollenParser()
    records = list(p.parse(data_dir / 's31fg.json'))
    p.exporter().export(iter(records), fingerprint=FINGERPRINT)
    rows = db.table('pollen')
    assert len(rows) == len(records) == 47
    key_fields = ['region_id', 'partregion_id', 'species', 'date']
    parsed_files = db.fetch("SELECT * FROM parsed_files")
    assert len(parsed_files) == 1
    # Re-export upserts instead of duplicating
    records[0]['index'] = '3'
    records[0]['severity'] = 3.
    p.exporter().export(iter(records))
    rows = db.table('pollen')
    assert len(rows) == 47
    updated = [
        row for row in rows
        if all(row[k] == records[0][k] for k in key_fields)]
    assert len(updated) == 1
    assert updated[0]['index'] == '3'
    assert updated[0]['severity'] == 3.
    assert updated[0]['sender'] == (
        'Deutscher Wetterdienst - Medizin-Meteorologie')


def test_biowetter_exporter(db, data_dir):
    from brightsky.parsers import BiowetterParser
    p = BiowetterParser()
    records = list(p.parse(data_dir / 'biowetter.json'))
    p.exporter().export(iter(records))
    rows = db.table('biowetter')
    assert len(rows) == len(records) == 10
    row = next(
        r for r in rows
        if r['zone_id'] == 'E' and str(r['date']) == '2026-07-13')
    # jsonb round-trip preserves the DWD effect tree
    assert isinstance(row['effects'], list)
    assert len(row['effects']) == 7
    assert row['effects'][0]['value'] == 'hohe Gefährdung'
    assert len(row['recommendations']) == 4
    assert row['sender'] == 'Medizin-Meteorologie'
    # Upsert, no duplication
    p.exporter().export(iter(records))
    assert len(db.table('biowetter')) == 10


def test_uv_index_exporter(db, data_dir):
    from brightsky.parsers import UVIndexParser
    p = UVIndexParser()
    records = list(p.parse(data_dir / 'uvi.json'))
    p.exporter().export(iter(records))
    rows = db.table('uv_index')
    assert len(rows) == len(records) == 9
    assert {
        r['uv_index'] for r in rows if r['city'] == 'Zugspitze'
    } == {9, 6, 5}
    p.exporter().export(iter(records))
    assert len(db.table('uv_index')) == 9


def test_thermal_hazard_exporter(db, data_dir):
    from brightsky.parsers import ThermalHazardParser
    p = ThermalHazardParser()
    records = list(p.parse(data_dir / 'gt.json'))
    p.exporter().export(iter(records))
    rows = db.table('thermal_hazard')
    assert len(rows) == len(records) == 26
    row = next(
        r for r in rows
        if r['city'] == 'Berlin' and r['timestamp'] == datetime.datetime(
            2026, 7, 13, 14, tzinfo=tzutc()))
    assert row['level'] == 'mittel'
    p.exporter().export(iter(records))
    assert len(db.table('thermal_hazard')) == 26
