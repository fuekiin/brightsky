# Biowetter, UV-Index, Thermal Hazard — Implementation Plan

Follows the pollen pattern end to end (see `2026-07-13-pollen-first-slice.md` for the step
mechanics: parser in dwdparse → migration → exporter → resolution → endpoint → tests, all
additive, commits per repo). This document records the product-specific facts and decisions.

## Verified facts (2026-07-14, live files + official PDFs + DWD GeoServer)

| | biowetter.json | gt.json | uvi.json |
|---|---|---|---|
| Product | Gefahrenindizes für Wetterfühlige | Thermischer Gefahrenindex | UV-Gefahrenindex |
| Spatial model | 11 zones, letters A–K | 34 named cities | 38 named cities |
| Content | per zone: 5 periods (`today_afternoon`, `tomorrow_{morning,afternoon}`, `dayafter_to_{morning,afternoon}`), each with own `date`, `value` (Wetterklasse code, e.g. `5-0-w2`), `effect[]` (name/value, optional `subeffect[]`), `recomms[]` | per city: 13 slots `{today,tomorrow,dayafter_to}_{03,09,15,21}MEZ` + `after_threedays_03MEZ`, category strings (keine/gering/mittel/hoch/stark) | per city: `today`, `tomorrow`, `dayafter_to` — integer UV index |
| Envelope quirks | `author` instead of `sender`; `name` = `dwd_healthweather`; timestamps `%Y-%m-%d %H:%M` (no "Uhr") | `sender`; ISO timestamps (naive, local); extra `forecast_day` | same as gt |
| Geometry | GeoServer layer `Biowettergebiete`: 11 features, `GF` 1–11, `GEN` names. **Verified `GF`→letter mapping: 1=A 2=B 3=C 4=D 5=E 6=G 7=F 8=H 9=I 10=J 11=K** (F and G are swapped relative to alphabetical position — confirmed by name matching, 9/11 exact + 2 unambiguous abbreviation variants) | GeoServer layer `Uv_Stationen` (global station list, `ALIASNAME` + point coords) covers 29/34 directly; `Frankfurt`→`Frankfurt/Main`, `List`→`List auf Sylt` (verified unique among DEU stations); **5 cities missing: Köln, Schwerin, Saarbrücken, Mannheim, Erfurt → small static coordinate table** (documented; city centroids are unambiguous public knowledge, and responses always return the matched city + distance, so the resolution is transparent, never silently wrong) | `Uv_Stationen` covers **all 38** by exact `ALIASNAME` match |

- `03MEZ` etc. are fixed CET (UTC+1, no DST) → stored as proper UTC timestamps.
- gt/uvi day offsets are relative to `forecast_day`; biowetter periods carry their own `date`.

## Decisions

- Names (tables = endpoints = response keys): `biowetter`, `uv_index`, `thermal_hazard`.
  "Biowetter" is a proper DWD product name and stays German; "thermal_hazard" because the gt
  product forecasts hazard categories, not felt temperatures.
- `author` (biowetter) is normalized into the `sender` field — one attribution column everywhere.
- Biowetter `effect[]`/`recomms[]` trees are passed through as-is (DWD German keys preserved)
  and stored as `jsonb` — no lossy flattening of medical content.
- dwdparse: `BiowetterParser`, `UVIndexParser`, `ThermalHazardParser`, all on
  `HealthForecastParser` (which gets per-class `TIMESTAMP_FORMAT` use; gt/uvi set
  `'%Y-%m-%dT%H:%M:%S'`, biowetter `'%Y-%m-%d %H:%M'`).
- brightsky: one generic `HealthExporter(DBExporter)` (upsert into TABLE on CONSTRAINT with
  ELEMENT_FIELDS — the generalization of PollenExporter, which becomes its first subclass),
  then three thin exporters.
- Resolution: `BiowetterZoneManager` (STRtree over the 11 polygons, like pollen);
  `CityLocationManager` (city→lat/lon from `Uv_Stationen` + alias map + the 5 static cities;
  nearest by haversine over ≤40 points, capped at 200 km → `NoData` beyond).
- Endpoints accept lat/lon OR `zone_id` (A–K) / `city`; uv/thermal with no location return all
  cities (mirrors `/alerts` behavior).
- History kept, nothing expires (same as pollen).

## Record schemas

```python
# BiowetterParser — one per zone × period (55/day)
{'zone_id': 'A', 'zone_name': 'Schleswig-Holstein, …', 'date': date, 'period': 'morning',
 'weather_class': '5-0-w1', 'effects': [...], 'recommendations': [...],
 'last_update': utc, 'next_update': utc, 'sender': 'Medizin-Meteorologie'}

# UVIndexParser — one per city × day (114/day)
{'city': 'Berlin', 'date': date, 'uv_index': 5, 'last_update': utc, 'next_update': utc,
 'sender': 'Deutscher Wetterdienst - Medizin-Meteorologie'}

# ThermalHazardParser — one per city × slot (442/day)
{'city': 'Berlin', 'timestamp': utc datetime (slot at fixed UTC+1), 'level': 'gering',
 'last_update': utc, 'next_update': utc, 'sender': …}
```

Upsert keys: `(zone_id, date, period)` / `(city, date)` / `(city, timestamp)`.

## Task list

1. dwdparse: three parsers + `get_parser` entries + trimmed real fixtures + tests → commit.
2. brightsky: migration `0020_health_products.sql` (3 tables) + `HealthExporter` refactor +
   3 exporters + wrapper parsers + `get_parser` + conftest cleanup + export tests → commit.
3. brightsky: settings URLs (`BIOWETTER_ZONES_URL`, `UV_STATIONS_URL`) + managers +
   `query.{biowetter,uv_index,thermal_hazard}` + endpoints/params/models/enhancements +
   web tests with trimmed real geo fixtures → commit.
4. Docs (README status, architecture, worklog) + bump dwdparse pin SHA + live smoke test of
   all three endpoints → commit.
