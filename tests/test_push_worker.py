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
