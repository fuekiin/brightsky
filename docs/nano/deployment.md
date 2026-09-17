# Deploying the fork to api.nano-wetter.de

Status 2026-07-14: **executed.** Production now runs `ghcr.io/fuekiin/brightsky:sha-a9000cd`.
`/pollen` is live. See `bright_sky_config` `docs/deploy-2026-07-14-pollen.md` for the
verified record and `docs/pre-deploy-2026-07-14-pollen.md` for the rollback snapshot.

## Current production setup (verified via ~/Development/bright_sky_config)

- `ubuntu@api.nano-wetter.de`, `/home/ubuntu/brightsky` = clone of
  `jdemaeyer/brightsky-infrastructure` @ `2792fa4`, overlays `brightsky traefik analytics`.
- The stack pulls the **upstream Docker Hub image** `jdemaeyer/brightsky:latest` — the server
  never builds code.
- Local deltas (workers=3, analytics labels, Postgres `ALTER SYSTEM` tuning inside
  `.data/postgres`) are documented in the `bright_sky_config` repo, which is the deployment
  source of truth.

## The change

Deploying the fork = the stack pulls **our image** instead. Config is safe by construction:
`brightsky.env`, compose overlays, traefik state and the Postgres data dir (including the
tuning) live outside the image and are untouched. The worker's `--migrate work` applies our
additive migrations (`0019_pollen.sql`, …) on first start.

## First deploy, step by step

1. Push `nano-health` in both forks (dwdparse first — requirements.txt pins its SHA as a
   GitHub **tarball** so the `python:3.14-slim` image build needs no git binary).
2. Build + push the image: `ghcr.io/fuekiin/brightsky:nano-<version>` (CI job, see below).
3. On the server, edit `brightsky.yml`: image → `ghcr.io/fuekiin/brightsky:${BRIGHTSKY_IMAGE_TAG}`
   (drop the `:-latest` default — always pin). If the GHCR package is private:
   one-time `docker login ghcr.io` with a read-only PAT.
4. `docker compose -f brightsky.yml -f traefik.yml -f analytics.yml pull`
   then `... up -d worker web`.
5. Verify `https://api.nano-wetter.de/pollen?lat=52.52&lon=13.41`. Data appears after the
   worker's first poll; force with
   `docker compose run --rm brightsky parse https://opendata.dwd.de/climate_environment/health/alerts/s31fg.json`.
6. Record the image-line delta + deployed tag in `bright_sky_config` (README table +
   `upstream.patch`), like the existing deltas.

**Rollback:** set the image back to `jdemaeyer/brightsky:<tag>`, `up -d`. New tables stay
behind unused — additive migrations make rollback a non-event.

## CI/CD (agreed direction)

Build pipeline yes, auto-deploy no:

- **dwdparse fork**: Actions on push → pytest.
- **brightsky fork**: Actions on push to `nano-health` → pytest (Postgres service container),
  build image, push to GHCR tagged `sha-<sha>` + human tag.
- **Deploy stays a manual one-liner** on the server (`BRIGHTSKY_IMAGE_TAG=… compose pull &&
  up -d`), optionally later a `workflow_dispatch` SSH job. Deploy scripts and the tag ledger
  belong in `bright_sky_config`, not in the forks.

## radar3d container

Status 2026-09-18: **deployed.** Production runs `ghcr.io/fuekiin/brightsky:sha-59a3173`
(`nano-health` tip, tag `nano-v3`) with the third container `brightsky-radar3d-1`; record and
post-deploy comparison in `bright_sky_config` `docs/deploy-2026-09-18-radar3d.md`.

The 3D radar pipeline (`docs/nano/architecture.md`, "Radar 3D pipeline") ships in the same
image but runs as its **own container**, so the ingest worker's CPU bursts stay unaffected.
Overlay for `bright_sky_config`'s `brightsky.yml` (the frames directory must be visible to
both the worker that writes it and `web` that serves crops from it):

```yaml
x-brightsky:
  &brightsky
  # ...existing...
  volumes:
    - .data/brightsky:/app/.cache
    - .data/radar3d:/app/.data/radar3d      # new: radar3d frames (~600 MB at 3 h retention)

services:
  radar3d:
    <<: *brightsky
    command: --migrate radar3d-work
    restart: always
```

Then `docker compose ... pull && up -d radar3d web`. Expectations on the 4-core / 7.7 GB box
(all three products, measured on the dev Mac 2026-09-17):

| | |
|---|---|
| RAM | rain geometry ~420 MiB + ICON steps (~15 MB each, ≤12 kept) + transients; sites are gridded one at a time. Watch the container's RSS after the first ICON run (`docker stats`). |
| CPU | rain 3–13 s per 5-min cycle; cloud frame ~0.5 s per cycle; ICON run load ~3–4 min per 3 h (374 GRIB decodes per step × 6 steps, in the ICON thread); forecast frames ~0.5 s each, 12 per run load + 1 per cycle; cells negligible |
| Disk | rain 15.3 MB + clouds 7.6 MB + flow 1.3 MB per 5 min → ~0.9 GB at 3 h retention; forecast ~150 MB per run key (12 × 12.7 MB, plus one frame per cycle), two run keys kept, 3 h retention by stamp; raw sweeps/GRIBs transient (≤ ~1 GB during an ICON download; downloads stop below `RADAR3D_MIN_FREE_GB` = 5 GB free) |
| Traffic | ~1 MB/min sweep listings + ~10 MB per cycle sweeps + ~450 MB per ICON run with the forecast (6 steps incl. `qr`; every 3 h, ~3.5 GB/day) + <0.1 MB/min KONRAD3D |

The image needs `eccodes` (pinned in `requirements.txt`; the wheel bundles the C library).
Migration `0021_radar3d.sql` is additive. Rollback: `docker compose stop radar3d`, the
`/radar3d` routes then answer 404.
