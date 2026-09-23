# nano push service — push.nano-wetter.de

Turns DWD data into notifications for rules people wrote in the app. Spec:
app repo `docs/superpowers/specs/2026-09-22-push-backend-design.md` (backend) and
`…-notification-rules-design.md` (what a rule means). Reference implementation the server
must agree with: `WeatherCore/Sources/WeatherCore/Notifications/` (`RuleEvaluator`,
`RuleWireMapper`, `NotificationComposer`), app commit `dc3bc1b` and later.

## Shape

Two containers from the **same fork image**, like radar3d:

| Container | Command | Job |
|---|---|---|
| `push-api` | `push-serve --bind 0.0.0.0:5001 --forwarded-allow-ips '*'` | registration, activity tokens, `/health` |
| `push-work` | `--migrate push-work` | warnings loop (60 s), forecast loop (15 min), cleanup |

Code: `brightsky/push/` — `rules.py` (wire → rule, per-rule rejection), `evaluator.py` (pure
port of `RuleEvaluator`), `firing.py` (transitions against `rule_state`), `dispatcher.py`
(coalescing, rate limit, payload), `sender.py` + `apns.py` (APNs), `sources.py`, `worker.py`,
`api.py`. Schema `push` (migration `0022_push.sql`), nothing references upstream tables.

## Overlay for bright_sky_config's `brightsky.yml`

```yaml
services:
  push-api:
    <<: *brightsky
    command: push-serve --bind 0.0.0.0:5001 --forwarded-allow-ips '*'
    restart: always
    labels:
      - traefik.enable=true
      - traefik.http.routers.push.rule=Host(`push.nano-wetter.de`)
      - traefik.http.routers.push.entrypoints=websecure
      - traefik.http.routers.push.tls.certresolver=<same resolver as web>
      - traefik.http.services.push.loadbalancer.server.port=5001
  push-work:
    <<: *brightsky
    command: --migrate push-work
    restart: always
    environment:
      BRIGHTSKY_PUSH_APNS_KEY_PATH: /run/secrets/apns.p8
      BRIGHTSKY_PUSH_APNS_KEY_ID: 8GS8RHYV67
      BRIGHTSKY_PUSH_APNS_TEAM_ID: <team id>
      BRIGHTSKY_PUSH_WEATHER_URL: http://web:5000
    volumes:
      - /home/ubuntu/secrets/AuthKey_8GS8RHYV67.p8:/run/secrets/apns.p8:ro
```

`environment:` on a service replaces the anchor's map in Compose merges — copy the anchor's
`BRIGHTSKY_DATABASE_URL`/`REDIS_URL` into it (or use `env_file: brightsky.env`, whichever the
stack uses). Traefik labels must match the existing `web` router's conventions (entrypoint and
resolver names) — check them in the config repo. The `.p8` lives on the host only, mode 600,
never in a repository. One key serves both APNs environments.

Without the key variables `push-work` runs in **dry-run**: it evaluates and logs payloads,
sends nothing.

## Decisions made while building (agreed with the app side)

- **Auth.** No bearer → create, or take over a known id with a fresh secret (the device id is
  122 random bits only device and server know). Bearer on an unknown id → adopt it (DB
  restore, no 401 round-trip). Wrong bearer on a known id → 401; the app's retry without it
  takes over. Other endpoints: 404 unknown device, 401 bad secret. Unauthenticated
  registration is limited per IP (`PUSH_REGISTER_RATE_LIMIT`/h; in memory, one process).
- **Rejections** (per rule, app reverts the switch): `unknown_kind`, `health_not_available`,
  `bad_cell_key`, `bad_conditions`, `unknown_metric`, `unknown_window`, `unknown_schedule`,
  `live_not_applicable`, `duplicate_id`, `device_limit` (50). A `once` rule that is over is
  accepted and not stored. Request-level problems → 422.
- **Live** is allowed on any warning family and on rain (rules design §19 supersedes the
  backend spec's family restriction).
- **cellKey** `"%.2f,%.2f"` of the app's quantised cell centre; cells are ≥ 0.038° apart, so
  keys never collide.
- **Notification payload**: `aps.alert` is the server-written fallback
  (`NotificationComposer.fallback`, ported), `mutable-content: 1`, `time-sensitive` for
  warning rules, `thread-id` = lead rule; `nano` = `{v, kind, ruleId, ruleIds, cellKey,
  occurrence, evidence}` where `evidence` is the app's `RuleEvidence` (values keyed by
  `Metric` raw value). One push per event per device; lead rule = strongest delivery, then
  the app's order.
