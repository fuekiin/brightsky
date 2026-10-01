"""push-work: the evaluation loops (design §2).

One process, several concurrent loops sharing the connection pool and the
per-cell forecast memo. A loop's failure is logged and retried next tick;
it never takes the process down.
"""

import asyncio
import datetime
import logging
import signal

from brightsky.push import (
    apns, berlin, dispatcher, evaluator as ev, firing, live, livectl,
    rules as rulemod, sender, sources, store,
)


logger = logging.getLogger('brightsky.push.worker')

CELL_RESOLVE_RETRY = datetime.timedelta(days=1)
CELL_RESOLVE_ERROR_RETRY = datetime.timedelta(minutes=10)
# The forecast loop spreads its requests over this share of its cycle, but
# never waits longer than FORECAST_MAX_SPACING between two of them.
# How often the nowcast loop asks whether a new radar frame is in.
NOWCAST_CHECK_S = 60
FORECAST_PACE = 0.8
FORECAST_MAX_SPACING = 1.0
# The digest spreads its forecast requests over this many seconds: at 07:00
# every digest cell meets the app's morning peak on `web`.
DIGEST_SPREAD_S = 300
# … at most 20 a second: with many cells the spread grows instead.
DIGEST_MIN_SPACING = 0.05
AUDIT_RETENTION = datetime.timedelta(days=30)
DEVICE_RETENTION = datetime.timedelta(days=90)
DISABLED_RETENTION = datetime.timedelta(days=7)


def utcnow():
    return datetime.datetime.now(datetime.timezone.utc)


class DryRunClient:
    """Stands in for APNs when no key is configured: logs, sends nothing."""

    async def send(self, token, environment, payload, **kwargs):
        logger.info('[dry run] %s push to %s…: %s', environment, token[:8],
                    payload)
        return apns.Result(-1, 'dry_run')

    async def aclose(self):
        pass


def rule_from_row(row):
    return rulemod.parse_rule({
        'id': str(row['id']), 'kind': row['kind'],
        'cellKey': row['cell_key'], 'params': row['params'],
        'schedule': row['schedule'], 'live': row['live'],
    })


RULES_SQL = """
    SELECT r.*, c.lat, c.lon, c.warn_cell_id,
           d.id AS d_id, d.apns_token, d.environment,
           d.push_to_start_token, d.live_activities_enabled,
           d.live_unconfirmed, d.live_confirmed
    FROM push.rules r
    JOIN push.cells c USING (cell_key)
    JOIN push.devices d ON d.id = r.device_id
    WHERE r.enabled AND r.kind = $1
      -- Morgenübersicht rules belong to the digest loop (design §12.6)
      AND r.schedule IS NULL
"""


DIGEST_SQL = """
    SELECT r.*, c.lat, c.lon, c.warn_cell_id,
           d.id AS d_id, d.apns_token, d.environment,
           d.push_to_start_token, d.live_activities_enabled,
           d.live_unconfirmed, d.live_confirmed
    FROM push.rules r
    JOIN push.cells c USING (cell_key)
    JOIN push.devices d ON d.id = r.device_id
    WHERE r.enabled AND r.schedule IS NOT NULL
"""
# The Morgenübersicht goes out at 07:00 Europe/Berlin; if a source is down
# it is retried every minute, but a digest after 10:00 is no longer one.
DIGEST_HOURS = range(7, 10)


async def load_states(conn, rule_ids):
    rows = await conn.fetch(
        'SELECT * FROM push.rule_state WHERE rule_id = ANY($1::uuid[])',
        rule_ids)
    out = {}
    for r in rows:
        out.setdefault(str(r['rule_id']), {})[r['occurrence_key']] = r
    return out


def device_of(row):
    return {'id': row['d_id'], 'apns_token': row['apns_token'],
            'environment': row['environment'],
            'push_to_start_token': row['push_to_start_token'],
            'live_activities_enabled': row['live_activities_enabled'],
            'live_unconfirmed': row.get('live_unconfirmed') or 0,
            'live_confirmed': row.get('live_confirmed', True)}


