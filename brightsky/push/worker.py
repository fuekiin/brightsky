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
    apns, dispatcher, evaluator as ev, firing, rules as rulemod, sender,
    sources, store,
)


logger = logging.getLogger('brightsky.push.worker')

CELL_RESOLVE_RETRY = datetime.timedelta(days=1)
FORECAST_CONCURRENCY = 4
AUDIT_RETENTION = datetime.timedelta(days=30)


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
           d.id AS d_id, d.apns_token, d.environment
    FROM push.rules r
    JOIN push.cells c USING (cell_key)
    JOIN push.devices d ON d.id = r.device_id
    WHERE r.enabled AND r.kind = $1
      -- Morgenübersicht rules belong to the digest loop (design §12.6)
      AND r.schedule IS NULL
"""


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
            'environment': row['environment']}


async def dispatch_all(conn, client, decided, now):
    by_device = {}
    for rule, row, decision in decided:
        if decision.writes or decision.fires:
            by_device.setdefault(str(row['d_id']), (row, []))[1].append(
                (rule, row, decision))
    for row, items in by_device.values():
        await dispatcher.apply(conn, client, device_of(row), items, now)


class Worker:

    def __init__(self, pool, http, client):
        self.pool = pool
        self.http = http
        self.client = client
        self.warnings = sources.WarningsSource(http)
        self.forecast = sources.ForecastSource(http)

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
            try:
                meta = await asyncio.to_thread(
                    _warn_cells.find, r['lat'], r['lon'])
                warn_cell_id = meta['warn_cell_id']
            except NoData:
                warn_cell_id = None
                logger.info('Cell %s is not covered by a DWD warn cell',
                            r['cell_key'])
            await conn.execute(
                'UPDATE push.cells SET warn_cell_id = $2, resolved_at = $3 '
                'WHERE cell_key = $1', r['cell_key'], warn_cell_id, now)

    async def warnings_tick(self, now):
        async with self.pool.acquire() as conn:
            await self.resolve_cells(conn, now)
            obs = await self.warnings.refresh(conn, now)
            rows = [r for r in await conn.fetch(RULES_SQL, 'dwd_warning')
                    if obs.lookup(r['warn_cell_id'])]
            states = await load_states(conn, [r['id'] for r in rows])
            decided = []
            for row in rows:
                rule = rule_from_row(row)
                hours = []
                if rule.values:
                    hours = await self.hours_for(row, now)
                matches = ev.warning_matches(
                    rule, obs.lookup(row['warn_cell_id']), hours, now)
                decision = firing.decide_warnings(
                    rule, matches, states.get(rule.id, {}), now)
                decided.append((rule, row, decision))
            await dispatch_all(conn, self.client, decided, now)
            await store.mark_source(conn, 'warnings', now)
        return len(rows)

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
        semaphore = asyncio.Semaphore(FORECAST_CONCURRENCY)
        failed = []

        async def fetch(row):
            async with semaphore:
                try:
                    await self.forecast.fetch(
                        row['cell_key'], row['lat'], row['lon'], now)
                except Exception as e:
                    failed.append(row['cell_key'])
                    logger.warning('Forecast for %s failed: %r',
                                   row['cell_key'], e)
        await asyncio.gather(*(fetch(r) for r in cells.values()))
        async with self.pool.acquire() as conn:
            states = await load_states(conn, [r['id'] for r in rows])
            decided = []
            for row in rows:
                if row['cell_key'] in failed:
                    continue
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

    # MARK: housekeeping

    async def cleanup_tick(self, now):
        async with self.pool.acquire() as conn:
            await conn.execute(
                'DELETE FROM push.rule_state WHERE expires_at < $1', now)
            await conn.execute(
                'DELETE FROM push.notifications_sent WHERE sent_at < $1',
                now - AUDIT_RETENTION)
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
                        await store.mark_source(conn, name, started, repr(e))
                except Exception:
                    logger.exception('Could not record %s failure', name)
            elapsed = (utcnow() - started).total_seconds()
            await asyncio.sleep(max(1.0, interval_s - elapsed))


async def run():
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
