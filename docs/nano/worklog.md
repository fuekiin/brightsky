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
- [x] CI workflows (done 2026-07-14): additive `.github/workflows/nano.yml` in both forks.
      dwdparse: lint+tests (py 3.9/3.14). brightsky: tests against Postgres 17 + linux/amd64
      image to `ghcr.io/fuekiin/brightsky` tagged `sha-<sha>` / `nano-health` / `nano-v*`,
      via GITHUB_TOKEN (repo granted Write on the package after the manual first push had
      claimed it — "Manage Actions access"). requirements-dev.txt now carries the same
      dwdparse tarball pin as requirements.txt (CI installs only the dev file). First green
      image: `sha-a9000cd`. Doc-only pushes skip CI (`paths-ignore: docs/**`).

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

## 2026-07-14 — Backend deploy: /pollen live on api.nano-wetter.de

**Done** (`bright_sky_config` `671cae1` + `7bc38c3`; server state only, no fork commits):

- Executed `deployment.md`'s approved plan: GHCR package `ghcr.io/fuekiin/brightsky` stayed
  private; authenticated via `docker login ghcr.io` on the server with a read-only PAT
  (scope `read:packages`) instead of flipping the package public.
- `brightsky.yml` image line repointed from upstream `jdemaeyer/brightsky:${TAG:-latest}` to
  `ghcr.io/fuekiin/brightsky:${BRIGHTSKY_IMAGE_TAG}` (pinned, no floating default);
  `BRIGHTSKY_IMAGE_TAG=sha-a9000cd` set in `.env`.
- `docker compose pull && up -d worker web` — only those two containers recreated;
  `postgres`/`redis`/`traefik`/`prometheus`/`grafana` and all bind-mounted data untouched.
- Verified: `pollen` table exists post-migration, existing endpoints kept serving `200 OK`
  through the restart, `GET /pollen?lat=52.52&lon=13.41` → live DWD data for
  Berlin/Brandenburg. Full record + rollback steps in `bright_sky_config`.
- **Still local-fork-only:** the app (`HealthViewModel.makeProvider()`) still points at the
  local dev fork, not `BrightSkyGateway` → prod. Routing the app at prod `/pollen` (and the
  other three health endpoints, which shipped in the same image/migration set but are
  unverified against real DWD data beyond pollen) is the next step before this is a real
  user-facing feature.

## 2026-09-17 — radar3d phase 1: `/radar3d` manifest + rain crops (branch `nano-radar3d`)

**Done** (brief: `../WeatherGermany/docs/superpowers/specs/2026-09-17-radar-3d-backend-brief.md`,
plan: `plans/2026-09-17-radar3d-phase1.md`):

- New `brightsky/radar3d/` package: `grid.py` (mercator voxel grid, bbox snapping), `frame.py`
  (`NANO3D` wire format), `sweeps.py` (sweep names + ODIM-HDF5 reading), `rain.py` (the ported
  voxel-centric gridder with **precomputed per-site index maps**), `store.py` (national `.npy`
  frames + `radar3d_frames` index), `ingest.py` (the stand-alone worker). Two additive routes in
  `web/app.py`, `migrations/0021_radar3d.sql`, `RADAR3D_*` settings, a `radar3d` compose service.
- **The port is byte-exact** against the app repo's `grid_radar_volume.py`: golden test
  `tests/test_radar3d_rain.py` (site isn, 2026-09-16 11:50 UTC, the 300×300 Isen grid; the ten
  sweeps and the reference volume live in `tests/data/radar3d/`).
- Measured on the dev Mac (M-series, 8 cores), Germany 1 km × 500 m grid (698 × 912 × 24):
  geometry precompute 0.1–0.6 s per site, 20–29 MiB per site (~420 MiB for 17, held in RAM);
  a full 17-site cycle (170 sweeps) grids in **~7 s** (10.7 s including the one-time precompute);
  a national frame is 15.3 MB on disk (~550 MB for the 3 h retention); crops over HTTP:
  100 km box at 1 km = 11 KB in ~0.1 s, 250 km box = 248 KB in ~0.09 s, 2 km = a third of that.
- Cross-check against the app session's 2 km national fixture (`radar3d-de-2026-09-16.json`,
  25 cycles 11:00–13:00): echo-presence Jaccard 0.82–0.83, column-composite correlation
  0.74–0.77, our echo tops +0.7 slabs higher, max dBZ within a few dB. Expected: `resolution=2000`
  is a 2×2 **max-pool** of the 1 km grid, the fixture point-sampled voxel centres at 2 km.
- Verified live locally: `radar3d-work` against opendata.dwd.de + `serve` on `127.0.0.1:5599`,
  frames decoded end to end with `frame.decode`. Full suite + ruff green (85 tests) against a
  local Postgres 16 with `cube`/`earthdistance` (no Docker on this machine; the zonky
  `embedded-postgres-binaries-darwin-arm64v8` tarball in the session scratchpad did the job).

**Deviations from the brief** (told the app session):

1. Header byte layout (the brief only listed the fields): little-endian, `NANO3D` u16 version
   u16 width u16 height u16 levels u16 channels u32 zlib-length u32 extra-length + 8 reserved.
2. Frames stored as uncompressed `.npy` under `RADAR3D_DATA_DIR` (default `.data/radar3d`, not
   `/data/radar3d`), memory-mapped for cropping; the `.bin` framing is applied per request.
3. Phase 1 manifest: `clouds`/`cells` are `null`, `flows_clouds: false`; `resolution=2000` is
   max-pooled rain. `distance` is metres (default 100000; caps 250 km / 600 km as in the brief).
4. Manifest timestamps are Bright Sky style (`+00:00`); frame URL paths use `…Z`. A `from`/`to`
   window may span at most 3 h (the retention).

**Open questions / follow-ups:**

- Listing traffic: each site listing is ~1 MB (5760 entries, 48 h, no gzip, no conditional GET
  on the DWD side), so per-minute polling of 17 sites is ~17 MB/min. Follow-up: predict the next
  cycle's file names from the per-tilt second offsets (stable per site) and only list every few
  minutes as a fallback.
- Geometry lives in RAM (~420 MiB). If the 7.7 GB prod box gets tight, save the four per-site
  arrays as `.npy` and `np.load(mmap_mode='r')` them — the code path is the same.
- `pro` (Prötzel) had gaps in the saved 2026-09-16 data; the cycle timeout (7 min after the cycle
  start) grids without it and records `sites` in the index/manifest.
- Not deployed. The prod overlay (service + bind mount on `web`) is written up in
  `deployment.md`; it is a `bright_sky_config` change plus a `docker compose up -d radar3d web`.
