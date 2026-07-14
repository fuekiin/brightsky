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

## 2026-07-14 — Pollen debug view in the nano app

**Done** (WeatherGermany repo, branch `debug/pollen-local`, commit `5baf23c`):

- `PollenResponse` models + `PollenClient` in WeatherCore (bypasses `BrightSkyGateway` on
  purpose — single dev host, default `http://127.0.0.1:5599`), decoding tests from a captured
  real response.
- "Pollen (lokal)" section in `DebugMenuView` (#if DEBUG): editable base URL, loads via the
  currently shown coordinate, shows region, issue time, DWD attribution, per-day species rows.
- Verified by hand in the iOS 26.5 Simulator against the locally running fork. Loopback HTTP
  needs no ATS exception.
- These models/client are the seed of the real pollen feature; when `api.nano-wetter.de`
  serves `/pollen`, route the client through `BrightSkyGateway`.

## 2026-07-14 — Biowetter, UV index, thermal hazard end to end

**Done** (dwdparse `eedb374`; brightsky `71af9fa` deployment doc + tarball pin, `0a15a0f`
ingestion, `fed84e8` endpoints, + this docs commit):

- Deployment plan for api.nano-wetter.de recorded in `deployment.md` (approved, NOT executed);
  dwdparse pin switched to a GitHub tarball (no git binary in the slim Docker image).
- dwdparse: `BiowetterParser`, `UVIndexParser`, `ThermalHazardParser` (54/54 tests).
- brightsky: migration `0020_health_products.sql` (three tables), `HealthExporter`
  generalization, three endpoints with zone/city resolution (63/63 tests).
- **Corrected brief assumptions:** gt.json is city-based (34 cities, "Thermischer
  Gefahrenindex" categories at fixed-CET slots), NOT region-based; uvi.json city-based as
  expected; biowetter.json has its own 11 lettered zones and different envelope conventions
  (`author`, no "Uhr" in timestamps).
- **Geometry all DWD-sourced with one documented exception:** `Biowettergebiete` layer for
  zones (GF numbering non-alphabetical: 6=G, 7=F — verified by names), `Uv_Stationen` layer
  for city coordinates; five gt-only cities (Köln, Schwerin, Saarbrücken, Mannheim, Erfurt)
  are absent from the layer and use static city-center coordinates in `CityLocationManager` —
  responses always return the matched city + distance, so nothing resolves silently wrong.
- Naming decision: endpoints/tables `biowetter`, `uv_index`, `thermal_hazard`; biowetter's
  `author` normalized into `sender`.

**Open questions / next steps:** (in addition to the 2026-07-13 list)

- [x] Bump + push both forks (done 2026-07-14; tarball pin `eedb374…` verified resolvable).
- [x] First image on GHCR (done 2026-07-14, manual `docker buildx --platform linux/amd64`
      from a Mac): `ghcr.io/fuekiin/brightsky:nano-v1` = `:sha-ba10fe5`, verified linux/amd64.
      Package is private until visibility is flipped (see deployment.md step 3).
- [ ] CI workflows (test + image build) per deployment.md — should replace manual pushes.

## 2026-07-14 — nano app: Gesundheit card for all four products

**Done** (WeatherGermany `debug/pollen-local`, commit `f31c304`):

- WeatherCore: `PollenClient` → `HealthClient` (all four endpoints), response models,
  decoding tests from captured responses (234/234 tests pass).
- DEBUG-only "Gesundheit" GlassCard on the Weather home (worst pollen today, UV max, current
  Wärmebelastung slot, worst Biowetter effect, severity-colored) + detail sheet with all four
  products and the CC BY attribution. Re-fetches when the displayed coordinate changes.
- Verified in the Simulator against the local fork (Stuttgart: pollen 110/112, zone I,
  city Stuttgart — all consistent).
- Follow-ups for the real feature: route through `BrightSkyGateway` once prod serves the
  endpoints, drop the `#if DEBUG` gate, App-Group caching, proper design pass.

## 2026-07-14 — Gesundheit feature complete: data layer, wizard, gate

**Done** (WeatherGermany `debug/pollen-local`, 13-task plan, final commit `2d052be`):

- Full feature built per spec (`docs/superpowers/specs/2026-07-14-health-feature-design.md`)
  and plan (`docs/superpowers/plans/2026-07-14-health-feature.md`) in the WeatherGermany repo:
  WeatherCore derivation layer (`HealthSignals.derive`, severity model), `HealthProviderActor` +
  `HealthProfileStore` (App-Group cached, stale-serving on error), a signal-list `HealthCard`,
  a reworked `HealthDetailSheet` (tinted hero, pollen matrix, UV/Wärmebelastung/Biowetter
  sections, attribution footer), and a 4-step `HealthSetupWizard` (Pollenarten mit
  Schwellwerten, Hauttyp/UV, Biowetter-Beschwerdebilder, Wärmeempfindlichkeit).
- Final wiring task: the Weather-home card call site still used a leftover `#if DEBUG` block
  from before the feature existed; replaced with the runtime `HealthFeature.isEnabled` check
  used everywhere else in the feature, and un-gated the `healthVM`/`healthTaskKey` declarations
  so the whole path compiles in Release too (only the flag hides it, not the compiler).
  `HealthFeature.isEnabled` is itself still `#if DEBUG` internally — that's the single
  intended seam, to be swapped for `entitlement.isPro` when the paywall ships.
- Verified: WeatherCore `swift test` full suite green (272/272), Debug and Release builds of
  the `WeatherGermany` scheme both succeed, and a Debug build launched clean in the iPhone 17
  Pro Simulator against this fork running locally on 127.0.0.1:5599 — the card rendered real
  signal rows (Gräser gering bis mittel, Wärmebelastung mittel, UV-Index 5 mittel) with no
  crash. Full interactive wizard/gear/stale-cache click-through from the plan's Step 3 was not
  re-run this session (no UI automation harness available); static launch verification plus
  the individual per-task manual passes already recorded in earlier entries above are the
  closure evidence for this pass.
- **Still pending before this ships to real users:** backend deploy of the fork to
  `api.nano-wetter.de` (plan recorded in `deployment.md`, approved but not executed — the
  `HealthViewModel.makeProvider()` DEBUG branch still points at the local fork by default) and
  the StoreKit paywall (Abo + Lifetime), which is what will eventually replace
  `HealthFeature.isEnabled`'s `#if DEBUG` body. Also deferred: widgets/watchOS/notifications
  consuming the same provider/store, and Aptabase funnel events.
