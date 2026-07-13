# DWD Pollen (s31fg.json) First Vertical Slice — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ingest the DWD Pollenflug-Gefahrenindex (`s31fg.json`) end to end — dwdparse parser → new `pollen` table → exporter → lat/lon→region resolution → `/pollen` endpoint — with tests, on branch `nano-health` in both forks.

**Architecture:** The parsing core lives in our dwdparse fork (`HealthForecastParser` base + `PollenParser`, stdlib only). Bright Sky wraps it with a mixin + `PollenExporter` + migration `0019_pollen.sql` + `query.pollen()` with a `PollenRegionManager` (STRtree point-in-polygon over the DWD GeoServer `Pollenfluggebiete` layer) + a `/pollen` FastAPI endpoint. Health data never touches the `weather` table.

**Tech Stack:** Python 3.12 (uv venv at `.venv`), PostgreSQL (via docker-compose `postgres` service), FastAPI/pydantic, shapely STRtree, psycopg2 (export) / asyncpg (query), pytest.

## Global Constraints

- DWD is the sole data source; poll only `https://opendata.dwd.de/` (region polygons from the DWD GeoServer `maps.dwd.de`).
- Health data gets its own table (`pollen`) and endpoint (`/pollen`) — never the `weather` table.
- Preserve DWD attribution: `sender`, `last_update`, `next_update` flow through to the API response (CC BY 4.0 Quellenvermerk).
- dwdparse stays standard-library only.
- Additive changes only: new files, new table, appended registry entries; never alter existing tables or reshape upstream code (keeps `git rebase upstream/master` clean).
- Commits on `nano-health` in each repo; dwdparse changes and brightsky changes in separate commits.

## Verified facts this plan is built on (2026-07-13)

- Live `s31fg.json`: keys `last_update`/`next_update` (`"2026-07-13 11:00 Uhr"`, Europe/Berlin local), `name`, `sender`, `legend`, `content` = 27 objects with `region_id`, `partregion_id` (`-1` when no part-regions), `region_name`, `partregion_name`, `Pollen` = {species: {`today`, `tomorrow`, `dayafter_to`}}.
- Official spec (`Beschreibung_pollen_s31fg.pdf`, Stand 2025-04-28): species keys are exactly `Hasel, Erle, Esche, Birke, Graeser, Roggen, Beifuss, Ambrosia`; index values `0, 0-1, 1, 1-2, 2, 2-3, 3`; **missing values are `-1`**; updated daily in the morning.
- DWD GeoServer WFS layer `Pollenfluggebiete` (`https://maps.dwd.de/geoserver/wfs?SERVICE=WFS&VERSION=2.0.0&REQUEST=GetFeature&TYPENAMES=Pollenfluggebiete&OUTPUTFORMAT=json`): 54 GeoJSON features (EPSG:4326, lon/lat axis order), property `GF` = part-region id (or region id where `partregion_id == -1`), `GEN` = name. All 27 forecast regions map 1:1 onto distinct `GF` values — verified against the live s31fg.json. **No geometry needs to be invented.**
- Highest existing migration is `0018_alerts_status.sql` → new one is `0019_pollen.sql`.
- `brightsky/polling.py` `DWDPoller.urls` crawls directories recursively; `get_parser(filename)` regex-matches basenames (`re.match`). The health directory also contains `uvi.json`, `biowetter.json`, `gt.json` and PDFs — unmatched files are simply skipped.
- Brightsky DB tests need `BRIGHTSKY_DATABASE_URL`; they drop/create that database (conftest). Postgres runs via `docker compose up -d postgres` (password `pgpass`).

## Record schema (the contract between dwdparse and brightsky)

`dwdparse.parsers.PollenParser.parse(path)` yields one dict per (region × species × forecast day):

