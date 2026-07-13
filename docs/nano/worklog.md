# Worklog — nano health data

Running, dated log of what was done, decisions made, and open questions. Newest entries at the
bottom. Every session that changes something appends an entry.

## 2026-07-13 — Pollen first slice, end to end

**Done** (brightsky `nano-health`: `44fb9ca`, `76cca78`, + docs commit; dwdparse `nano-health`: `514ed46`):

- Verified the whole pipeline against live sources before writing code (live `s31fg.json`,
  the official PDF spec, the GeoServer layer).
- dwdparse: `HealthForecastParser` base + `PollenParser` (stdlib only), `get_parser` entry,
  trimmed real fixture, parser test. 51/51 tests pass.
- brightsky: migration `0019_pollen.sql` (new `pollen` table only), `PollenExporter` (upsert on
  `region_id, partregion_id, species, date`), `PollenParser` wrapper, health/alerts polling URL,
  `POLLEN_REGIONS_URL` setting, `PollenRegionManager` + `query.pollen()`, `GET /pollen`
  (lat/lon or region_id), enhancements, tests. 57/57 tests pass.
- Live smoke test: fresh DB → migrate → `brightsky parse <live URL>` (648 records = 27×8×3) →
  `brightsky serve` → curl: Munich → (120, 121 Allgäu/Oberbayern/Bay. Wald), Berlin → (50, −1),
  `region_id=103` → Saarland, `tz=Europe/Berlin` conversion works, 404 outside coverage,
  422 without params.
- Docs: this directory (README, architecture, fork-management, worklog, plans/).
- `requirements.txt`: `dwdparse` now pinned to our fork at `514ed46...` (git ref). **The SHA is
  not yet pushed** — `git push origin nano-health` in `../dwdparse` is required before any
  non-local install of this requirements.txt works.

**Decisions:**

- **Region geometry solved with an authoritative DWD source**: the GeoServer WFS layer
  `Pollenfluggebiete` (property `GF` = partregion_id, or region_id where partregion_id is −1;
  verified 1:1 against all 27 regions in the live s31fg.json). No reconstructed/third-party
  polygons needed — the brief's "geometry gap" blocker does not exist. (`ekeih/dwdpollen` was
  the reference pointer, but the GeoServer layer supersedes it.)
- One row per (region × species × day), raw `index` string preserved, plus numeric `severity`
  (0–3 in 0.5 steps) for app convenience. `-1` (missing, per DWD spec) is skipped at parse time.
- `last_update`/`next_update` are Berlin-local in the file; stored as UTC; forecast-day dates
  are derived from the Berlin-local date of `last_update`.
- Attribution (`sender`) stored per row and returned by the endpoint (CC BY 4.0 Quellenvermerk).
- API `region_id` parameter uses the `GF` convention (part-region id where one exists). The
  response `location` block always carries both DWD ids and both names.
- Old pollen rows (past `date`s) are kept as history; `brightsky clean` does not touch them.
- Test venv: system Python is 3.9, codebase needs ≥3.11 (`datetime.UTC`) → uv-managed 3.12 in
  `.venv`. Postgres for tests runs as a standalone container (`brightsky-test-pg`) because
  Docker Desktop denies the compose file's `./.data` bind mount on this machine.
- Fixed dwdparse `upstream` remote → `jdemaeyer/dwdparse` (was pointing at our own fork).

**Open questions / next steps:**

- [ ] Push `nano-health` in both repos (pin SHA must be public; see fork-management.md).
- [ ] Should `brightsky clean` expire old pollen rows, or is the history valuable for nano?
      (~650 rows/day upserted, ~216 net new — years until this matters.)
- [ ] Biowetter (`biowetter.json`) and Gefühlte Temperatur (`gt.json`): verify their region
      scheme + polygon source before building; expected to reuse `HealthForecastParser`.
- [ ] UVI (`uvi.json`): point-based (cities/mountains), needs nearest-point resolution — last.
- [ ] Consider offering `PollenParser` upstream to jdemaeyer/dwdparse (it's written
      upstream-shaped for exactly that).
- [ ] `pyproject.toml` `requires-python = ">= 3.8"` is stale upstream (code uses
      `datetime.UTC`, 3.11+) — not our fight; noted in case a rebase trips on it.