# A running activity whose rule is disabled (missing from the device's last
# registration) is as gone as one whose rule was deleted: rule_id, cell_key
# and rule_params are NULL.
RUNNING_SQL = """
    SELECT la.device_id, la.activity_id, la.phase, la.activity_token,
           la.started_at, la.last_update_at, la.last_content, la.state,
           la.ended_at, la.cooldown_until,
           r.id AS rule_id, r.cell_key, r.params AS rule_params,
           c.warn_cell_id, c.lat, c.lon,
           d.id AS d_id, d.apns_token, d.environment,
           d.push_to_start_token, d.live_activities_enabled,
           d.live_unconfirmed, d.live_confirmed
    FROM push.live_activities la
    JOIN push.devices d ON d.id = la.device_id
    LEFT JOIN push.rules r ON r.id = la.rule_id AND r.enabled
    LEFT JOIN push.cells c ON c.cell_key = r.cell_key
    WHERE la.ended_at IS NULL AND la.phase = $1
"""


def rain_threshold(params):
    """The running activity's own rule's `rain.min`, in mm per 5 min."""
    for c in (params or {}).get('all', ()):
        if isinstance(c, dict) and isinstance(c.get('rain'), dict):
            minimum = c['rain'].get('min', 'light')
            if minimum in rulemod.RAIN_MINIMUM_MM_PER_H:
                return rulemod.RAIN_MINIMUM_MM_PER_H[minimum] / 12
    return live.RAIN_MM_PER_5MIN


class isolated:
    """One rule, cell or device failing — a bad stored rule, a forecast
    that times out — is logged and skipped; it must not stop the loop for
    everyone else (review #6)."""

    def __init__(self, what, key):
        self.what, self.key = what, key

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is None or not issubclass(exc_type, Exception):
            return False
        logger.error('Skipping %s %s: %r', self.what, self.key, exc,
                     exc_info=(exc_type, exc, tb))
        return True


async def running_threads(conn, rows):
    """The alert ids of each running warning activity's thread, from its
    rule's `rule_state` (device id → set of alert ids)."""
    pairs = [(r['rule_id'], r['state'].get('event')) for r in rows
             if r['rule_id'] is not None and r['state'].get('event')]
    if not pairs:
        return {}
    found = await conn.fetch(
        """
        SELECT s.rule_id, s.occurrence_key, s.state
        FROM push.rule_state s
        JOIN unnest($1::uuid[], $2::text[]) AS p(rule_id, occurrence_key)
          USING (rule_id, occurrence_key)
        """, [p[0] for p in pairs], [p[1] for p in pairs])
    by_rule = {(str(f['rule_id']), f['occurrence_key']):
               set(f['state'].get('alert_ids', ())) for f in found}
    return {str(r['d_id']): by_rule.get(
                (str(r['rule_id']), r['state'].get('event')))
            for r in rows if r['rule_id'] is not None}


def warning_status(row, alert_ids, obs):
    """What became of a running warning activity's warning (review #5):

    - 'gone': its rule was deleted or disabled;
    - 'cancelled': none of its thread's alerts is in DWD's snapshot any
      more, anywhere — DWD withdrew it;
    - 'elsewhere': it still exists, but not at the rule's current warn
      cell (a „Mein Standort" rule moved);
    - 'here': it is at the rule's cell;
    - 'unknown': nothing to judge by (thread state gone, or the new
      cell's warn cell not resolved yet) — leave the activity alone.
    """
    if row['rule_id'] is None:
        return 'gone'
    if alert_ids:
        if not alert_ids & obs.ids:
            return 'cancelled'
        if row['warn_cell_id'] is None:
            return 'unknown'
        here = {w.id for w in obs.lookup(row['warn_cell_id'])}
        return 'here' if alert_ids & here else 'elsewhere'
    family = row['state'].get('family')
    if row['warn_cell_id'] is not None and any(
            w.family == family for w in obs.lookup(row['warn_cell_id'])):
        return 'here'
    return 'unknown'


# rule_state occurrence of a live rain rule whose start waits for a second
# radar frame (`confirmed`); fired_at is the first frame that showed it.
LIVE_SEEN = 'liveSeen'
LIVE_SEEN_KEEP = datetime.timedelta(hours=6)


def confirmed(rain, seen, now):
    """May this rain start an activity now? At once when it already rains
    or the rain is heavy; otherwise only when an earlier radar frame
    showed it too — one frame's shower often vanishes in the next one
    (review 2026-10-01)."""
    return (rain.state == 'raining' or rain.heavy
            or (seen is not None
                and now - seen['fired_at'] >= live.CONFIRM_AFTER))


