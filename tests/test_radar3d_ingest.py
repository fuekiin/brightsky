import datetime
import math
import shutil

import numpy as np
import pytest

from brightsky.radar3d import ingest as ingest_module
from brightsky.radar3d.grid import Grid
from brightsky.radar3d.icon import StepFields
from brightsky.radar3d.ingest import (
    KonradSource,
    Radar3DIngest,
    SweepSource,
)
from brightsky.radar3d.store import FrameStore
from brightsky.radar3d.sweeps import parse_sweep_name


CYCLE = datetime.datetime(2026, 9, 16, 11, 50, tzinfo=datetime.UTC)
CYCLE_LEN = datetime.timedelta(minutes=5)
LISTING = """
<html><body><h1>Index of /x/</h1><hr><pre><a href="../">../</a>
<a href="ras07-vol5minng01_sweeph5onem_dbzh_00-2026091611505700-isn-10873-hd5">ras07-...</a> 16-Sep-2026 11:51:20               62576
<a href="ras07-vol5minng01_sweeph5onem_dbzh_09-2026091611540200-isn-10873-hd5">ras07-...</a> 16-Sep-2026 11:54:30               53973
<a href="Beschreibung.pdf">Beschreibung.pdf</a> 01-Jan-2026 00:00 1234
</pre><hr></body></html>
"""  # noqa


def test_source_parses_listing_and_predicts_names():
    source = SweepSource('https://example.test/sweep_vol_z/')
    url = 'https://example.test/sweep_vol_z/isn/hdf5/filter_polarimetric/'
    assert source.site_url('isn') == url
    sweeps = source.parse_listing('isn', url, LISTING)
    assert [(s.tilt, u.rsplit('/', 1)[1][:40]) for s, u in sweeps] == [
        (0, 'ras07-vol5minng01_sweeph5onem_dbzh_00-20'),
        (9, 'ras07-vol5minng01_sweeph5onem_dbzh_09-20'),
    ]
    ts = datetime.datetime(2026, 9, 16, 11, 54, 2, tzinfo=datetime.UTC)
    assert source.predicted_url('isn', 9, '10873', ts) == url + \
        'ras07-vol5minng01_sweeph5onem_dbzh_09-2026091611540200-isn-10873-hd5'


def test_konrad_names():
    ts = KonradSource.parse_name('KONRAD3D_20260916T115000.xml')
    assert ts == CYCLE
    assert KonradSource.parse_name('readme.txt') is None


def _shifted(name, minutes):
    info = parse_sweep_name(name)
    ts = info.timestamp + datetime.timedelta(minutes=minutes)
    return name.replace(f'{info.timestamp:%Y%m%d%H%M%S}', f'{ts:%Y%m%d%H%M%S}')


class FakeSource:
    """Serves the isn fixture files, optionally holding some back. With
    `history`, the listing also shows the previous cycle (same files under
    names five minutes earlier), as a real 48 h listing would."""

    def __init__(self, sweep_dir, hold=(), history=False):
        self.files = {p.name: p for p in sweep_dir.glob('*-hd5')}
        if history:
            self.files.update({
                _shifted(name, -5): path for name, path in self.files.items()})
        self.hold = set(hold)
        self.downloads = 0
        self.listings = 0
        self.fetches = 0
        self.fetched_names = []
        self.base = 'fake://sweeps/'

    def site_url(self, site):
        return f'{self.base}{site}/'

    def list_site(self, site):
        self.listings += 1
        return [
            (parse_sweep_name(name), f'{self.site_url(site)}{name}')
            for name in sorted(self.files) if name not in self.hold
            and parse_sweep_name(name).site == site
        ]

    def predicted_url(self, site, tilt, wmo, timestamp):
        return self.site_url(site) + SweepSource.predicted_name(
            site, tilt, wmo, timestamp)

    def fetch(self, url, dest):
        self.fetches += 1
        name = url.rsplit('/', 1)[1]
        self.fetched_names.append(name)
        if name not in self.files or name in self.hold:
            return False
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(self.files[name], dest)
        return True

    def download(self, url, dest):
        self.downloads += 1
        shutil.copy(self.files[url.rsplit('/', 1)[1]], dest)


class FakeKonrad:

    def __init__(self, path):
        self.path = path

    def list_files(self):
        return [(CYCLE, 'fake://konrad/KONRAD3D_20260916T115000.xml')]

    def download(self, url, dest):
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(self.path, dest)


