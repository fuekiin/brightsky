"""push-work end to end against the test database: registered rules, the
fork's alerts table, a stub APNs."""

import asyncio
import datetime
import json

import httpx
import pytest

from brightsky.push import sources, store
from brightsky.push.worker import Worker

from .test_push_apns import StubAPNs, make_client


UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 9, 23, 12, tzinfo=UTC)
H = datetime.timedelta(hours=1)
DEVICE = '00000000-0000-0000-0000-00000000000d'
WARN_RULE = '00000000-0000-0000-0000-0000000000a1'
WARN_RULE_2 = '00000000-0000-0000-0000-0000000000a2'
CELL = '53.55,10.01'
WARN_CELL = 802000000


@pytest.fixture
def push_db(db):
    yield db
    with db.cursor() as cur:
        cur.execute('DELETE FROM push.devices; DELETE FROM push.cells; '
                    'DELETE FROM push.notifications_sent; '
                    'DELETE FROM push.source_status; DELETE FROM alerts;')
    db.commit()


def warning_rule(rule_id, level=2, live=None):
    r = {'id': rule_id, 'kind': 'dwd_warning', 'cellKey': CELL,
         'params': {'all': [{'warning': {'minLevel': level,
                                         'families': []}}],
                    'window': {'nextHours': 12}}}
    if live:
        r['live'] = live
    return r


def run(push_db, coro_fn, stub, monkeypatch):
    """Run `coro_fn(worker)` with the DWD sync check stubbed to in-sync."""
    async def in_sync(self, conn):
        return True
    monkeypatch.setattr(sources.WarningsSource, 'in_sync', in_sync)

    async def main():
        async with store.pool(max_size=2) as pool:
            async with httpx.AsyncClient() as http:
                worker = Worker(pool, http, make_client(stub))
                return await coro_fn(worker, pool)
    return asyncio.run(main())


def register(pool_rules):
    async def fn(worker, pool):
        async with pool.acquire() as conn:
            await store.register(conn, {
                'deviceId': DEVICE, 'apnsToken': 'ab' * 32,
                'environment': 'sandbox', 'liveActivitiesEnabled': True,
                'rules': pool_rules}, None, NOW)
            await conn.execute(
                'UPDATE push.cells SET warn_cell_id = $1, resolved_at = $2',
                WARN_CELL, NOW)
    return fn


def add_alert(push_db, alert_id, severity, onset=NOW + H, hours=3,
              event='GEWITTER', status='actual'):
    push_db.insert('alerts', [{
        'alert_id': alert_id, 'effective': NOW, 'onset': onset,
        'expires': onset + hours * H, 'severity': severity,
        'event_code': 31, 'event_de': event, 'headline_en': 'h',
        'headline_de': f'Amtliche WARNUNG vor {event}',
        'description_en': 'd', 'description_de': 'd', 'status': status,
    }])
    [(pk,)] = push_db.fetch(
        f"SELECT id FROM alerts WHERE alert_id = '{alert_id}'")
    push_db.insert('alert_cells', [{'alert_id': pk,
                                    'warn_cell_id': WARN_CELL}])


def tick(at):
    async def fn(worker, pool):
        return await worker.warnings_tick(at)
    return fn


