"""Where the evaluator gets its values (design §3)."""

import datetime
import logging
from dataclasses import dataclass, field

import httpx

from brightsky.polling import DWDPoller
from brightsky.push import evaluator as ev
from brightsky.settings import settings


logger = logging.getLogger('brightsky.push.sources')

CAP_LISTING_URL = (
    'https://opendata.dwd.de/weather/alerts/cap/COMMUNEUNION_DWD_STAT/')
# Bounded staleness, the inverse of the app's policy (design §9): acting on
# an old snapshot can fire a warning that has been cancelled.
WARNINGS_MAX_AGE = datetime.timedelta(minutes=10)


class Stale(Exception):
    pass


@dataclass
class WarningsObservation:
    fetched_at: datetime.datetime
    by_cell: dict = field(default_factory=dict)   # warn_cell_id → [Warning]

    def lookup(self, warn_cell_id):
        return self.by_cell.get(warn_cell_id, [])


class WarningsSource:
    """The fork's `alerts` table — the nationwide DWD snapshot the ingest
    worker keeps — plus a check against DWD's listing that the table holds
    the newest file. One listing request per minute, whatever the number of
    users."""

    id = 'warnings'
    interval_s = 60

    def __init__(self, http):
        self.http = http
        self.synced_at = None

    async def in_sync(self, conn):
        resp = await self.http.get(CAP_LISTING_URL)
        resp.raise_for_status()
        files = list(DWDPoller().parse(CAP_LISTING_URL, resp.text))
        if not files:
            raise RuntimeError('No CAP snapshot in the DWD listing')
        newest = max(files, key=lambda f: f['last_modified'])
        row = await conn.fetchrow(
            'SELECT * FROM parsed_files WHERE url = $1', newest['url'])
        return row is not None and DWDPoller().matches_known_fingerprint(
            {newest['url']: row}, newest)

    async def refresh(self, conn, now):
        if await self.in_sync(conn):
            self.synced_at = now
        if self.synced_at is None or now - self.synced_at > WARNINGS_MAX_AGE:
            raise Stale(
                'alerts table has not matched the DWD listing since '
                f'{self.synced_at}')
        rows = await conn.fetch(
            """
            SELECT a.alert_id, a.severity::text AS severity, a.event_code,
                   a.event_de, a.headline_de, a.onset, a.expires,
                   array_agg(c.warn_cell_id) AS cells
            FROM alerts a JOIN alert_cells c ON c.alert_id = a.id
            WHERE a.status = 'actual'
            GROUP BY a.id
            """)
        obs = WarningsObservation(fetched_at=self.synced_at)
        for r in rows:
            if r['severity'] not in ev.SEVERITY_LEVELS:
                continue
            w = ev.Warning(
                id=r['alert_id'], level=ev.SEVERITY_LEVELS[r['severity']],
                family=ev.classify(r['event_de']), event=r['event_de'] or '',
                headline=r['headline_de'], onset=r['onset'],
                expires=r['expires'], event_code=r['event_code'])
            for cell in r['cells']:
                obs.by_cell.setdefault(cell, []).append(w)
        return obs


class ForecastSource:
    """Hourly forecast per cell, by loopback HTTP against our own `web`
    container — `/weather` is a documented contract, the tables behind it
    are not (design §3)."""

    id = 'forecast'
    interval_s = 15 * 60
    DAYS_BACK = 1
    DAYS_AHEAD = 10

    def __init__(self, http):
        self.http = http
        self.hours = {}          # cell_key → [Hour]
        self.fetched_at = {}     # cell_key → datetime

    async def fetch(self, cell_key, lat, lon, now):
        date = (now - datetime.timedelta(days=self.DAYS_BACK)).isoformat()
        last = (now + datetime.timedelta(days=self.DAYS_AHEAD)).isoformat()
        resp = await self.http.get(
            f'{settings.PUSH_WEATHER_URL}/weather',
            params={'lat': lat, 'lon': lon, 'date': date, 'last_date': last,
                    'tz': 'UTC'})
        resp.raise_for_status()
        hours = [ev.Hour.from_brightsky(r) for r in resp.json()['weather']]
        self.hours[cell_key] = hours
        self.fetched_at[cell_key] = now
        return hours

    def lookup(self, cell_key):
        return self.hours.get(cell_key)

    def evict(self, keep, now):
        """Forget cells no rule uses any more (review #20); a cell that
        warnings or the digest still read is fetched again on demand."""
        for cell_key in list(self.hours):
            if cell_key not in keep and now - self.fetched_at[cell_key] \
                    > datetime.timedelta(hours=1):
                del self.hours[cell_key]
                del self.fetched_at[cell_key]


class NowcastSource:
    """The radar point nowcast per cell, as the app reads it: `/radar` at
    the cell centre, `distance=1`, `precipitation_5` in 1/100 mm per
    5 minutes (`RadarClient.fetchRadar`)."""

    id = 'nowcast'
    interval_s = 5 * 60

    def __init__(self, http):
        self.http = http

    async def fetch(self, lat, lon, now):
        from brightsky.push.live import Point
        resp = await self.http.get(
            f'{settings.PUSH_WEATHER_URL}/radar',
            params={'lat': lat, 'lon': lon, 'distance': 1,
                    'date': now.isoformat(), 'format': 'plain',
                    'tz': 'UTC'})
        resp.raise_for_status()
        points = []
        for entry in resp.json()['radar']:
            grid = entry.get('precipitation_5') or [[0]]
            value = (grid[0] or [0])[0] or 0
            points.append(Point(
                datetime.datetime.fromisoformat(entry['timestamp']),
                value / 100))
        return sorted(points, key=lambda p: p.timestamp)


def http_client():
    return httpx.AsyncClient(
        timeout=30, headers={'User-Agent': 'nano-push (push.nano-wetter.de)'})