```python
{
    'region_id': 50,                  # int, DWD Hauptgebiet id
    'partregion_id': -1,              # int, DWD Teilbereich id, -1 if none
    'region_name': 'Brandenburg und Berlin',
    'partregion_name': None,          # str or None (None when partregion_id == -1)
    'species': 'graeser',             # lowercased DWD key: ambrosia|beifuss|birke|erle|esche|graeser|hasel|roggen
    'date': datetime.date(2026, 7, 13),   # last_update's Berlin-local date + 0/1/2
    'index': '1-2',                   # raw DWD Belastungsstufe string
    'severity': 1.5,                  # numeric mapping 0/0.5/1/1.5/2/2.5/3, None if unknown raw value
    'last_update': datetime(2026, 7, 13, 9, 0, tzinfo=utc),   # converted to UTC
    'next_update': datetime(2026, 7, 14, 9, 0, tzinfo=utc),
    'sender': 'Deutscher Wetterdienst - Medizin-Meteorologie',
}
```

Values of `-1` (missing) are skipped, not emitted.

---

### Task 0: Environment + fork hygiene (mostly done during inspection — verify and record)

**Files:** none (environment only)

- [x] **Step 1:** `uv venv --python 3.12 .venv` in brightsky; `uv pip install --python .venv/bin/python -r requirements.txt -r requirements-dev.txt -e ../dwdparse -e .` (done 2026-07-13; system python is 3.9, too old for `datetime.UTC`).
- [ ] **Step 2:** Fix dwdparse upstream remote (it wrongly points at our own fork):

```bash
cd ../dwdparse && git remote set-url upstream git@github.com:jdemaeyer/dwdparse.git
```

- [ ] **Step 3:** Start Postgres for tests: `docker compose up -d postgres` (in brightsky).

### Task 1: `PollenParser` in dwdparse

**Files:**
- Modify: `../dwdparse/dwdparse/parsers.py` (add `HealthForecastParser`, `PollenParser` after `CAPParser`; add `s31fg` entry to `get_parser`)
- Create: `../dwdparse/tests/data/s31fg.json` (trimmed live sample: regions (10,11) and (50,-1); one value doctored to `"-1"` to cover the skip path)
- Test: `../dwdparse/tests/test_parsers.py`

**Interfaces:**
- Consumes: `dwdparse.parsers.Parser` base class.
- Produces: `PollenParser().parse(path)` → iterator of record dicts per the schema above; `get_parser('s31fg.json') is PollenParser`.

- [ ] **Step 1: Create the fixture** — trim the live download (kept in scratchpad) to regions (10,11) and (50,-1), then set `content[1].Pollen.Ambrosia.dayafter_to = "-1"`:

```python
import json
src = json.load(open('<scratchpad>/s31fg.json'))
src['content'] = [r for r in src['content']
                  if (r['region_id'], r['partregion_id']) in [(10, 11), (50, -1)]]
src['content'].sort(key=lambda r: r['region_id'])
src['content'][1]['Pollen']['Ambrosia']['dayafter_to'] = '-1'
json.dump(src, open('tests/data/s31fg.json', 'w'), ensure_ascii=False, indent=1)
```

- [ ] **Step 2: Write the failing tests** in `tests/test_parsers.py` (import `PollenParser` at top; fixture `last_update` is `2026-07-13 11:00 Uhr`):

