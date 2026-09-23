# Runbook: deploy the push service to push.nano-wetter.de

Draft for `brightsky-config` `docs/deploy-2026-09-push.md` — move it there with the overlay
below once the config branch exists. **Not executed. No step here runs without Benjamin's OK.**

Adds two containers (`push-api`, `push-work`) from the same fork image the stack already
runs. Additive only: new schema `push` (migration `0022_push.sql`), new Traefik router, no
change to `web`, `worker` or `radar3d`.

## 0. Preconditions

- `nano-push` reviewed and merged into `nano-health`; CI green; note the image tag
  `sha-<commit>` it produced on GHCR.
- DNS: `push.nano-wetter.de` → the VPS IP (same as `api.nano-wetter.de`). Let's Encrypt needs
  it resolving before the first start, or Traefik retries until it does.
- **Capture the live compose file first.** `server/brightsky.yml` in brightsky-config is the
  July capture and lacks the September radar3d changes. Before editing anything:
  ```bash
  scp ubuntu@api.nano-wetter.de:~/brightsky/brightsky.yml server/brightsky.yml
  git diff server/brightsky.yml    # expect the radar3d service + volumes; commit that capture on its own
  ```

## 1. Secret

The APNs key (`AuthKey_8GS8RHYV67.p8`, key id `8GS8RHYV67`, team id from `team.env`) lives on
the host only:

```bash
ssh ubuntu@api.nano-wetter.de 'mkdir -p ~/secrets && chmod 700 ~/secrets'
scp ~/Development/nano-push-secrets/AuthKey_8GS8RHYV67.p8 ubuntu@api.nano-wetter.de:~/secrets/
ssh ubuntu@api.nano-wetter.de 'chmod 600 ~/secrets/AuthKey_8GS8RHYV67.p8'
```

Never in a repository. One key serves both APNs environments and does not expire.

## 2. Env

`~/brightsky/.env` (from `server/env.example`) gains `HOSTNAME_PUSH=push.nano-wetter.de` and the
new `BRIGHTSKY_IMAGE_TAG=sha-<commit>` from step 0.

A new `~/brightsky/push.env`, mode 600, holds the APNs settings, so no key material or IDs sit
in the compose file:

```
BRIGHTSKY_PUSH_APNS_KEY_PATH=/run/secrets/apns.p8
BRIGHTSKY_PUSH_APNS_KEY_ID=8GS8RHYV67
BRIGHTSKY_PUSH_APNS_TEAM_ID=<TEAM_ID from team.env>
BRIGHTSKY_PUSH_WEATHER_URL=http://web:5000
```

## 3. Compose overlay

Add to the live `brightsky.yml` under `services:` (the `x-brightsky` anchor only exists in that
file, so this cannot be a separate overlay file):

```yaml
  push-api:
    <<: *brightsky
    command: push-serve --bind 0.0.0.0:5001 --forwarded-allow-ips <compose network CIDR>
    restart: always
    mem_limit: 256m
    labels:
      - traefik.enable=true
      - traefik.http.routers.push.rule=Host(`${HOSTNAME_PUSH}`)
      - traefik.http.routers.push.entrypoints=websecure
      - traefik.http.routers.push.tls.certresolver=letsencrypt
      - traefik.http.services.push.loadbalancer.server.port=5001
  push-work:
    <<: *brightsky
    command: push-work
    restart: always
    mem_limit: 512m
    env_file:
      - brightsky.env
      - push.env
    volumes:
      - .data/brightsky:/app/.cache
      - /home/ubuntu/secrets/AuthKey_8GS8RHYV67.p8:/run/secrets/apns.p8:ro
```

Notes:
- `--forwarded-allow-ips`: only Traefik may set `X-Forwarded-For`, or anyone could pick the IP
  the registration rate limit counts. Take the stack network's subnet from
  `docker network inspect brightsky_default -f '{{(index .IPAM.Config 0).Subnet}}'`.
  `push-api` publishes no port, so every request comes through Traefik.
- `env_file` on a service replaces the anchor's `env_file`, so `brightsky.env` is listed again.
  The anchor's `environment` list (database and Redis URLs) still applies.
- The router labels follow the grafana router in `analytics.yml` (`websecure`, `letsencrypt`).
  Check the `web` router in the upstream `traefik.yml` uses the same names.
- `push-work` reads forecasts and the radar from `web` over the compose network (design §3),
  never through Traefik, so its load does not show on the public routers. It is roughly one
  request per distinct rule cell every 5–15 min.
- `mem_limit`s isolate a leak from `web` (design §9). Measured locally on 2026-09-24: 46 MB
  (`push-serve`) and 53 MB (`push-work`) RSS. `push-work` additionally loads DWD's warn-cell
  polygons (the same tree `web` uses for `/alerts`) the first time a new cell needs resolving;
  that has not been measured. Watch `docker stats` after the first registrations and raise the
  limit if it gets close.

## 4. Migrate, then start

`push-work` does not migrate. Migration 0022 is applied by the `worker` container
(`--migrate work`), or explicitly first:

```bash
cd ~/brightsky
C="docker compose -f brightsky.yml -f traefik.yml -f analytics.yml"
$C pull
$C run --rm brightsky migrate      # applies 0022_push.sql; safe to run twice
$C up -d worker web radar3d        # the new image tag recreates them anyway
$C up -d push-work push-api
```

Concurrent `--migrate` containers are safe since this release: `migrate()` holds a Postgres
advisory lock.

## 5. Verify

```bash
curl -s https://push.nano-wetter.de/health | jq .
#   status ok, database true; after ~1 min warnings/forecast/nowcast lastSuccessAgeSeconds small,
#   warnings lastError null (it checks the alerts table against DWD's CAP listing)
curl -sI https://push.nano-wetter.de/v1/catalog | grep -i cache-control   # max-age=86400
docker logs --since 5m brightsky-push-work-1 | grep -v DEBUG | tail
#   "warnings: evaluated N rules", no "dry-run mode" line (that would mean the key is missing)
```

Then point a phone's Debug → Push-Server at production (or ship the build that defaults to it),
check that the device appears in `push.devices`, and send one hand-made push:

```bash
docker compose -f brightsky.yml run --rm push-work push-send <deviceId>
```

The production app then registers against `https://push.nano-wetter.de`; every registration
is logged by `push-api`.

## 6. Rollback

```bash
docker compose -f brightsky.yml -f traefik.yml -f analytics.yml stop push-api push-work
```

The app treats an unreachable push server like any other outage: rules stay on the device, and
registration is retried on the next launch. Schema `push` stays behind unused (additive
migration, like pollen and radar3d). To remove it entirely: `DROP SCHEMA push CASCADE;` and
`DELETE FROM migrations WHERE id = 22;`. Only do that when the push service is not coming back
soon, since it discards every registration.

## 7. Record

Like the earlier deploys: the image tag and the overlay go in the config repo (`server/brightsky.yml`,
README table), plus a `docs/deploy-2026-09-push.md` entry recording what was verified.
