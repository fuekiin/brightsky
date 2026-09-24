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
| `push-api` | `push-serve --bind 0.0.0.0:5001 --forwarded-allow-ips <network CIDR>` | registration, activity tokens, catalogue, `/health` |
| `push-work` | `push-work` | warnings (60 s), nowcast and rain/warning Live Activities (5 min), forecast (15 min), digest (07:00), cleanup (hourly) |

Code: `brightsky/push/` — `rules.py` (wire → rule, per-rule rejection), `evaluator.py` (pure
port of `RuleEvaluator`), `firing.py` (transitions against `rule_state`), `dispatcher.py`
(coalescing, rate limit, payload), `sender.py` + `apns.py` (APNs), `sources.py`, `worker.py`,
`api.py`. Schema `push` (migration `0022_push.sql`), nothing references upstream tables.

## Deployment

[push-deploy-runbook.md](push-deploy-runbook.md): the overlay in `brightsky-config`'s conventions,
the secret layout, migrate, start, verify, rollback. It moves to `brightsky-config`
(`docs/deploy-2026-09-push.md`) on the branch the app session prepares there. `push-work` does
not migrate itself; `migrate()` holds an advisory lock, so concurrent migrators are safe.

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

## After the review (2026-09-24)

An independent review of `663146e..e9777e4` confirmed the evaluator port, wire parsing,
firing, payloads, APNs handling and SQL. Changed as a result:

- Rules re-arm only when kind or params change — not when a „Mein Standort" rule moves cell.
- A dismissed warning activity stays dismissed unless the warning escalates; a warning that
  outlives iOS's ~8 h is ended once, not restarted. Expiry ends quietly; only a warning that
  vanished from DWD's snapshot shows „aufgehoben".
- Rain bars go out in mm/h (`RainLive.buckets`). Real rain is one definition everywhere:
  ≥ 0.025 mm per 5 min for ≥ 10 min (`RuleEvaluator.firstRealRain`). A running rain
  activity is judged over the full 2 h at its own cell and left alone without data; only the
  winning rain event rides on the activity. Rain notifications are keyed by place and re-arm
  only when the nowcast shows no real rain.
- `together` windows look back 7 days; all seven days held together count day by day (app
  `04b7648`).
- Abuse limits: adopting needs a ≥ 32-character secret and counts against the per-IP limit;
  cells outside the app's coverage box (47.0–55.5 °N, 5.5–15.5 °E) are rejected with
  `outside_coverage`; at most `PUSH_MAX_CELLS` (20 000) cells, beyond that `capacity`, and
  `/health` warns from 80 %. Bodies are capped at 256 KB, 200 rules, and short strings.
- One failing rule, cell or device is logged and skipped; it no longer stops a loop.
- Live Activity robustness: one lock per device across loops, `timestamp` stamped at send,
  the row written before a start is sent, token reports never clobbered, a late DELETE cannot
  end a newer activity, a dead activity token ends the row, starts are never retried,
  `apns-expiration` on every push.
- Devices unseen for 90 days are deleted; `/health` no longer shows the device count or URLs.

## Rules redesign (2026-09-24)