def test_warning_end_to_end(push_db, monkeypatch):
    stub = StubAPNs()
    run(push_db, register([warning_rule(WARN_RULE)]), stub, monkeypatch)
    add_alert(push_db, 'A', 'moderate')
    run(push_db, tick(NOW), stub, monkeypatch)
    assert len(stub.requests) == 1
    req = stub.requests[0]
    assert req.url.path == '/3/device/' + 'ab' * 32
    assert req.headers['apns-collapse-id'] == 'dwd:A'
    payload = json.loads(req.content)
    assert payload['aps']['alert'] == {
        'title': 'Markante Warnung', 'body': 'Gewitter, ab 15:00'}
    assert payload['aps']['interruption-level'] == 'time-sensitive'
    assert payload['aps']['mutable-content'] == 1
    assert payload['nano'] == {
        'v': 1, 'kind': 'dwd_warning', 'ruleId': WARN_RULE,
        'ruleIds': [WARN_RULE], 'cellKey': CELL, 'occurrence': 'dwd:A',
        'evidence': {'values': {}, 'day': 'heute', 'time': 'ab 15:00',
                     'warning': {'level': 2, 'family': 'gewitter',
                                 'onset': 'ab 15:00'}},
    }
    # Next minute: nothing new.
    run(push_db, tick(NOW + datetime.timedelta(minutes=1)), stub,
        monkeypatch)
    assert len(stub.requests) == 1
    # DWD re-issues it under a new id, same level: silent.
    push_db.fetch("DELETE FROM alerts WHERE alert_id = 'A' RETURNING id")
    add_alert(push_db, 'B', 'moderate', hours=4)
    run(push_db, tick(NOW + datetime.timedelta(minutes=2)), stub,
        monkeypatch)
    assert len(stub.requests) == 1
    # … and escalated: fires.
    add_alert(push_db, 'C', 'severe', hours=4)
    run(push_db, tick(NOW + datetime.timedelta(minutes=3)), stub,
        monkeypatch)
    assert len(stub.requests) == 2
    assert json.loads(stub.requests[1].content)['aps']['alert']['title'] \
        == 'Unwetterwarnung'
    statuses = push_db.fetch(
        'SELECT apns_status FROM push.notifications_sent')
    assert [s[0] for s in statuses] == [200, 200]


def test_two_rules_one_alert_one_push_lead_by_delivery(push_db, monkeypatch):
    stub = StubAPNs()
    rules = [warning_rule(WARN_RULE),
             warning_rule(WARN_RULE_2, live={'night': False})]
    run(push_db, register(rules), stub, monkeypatch)
    add_alert(push_db, 'A', 'severe')
    run(push_db, tick(NOW), stub, monkeypatch)
    assert len(stub.requests) == 1
    nano = json.loads(stub.requests[0].content)['nano']
    assert nano['ruleId'] == WARN_RULE_2
    assert nano['ruleIds'] == [WARN_RULE, WARN_RULE_2]


def test_below_level_and_test_alerts_do_not_fire(push_db, monkeypatch):
    stub = StubAPNs()
    run(push_db, register([warning_rule(WARN_RULE, level=3)]), stub,
        monkeypatch)
    add_alert(push_db, 'A', 'moderate')
    add_alert(push_db, 'T', 'extreme', status='test')
    run(push_db, tick(NOW), stub, monkeypatch)
    assert stub.requests == []


def test_stale_snapshot_stops_evaluation(push_db, monkeypatch):
    stub = StubAPNs()
    run(push_db, register([warning_rule(WARN_RULE)]), stub, monkeypatch)
    add_alert(push_db, 'A', 'moderate')

    async def out_of_sync(self, conn):
        return False

    async def fn(worker, pool):
        monkeypatch.setattr(sources.WarningsSource, 'in_sync', out_of_sync)
        with pytest.raises(sources.Stale):
            await worker.warnings_tick(NOW)
    run(push_db, fn, stub, monkeypatch)
    assert stub.requests == []


def test_dead_token_deletes_the_device(push_db, monkeypatch):
    stub = StubAPNs([(410, 'Unregistered')])
    run(push_db, register([warning_rule(WARN_RULE)]), stub, monkeypatch)
    add_alert(push_db, 'A', 'moderate')
    run(push_db, tick(NOW), stub, monkeypatch)
    assert push_db.fetch('SELECT * FROM push.devices') == []


