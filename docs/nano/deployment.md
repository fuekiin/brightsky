# Deploying the fork to api.nano-wetter.de

Status 2026-07-14: **plan approved, not yet executed.** The production server still runs the
upstream image. Nothing below has been applied.

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