The app now has three features per place in one „Mitteilungen" sheet: official warnings and a
rain countdown (switches, free) and own notifications (forecast values, Rückenwind). The
switches register as ordinary `dwd_warning` / `rain_nowcast` rules (warnings: `nextHours 48`,
`live {"night": true}` when „kurze Unwetter live" is on; rain: always live). Server side:

- **`rain.min`** — `{"rain": {"min": "light" | "moderate" | "heavy"}}`: real rain is ≥ 0.3,
  2.5 or 10 mm/h for ≥ 10 minutes; absent means light; anything else is rejected with
  `unknown_intensity`. The same threshold decides the match and the activity's states.
- **Notice gates** — the same day opens at 07:00, the day before and two days before at
  18:00 that evening; `today` behaves as the same day, `tomorrow` as the day before. This
  replaces the missing gate that let a „morgen" rule arrive at 01:23 in the device test.
- **No quiet hours** — decided 2026-09-24 („maybe later"): forecast rules decide at any hour.
  The notice gates above are not quiet hours; they say when an occasion may first report.
- **Catalogue** — copied again (version 2, app `172912b`): the warning and rain presets are gone
  (they are switches).
- `tier` is now `free` or `supporter` — still a client claim the server does not enforce.

## Warning Live Activities: short events, level 4 always (2026-09-24)

- A `dwd_warning` registration with `live` („Kurze Unwetter live") makes only short events live:
  families `gewitter` (hail included), `sturm`, `regen`. Frost, heat, fog and the rest arrive as
  notifications.
- An **extreme warning (level 4)** is live for every matching `dwd_warning` registration, of any
  family, even without `live` — still only with Live Activities enabled and a push-to-start token,
  and with the same 8 h lifetime. In the precedence it comes before level 3, which comes before
  everything else.
- Level 4 is titled in DWD's capitals, **„EXTREMES UNWETTER"**, in the Live Activity alert and in
  the notification's fallback; levels 1–3 keep their names.
- Level 4 alerts again, with sound, when it begins (upcoming → active); levels 1–3 begin silently.

## One warning, one notification per device (2026-09-25)

A saved place with automatic switching („Zuhause") and the „Mein Standort" switch cover the same
area while at home; both `dwd_warning` registrations match the same DWD warning.

- **Warnings:** a device is told about each warning once. Within a tick the best-ranked
  registration speaks: a place the user chose before „Mein Standort" (`params.origin ==
  "current"`, anything else counts as chosen), then the broader switch (lower `minLevel`), then
  the lower rule id. Across ticks the registration that already told keeps the warning; another
  one that matches later gets its thread marked `covered_by` and never notifies — escalations
  follow the single thread (`firing.dedupe_warnings`, before the Live Activity takes its share).
- **Rain** is a local event, so it is deduplicated per device *and area*: a device's rain
  registrations belong together when their cell centres are at most 10 km apart (the app's largest
  geofence radius; single link, so a chain of close places is one area). A running activity is the
  notification for its own area only; rain elsewhere still notifies. Without an activity, per area:
  while one registration there has told (disarmed), no other does; within a tick a chosen place,
  then the broader threshold, then the lower rule id (`firing.dedupe_rain`, `rain_areas`).
- User rules are untouched: they are distinct rules.

## Load on `web` (2026-09-24)

Measured against production over 7 days (Traefik via Prometheus): 3 req/s at night, 23–31 by
day (peak 33.5); by day p95 1.3–3 s, 0.8 % of requests over 5 s and 1.6 % ending in 499 —
before any push traffic. So `push-work` must add little and, above all, no bursts:

- **Nowcast: aligned to DWD's radar frames.** DWD publishes a frame every 5 minutes (about :x3:40
  and :x8:40) and the ingest worker has it about 40 s later. The loop checks the newest radar
  timestamp every minute (`SELECT max(timestamp) FROM radar`, well under 1 ms) and evaluates
  when a new frame is in — within a minute instead of 0–5 minutes (2.5 on average) — and at
  least every 5 minutes if ingest stalls. The dev setup has no radar table and runs on the floor.
- **Nowcast: one request per evaluation.** `/radar?format=compressed` without a bounding box, every
  5 minutes — the stored national frames as they are (about 2.5 MB, 24 frames), a plain read
  for `web` whatever the number of users. Each cell reads its own pixel, the same one
  `/radar?distance=1` crops (verified against production: 120 of 120 values identical).
- **Forecast: one request at a time, spread over the cycle.** `/weather` (about 15 ms of
  server time) per distinct cell, spaced over 80 % of the 15 minutes, never more than 1 s
  apart: 1,000 cells ≈ 1.1 req/s, evenly. The load grows with distinct cells, not users.
- Warnings cost `web` nothing (the loop reads the alerts table), plus one DWD listing a minute.

The switch criterion from the design stands: if the public routers' tail latency answers to
the evaluation load, move `/weather` lookups to direct SQL behind the same source interface.

## Known limits (deferred, 2026-09-24)

Three findings of the PR review ([review 5310197722](https://github.com/fuekiin/brightsky/pull/1#pullrequestreview-5310197722),
#2, #10, #11) were deliberately left for when the load is real. None matters at a few hundred
cells; each has a clear trigger to watch for and a planned fix.

### Forecast pacing ignores request time (review #2)

`forecast_tick` fetches `/weather` one cell at a time and waits `spacing` between requests,
without subtracting how long each request took. With `/weather` at ~15 ms that is fine, but at a
daytime peak (1–3 s per request) a 15-minute cycle stops fitting after roughly 600 cells, and
at the 20,000-cell cap one tick takes hours: rules are then decided on stale forecasts. Worse,
the warnings loop calls `hours_for` for warning rules with value conditions and fetches
`/weather` itself, serially, which delays warnings for everyone.

- **Trigger:** the forecast tick's duration (logged as „forecast: evaluated N rules in Xs")
  approaches its 15-minute interval, or `/health` shows `forecast` older than ~20 minutes.
- **Fix:** subtract each request's own duration from the spacing; when behind schedule, fetch
  with a small bounded concurrency (2); let the warnings loop use cached hours only and never
  fetch.

### Registration: one statement per rule (review #10)

`store.register` runs on every app launch and issues 3–4 statements per rule (cell insert,
previous-params select, rule upsert, possible state delete), up to about 200 round trips inside
a transaction that holds the device row lock.

- **Trigger:** `POST /v1/devices` latency in Traefik (router `push`) climbing above ~100 ms, or
  launch spikes (a new app release) queueing registrations.
- **Fix:** batch them: one multi-row `INSERT … ON CONFLICT` for cells, one select of all
  previous params, one multi-row upsert for rules, one `DELETE … WHERE rule_id = ANY(…)` for the
  state of edited rules.

### Database pool of 4 for 5 loops (review #11)

`push-work` opens `store.pool(max_size=4)` for the warnings, forecast, nowcast, digest and
cleanup loops. The warnings and digest loops keep their connection during HTTP and APNs calls,
so with both busy the per-minute nowcast check (the frame-aligned trigger) can wait for a
connection.

- **Trigger:** the nowcast loop evaluating noticeably later than a minute after a new radar
  frame (compare its log with `parsed_files` for `composite/rv`), mostly around 07:00 (digest)
  or during widespread warnings.
- **Fix:** release connections around network I/O (acquire per database step, not per tick),
  or simply raise the pool to 8 — Postgres has 100 connections, `web` and the workers use about
  25.

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