```python
def test_pollen_parser(data_dir):
    p = PollenParser()
    records = list(p.parse(data_dir / 's31fg.json'))
    # 2 regions x 8 species x 3 days, minus one '-1' value
    assert len(records) == 47
    first = next(
        r for r in records
        if r['region_id'] == 10 and r['species'] == 'graeser'
        and r['date'] == datetime.date(2026, 7, 13))
    assert first == {
        'region_id': 10,
        'partregion_id': 11,
        'region_name': 'Schleswig-Holstein und Hamburg',
        'partregion_name': 'Inseln und Marschen',
        'species': 'graeser',
        'date': datetime.date(2026, 7, 13),
        'index': '1-2',
        'severity': 1.5,
        'last_update': datetime.datetime(
            2026, 7, 13, 9, 0, tzinfo=datetime.timezone.utc),
        'next_update': datetime.datetime(
            2026, 7, 14, 9, 0, tzinfo=datetime.timezone.utc),
        'sender': 'Deutscher Wetterdienst - Medizin-Meteorologie',
    }
    # partregion_id -1 -> partregion_name None
    assert all(
        r['partregion_name'] is None
        for r in records if r['region_id'] == 50)
    # tomorrow / day-after dates
    dates = {r['date'] for r in records}
    assert dates == {
        datetime.date(2026, 7, 13),
        datetime.date(2026, 7, 14),
        datetime.date(2026, 7, 15),
    }
    # the doctored '-1' value is skipped
    assert not [
        r for r in records
        if r['region_id'] == 50 and r['species'] == 'ambrosia'
        and r['date'] == datetime.date(2026, 7, 15)]
```