def test_forecast_rule_fires_once_per_night(push_db, monkeypatch):
    from brightsky.push import berlin, evaluator as ev
    frost = {'id': WARN_RULE, 'kind': 'user_rule', 'cellKey': CELL,
             'params': {'all': [{'metric': 'temp', 'cmp': 'lt',
                                 'value': 0}],
                        'window': {'days': 'today', 'part': 'night'}}}
    stub = StubAPNs()
    run(push_db, register([frost]), stub, monkeypatch)

    def b(d, h):
        return datetime.datetime(2026, 9, d, h, tzinfo=berlin.TZ)
    hours = [ev.Hour(b(23, 22), temperature=2),
             ev.Hour(b(24, 5), temperature=-2)]

    async def fake_fetch(self, cell_key, lat, lon, now):
        self.hours[cell_key] = hours
        self.fetched_at[cell_key] = now
        return hours
    monkeypatch.setattr(sources.ForecastSource, 'fetch', fake_fetch)

    def ftick(at):
        async def fn(worker, pool):
            return await worker.forecast_tick(at)
        return fn
    run(push_db, ftick(NOW), stub, monkeypatch)
    run(push_db, ftick(NOW + 15 * datetime.timedelta(minutes=1)), stub,
        monkeypatch)
    assert len(stub.requests) == 1
    payload = json.loads(stub.requests[0].content)
    assert payload['aps']['alert'] == {
        'title': 'Temperaturgrenze erreicht',
        'body': 'Die Temperatur sinkt voraussichtlich auf −2 °C.'}
    assert 'interruption-level' not in payload['aps']
    assert payload['nano']['evidence'] == {
        'values': {'temp': -2.0}, 'day': 'morgen', 'time': 'gegen 5 Uhr'}
    assert payload['nano']['occurrence'] == f'rule:{WARN_RULE}:2026-09-23'


# MARK: - Live Activities

RAIN_RULE = '00000000-0000-0000-0000-0000000000b1'


def rain_rule(live_=True):
    r = {'id': RAIN_RULE, 'kind': 'rain_nowcast', 'cellKey': CELL,
         'params': {'all': [{'rain': {}}], 'window': {'nextHours': 1}}}
    if live_:
        r['live'] = {'night': False}
    return r


def register_live(rules, push_to_start='cd' * 32):
    async def fn(worker, pool):
        async with pool.acquire() as conn:
            await store.register(conn, {
                'deviceId': DEVICE, 'apnsToken': 'ab' * 32,
                'pushToStartToken': push_to_start,
                'environment': 'sandbox', 'liveActivitiesEnabled': True,
                'rules': rules}, None, NOW)
            await conn.execute(
                'UPDATE push.cells SET warn_cell_id = $1, resolved_at = $2',
                WARN_CELL, NOW)
    return fn


def radar(monkeypatch, mm):
    from brightsky.push import live

    async def fetch(self, lat, lon, now):
        return [live.Point(NOW + i * datetime.timedelta(minutes=5), v)
                for i, v in enumerate(mm)]
    monkeypatch.setattr(sources.NowcastSource, 'fetch', fetch)


def ntick(at):
    async def fn(worker, pool):
        return await worker.nowcast_tick(at)
    return fn


def report_token(push_db, token='ef' * 32):
    with push_db.cursor() as cur:
        cur.execute("UPDATE push.live_activities SET activity_token = %s, "
                    "activity_id = 'X'", (token,))
    push_db.commit()


def bodies(stub):
    return [(r.headers['apns-push-type'], r.url.path.rsplit('/', 1)[1][:2],
             json.loads(r.content)) for r in stub.requests]


def test_rain_activity_starts_updates_quietly_and_ends(push_db, monkeypatch):
    stub = StubAPNs()
    run(push_db, register_live([rain_rule()]), stub, monkeypatch)
    radar(monkeypatch, [0.2 if 4 <= i <= 12 else 0 for i in range(24)])
    run(push_db, ntick(NOW), stub, monkeypatch)
    [(push_type, token, payload)] = bodies(stub)
    assert (push_type, token) == ('liveactivity', 'cd')
    aps = payload['aps']
    assert aps['event'] == 'start'
    assert aps['alert']['body'] == 'Regen in 20 Min.'
    assert 'sound' not in aps['alert']            # > 15 min away: silent
    state = aps['content-state']
    assert state['ruleId'] == RAIN_RULE and state['placeName'] == ''
    assert state['phase']['rain']['_0']['state'] == 'coming'
    # No ordinary notification on top of the activity.
    assert all(r.headers['apns-push-type'] == 'liveactivity'
               for r in stub.requests)
    report_token(push_db)
    # Same forecast five minutes later: nothing moved, nothing sent.
    run(push_db, ntick(NOW + datetime.timedelta(minutes=5)), stub,
        monkeypatch)
    assert len(stub.requests) == 1
    # Dry for two hours: end with a dismissal date, cooldown starts.
    radar(monkeypatch, [0] * 24)
    run(push_db, ntick(NOW + datetime.timedelta(minutes=10)), stub,
        monkeypatch)
    push_type, token, payload = bodies(stub)[-1]
    assert (push_type, token, payload['aps']['event']) == (
        'liveactivity', 'ef', 'end')
    assert payload['aps']['dismissal-date'] > payload['aps']['timestamp']
    [(cooldown,)] = push_db.fetch(
        'SELECT cooldown_until IS NOT NULL FROM push.live_activities')
    assert cooldown