@pytest.fixture
def small_grid():
    clat, clon = 48.174705, 12.101779
    dlat = 50.0 / 111.195
    dlon = 50.0 / (111.195 * math.cos(math.radians(clat)))
    return Grid(clat - dlat, clat + dlat, clon - dlon, clon + dlon, 100, 100)


@pytest.fixture
def ingest(tmp_path, data_dir, small_grid):
    def make(hold=(), now=CYCLE + datetime.timedelta(minutes=5),
             history=False):
        indexed = {}
        source = FakeSource(data_dir / 'radar3d', hold, history)
        ing = Radar3DIngest(
            FrameStore(tmp_path / 'frames'), tmp_path / 'raw',
            grid=small_grid, sites=['isn'], source=source,
            cloud_grid=Grid(small_grid.min_lat, small_grid.max_lat,
                            small_grid.min_lon, small_grid.max_lon, 50, 50),
            konrad_source=FakeKonrad(
                data_dir / 'radar3d' / 'KONRAD3D_20260916T115000_3cells.xml'),
            index=lambda product, ts, path, sites:
                indexed.setdefault(product, []).append((ts, sites)),
            indexed=lambda product, since:
                {ts for ts, _ in indexed.get(product, [])},
            now=lambda: now,
        )
        ing.settings = dict(
            poll_interval=60, cycle_timeout=420, backfill_minutes=70,
            retention_hours=3, listing_interval=900, icon_steps=2,
            forecast_hours=2, min_free_gb=0.0)
        return ing, source, indexed
    return make


def test_complete_cycle_is_gridded_and_indexed(ingest, tmp_path):
    ing, source, indexed = ingest()
    ing.poll_once()
    assert indexed['rain'] == [(CYCLE, 1)]
    assert source.downloads == 10 and source.listings == 1
    vol = np.load(tmp_path / 'frames' / 'rain' / '20260916T1150Z.npy')
    assert vol.shape == (24, 100, 100) and vol.max() > 150
    assert not (tmp_path / 'raw' / '20260916T1150Z').exists()
    assert 'clouds' not in indexed              # no model loaded yet
    # A second poll neither re-lists, re-downloads nor re-grids
    ing.poll_once()
    assert indexed['rain'] == [(CYCLE, 1)] and source.downloads == 10
    assert source.listings == 1
    assert ing.schedule['isn'][9] == ('10873', 242)     # 11:54:02


def test_predicted_fetch_after_learning_the_schedule(ingest):
    hold = ['ras07-vol5minng01_sweeph5onem_dbzh_09-2026091611540200-isn'
            '-10873-hd5']
    ing, source, indexed = ingest(hold=hold, now=CYCLE + datetime.timedelta(
        minutes=4), history=True)
    ing.poll_once()                              # listing: 9 of 10 tilts
    assert source.listings == 1
    assert [ts for ts, _ in indexed['rain']] == [CYCLE - CYCLE_LEN]
    source.hold.clear()                          # the file appears on DWD
    ing.now = lambda: CYCLE + datetime.timedelta(minutes=4, seconds=30)
    ing.poll_once()                              # predicted name, no listing
    assert source.listings == 1 and source.fetches >= 1
    assert [ts for ts, _ in indexed['rain']] == [CYCLE - CYCLE_LEN, CYCLE]


def test_overdue_prediction_forces_one_listing_then_gives_up(ingest):
    hold = ['ras07-vol5minng01_sweeph5onem_dbzh_09-2026091611540200-isn'
            '-10873-hd5']
    ing, source, indexed = ingest(hold=hold, now=CYCLE + datetime.timedelta(
        minutes=4), history=True)
    ing.poll_once()
    ing.now = lambda: CYCLE + datetime.timedelta(minutes=6, seconds=30)
    ing.poll_once()                              # >120 s overdue → listing
    assert source.listings == 2
    assert ('isn', CYCLE, 9) in ing.gave_up
    source.fetched_names.clear()
    ing.poll_once()                  # given up: that tilt is not retried
    assert not [n for n in source.fetched_names if '_09-2026091611' in n]
    assert source.listings == 2
    ing.now = lambda: CYCLE + datetime.timedelta(seconds=421)
    ing.poll_once()                              # timed out: grid 9 tilts
    assert indexed['rain'][-1] == (CYCLE, 1)


def test_old_cycles_are_ignored_and_cleaned(ingest, tmp_path):
    ing, source, indexed = ingest(now=CYCLE + datetime.timedelta(hours=4))
    ing.poll_once()
    assert indexed == {} and source.downloads == 0
    store = ing.store
    store.write('rain', CYCLE, np.zeros((24, 100, 100), np.uint8))
    store.write_json('cells', CYCLE, {})
    ing.clean()
    assert store.timestamps('rain') == [] and store.timestamps('cells') == []