- **Live Activity content-state** is `WeatherLiveContent` as Swift's synthesized Codable
  writes it (dates as seconds since 2001), `placeName: ""`, no `title`, `ruleId` of the lead
  rule — the widget resolves names from the App Group.
- **Warning threads.** DWD re-issues warnings under new alert ids. A new alert of the same
  family overlapping a thread's end (± 1 h) continues it and fires only on escalation.
- **Staleness.** The warnings loop compares DWD's CAP listing with `parsed_files` once a
  minute; if the alerts table has not matched for 10 minutes it stops evaluating and
  `/health` shows the error.
- **State before send** (at-most-once): a crash loses a notification rather than repeating
  it. Per-device limit `PUSH_MAX_ALERTS_PER_HOUR` (10), logged and audited when it trips.
- `rules.warn_cell_id` from the spec is not stored; the warn cell lives on `cells` and is
  joined.

## Status

| Step (backend §12) | State |
|---|---|
| 1 schema, `POST /v1/devices`, `/health` | done, verified with the simulator and the iPhone |
| 2 sender, `push-send` | done; alert, push-to-start, update and end delivered to the iPhone (production), the activity token PUT arrives in the background |
| 3 warnings loop | done, runs against live DWD data locally |
| 4 forecast loop, `user_rule` | done |
| 5 `GET /v1/catalog` | done — serves `brightsky/push/catalog.json` verbatim, 24 h cache |
| 6 digest (07:00) | done — one Morgenübersicht per device, retried until 10:00 |
| 7 nowcast, rain + warning Live Activities | done; an automatic rain start went out from real radar data (sandbox 200) |

**The catalogue is owned by the app repo** (`WeatherGermany/docs/push/catalog.json`, generated
from `RuleCatalog.fallback` and pinned by a WeatherCore test). Never edit the copy here — copy
it again. `test_catalog_templates_are_rules_the_server_accepts` checks every preset registers.

**Morgenübersicht payload**: fallback „Morgenübersicht: 2 Regeln" / „2 deiner Regeln treffen
heute zu.", thread-id `digest`, `nano = {v, kind: "digest", date, ruleIds, items: [{ruleId,
ruleIds, kind, cellKey, occurrence, evidence}]}` — the extension composes
`NotificationComposer.digest(titles:)` from the device's rule names.

**Live Activities**: rain starts on real rain (≥ 0.3 mm/h for ≥ 10 min) within 60 min, updates
only when the predicted change moves > 5 min or the class changes (≥ 10 min apart unless it
escalates), ends with `dismissal-date` +15 min and a 60-min cooldown, 4 h at most. A warning takes
over the same activity by update (upcoming → active, escalation with alert, cancelled → end,
expiry → end). Between 22 and 6 starts are silent except warnings from level 3. Without a
push-to-start token, with Live Activities off, or when APNs refuses the start, the event arrives
as a notification. The server cannot rank „the place you are at" (it does not know which cell
is the current location), so precedence is: severe warnings, then warnings before rain, then
the earlier.

## Local development

```bash
createdb -h localhost brightsky_push
export BRIGHTSKY_DATABASE_URL=postgres://localhost/brightsky_push
.venv/bin/python -m brightsky migrate
.venv/bin/python -m brightsky push-serve --bind 127.0.0.1:5001
# alerts, as the ingest worker would keep them:
.venv/bin/python -m brightsky parse https://opendata.dwd.de/weather/alerts/cap/COMMUNEUNION_DWD_STAT/Z_CAP_C_EDZW_LATEST_PVW_STATUS_PREMIUMDWD_COMMUNEUNION_MUL.zip
BRIGHTSKY_PUSH_WEATHER_URL=https://api.nano-wetter.de .venv/bin/python -m brightsky push-work
.venv/bin/python -m brightsky push-send <deviceId> [--kind alert|start|update|end]
```

Tests: `tests/test_push_*.py`; `test_push_evaluator.py` mirrors the app's
`RuleEvaluatorTests` — change both together.
