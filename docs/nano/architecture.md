# Architecture: DWD health data pipeline (pollen slice)

Data flows: **polling → parser selection → parse (dwdparse) → export to DB (brightsky) →
query → web**. This document maps that flow to the actual code, as built for pollen
(`s31fg.json`). File/line references are as of 2026-07-13 on `nano-health`.

## Why health data has its own tables and endpoints

This is a firm architectural decision, not a preference:

- The `weather`/`synop` tables are keyed by `(timestamp, source_id)` where a *source* is a
  weather station or forecast grid point with a lat/lon. Pollen has no stations — the DWD
  publishes one value per **region polygon** per **day**. Forcing that into `sources`/`weather`
  would corrupt the source model and the fallback logic built on top of it.
- Cadence differs: hourly/10-minutely vs. once daily.
- Upstream rebases stay trivial when our data lives in tables upstream never touches.

Note: the `alert_category` enum from `0016_alerts.sql` contains a `'health'` value. That is for
**CAP health warnings** (e.g. heat warnings) inside the alerts subsystem — completely unrelated
to these JSON health forecast products. Don't conflate them.

## Pipeline stations

### 1. Polling — `brightsky/polling.py`
`DWDPoller.urls` contains `https://opendata.dwd.de/climate_environment/health/alerts/`.
The poller crawls the directory listing, fingerprints files (size + mtime), and yields changed
files whose basename matches a parser. The directory also contains `uvi.json`, `biowetter.json`,
`gt.json` and PDF format descriptions — those match no parser regex and are skipped for now.

### 2. Parser selection — `brightsky/parsers.py` `get_parser()`
`r's31fg\.json$'` → `brightsky.parsers.PollenParser`. Patterns are matched with `re.match`
against the file's basename.

### 3. Parsing — `../dwdparse/dwdparse/parsers.py`
- `HealthForecastParser(Parser)` — base for the shared JSON envelope of all four health
  products: loads the file, parses `last_update`/`next_update` (format `%Y-%m-%d %H:%M Uhr`,
  **Europe/Berlin local time**, converted to UTC) and passes `sender` through for attribution.
- `PollenParser(HealthForecastParser)` — yields **one record per (region × species × forecast
  day)**. `today`/`tomorrow`/`dayafter_to` resolve to `last_update`'s Berlin-local date +0/+1/+2.
  Values of `'-1'` mean "missing" (per `Beschreibung_pollen_s31fg.pdf`) and are skipped.
  Raw index strings (`0`, `0-1`, `1`, `1-2`, `2`, `2-3`, `3`) are kept in `index` and mapped to
  a numeric `severity` (0.0–3.0 in 0.5 steps); unknown raw values are kept with
  `severity = None` and a warning.
- dwdparse is standard-library only (`json`, `zoneinfo`).

**The record schema** (the contract between the two repos):

```python
{
    'region_id': 50,            # DWD Hauptgebiet (tens: 10, 20, ... 120)
    'partregion_id': -1,        # DWD Teilbereich (units: 11, 12, ...), -1 if none
    'region_name': 'Brandenburg und Berlin',
    'partregion_name': None,    # None when partregion_id == -1 (DWD sends '')
    'species': 'graeser',       # ambrosia|beifuss|birke|erle|esche|graeser|hasel|roggen
    'date': datetime.date(2026, 7, 13),
    'index': '1-2',
    'severity': 1.5,
    'last_update': datetime.datetime(2026, 7, 13, 9, 0, tzinfo=utc),
    'next_update': datetime.datetime(2026, 7, 14, 9, 0, tzinfo=utc),
    'sender': 'Deutscher Wetterdienst - Medizin-Meteorologie',
}
```

### 4. Bright Sky wrapper — `brightsky/parsers.py`
`class PollenParser(BrightSkyMixin, dwdparse.parsers.PollenParser)` with
`exporter = PollenExporter`. This is the seam between parsing core and service.

### 5. Export — `brightsky/export.py` `PollenExporter(DBExporter)`
Plain upsert into the `pollen` table, `ON CONFLICT ON CONSTRAINT pollen_key DO UPDATE`
(conflict key: `region_id, partregion_id, species, date`). Unlike `DBExporter` there are no
sources to maintain, and unlike `AlertExporter` no join table — each record belongs to exactly
one region. Re-ingesting the same day's file overwrites in place; rows for past dates remain as
history (nothing expires them; see open questions in the worklog).

### 6. Schema — `migrations/0019_pollen.sql`
New table only, strictly additive. `index` is a non-reserved word in PostgreSQL and is always
emitted quoted (`sql.Identifier`) by the exporter.