async def mark_seen(conn, new, gone, now):
    if new:
        await conn.execute(
            """
            INSERT INTO push.rule_state (
              rule_id, occurrence_key, state, fired_at, expires_at)
            SELECT id, $2, '{}'::jsonb, $3, $4 FROM unnest($1::uuid[]) id
            ON CONFLICT (rule_id, occurrence_key) DO NOTHING
            """, new, LIVE_SEEN, now, now + LIVE_SEEN_KEEP)
    if gone:
        await conn.execute(
            'DELETE FROM push.rule_state '
            'WHERE occurrence_key = $2 AND rule_id = ANY($1::uuid[])',
            gone, LIVE_SEEN)


def hold_decisions(decided, waiting, carried):
    """For devices whose live rain start waits for its second frame
    (`waiting`: device → cell) and that no activity carries: the rain rules
    of that area neither fire nor write state this tick."""
    for rule, row, decision in decided:
        device_id = str(row['d_id'])
        cell = waiting.get(device_id)
        if cell is None or device_id in carried:
            continue
        if firing._km(rule.lat_lon, rulemod.parse_cell_key(cell)) \
                <= firing.RAIN_AREA_KM:
            decision.fires = []
            decision.writes = {}


def force_rain(decided, forced, matches, now):
    """A new episode the daily cap allows no card for is told once as a
    notification of the rule that would have started it — whether or not
    that rule has re-armed: its re-arm waits for 90 min without rain, which
    showery weather never has (third review 2026-10-01: the fallback caught
    1 capped phase in 5). livectl recorded the episode, so it is told once.
    `forced`: device → livectl.NotifyInstead."""
    for rule, row, decision in decided:
        f = forced.get(str(row['d_id']))
        if f is None or rule.id != f.rule_id or decision.fires:
            continue
        match = matches.get(rule.id)
        if match is None:
            continue
        decision.fires.append(firing.Fire(
            rule.id, 'rain', f'rain:{rule.cell_key}', match.evidence, 'new'))
        decision.writes['rain'] = ({'armed': False}, now, None)


def drop_carried(decided, carried):
    """A live event the activity carries needs no notification: the
    start or update push is the notification."""
    for rule, row, decision in decided:
        decision.fires = [f for f in decision.fires
                          if (str(row['d_id']), f.event_key) not in carried]


async def dispatch_all(conn, client, decided, now):
    by_device = {}
    for rule, row, decision in decided:
        if decision.writes or decision.fires:
            by_device.setdefault(str(row['d_id']), (row, []))[1].append(
                (rule, row, decision))
    outgoing, pending = [], {}
    for row, items in by_device.values():
        with isolated('delivery for device', row['d_id']):
            outgoing += await dispatcher.apply(
                conn, device_of(row), items, now, pending=pending)
    with isolated('sending', f'{len(outgoing)} pushes'):
        await dispatcher.send_all(conn, client, outgoing, now)


