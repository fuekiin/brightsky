# nano — DWD Health Data in Bright Sky

This directory documents the **nano** extensions to our Bright Sky fork: serving the DWD's
health/biometeorology products to the *nano – Wetter für Deutschland* app.

## Why this exists

nano is a privacy-first German weather app: **DWD open data only**, no third-party APIs, no ads,
no tracking, no login. The DWD publishes health products (pollen, UV index, Biowetter, Gefühlte
Temperatur) on its Open Data server, but upstream Bright Sky does not ingest them. We extend our
fork — and a companion fork of `dwdparse` — to do so.

Two hard rules shape everything here:

1. **DWD is the sole data source.** Everything is polled from `https://opendata.dwd.de/`
   (region polygons come from the DWD's own GeoServer at `https://maps.dwd.de/`).
2. **Health data never goes into the `weather` table.** It has a different spatial model
   (forecast *regions*, not stations/grid points) and a different cadence (daily, not hourly).
   Each product gets its own table and endpoint.

DWD data is CC BY 4.0: commercial use is allowed, but a *Quellenvermerk* is mandatory. The
API responses therefore preserve the DWD's `sender` attribution field — do not strip it.

## The four products and their status

All four live at `https://opendata.dwd.de/climate_environment/health/alerts/` as JSON files
with a shared envelope (`last_update`, `next_update`, `sender`, `legend`, `content`).

| Product | File | Spatial model | Status |
|---|---|---|---|
| Pollen hazard index | `s31fg.json` | 27 regions (*Pollenflugbereiche*) | ✅ **Shipped** (2026-07-13): parser, `pollen` table, `/pollen` endpoint, tests |
| Biowetter | `biowetter.json` | regions (own scheme — verify before building) | ⬜ Open |
| Gefühlte Temperatur | `gt.json` | regions (own scheme — verify before building) | ⬜ Open |
| UV index | `uvi.json` | **points** (selected cities/mountains) | ⬜ Open — do LAST; needs nearest-point resolution, not polygons |

Biowetter and GT are expected to be near-mechanical repeats of the pollen slice (same
`HealthForecastParser` base in dwdparse, own table + endpoint here) — but **verify their region
scheme and polygon source first**; do not assume they use the Pollenflugbereiche. UVI is
point-based and shares none of the region machinery.

## Where things live

- **Parsing** — our dwdparse fork (sibling checkout `../dwdparse`, branch `nano-health`):
  `dwdparse/parsers.py` → `HealthForecastParser`, `PollenParser`.
- **Everything else** — this repo, branch `nano-health`: polling, exporter, migration,
  region resolution, endpoint. See [architecture.md](architecture.md) for the full pipeline map.
- **How the two forks are managed and kept rebaseable** — [fork-management.md](fork-management.md).
- **What happened when, decisions, open questions** — [worklog.md](worklog.md). Every session
  that changes something appends an entry.
- **The original implementation plan for the pollen slice** —
  [plans/2026-07-13-pollen-first-slice.md](plans/2026-07-13-pollen-first-slice.md).

## Quick start for future-us

```bash
# Setup (macOS; system python is too old, use uv)
cd brightsky
uv venv --python 3.12 .venv
uv pip install --python .venv/bin/python -r requirements.txt -r requirements-dev.txt -e ../dwdparse -e .

# Tests (Postgres without a bind mount; docker-compose's .data mount may be
# denied by Docker Desktop file-sharing settings)
docker run -d --name brightsky-test-pg -e POSTGRES_PASSWORD=pgpass -p 5432:5432 postgres:17-alpine
BRIGHTSKY_DATABASE_URL=postgres://postgres:pgpass@localhost/brightsky_test .venv/bin/python -m pytest
cd ../dwdparse && ../brightsky/.venv/bin/python -m pytest

# Live smoke test
export BRIGHTSKY_DATABASE_URL=postgres://postgres:pgpass@localhost/brightsky_smoke
.venv/bin/python -m brightsky migrate
.venv/bin/python -m brightsky parse https://opendata.dwd.de/climate_environment/health/alerts/s31fg.json
.venv/bin/python -m brightsky serve --bind 127.0.0.1:5599
curl 'http://127.0.0.1:5599/pollen?lat=52.52&lon=13.41'
```