### 7. Region resolution — `brightsky/query.py` `PollenRegionManager`
**Where the polygons come from:** the DWD GeoServer WFS layer `Pollenfluggebiete`
(`settings.POLLEN_REGIONS_URL`), cached at `<tempdir>/pollen_regions.json`. It serves 54
GeoJSON features (EPSG:4326, lon/lat order). The feature property **`GF` equals s31fg.json's
`partregion_id`** — or `region_id` for the three regions without part-regions (20
Mecklenburg-Vorpommern, 50 Brandenburg/Berlin, and any future ones). `GEN` is the region name.
Verified 2026-07-13: all 27 forecast regions map 1:1 onto distinct `GF` values; multiple
features may share one `GF` (e.g. islands).

**How lat/lon resolves:** same pattern as `WarnCellManager` — shapely `MultiPolygon`s in an
`STRtree`; `find(lat, lon)` takes the nearest geometry and raises `NoData` if it is more than
0.01° away (i.e. the point is outside DWD coverage).

`query.pollen(conn, lat, lon, region_id)` accepts lat/lon *or* a `region_id` (in `GF`
convention: part-region id where part-regions exist, region id otherwise). It returns rows with
`date >= current_date`, i.e. today + up to two days ahead, plus a `location` block and the
attribution metadata pulled from the rows.

### 8. Web — `brightsky/web/`
- `params.py` `PollenParams` (lat/lon or region_id, plus `tz`)
- `models.py` `PollenRecord` / `PollenLocation` / `PollenResponse` (OpenAPI docs)
- `app.py` `GET /pollen`
- `enhancements.py` `enhance_pollen` converts `last_update`/`next_update` into the requested
  timezone. `date` values are calendar dates and not converted.

### 9. Tests
- dwdparse: `tests/test_parsers.py::test_pollen_parser` with `tests/data/s31fg.json` — a
  trimmed real sample (regions (10,11) and (50,−1)) with **one value hand-edited to `"-1"`**
  (region 50, Ambrosia, day-after) to cover the missing-value path.
- brightsky: `tests/test_export.py::test_pollen_exporter` (upsert semantics),
  `tests/test_web.py::test_pollen_response` (endpoint, both lookup modes, 404/422 paths).
  `tests/data/pollen_regions.json` contains the **real** GeoServer features for GF 50 and 92,
  geometry simplified (~0.005°) to keep the fixture small — production always uses the live
  layer. The web fixture shifts record dates so the file's "today" is the test run's today
  (the query filters on `current_date`).

## The other three products (added 2026-07-14)

All follow the same pipeline; this section records only where they differ from pollen. Full
verified facts in `plans/2026-07-14-remaining-health-products.md`.

### Biowetter (`biowetter.json` → `biowetter` table → `/biowetter`)

- 11 zones with letter ids A–K; one record per zone × half-day (5 per zone: today's afternoon
  + both halves of the next two days, each entry carrying its own `date`).
- Envelope quirks: attribution is in `author` (normalized to `sender` by the parser),
  timestamps are `%Y-%m-%d %H:%M` without "Uhr".
- The DWD `effect[]`/`recomms[]` trees (medical categories with optional `subeffect` lists,
  German) are passed through unchanged and stored as `jsonb`.
- Resolution: `BiowetterZoneManager` over the GeoServer `Biowettergebiete` layer. **Careful:**
  the layer's `GF` numbering is the DWD's canonical zone order, which is NOT alphabetical —
  `6=G`, `7=F` (all 11 verified by name matching on 2026-07-14; mapping hard-coded in the
  manager).

### UV index (`uvi.json` → `uv_index` table → `/uv_index`) and thermal hazard (`gt.json` → `thermal_hazard` table → `/thermal_hazard`)

- Both are **city-based**: 38 cities/mountains (UVI, integer index per day) and 34 cities
  (thermal, hazard category per fixed-CET slot 03/09/15/21 — `MEZ` means UTC+1 even in summer;
  stored as UTC timestamps). Day offsets are relative to the files' `forecast_day`.
- The files identify locations **by name only**. `CityLocationManager` supplies coordinates:
  primary source is the GeoServer `Uv_Stationen` layer (global station list; all 38 UVI cities
  match `ALIASNAME` exactly; gt's `Frankfurt`/`List` map via an alias table to
  `Frankfurt/Main`/`List auf Sylt`). **Five gt-only cities are missing from the layer**
  (Köln, Schwerin, Saarbrücken, Mannheim, Erfurt) and use a static city-center coordinate
  table in the manager — the one deliberate exception to "all geometry from the DWD",
  documented here and transparent in responses (matched `city` + `distance` always returned).
- Nearest-city resolution is a plain haversine over ≤40 points, capped at 200 km (`NoData`
  beyond). The endpoints also accept `?city=<name>` or, with no location criteria, return all
  cities (mirroring `/alerts`).

### If the DWD changes something

The parsers warn on unknown pollen index values and unknown gt slot keys, and skip missing
values (`-1` for pollen, `null` elsewhere). New zone/city additions flow through automatically
— except a new gt city missing from `Uv_Stationen`, which silently becomes unresolvable by
lat/lon (it still appears in `?city=`/all-cities responses) until added to the static table.