class Worker:

    def __init__(self, pool, http, client, sleep=asyncio.sleep):
        self.sleep = sleep
        self.pool = pool
        self.http = http
        self.client = client
        self.warnings = sources.WarningsSource(http)
        self.forecast = sources.ForecastSource(http)
        self.nowcast = sources.NowcastSource(http)
        self._radar_seen = None      # newest radar timestamp evaluated
        self._nowcast_at = None      # when the nowcast loop last evaluated
        self._nowcast_failed_at = None   # last failed evaluation (back-off)

    # MARK: warnings

    async def resolve_cells(self, conn, now):
        """Cell centroid → DWD warn cell, once per new cell (design §3)."""
        from brightsky.query import NoData, _warn_cells
        rows = await conn.fetch(
            """
            SELECT cell_key, lat, lon FROM push.cells
            WHERE warn_cell_id IS NULL
              AND (resolved_at IS NULL OR resolved_at < $1)
            """, now - CELL_RESOLVE_RETRY)
        for r in rows:
            resolved_at = now
            try:
                meta = await asyncio.to_thread(
                    _warn_cells.find, r['lat'], r['lon'])
                warn_cell_id = meta['warn_cell_id']
            except NoData:
                warn_cell_id = None
                logger.info('Cell %s is not covered by a DWD warn cell',
                            r['cell_key'])
            except Exception as e:
                # The warn-cell polygons come from DWD's GeoServer once; if
                # it is down, these cells have no warnings for now — the
                # warnings of every other cell still go out. Retry in
                # CELL_RESOLVE_ERROR_RETRY, and stop asking this tick.
                logger.warning('Warn cells unavailable (%s: %s); retrying',
                               type(e).__name__, e)
                await conn.execute(
                    'UPDATE push.cells SET resolved_at = $2 '
                    'WHERE warn_cell_id IS NULL AND cell_key = ANY($1)',
                    [x['cell_key'] for x in rows],
                    now - CELL_RESOLVE_RETRY + CELL_RESOLVE_ERROR_RETRY)
                return
            await conn.execute(
                'UPDATE push.cells SET warn_cell_id = $2, resolved_at = $3 '
                'WHERE cell_key = $1', r['cell_key'], warn_cell_id,
                resolved_at)

    async def warnings_tick(self, now):
        async with self.pool.acquire() as conn:
            await self.resolve_cells(conn, now)
            obs = await self.warnings.refresh(conn, now)
            # Only rules at a warn cell with warnings: nothing else can
            # match or write state.
            rows = await conn.fetch(
                RULES_SQL + ' AND c.warn_cell_id = ANY($2::int[])',
                'dwd_warning', list(obs.by_cell))
            states = await load_states(conn, [r['id'] for r in rows])
            decided = []
            candidates = {}
            for row in rows:
                with isolated('warning rule', row['id']):
                    rule = rule_from_row(row)
                    hours = []
                    if rule.values:
                        hours = await self.hours_for(row, now)
                    matches = ev.warning_matches(
                        rule, obs.lookup(row['warn_cell_id']), hours, now)
                    decision = firing.decide_warnings(
                        rule, matches, states.get(rule.id, {}), now)
                    decided.append((rule, row, decision))
                    self.warning_candidates(
                        candidates, rule, row, matches,
                        states.get(rule.id, {}), decision, now)
            # One notification per warning and device, recorded before the
            # Live Activity takes its share, so the covered threads stay
            # silent later too.
            firing.dedupe_warnings(decided, states)
            carried = await self.live_warnings(conn, candidates, obs, now)
            drop_carried(decided, carried)
            await dispatch_all(conn, self.client, decided, now)
            await store.mark_source(conn, 'warnings', now)
        return len(rows)

    @staticmethod
    def warning_candidates(candidates, rule, row, matches, states, decision,
                           now):
        threads = {k: v['state'] for k, v in states.items()}
        threads.update({k: w[0] for k, w in decision.writes.items()})
        for m in matches:
            w = m.warning
            if w.onset > now + live.LIVE_LEAD:
                continue
            # „Kurze Unwetter live" covers short events only; an extreme
            # warning is live for every matching registration, of any
            # family, even with the switch off (decision 2026-09-24).
            if w.level != ev.EXTREME and not (
                    row['live'] is not None
                    and w.family in ev.SHORT_FAMILIES):
                continue
            thread = next((k for k, t in threads.items()
                           if k.startswith('dwd:')
                           and w.id in t.get('alert_ids', ())), None)
            context = None
            if m.evidence.values:
                context = ev.context_phrase(rule.values, [
                    ev.Held(c.metric, m.evidence.values[c.metric], now)
                    for c in rule.values])
            candidates.setdefault(str(row['d_id']), (row, []))[1].append(
                live.Candidate('warning', rule.id, w.onset, level=w.level,
                               warning=w, context=context, thread=thread))

    async def live_warnings(self, conn, candidates, obs, now):
        """Each device's one activity against its live warning rules.
        Returns {(device id, event key)} the activity carries."""
        running = {str(r['d_id']): r
                   for r in await conn.fetch(RUNNING_SQL, 'warning')}
        threads = await running_threads(conn, running.values())
        carried = set()
        for device_id in set(candidates) | set(running):
            with isolated('live warning for device', device_id):
                row, cands = candidates.get(device_id, (None, []))
                device = device_of(row or running[device_id])
                winner = live.winner(cands, now)
                status = 'here'
                if device_id in running:
                    r = running[device_id]
                    status = warning_status(r, threads.get(device_id), obs)
                if await livectl.warning_tick(
                        conn, self.client, device, winner, status,
                        now) and winner:
                    carried.add((device_id, f'dwd:{winner.warning.id}'))
        return carried

    # MARK: forecast

    def forecast_fresh(self, cell_key, now):
        fetched = self.forecast.fetched_at.get(cell_key)
        return fetched is not None and now - fetched <= datetime.timedelta(
            seconds=self.forecast.interval_s)

    async def hours_for(self, row, now):
        if self.forecast_fresh(row['cell_key'], now):
            return self.forecast.lookup(row['cell_key'])
        return await self.forecast.fetch(
            row['cell_key'], row['lat'], row['lon'], now)

    async def forecast_tick(self, now):
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(RULES_SQL, 'user_rule')
        cells = {r['cell_key']: r for r in rows}
        failed = set()
        # One request at a time, spread over most of the cycle: `web` also
        # serves the app, and a burst of lookups is what starves it
        # (incident 2026-07-10). With few cells the spacing is capped, so a
        # handful of rules still decide within seconds.
        spacing = min(FORECAST_MAX_SPACING,
                      FORECAST_PACE * self.forecast.interval_s
                      / max(1, len(cells)))
        for row in cells.values():
            try:
                await self.forecast.fetch(
                    row['cell_key'], row['lat'], row['lon'], now)
            except Exception as e:
                failed.add(row['cell_key'])
                logger.warning('Forecast for %s failed: %r',
                               row['cell_key'], e)
            await self.sleep(spacing)
        self.forecast.evict(set(cells), now)
        async with self.pool.acquire() as conn:
            states = await load_states(conn, [r['id'] for r in rows])
            decided = []
            for row in rows:
                if row['cell_key'] in failed:
                    continue
                with isolated('forecast rule', row['id']):
                    rule = rule_from_row(row)
                    if rulemod.is_expired(rule.window, now):
                        continue
                    matches = ev.value_matches(
                        rule, self.forecast.lookup(row['cell_key']), now)
                    decision = firing.decide_values(
                        rule, matches, states.get(rule.id, {}), now)
                    decided.append((rule, row, decision))
            await dispatch_all(conn, self.client, decided, now)
            if cells and len(failed) == len(cells):
                raise RuntimeError('every forecast lookup failed')
            await store.mark_source(conn, 'forecast', now)
        return len(rows)

    # MARK: nowcast

    async def nowcast_due(self, conn, now):
        """DWD publishes a radar frame every 5 minutes (at :x3:40 and
        :x8:40) and the ingest worker has it about 40 s later. Checking
        every minute and fetching only when a new one is in uses each frame
        within a minute of its arrival instead of 0–5 minutes later — at the
        same load. Without a new frame the loop still runs every 5 minutes,
        so running activities keep moving if ingest stalls."""
        newest = await conn.fetchval('SELECT max(timestamp) FROM radar')
        fresh = newest is not None and newest != self._radar_seen
        interval = datetime.timedelta(seconds=self.nowcast.interval_s)
        floor = self._nowcast_at is None or now - self._nowcast_at >= interval
        # After a failure, wait for the floor: retrying every minute would
        # repeat the multi-MB national request against a struggling web.
        backing_off = (self._nowcast_failed_at is not None
                       and now - self._nowcast_failed_at < interval)
        return (fresh or floor) and not backing_off, newest

    async def nowcast_tick(self, now):
        try:
            return await self._nowcast_tick(now)
        except Exception:
            self._nowcast_failed_at = now
            raise

    async def _nowcast_tick(self, now):
        async with self.pool.acquire() as conn:
            due, newest = await self.nowcast_due(conn, now)
            if not due:
                return None
            rows = await conn.fetch(RULES_SQL, 'rain_nowcast')
            running = {str(r['d_id']): r
                       for r in await conn.fetch(RUNNING_SQL, 'rain')}
        cells = {r['cell_key']: (r['lat'], r['lon']) for r in rows}
        for r in running.values():
            if r['cell_key']:
                cells.setdefault(r['cell_key'], (r['lat'], r['lon']))
        # One national request per cycle, whatever the number of cells.
        points = await self.nowcast.fetch_all(cells, now) if cells else {}
        self.nowcast.forget(set(cells))
        async with self.pool.acquire() as conn:
            states = await load_states(conn, [r['id'] for r in rows])
            decided = []
            candidates = {}
            seen_new, seen_gone = [], []
            waiting = {}     # device → cell of a start awaiting its frame
            matches = {}     # rule id → its rain match this tick
            for row in rows:
                if row['cell_key'] not in points:
                    continue   # no data: neither fire nor re-arm
                with isolated('rain rule', row['id']):
                    rule = rule_from_row(row)
                    device_id = str(row['d_id'])
                    # „Nächster Schauer" belongs to the running activity's
                    # own place only; rain elsewhere starts as „Regen in".
                    r = running.get(device_id)
                    rain = live.analyze_rain(
                        points[row['cell_key']], now,
                        in_phase=(r is not None
                                  and r['cell_key'] == row['cell_key']),
                        threshold=rule.rain_threshold)
                    if rain is None:
                        continue
                    hours = (await self.hours_for(row, now)
                             if rule.values else [])
                    match = ev.rain_match(rule, rain, hours, now)
                    matches[rule.id] = match
                    clear = (rain.first_rain_at is None
                             and rain.state != 'raining')
                    decision = firing.decide_rain(
                        rule, match, states.get(rule.id, {}), now, clear)
                    # A live rule on a device that can show its card speaks
                    # through the card only: rain too short for one is not
                    # told, and the rule stays armed — if it grows, the card
                    # tells it (a notification followed by a card for the
                    # same rain 1,588 times in the 1 Oct replay).
                    # Only the fire (and its „told" write) is dropped; any
                    # other bookkeeping — rain resetting `clearSince` —
                    # still happens.
                    quiet = (row['live'] is not None and match is not None
                             and bool(decision.fires)
                             and not rain.may_start
                             and livectl.can_start(device_of(row)))
                    if not quiet:
                        decided.append((rule, row, decision))
                    if row['live'] is not None:
                        candidates.setdefault(device_id, (row, []))
                        soon = rain.state == 'raining' or (
                            rain.first_rain_at is not None
                            and rain.first_rain_at
                            <= now + live.START_WITHIN)
                        seen = states.get(rule.id, {}).get(LIVE_SEEN)
                        # the activity starts only where a match exists,
                        # and only on rain that holds up (`confirmed`)
                        if soon and match is not None and rain.may_start:
                            if confirmed(rain, seen, now):
                                candidates[device_id][1].append(
                                    live.Candidate(
                                        'rain', rule.id,
                                        rain.first_rain_at or now,
                                        rain=rain, context=match.context,
                                        cell_key=rule.cell_key))
                            elif livectl.can_start(device_of(row)):
                                waiting.setdefault(device_id, rule.cell_key)
                                if seen is None:
                                    seen_new.append(rule.id)
                        elif seen is not None:
                            seen_gone.append(rule.id)
            await mark_seen(conn, seen_new, seen_gone, now)
            carried = {}
            forced = {}      # device → livectl.NotifyInstead
            for device_id in set(candidates) | set(running):
                with isolated('live rain for device', device_id):
                    row, cands = candidates.get(device_id, (None, []))
                    device = device_of(row or running[device_id])
                    winner = live.winner(cands, now)
                    # A running activity is judged at its own rule's cell,
                    # over the whole 2 h: a gap before the next shower does
                    # not end it; no data leaves it alone.
                    current = running_cell = None
                    r = running.get(device_id)
                    if r is not None:
                        running_cell = r['cell_key']
                        if running_cell in points:
                            current = live.analyze_rain(
                                points[running_cell], now, in_phase=True,
                                threshold=rain_threshold(r['rule_params']))
                    told = await livectl.rain_tick(
                        conn, self.client, device, winner, current,
                        running_cell, now)
                    if isinstance(told, livectl.NotifyInstead):
                        forced[device_id] = told
                    elif told is not None:
                        carried[device_id] = told
            # A start waiting for its second frame is not told as a
            # notification meanwhile — the activity will tell it, or it was
            # a one-frame ghost. Its area decides nothing this tick, so the
            # rules stay armed for whatever comes next.
            forced_cells = {d: f.cell_key for d, f in forced.items()}
            hold_decisions(decided, waiting, {**carried, **forced_cells})
            force_rain(decided, forced, matches, now)
            # The activity is the notification for its own area; elsewhere,
            # and without an activity, rain is told once per device and area
            # (a chosen place before „Mein Standort").
            firing.dedupe_rain(decided, states, carried, now,
                               forced=forced_cells)
            await dispatch_all(conn, self.client, decided, now)
            await store.mark_source(conn, 'nowcast', now)
        self._radar_seen, self._nowcast_at = newest, now
        self._nowcast_failed_at = None
        return len(rows)

    # MARK: digest

    async def digest_tick(self, now):
        local = now.astimezone(berlin.TZ)
        if local.hour not in DIGEST_HOURS:
            return None
        today = local.date()
        async with self.pool.acquire() as conn:
            done = await conn.fetchval(
                "SELECT last_success FROM push.source_status "
                "WHERE source = 'digest'")
            if done is not None and berlin.local_date(done) == today:
                return None
            rows = await conn.fetch(DIGEST_SQL)
        parsed = []
        for row in rows:
            with isolated('digest rule', row['id']):
                rule = rule_from_row(row)
                if not rulemod.is_expired(rule.window, now):
                    parsed.append((rule, row))
        # First the forecasts, outside the connection and paced like the
        # forecast loop's; then one national radar request for every rain
        # rule in the digest.
        failed = await self.digest_forecasts(
            [row for rule, row in parsed if rule.values], now)
        rain_cells = {row['cell_key']: (row['lat'], row['lon'])
                      for rule, row in parsed if rule.kind == 'rain_nowcast'}
        radar = (await self.nowcast.fetch_all(rain_cells, now)
                 if rain_cells else {})
        async with self.pool.acquire() as conn:
            # Checked only now, right before deciding: the fetches above may
            # take minutes, and the snapshot must be fresh when it is used.
            obs = None
            if any(rule.kind == 'dwd_warning' for rule, _ in parsed):
                try:
                    obs = await self.warnings.refresh(conn, now)
                except sources.Stale:
                    if local.hour < DIGEST_HOURS[-1]:
                        raise   # retry every minute for a while …
                    # … then send the rest without the warning rules
                    logger.warning('Digest: warnings stale, sending '
                                   'without warning rules')
            # A registration during the fetches may have dropped rules
            current = {str(r['id']) for r in await conn.fetch(
                'SELECT id FROM push.rules '
                'WHERE id = ANY($1::uuid[]) AND enabled',
                [rule.id for rule, _ in parsed])}
            parsed = [(rule, row) for rule, row in parsed
                      if rule.id in current
                      and not (rule.kind == 'dwd_warning' and obs is None)
                      and not (rule.values and row['cell_key'] in failed)]
            states = await load_states(conn, [row['id'] for _, row in parsed])
            decided = []
            for rule, row in parsed:
                with isolated('digest rule', row['id']):
                    decision = await self.digest_decision(
                        rule, row, obs, radar, states.get(rule.id, {}), now)
                    decided.append((rule, row, decision))
            by_device = {}
            for rule, row, decision in decided:
                by_device.setdefault(str(row['d_id']), (row, []))[1].append(
                    (rule, row, decision))
            outgoing = []
            for row, items in by_device.values():
                with isolated('digest for device', row['d_id']):
                    outgoing += await dispatcher.apply(
                        conn, device_of(row), items, now, digest_date=today)
            with isolated('sending', f'{len(outgoing)} digests'):
                await dispatcher.send_all(conn, self.client, outgoing, now)
            await store.mark_source(conn, 'digest', now)
        return len(rows)

    async def digest_forecasts(self, rows, now):
        """Fetch the forecasts the digest needs, one at a time, spread over
        DIGEST_SPREAD_S. Returns the cells that failed."""
        cells = {row['cell_key']: row for row in rows
                 if not self.forecast_fresh(row['cell_key'], now)}
        spacing = max(DIGEST_MIN_SPACING, min(
            FORECAST_MAX_SPACING, DIGEST_SPREAD_S / max(1, len(cells))))
        failed = set()
        for row in cells.values():
            try:
                await self.forecast.fetch(
                    row['cell_key'], row['lat'], row['lon'], now)
            except Exception as e:
                failed.add(row['cell_key'])
                logger.warning('Digest: forecast for %s failed: %r',
                               row['cell_key'], e)
            await self.sleep(spacing)
        return failed

    async def digest_decision(self, rule, row, obs, radar, prior, now):
        hours = await self.hours_for(row, now) if rule.values else []
        if rule.kind == 'user_rule':
            return firing.decide_values(
                rule, ev.value_matches(rule, hours, now, digest=True),
                prior, now)
        if rule.kind == 'dwd_warning':
            matches = ev.warning_matches(
                rule, obs.lookup(row['warn_cell_id']), hours, now,
                digest=True)
            return firing.decide_warnings(rule, matches, prior, now)
        rain = live.analyze_rain(radar.get(row['cell_key'], []), now,
                                 threshold=rule.rain_threshold)
        clear = rain is None or (rain.first_rain_at is None
                                 and rain.state != 'raining')
        return firing.decide_rain(
            rule, ev.rain_match(rule, rain, hours, now), prior, now, clear)

    # MARK: housekeeping

    async def cleanup_tick(self, now):
        async with self.pool.acquire() as conn:
            await conn.execute(
                'DELETE FROM push.rule_state WHERE expires_at < $1', now)
            await conn.execute(
                'DELETE FROM push.notifications_sent WHERE sent_at < $1',
                now - AUDIT_RETENTION)
            # The app re-registers on every launch; a device silent for
            # 90 days is gone (review #20). Rules and state cascade.
            await conn.execute(
                'DELETE FROM push.devices WHERE last_seen < $1',
                now - DEVICE_RETENTION)
            # Rules the app stopped sending a week ago are gone for good;
            # their state cascades.
            await conn.execute(
                'DELETE FROM push.rules '
                'WHERE NOT enabled AND disabled_at < $1',
                now - DISABLED_RETENTION)
            livectl.prune_locks()
            # Cells no rule refers to any more
            await conn.execute(
                'DELETE FROM push.cells c WHERE NOT EXISTS ('
                'SELECT 1 FROM push.rules r WHERE r.cell_key = c.cell_key)')

    async def loop(self, name, tick, interval_s):
        while True:
            started = utcnow()
            try:
                n = await tick(started)
                if n is not None:
                    logger.info('%s: evaluated %d rules in %.1fs', name, n,
                                (utcnow() - started).total_seconds())
            except asyncio.CancelledError:
                raise
            except Exception as e:
                level = (logging.WARNING if isinstance(e, sources.Stale)
                         else logging.ERROR)
                logger.log(level, '%s tick failed: %r', name, e,
                           exc_info=level == logging.ERROR)
                try:
                    async with self.pool.acquire() as conn:
                        await store.mark_source(
                            conn, name, started, f'{type(e).__name__}: {e}')
                except Exception:
                    logger.exception('Could not record %s failure', name)
            elapsed = (utcnow() - started).total_seconds()
            await asyncio.sleep(max(1.0, interval_s - elapsed))