def test_rain_without_push_to_start_is_a_notification(push_db, monkeypatch):
    stub = StubAPNs()
    run(push_db, register_live([rain_rule()], push_to_start=None), stub,
        monkeypatch)
    radar(monkeypatch, [0.2 if 4 <= i <= 12 else 0 for i in range(24)])
    run(push_db, ntick(NOW), stub, monkeypatch)
    [(push_type, _, payload)] = bodies(stub)
    assert push_type == 'alert'
    assert payload['aps']['alert'] == {'title': 'Regen zieht auf',
                                       'body': 'Regen in der Nähe erwartet'}
    assert payload['nano']['evidence']['rain'] == {
        'startsInMinutes': 20, 'durationMinutes': 45}
    # The same shower next cycle: no second notification.
    run(push_db, ntick(NOW + datetime.timedelta(minutes=5)), stub,
        monkeypatch)
    assert len(stub.requests) == 1


def test_warning_takes_over_escalates_and_is_cancelled(push_db, monkeypatch):
    stub = StubAPNs()
    run(push_db, register_live([
        rain_rule(), warning_rule(WARN_RULE, live={'night': False})]),
        stub, monkeypatch)
    radar(monkeypatch, [0.2] * 24)
    run(push_db, ntick(NOW), stub, monkeypatch)
    assert bodies(stub)[-1][2]['aps']['event'] == 'start'
    report_token(push_db)
    add_alert(push_db, 'A', 'moderate')
    run(push_db, tick(NOW + datetime.timedelta(minutes=1)), stub,
        monkeypatch)
    push_type, token, payload = bodies(stub)[-1]
    assert (token, payload['aps']['event']) == ('ef', 'update')
    phase = payload['aps']['content-state']['phase']
    assert phase['warning']['_0']['stage'] == 'upcoming'
    assert len(stub.requests) == 2    # no notification alongside
    # Rain does not take it back while the warning runs.
    run(push_db, ntick(NOW + datetime.timedelta(minutes=5)), stub,
        monkeypatch)
    assert len(stub.requests) == 2
    # Re-issued and escalated: update with alert.
    push_db.fetch("DELETE FROM alerts WHERE alert_id = 'A' RETURNING id")
    add_alert(push_db, 'B', 'severe')
    run(push_db, tick(NOW + datetime.timedelta(minutes=6)), stub,
        monkeypatch)
    payload = bodies(stub)[-1][2]
    assert payload['aps']['event'] == 'update'
    assert payload['aps']['alert']['title'] == 'Unwetterwarnung'
    w = payload['aps']['content-state']['phase']['warning']['_0']
    assert (w['level'], w['escalatedFrom']) == (3, 2)
    # Cancelled: „aufgehoben", then end.
    push_db.fetch("DELETE FROM alerts WHERE alert_id = 'B' RETURNING id")
    add_alert(push_db, 'OTHER', 'minor', event='NEBEL')
    run(push_db, tick(NOW + datetime.timedelta(minutes=7)), stub,
        monkeypatch)
    payload = bodies(stub)[-1][2]
    assert payload['aps']['event'] == 'end'
    assert payload['aps']['content-state']['phase']['warning']['_0'][
        'stage'] == 'cancelled'