(Adjust `first['index']`/`severity` to whatever the real trimmed sample contains — read the fixture, don't guess.) Also add `'s31fg.json': PollenParser` to `test_get_parser`'s `expected` dict.

- [ ] **Step 3:** Run `python -m pytest tests/test_parsers.py -k pollen` (brightsky venv) — expect ImportError/failure.
- [ ] **Step 4: Implement** in `dwdparse/parsers.py` after `CAPParser` (add `import zoneinfo` to the imports):

```python
class HealthForecastParser(Parser):
    """Base for DWD health forecast JSON products.

    Covers the shared shape of the files under
    opendata.dwd.de/climate_environment/health/alerts/ (pollen,
    Biowetter, Gefühlte Temperatur): a flat JSON object with
    'last_update'/'next_update' local-time strings, a 'sender'
    attribution, and a 'content' list keyed by DWD health regions.
    """

    TIMESTAMP_FORMAT = '%Y-%m-%d %H:%M Uhr'
    TIMEZONE = zoneinfo.ZoneInfo('Europe/Berlin')

    def parse(self, path):
        with open(path, encoding='utf-8') as f:
            data = json.load(f)
        yield from self.parse_data(data)

    def parse_data(self, data):
        raise NotImplementedError

    def parse_metadata(self, data):
        return {
            'last_update': self._parse_local_timestamp(
                data['last_update']).astimezone(datetime.timezone.utc),
            'next_update': self._parse_local_timestamp(
                data['next_update']).astimezone(datetime.timezone.utc),
            'sender': data['sender'],
        }

    def _parse_local_timestamp(self, value):
        return datetime.datetime.strptime(
            value, self.TIMESTAMP_FORMAT).replace(tzinfo=self.TIMEZONE)


class PollenParser(HealthForecastParser):

    FORECAST_DAY_KEYS = ['today', 'tomorrow', 'dayafter_to']
    INDEX_SEVERITIES = {
        '0': 0.,
        '0-1': .5,
        '1': 1.,
        '1-2': 1.5,
        '2': 2.,
        '2-3': 2.5,
        '3': 3.,
    }

    def parse_data(self, data):
        meta = self.parse_metadata(data)
        base_date = self._parse_local_timestamp(data['last_update']).date()
        for region in data['content']:
            yield from self.parse_region(region, base_date, meta)

    def parse_region(self, region, base_date, meta):
        for species, days in region['Pollen'].items():
            for offset, day_key in enumerate(self.FORECAST_DAY_KEYS):
                raw = days.get(day_key)
                if raw is None or raw == '-1':
                    continue
                severity = self.INDEX_SEVERITIES.get(raw)
                if severity is None:
                    self.logger.warning(
                        "Unknown pollen index value %r for %s in %s",
                        raw, species, region['region_name'])
                yield {
                    'region_id': region['region_id'],
                    'partregion_id': region['partregion_id'],
                    'region_name': region['region_name'],
                    'partregion_name': region['partregion_name'] or None,
                    'species': species.lower(),
                    'date': base_date + datetime.timedelta(days=offset),
                    'index': raw,
                    'severity': severity,
                    **meta,
                }
```

Add to `get_parser`'s dict: `r's31fg\.json$': PollenParser,`.

- [ ] **Step 5:** Run `python -m pytest tests/ -x` in dwdparse — all pass.
- [ ] **Step 6:** Commit in dwdparse: `feat: add PollenParser for DWD pollen hazard index (s31fg.json)`.

### Task 2: Migration + `PollenExporter` in brightsky

**Files:**
- Create: `migrations/0019_pollen.sql`
- Modify: `brightsky/export.py` (append `PollenExporter`)
- Modify: `tests/conftest.py` (add `DELETE FROM pollen;` to the `db` fixture cleanup)
- Create: `tests/data/s31fg.json` (same fixture as dwdparse)
- Test: `tests/test_export.py`

**Interfaces:**
- Consumes: record dicts per the schema above.
- Produces: `PollenExporter().export(records, fingerprint=None)`; rows in table `pollen` upserted on `(region_id, partregion_id, species, date)`.

- [ ] **Step 1:** `migrations/0019_pollen.sql`:

```sql
CREATE TABLE pollen (
  id               serial PRIMARY KEY,
  region_id        smallint NOT NULL,
  partregion_id    smallint NOT NULL,
  region_name      text NOT NULL,
  partregion_name  text,
  species          text NOT NULL,
  date             date NOT NULL,
  index            text NOT NULL,
  severity         real,
  last_update      timestamptz NOT NULL,
  next_update      timestamptz,
  sender           text,

  CONSTRAINT pollen_key UNIQUE (region_id, partregion_id, species, date)
);
```

- [ ] **Step 2: Failing test** in `tests/test_export.py`:

```python
def test_pollen_exporter(db, data_dir):
    from brightsky.parsers import PollenParser
    p = PollenParser()
    records = list(p.parse(data_dir / 's31fg.json'))
    p.exporter().export(iter(records))
    rows = db.table('pollen')
    assert len(rows) == len(records) == 47
    # Re-export upserts instead of duplicating
    records[0]['index'] = '3'
    records[0]['severity'] = 3.0
    p.exporter().export(iter(records))
    rows = db.table('pollen')
    assert len(rows) == 47
    updated = [
        r for r in rows
        if (r['region_id'], r['partregion_id'], r['species'], r['date']) == (
            records[0]['region_id'], records[0]['partregion_id'],
            records[0]['species'], records[0]['date'])]
    assert updated[0]['index'] == '3'
```

(This test also needs Task 3's `PollenParser` wrapper; write both, then run.)

- [ ] **Step 3: Implement** `PollenExporter` in `brightsky/export.py`:

```python
class PollenExporter(DBExporter):

    UPDATE_POLLEN_STMT = sql.SQL("""
        INSERT INTO pollen ({fields})
        VALUES %s
        ON CONFLICT
            ON CONSTRAINT pollen_key DO UPDATE SET
                {conflict_updates};
    """)
    ELEMENT_FIELDS = [
        'region_id',
        'partregion_id',
        'region_name',
        'partregion_name',
        'species',
        'date',
        'index',
        'severity',
        'last_update',
        'next_update',
        'sender',
    ]

    def export(self, records, fingerprint=None):
        with get_connection() as conn:
            for batch in batched(records, self.BATCH_SIZE):
                self.update_pollen(conn, batch)
            if fingerprint:
                self.update_parsed_files(conn, fingerprint)
            conn.commit()

    def update_pollen(self, conn, records):
        for fields, records in self.make_batches(records).items():
            logger.info(
                "Exporting %d pollen records with fields %s",
                len(records), tuple(fields))
            stmt = self.UPDATE_POLLEN_STMT.format(
                fields=sql.SQL(', ').join(sql.Identifier(f) for f in fields),
                conflict_updates=sql.SQL(', ').join(
                    sql.SQL('{field} = EXCLUDED.{field}').format(
                        field=sql.Identifier(f))
                    for f in fields),
            )
            template = sql.SQL('({values})').format(
                values=sql.SQL(', ').join(
                    sql.Placeholder(f) for f in fields),
            )
            with conn.cursor() as cur:
                execute_values(cur, stmt, records, template, page_size=1000)
```

- [ ] **Step 4:** Add `DELETE FROM pollen;` to the `db` fixture cleanup block in `tests/conftest.py`.

### Task 3: Brightsky wrapper parser + polling

**Files:**
- Modify: `brightsky/parsers.py` (add `PollenParser` wrapper + `get_parser` entry)
- Modify: `brightsky/polling.py` (add health/alerts URL)
- Test: `tests/test_parsers.py` (`test_get_parser` expected dict)

**Interfaces:**
- Consumes: `dwdparse.parsers.PollenParser`, `PollenExporter` (Task 2).
- Produces: `brightsky.parsers.PollenParser` with `exporter = PollenExporter`; polling covers `https://opendata.dwd.de/climate_environment/health/alerts/`.

- [ ] **Step 1:** In `brightsky/parsers.py` (import `PollenExporter`; class after `CAPParser`):

```python
class PollenParser(BrightSkyMixin, dwdparse.parsers.PollenParser):

    PRIORITY = 30
    exporter = PollenExporter
```

Add to `get_parser`'s dict: `r's31fg\.json$': PollenParser,`.

- [ ] **Step 2:** In `brightsky/polling.py`, append to `DWDPoller.urls` list:

```python
'https://opendata.dwd.de/climate_environment/health/alerts/',
```

- [ ] **Step 3:** Add `'s31fg.json': PollenParser` to `test_get_parser` in `tests/test_parsers.py`.
- [ ] **Step 4:** Run `python -m pytest tests/test_parsers.py tests/test_export.py -x` — pass (with `BRIGHTSKY_DATABASE_URL=postgres://postgres:pgpass@localhost/brightsky_test`).
- [ ] **Step 5:** Commit: `feat: ingest DWD pollen hazard index into new pollen table`.

### Task 4: Region resolution + query

**Files:**
- Modify: `brightsky/settings.py` (add `POLLEN_REGIONS_URL` below `WARN_CELLS_URL`)
- Modify: `brightsky/query.py` (add `pollen()` + `PollenRegionManager` + `_pollen_regions`)
- Create: `tests/data/pollen_regions.json` (features GF 50 + GF 92 extracted from the live GeoServer response, geometry simplified for size — test fixture only, production uses the live URL)

**Interfaces:**
- Consumes: table `pollen`, `settings.POLLEN_REGIONS_URL`, `NoData`, `topg`, `make_dicts`.
- Produces: `async def pollen(conn, lat=None, lon=None, region_id=None)` → `{'pollen': [...], 'location': {...}, 'last_update': dt, 'next_update': dt, 'sender': str}`; `PollenRegionManager.find(lat, lon)` → `{'region_id': int, 'name': str}`.

- [ ] **Step 1:** `brightsky/settings.py`:

```python
POLLEN_REGIONS_URL = (
    'https://maps.dwd.de/geoserver/wfs'
    '?SERVICE=WFS&VERSION=2.0.0&REQUEST=GetFeature'
    '&TYPENAMES=Pollenfluggebiete&OUTPUTFORMAT=json'
)
```

- [ ] **Step 2:** `brightsky/query.py`, after the alerts section:

```python
async def pollen(
    conn,
    lat=None,
    lon=None,
    region_id=None,
):
    if lat is not None and lon is not None:
        region_id = _pollen_regions.find(lat, lon)['region_id']
    elif region_id is None:
        raise ValueError("Please supply lat & lon, or region_id")
    sql = """
        SELECT *
        FROM pollen
        WHERE
            (partregion_id = {region_id} OR
             (partregion_id = -1 AND region_id = {region_id})) AND
            date >= current_date
        ORDER BY date, species
    """
    params = {'region_id': region_id}
    sql, params = topg(sql, params)
    rows = make_dicts(await conn.fetch(sql, *params))
    if not rows:
        raise NoData("No pollen data for the given location criteria")
    return {
        'pollen': [
            {k: row[k] for k in ['species', 'date', 'index', 'severity']}
            for row in rows
        ],
        'location': {
            k: rows[0][k]
            for k in [
                'region_id', 'partregion_id', 'region_name',
                'partregion_name',
            ]
        },
        'last_update': rows[0]['last_update'],
        'next_update': rows[0]['next_update'],
        'sender': rows[0]['sender'],
    }


class PollenRegionManager:

    REGIONS_CACHE_PATH = os.path.join(
        tempfile.gettempdir(), 'pollen_regions.json')

    @cached_property
    def tree(self):
        self.region_meta = {}
        for f in self.get_region_data()['features']:
            geometry = f['geometry']
            if geometry['type'] == 'Polygon':
                coordinates = [geometry['coordinates']]
            else:
                coordinates = geometry['coordinates']
            polygons = [
                # shell, holes
                (c[0], c[1:])
                for c in coordinates
            ]
            p = MultiPolygon(polygons)
            self.region_meta[p] = {
                'region_id': f['properties']['GF'],
                'name': f['properties']['GEN'],
            }
        return STRtree(list(self.region_meta.keys()))

    def get_region_data(self):
        path = self.REGIONS_CACHE_PATH
        if not os.path.isfile(path):
            resp = requests.get(
                settings.POLLEN_REGIONS_URL,
                headers={'User-Agent': USER_AGENT},
            )
            with open(path, 'wb') as f:
                f.write(resp.content)
        with open(path) as f:
            return json.load(f)

    def find(self, lat, lon):
        p = Point(lon, lat)
        region = self.tree.geometries[self.tree.nearest(p)]
        if region.distance(p) > 0.01:
            raise NoData("Requested position is not covered by the DWD")
        return self.region_meta[region]


_pollen_regions = PollenRegionManager()
```

- [ ] **Step 3:** Build `tests/data/pollen_regions.json` from the scratchpad GeoServer download: keep features with `GF in (50, 92)`, `shapely.simplify(0.005)` on the geometry, write compact JSON.

### Task 5: `/pollen` endpoint

**Files:**
- Modify: `brightsky/web/params.py` (add `PollenRegion`, `PollenParams`)
- Modify: `brightsky/web/models.py` (add `PollenRecord`, `PollenLocation`, `PollenResponse`)
- Modify: `brightsky/web/app.py` (import + route)
- Modify: `brightsky/enhancements.py` (timestamps for `last_update`/`next_update`)
- Test: `tests/test_web.py`

**Interfaces:**
- Consumes: `query.pollen` (Task 4).
- Produces: `GET /pollen?lat=&lon=` and `GET /pollen?region_id=`.

- [ ] **Step 1: Failing tests** in `tests/test_web.py`:

```python
@pytest.fixture
def pollen_data(db, data_dir):
    from brightsky.query import _pollen_regions
    p = PollenParser()
    p.exporter().export(p.parse(data_dir / 's31fg.json'))
    _pollen_regions.REGIONS_CACHE_PATH = data_dir / 'pollen_regions.json'


def test_pollen_response(pollen_data, api):
    # By lat/lon (Berlin -> region 50, partregion -1)
    resp = api.get('/pollen?lat=52.52&lon=13.41')
    assert resp.status_code == 200
    data = resp.json()
    assert data['location']['region_id'] == 50
    assert data['location']['partregion_id'] == -1
    assert data['location']['region_name'] == 'Brandenburg und Berlin'
    assert data['sender'] == (
        'Deutscher Wetterdienst - Medizin-Meteorologie')
    assert all(
        set(r) == {'species', 'date', 'index', 'severity'}
        for r in data['pollen'])
    # By region id (partregion 11)
    resp = api.get('/pollen?region_id=11')
    assert resp.status_code == 200
    assert resp.json()['location']['partregion_id'] == 11
    # Region resolvable but no data ingested for it (Frankfurt -> GF 92)
    assert api.get('/pollen?lat=50.11&lon=8.68').status_code == 404
    # Outside DWD coverage
    assert api.get('/pollen?lat=32&lon=7.6').status_code == 404
    # Missing parameters
    assert api.get('/pollen').status_code == 422
```

Note: the fixture data's forecast dates are 2026-07-13..15; the query filters `date >= current_date`, so freeze expectations accordingly — the test must regenerate fixture dates or the s31fg fixture's `last_update` must be rewritten to "today" at export time. Simplest robust approach: in `pollen_data`, rewrite the parsed records' dates relative to `datetime.date.today()` before export (shift by `today - records[0]['date']`).

- [ ] **Step 2: Models** (`web/models.py`):

```python
class PollenRecord(ResponseModel):
    species: Literal[
        'ambrosia', 'beifuss', 'birke', 'erle',
        'esche', 'graeser', 'hasel', 'roggen',
    ] = Field(
        description="Pollen species (DWD naming)",
    )
    date: datetime.date = Field(
        description="Forecast day",
        examples=["2026-07-13"],
    )
    index: str = Field(
        description="DWD pollen hazard index ('0', '0-1', '1', '1-2', '2', '2-3', '3')",  # noqa
        examples=["1-2"],
    )
    severity: float = Field(
        description="Numeric representation of `index`, from 0 (keine Belastung) to 3 (hohe Belastung) in steps of 0.5",  # noqa
        examples=[1.5],
        json_schema_extra={'nullable': True},
    )


class PollenLocation(ResponseModel):
    region_id: int = Field(
        description="DWD pollen region (Pollenflugbereich) ID",
        examples=[50],
    )
    partregion_id: int = Field(
        description="DWD pollen part-region ID, -1 if the region has no part-regions",  # noqa
        examples=[-1],
    )
    region_name: str = Field(
        description="Pollen region name",
        examples=["Brandenburg und Berlin"],
    )
    partregion_name: str = Field(
        description="Pollen part-region name",
        json_schema_extra={'nullable': True},
    )


class PollenResponse(ResponseModel):
    pollen: list[PollenRecord]
    location: PollenLocation
    last_update: datetime.datetime = Field(
        description="Time this forecast was issued by the DWD",
    )
    next_update: datetime.datetime = Field(
        description="Time the next forecast will be issued",
        json_schema_extra={'nullable': True},
    )
    sender: str = Field(
        description="Issuer of the data (DWD attribution, CC BY 4.0)",
        examples=["Deutscher Wetterdienst - Medizin-Meteorologie"],
    )
```

- [ ] **Step 3: Params** (`web/params.py`):

```python
class PollenRegion(BaseModel):
    region_id: int = Field(
        default=None,
        description="DWD pollen region ID. Use the part-region ID (e.g. 11 for 'Inseln und Marschen') where part-regions exist, and the region ID otherwise (e.g. 50 for 'Brandenburg und Berlin').",  # noqa
        examples=[11, 50, 124],
    )


class PollenParams(
    Timezone,
    PollenRegion,
    LatLon,
):
    @model_validator(mode='after')
    def validate_at_least_one_option(self):
        if self.lat is not None and self.lon is not None:
            return self
        elif self.region_id is not None:
            return self
        raise ValueError("Please supply lat & lon, or region_id")
```

- [ ] **Step 4: Enhancements** (`enhancements.py`): in `enhance()` add

```python
if 'pollen' in result:
    enhance_pollen(result, timezone=timezone)
```

and

```python
def enhance_pollen(result, timezone=None):
    for key in ['last_update', 'next_update']:
        process_timestamp(result, key, timezone)
```

- [ ] **Step 5: Route** (`web/app.py`, after `alerts`; extend the two import blocks):

```python
@app.get(
    '/pollen',
    operation_id='getPollen',
    summary='Pollen',
    responses=common_responses,
)
async def pollen(
    q: Annotated[PollenParams, Query()],
) -> PollenResponse:
    """
    Returns the DWD pollen hazard index ("Pollenflug-Gefahrenindex") for
    today, tomorrow, and the day after tomorrow, for the pollen region
    matching the given location.

    You must supply both `lat` and `lon` _or_ a `region_id`.

    ### Notes

    * The DWD publishes this forecast for 27 pollen regions
      (_Pollenflugbereiche_) covering Germany, once per day in the
      morning. Eight species are covered: hazel (`hasel`), alder
      (`erle`), ash (`esche`), birch (`birke`), grasses (`graeser`),
      rye (`roggen`), mugwort (`beifuss`), and ragweed (`ambrosia`).
    * `index` is the raw DWD load level, `severity` a numeric mapping
      (`0` none … `3` high, in steps of `0.5`).
    * Data source: Deutscher Wetterdienst (CC BY 4.0). The `sender`
      field contains the attribution.

    ### Additional resources

    * [Raw data on the Open Data Server](https://opendata.dwd.de/climate_environment/health/alerts/)
    * [DWD file format description (German)](https://opendata.dwd.de/climate_environment/health/alerts/Beschreibung_pollen_s31fg.pdf)
    * [DWD pollen forecast page](https://www.dwd.de/pollenflug)
    """
    result = await query.pollen(
        ctx['pool'],
        lat=q.lat,
        lon=q.lon,
        region_id=q.region_id,
    )
    enhance(result, timezone=q.timezone)
    return ORJSONResponse(result)
```

- [ ] **Step 6:** Run `python -m pytest tests/ -x` (full brightsky suite) — pass.
- [ ] **Step 7:** Commit: `feat: add /pollen endpoint with DWD pollen region resolution`.

### Task 6: Docs, dependency pin, final verification

**Files:**
- Create: `docs/nano/README.md`, `docs/nano/architecture.md`, `docs/nano/fork-management.md`, `docs/nano/worklog.md`
- Modify: `requirements.txt` (point `dwdparse` at our fork's commit SHA)

- [ ] **Step 1:** Write the four docs (content per brief §5; architecture doc must match the code as built, including the GeoServer polygon source and the `GF` mapping).
- [ ] **Step 2:** Update `requirements.txt`: replace `dwdparse==0.9.22` with `dwdparse @ git+https://github.com/fuekiin/dwdparse.git@<sha-of-task-1-commit>` and note the build order (tag/push dwdparse first) in fork-management.md. Note: the SHA only resolves once `nano-health` is pushed to the dwdparse fork.
- [ ] **Step 3:** End-to-end smoke test against the live DWD file: parse the scratchpad `s31fg.json` through `brightsky.parsers.PollenParser`, export into the test DB, run `query.pollen` for Berlin, verify sensible output.
- [ ] **Step 4:** Run both full test suites one last time; `git rebase upstream/master --dry-run`-equivalent sanity check (`git fetch upstream && git merge-base ...`) is out of scope, but confirm our diff against `master` only appends.
- [ ] **Step 5:** Commit docs: `docs: add nano health-data documentation and worklog`.

## Self-review notes

- Spec coverage: parser ✓ (Task 1), polling ✓ (3), migration ✓ (2), exporter ✓ (2), region resolution ✓ (4), endpoint ✓ (5), tests ✓ (1,2,3,5), real sourced polygons ✓ (GeoServer, Task 4), docs ✓ (6), additive-only ✓ (all new files/appends).
- UVI/Biowetter/GT deliberately out of scope (first slice); the `HealthForecastParser` base and docs leave the seam for them.
- `index` is a non-reserved word in PostgreSQL and always emitted quoted via `sql.Identifier` — safe as a column name.
- Test-date pitfall (fixture dates vs `current_date`) is handled by shifting record dates at export time in the `pollen_data` fixture.