async def run():
    # One line per request per minute drowns the loop's own log.
    for name in ('httpx', 'httpcore', 'hpack'):
        logging.getLogger(name).setLevel(logging.WARNING)
    try:
        client = sender.client_from_settings()
    except RuntimeError as e:
        logger.warning('%s — running in dry-run mode', e)
        client = DryRunClient()
    async with store.pool(max_size=4) as pool, sources.http_client() as http:
        async with pool.acquire() as conn:
            missing = await store.missing_columns(conn)
        if missing:
            raise SystemExit(
                f'push schema is missing {missing}: run the migrations '
                '(the worker container, `--migrate`) before push-work')
        worker = Worker(pool, http, client)
        tasks = [
            asyncio.create_task(worker.loop(
                'warnings', worker.warnings_tick,
                sources.WarningsSource.interval_s)),
            asyncio.create_task(worker.loop(
                'forecast', worker.forecast_tick,
                sources.ForecastSource.interval_s)),
            asyncio.create_task(worker.loop(
                'nowcast', worker.nowcast_tick, NOWCAST_CHECK_S)),
            asyncio.create_task(worker.loop(
                'digest', worker.digest_tick, 60)),
            asyncio.create_task(worker.loop(
                'cleanup', worker.cleanup_tick, 3600)),
        ]
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop.set)
        await stop.wait()
        logger.info('Shutting down')
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await client.aclose()