def test_failing_cycle_is_discarded_not_fatal(ingest, tmp_path):
    ing, source, indexed = ingest()

    def boom(cycle):
        raise RuntimeError('corrupt sweep')
    ing.process_cycle = boom
    ing.poll_once()                          # must not raise
    assert indexed == {} and CYCLE in ing.done
    assert not (tmp_path / 'raw' / '20260916T1150Z').exists()


def test_cells_are_parsed_and_indexed(ingest):
    ing, source, indexed = ingest()
    ing.poll_cells()
    assert indexed['cells'] == [(CYCLE, 3)]
    data = ing.store.read_json('cells', CYCLE)
    assert data['timestamp'] == '2026-09-16T11:50:00Z'
    assert {c['id'] for c in data['cells']} == {112, 26, 115}
    ing.poll_cells()                         # idempotent
    assert indexed['cells'] == [(CYCLE, 3)]


class _FakeConn:
    def __init__(self, sink):
        self.sink = sink

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FakeIcon:
    """Pretends every file exists; the loader is patched to synthesise."""

    def __init__(self):
        self.fetched = 0
        self.vars = set()

    def url(self, run, step, level, var):
        return f'fake://icon/{run:%H}/{var}/{step}/{level}'

    def fetch(self, url, dest):
        self.fetched += 1
        self.vars.add(url.split('/')[4])
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(b'')
        return True


def test_icon_run_produces_cloud_frames_on_rain_stamps(
        ingest, monkeypatch):
    run = datetime.datetime(2026, 9, 16, 9, tzinfo=datetime.UTC)
    ing, source, indexed = ingest(now=CYCLE + datetime.timedelta(minutes=5))
    ing.icon_source = FakeIcon()
    L, H, W = ing.cloud_grid.shape

    def fake_step(run_dir, run_, step, sampler, full_h, q_levels, w_levels):
        lwc = np.zeros((L, H, W), np.float32)
        lwc[8, 20:30, 20:30] = 0.6
        return StepFields(
            run_ + datetime.timedelta(hours=step), lwc.astype(np.float16),
            np.full((L, H, W), 60.0, np.float16),
            np.full((H, W), 5.0, np.float32), np.zeros((H, W), np.float32))
    monkeypatch.setattr(ingest_module, 'load_step', fake_step)
    ing.full_h = np.linspace(20000, 0, 65)[:, None, None] \
        + np.zeros((65, H, W))
    # rain first: no clouds yet
    ing.poll_once()
    assert 'clouds' not in indexed
    # steps 1..2 of the 09 run cover 10:00–11:00 only → not yet
    ing.poll_icon()
    assert 'clouds' not in indexed and run in ing.icon_loaded
    # a later run whose steps bracket 11:50
    ing.settings['icon_steps'] = 3
    later = datetime.datetime(2026, 9, 16, 9, tzinfo=datetime.UTC)
    ing.icon_loaded.discard(later)
    ing.poll_icon()
    assert indexed['clouds'] == [(CYCLE, None)]
    rg = ing.store.open('clouds', CYCLE)
    flow = ing.store.open('clouds_flow', CYCLE)
    assert rg.shape == (L, H, W, 2) and flow.shape == (H, W, 2)
    assert rg[8, :, :, 0].max() == 60 and rg[8, 25, 25, 1] == 125
    assert rg[7, :, :, 0].max() == 0                 # 0.6 g/m³ on slab 8
    assert flow[0, 0, 0] > 0 and flow[0, 0, 1] == 0  # wind blows east
    # the next gridded cycle gets its cloud frame immediately
    ing.done.discard(CYCLE)
    ing.poll_once()
    assert indexed['clouds'][-1] == (CYCLE, None)


