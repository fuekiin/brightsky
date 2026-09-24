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
AUDIT_RETENTION = datetime.timedelta(days=30)
DEVICE_RETENTION = datetime.timedelta(days=90)


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
           d.push_to_start_token, d.live_activities_enabled
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
           d.push_to_start_token, d.live_activities_enabled
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
            'live_activities_enabled': row['live_activities_enabled']}


RUNNING_SQL = """
    SELECT la.*, c.warn_cell_id, c.lat, c.lon, r.cell_key,
           r.params AS rule_params,
           d.id AS d_id, d.apns_token, d.environment,
           d.push_to_start_token, d.live_activities_enabled
    FROM push.live_activities la
    JOIN push.devices d ON d.id = la.device_id
    LEFT JOIN push.rules r ON r.id = la.rule_id
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
    for row, items in by_device.values():
        with isolated('delivery for device', row['d_id']):
            await dispatcher.apply(conn, client, device_of(row), items, now)


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
            rows = [r for r in await conn.fetch(RULES_SQL, 'dwd_warning')
                    if obs.lookup(r['warn_cell_id'])]
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
        carried = set()
        for device_id in set(candidates) | set(running):
            with isolated('live warning for device', device_id):
                row, cands = candidates.get(device_id, (None, []))
                device = device_of(row or running[device_id])
                winner = live.winner(cands, now)
                present = True
                if device_id in running:
                    r = running[device_id]
                    family = r['state'].get('family')
                    present = any(
                        w.family == family and w.end > now
                        for w in obs.lookup(r['warn_cell_id']))
                if await livectl.warning_tick(
                        conn, self.client, device, winner, present,
                        now) and winner:
                    carried.add((device_id, f'dwd:{winner.warning.id}'))
        return carried

    # MARK: forecast

    async def hours_for(self, row, now):
        hours = self.forecast.lookup(row['cell_key'])
        fetched = self.forecast.fetched_at.get(row['cell_key'])
        if hours is None or now - fetched > datetime.timedelta(
                seconds=self.forecast.interval_s):
            hours = await self.forecast.fetch(
                row['cell_key'], row['lat'], row['lon'], now)
        return hours

    async def forecast_tick(self, now):
        async with self.pool.acquire() as conn:
            rows = await conn.fetch(RULES_SQL, 'user_rule')
        cells = {r['cell_key']: r for r in rows}
        failed = []
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
                failed.append(row['cell_key'])
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
        floor = (self._nowcast_at is None or now - self._nowcast_at
                 >= datetime.timedelta(seconds=self.nowcast.interval_s))
        return (fresh or floor), newest

    async def nowcast_tick(self, now):
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
            for row in rows:
                if row['cell_key'] not in points:
                    continue   # no data: neither fire nor re-arm
                with isolated('rain rule', row['id']):
                    rule = rule_from_row(row)
                    device_id = str(row['d_id'])
                    rain = live.analyze_rain(points[row['cell_key']], now,
                                             in_phase=device_id in running,
                                             threshold=rule.rain_threshold)
                    if rain is None:
                        continue
                    hours = (await self.hours_for(row, now)
                             if rule.values else [])
                    match = ev.rain_match(rule, rain, hours, now)
                    clear = (rain.first_rain_at is None
                             and rain.state != 'raining')
                    decision = firing.decide_rain(
                        rule, match, states.get(rule.id, {}), now, clear)
                    decided.append((rule, row, decision))
                    if row['live'] is not None:
                        candidates.setdefault(device_id, (row, []))
                        soon = rain.state == 'raining' or (
                            rain.first_rain_at is not None
                            and rain.first_rain_at
                            <= now + live.START_WITHIN)
                        # the activity starts only where a match exists
                        if soon and match is not None:
                            candidates[device_id][1].append(live.Candidate(
                                'rain', rule.id, rain.first_rain_at or now,
                                rain=rain, context=match.context,
                                cell_key=rule.cell_key))
            carried = set()
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
                    if await livectl.rain_tick(conn, self.client, device,
                                               winner, current, running_cell,
                                               now) and winner:
                        # only the winner rides on the activity (§19);
                        # other places notify as usual
                        carried.add((device_id, f'rain:{winner.cell_key}'))
            drop_carried(decided, carried)
            await dispatch_all(conn, self.client, decided, now)
            await store.mark_source(conn, 'nowcast', now)
        # Only now: a tick that failed is retried at the next check.
        self._radar_seen, self._nowcast_at = newest, now
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
            obs = None
            if any(r['kind'] == 'dwd_warning' for r in rows):
                try:
                    obs = await self.warnings.refresh(conn, now)
                except sources.Stale:
                    if local.hour < DIGEST_HOURS[-1]:
                        raise   # retry every minute for a while …
                    # … then send the rest without the warning rules
                    logger.warning('Digest: warnings stale, sending '
                                   'without warning rules')
            states = await load_states(conn, [r['id'] for r in rows])
            decided = []
            for row in rows:
                with isolated('digest rule', row['id']):
                    rule = rule_from_row(row)
                    if rulemod.is_expired(rule.window, now):
                        continue
                    if rule.kind == 'dwd_warning' and obs is None:
                        continue
                    prior = states.get(rule.id, {})
                    hours = (await self.hours_for(row, now)
                             if rule.values else [])
                    if rule.kind == 'user_rule':
                        decision = firing.decide_values(
                            rule, ev.value_matches(rule, hours, now), prior,
                            now)
                    elif rule.kind == 'dwd_warning':
                        matches = ev.warning_matches(
                            rule, obs.lookup(row['warn_cell_id']), hours,
                            now)
                        decision = firing.decide_warnings(rule, matches,
                                                          prior, now)
                    else:
                        cell = {row['cell_key']: (row['lat'], row['lon'])}
                        found = await self.nowcast.fetch_all(cell, now)
                        rain = live.analyze_rain(
                            found.get(row['cell_key'], []), now,
                            threshold=rule.rain_threshold)
                        clear = rain is None or (
                            rain.first_rain_at is None
                            and rain.state != 'raining')
                        decision = firing.decide_rain(
                            rule, ev.rain_match(rule, rain, hours, now),
                            prior, now, clear)
                    decided.append((rule, row, decision))
            by_device = {}
            for rule, row, decision in decided:
                by_device.setdefault(str(row['d_id']), (row, []))[1].append(
                    (rule, row, decision))
            for row, items in by_device.values():
                with isolated('digest for device', row['d_id']):
                    await dispatcher.apply(conn, self.client, device_of(row),
                                           items, now, digest_date=today)
            await store.mark_source(conn, 'digest', now)
        return len(rows)

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
            # Locks of devices that are not in the middle of a decision
            for key in [k for k, v in livectl.LOCKS.items()
                        if not v.locked()]:
                del livectl.LOCKS[key]
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
