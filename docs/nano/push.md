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
- **Quiet hours** — no forecast (`user_rule`) notifications between 22:00 and 07:00. Nothing
  is decided during them; the forecast loop wakes at 07:00 and decides on the morning's
  forecast, so what still holds and is still open goes out then, worded for the morning.
  Warnings, rain and the Morgenübersicht are unaffected.
- **Catalogue** — copied again: the warning and rain presets are gone (they are switches).
- `tier` is now `free` or `supporter` — still a client claim the server does not enforce.

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