def test_nowcast_forecast_after_the_newest_observed_frame(
        ingest, monkeypatch, tmp_path):
    ing, source, indexed = ingest(now=CYCLE + datetime.timedelta(minutes=5))
    ing.icon_source = FakeIcon()
    ing.settings['icon_steps'] = 3
    ing.settings['forecast_minutes'] = 15
    L, H, W = ing.cloud_grid.shape

    def fake_step(run_dir, run_, step, sampler, full_h, q_levels, w_levels):
        qr = np.zeros((L, H, W), np.float32)
        qr[4, 10, 10] = 1.0
        zero = np.zeros((L, H, W), np.float16)
        return StepFields(
            run_ + datetime.timedelta(hours=step),
            zero, zero,
            np.zeros((H, W), np.float32), np.zeros((H, W), np.float32),
            qr=qr.astype(np.float16), qs=zero, qg=zero)
    monkeypatch.setattr(ingest_module, 'load_step', fake_step)
    ing.full_h = np.linspace(20000, 0, 65)[:, None, None] \
        + np.zeros((65, H, W))
    ing.poll_once()                              # rain 11:50 gridded
    assert 'rain' in indexed and not [k for k in indexed if 'forecast' in k]
    ing.poll_icon()                              # 06 + 09 UTC runs load
    assert ing.icon_source.vars == {'qc', 'qi', 'qs', 'qg', 'qr', 'clc',
                                    'u', 'v'}
    newest = max(ing.icon_runs)
    key = f'forecast_rain/{newest:%Y%m%dT%HZ}-{CYCLE:%Y%m%dT%H%MZ}'
    stamps = [ts for ts, _ in indexed[key]]
    # 09 UTC run covers 10:00–12:00: 11:55 and 12:00 lie inside, 12:05 not
    assert stamps == [CYCLE + k * CYCLE_LEN for k in (1, 2)]
    rain = ing.store.open(key, stamps[0])
    assert rain.shape == (L, H, W)
    # +5 min (model weight 1/144): the observed echo persists where the
    # radar had it, and the model's 1 g/m³ blob shows only faintly
    from brightsky.radar3d import nowcast
    obs = np.load(tmp_path / 'frames' / 'rain' / '20260916T1150Z.npy')
    obs2 = nowcast.column_max(nowcast.maxpool2(obs))
    assert (nowcast.column_max(rain) > 0).sum() > 0.8 * (obs2 > 0).sum()
    assert 0 < rain[4, 10, 10] < 152
    later = ing.store.open(key, stamps[1])       # +10 min: blob brighter
    assert rain[4, 10, 10] < later[4, 10, 10] < 152
    flow = ing.store.open(
        f'forecast_flow/{newest:%Y%m%dT%HZ}-{CYCLE:%Y%m%dT%H%MZ}', stamps[0])
    assert flow.shape == (H, W, 2)               # the motion used
    assert (ing.store.root / f'forecast_clouds/{key.split("/")[1]}').is_dir()
    # the older run's frames were dropped; the same basis is not redone
    older = min(ing.icon_runs)
    assert not list((ing.store.root / 'forecast_rain').glob(
        f'{older:%Y%m%dT%HZ}-*'))
    assert ing.write_forecast(CYCLE) == 0
    # keys known only to the index (files gone) are dropped as well: the
    # newest two survive, older ones go
    ing.indexed_forecast_products = lambda: {
        'forecast_rain/20260901T00Z-20260901T0000Z',
        'forecast_rain/20260901T03Z-20260901T0300Z', key}
    dropped = []
    ing.index = lambda *a: None
    ing._index_db = ing.index                    # pretend the DB path
    monkeypatch.setattr(ingest_module, 'get_connection',
                        lambda: _FakeConn(dropped))
    monkeypatch.setattr(ingest_module, 'delete_index_before',
                        lambda conn, product, cutoff: dropped.append(product))
    ing.clean_forecasts(keep=key.split('/', 1)[1])
    assert 'forecast_rain/20260901T00Z-20260901T0000Z' in dropped
    assert 'forecast_rain/20260901T03Z-20260901T0300Z' not in dropped


def test_disk_floor_stops_downloads(ingest):
    ing, source, indexed = ingest()
    ing.settings['min_free_gb'] = 10 ** 6            # nothing has that much
    ing.poll_once()
    assert source.listings == 0 and source.downloads == 0
    ing.icon_source = FakeIcon()
    ing.poll_icon()
    assert ing.icon_source.fetched == 0


def test_stale_raw_icon_runs_are_removed(ingest, tmp_path):
    ing, source, indexed = ingest()
    stale = tmp_path / 'raw' / 'icon' / '20260901T00Z'
    stale.mkdir(parents=True)
    (stale / 'x.grib2.bz2').write_bytes(b'old')
    junk = tmp_path / 'raw' / 'icon' / 'notarun'
    junk.mkdir()
    ing.icon_source = FakeIcon()
    ing.settings['icon_steps'] = 0                   # nothing to load
    ing.poll_icon()
    assert not stale.exists() and not junk.exists()
