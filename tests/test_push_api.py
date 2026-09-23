import uuid

import pytest
from fastapi.testclient import TestClient


APNS = 'ab' * 32


@pytest.fixture
def push(db):
    from brightsky.push import api
    api.rate_limiter.hits.clear()
    with TestClient(api.app) as client:
        yield client
    with db.cursor() as cur:
        cur.execute('DELETE FROM push.devices; DELETE FROM push.cells; '
                    'DELETE FROM push.source_status;')
    db.commit()


def rule(**overrides):
    r = {
        'id': str(uuid.uuid4()).upper(),
        'kind': 'user_rule',
        'cellKey': '53.55,10.01',
        'params': {
            'ruleId': 'x',
            'all': [{'metric': 'temp', 'cmp': 'lt', 'value': 0.0}],
            'window': {'days': 'today', 'part': 'night'},
        },
    }
    r.update(overrides)
    return r


def device(rules=(), **overrides):
    d = {
        'deviceId': str(uuid.uuid4()).upper(),
        'apnsToken': APNS,
        'liveActivitiesEnabled': True,
        'environment': 'sandbox',
        'appVersion': '2.4.0',
        'tier': 'free',
        'rules': list(rules),
    }
    d.update(overrides)
    return d


def auth(secret):
    return {'Authorization': f'Bearer {secret}'}


def test_first_registration_returns_secret(push, db):
    r = rule()
    resp = push.post('/v1/devices', json=device([r]))
    assert resp.status_code == 200
    body = resp.json()
    assert body['accepted'] == [r['id'].lower()]
    assert body['rejected'] == []
    assert len(body['deviceSecret']) > 30
    rows = db.fetch('SELECT kind, cell_key FROM push.rules')
    assert [tuple(x) for x in rows] == [('user_rule', '53.55,10.01')]
    cells = db.fetch('SELECT lat, lon FROM push.cells')
    assert [tuple(x) for x in cells] == [(53.55, 10.01)]


def test_authenticated_update_replaces_rule_set(push, db):
    a, b = rule(), rule()
    d = device([a, b])
    secret = push.post('/v1/devices', json=d).json()['deviceSecret']
    d['rules'] = [b]
    resp = push.post('/v1/devices', json=d, headers=auth(secret))
    assert resp.status_code == 200
    assert 'deviceSecret' not in resp.json()
    ids = [str(x[0]) for x in db.fetch('SELECT id FROM push.rules')]
    assert ids == [b['id'].lower()]


def test_wrong_secret_is_401_and_retry_takes_over(push):
    d = device()
    first = push.post('/v1/devices', json=d).json()['deviceSecret']
    resp = push.post('/v1/devices', json=d, headers=auth('nope'))
    assert resp.status_code == 401
    resp = push.post('/v1/devices', json=d)
    second = resp.json()['deviceSecret']
    assert second != first
    assert push.post('/v1/devices', json=d,
                     headers=auth(first)).status_code == 401
    assert push.post('/v1/devices', json=d,
                     headers=auth(second)).status_code == 200


def test_unknown_device_with_secret_is_adopted(push):
    d = device()
    resp = push.post('/v1/devices', json=d, headers=auth('kept-secret'))
    assert resp.status_code == 200
    assert 'deviceSecret' not in resp.json()
    assert push.post('/v1/devices', json=d,
                     headers=auth('kept-secret')).status_code == 200


@pytest.mark.parametrize('overrides, reason', [
    ({'kind': 'pollen'}, 'health_not_available'),
    ({'kind': 'nope'}, 'unknown_kind'),
    ({'cellKey': '53.5,10.01'}, 'bad_cell_key'),
    ({'params': {'all': [{'metric': 'ozone', 'cmp': 'gt', 'value': 1}],
                 'window': {'nextHours': 6}}}, 'unknown_metric'),
    ({'params': {'all': [{'metric': 'temp', 'cmp': 'lt', 'value': 0}],
                 'window': {'nextHours': 99}}}, 'unknown_window'),
    ({'params': {'all': [{'metric': 'temp', 'cmp': 'lt', 'value': 0}],
                 'window': {'days': 'fortnight', 'part': 'night'}}},
     'unknown_window'),
    ({'kind': 'dwd_warning'}, 'bad_conditions'),
    ({'live': {'night': False}}, 'live_not_applicable'),
    ({'schedule': {'at': '06:00', 'tz': 'Europe/Berlin'}},
     'unknown_schedule'),
])
def test_rejections(push, overrides, reason):
    r = rule(**overrides)
    body = push.post('/v1/devices', json=device([r])).json()
    assert body['accepted'] == []
    assert body['rejected'] == [{'ruleId': r['id'].lower(), 'reason': reason}]


def test_accepts_every_app_window_shape(push, db):
    windows = [
        {'nextHours': 6.0},
        {'days': 'today', 'part': 'allDay'},
        {'days': 'tomorrow', 'part': {'from': 22.0, 'to': 6.0}},
        {'days': 'weekdays', 'weekdays': [6.0, 7.0], 'notice': 'dayBefore',
         'together': True, 'part': 'midday'},
        {'days': 'weekdays', 'weekdays': [1], 'notice': 'sameDay',
         'part': 'morning'},
        {'days': 'once', 'dates': ['2099-09-27', '2099-09-28'],
         'notice': 'twoDaysBefore', 'part': 'evening'},
        {'days': 'nextDays', 'count': 3.0, 'part': 'night'},
    ]
    rules = [rule(params={'all': [{'metric': 'gust', 'cmp': 'gt',
                                   'value': 60.0}], 'window': w})
             for w in windows]
    warning = rule(kind='dwd_warning', live={'night': False}, params={
        'all': [{'warning': {'minLevel': 3.0, 'families': []}},
                {'metric': 'temp', 'cmp': 'lt', 'value': 1.0}],
        'window': {'nextHours': 12}})
    fog = rule(kind='dwd_warning', live={'night': False}, params={
        'all': [{'warning': {'minLevel': 2, 'families': ['nebel']}}],
        'window': {'nextHours': 6}})
    rain = rule(kind='rain_nowcast', live={'night': True}, params={
        'all': [{'rain': {}}], 'window': {'nextHours': 2}})
    digest = rule(schedule={'at': '07:00', 'tz': 'Europe/Berlin'})
    all_rules = rules + [warning, fog, rain, digest]
    body = push.post('/v1/devices', json=device(all_rules)).json()
    assert body['rejected'] == []
    assert len(body['accepted']) == len(all_rules)


def test_past_once_rule_is_accepted_but_not_stored(push, db):
    r = rule(params={'all': [{'metric': 'temp', 'cmp': 'lt', 'value': 0}],
                     'window': {'days': 'once', 'dates': ['2020-01-01'],
                                'notice': 'dayBefore', 'part': 'allDay'}})
    body = push.post('/v1/devices', json=device([r])).json()
    assert body['accepted'] == [r['id'].lower()]
    assert db.fetch('SELECT id FROM push.rules') == []


def test_rule_id_of_another_device_is_rejected(push):
    r = rule()
    push.post('/v1/devices', json=device([r]))
    body = push.post('/v1/devices', json=device([r])).json()
    assert body['rejected'] == [{'ruleId': r['id'].lower(),
                                 'reason': 'duplicate_id'}]


def test_device_limit(push, monkeypatch):
    from brightsky.settings import settings
    monkeypatch.setitem(settings, 'PUSH_MAX_RULES_PER_DEVICE', 2)
    rules = [rule() for _ in range(3)]
    body = push.post('/v1/devices', json=device(rules)).json()
    assert len(body['accepted']) == 2
    assert body['rejected'][0]['reason'] == 'device_limit'


def test_edited_rule_rearms(push, db):
    r = rule()
    d = device([r])
    secret = push.post('/v1/devices', json=d).json()['deviceSecret']
    rid = r['id'].lower()
    db.insert('push.rule_state', [{
        'rule_id': rid, 'occurrence_key': 'k', 'state': '{}'}])
    push.post('/v1/devices', json=d, headers=auth(secret))
    assert len(db.fetch('SELECT * FROM push.rule_state')) == 1
    r['params']['all'][0]['value'] = -5.0
    push.post('/v1/devices', json=d, headers=auth(secret))
    assert db.fetch('SELECT * FROM push.rule_state') == []


@pytest.mark.parametrize('overrides', [
    {'environment': 'development'},
    {'apnsToken': 'not hex'},
    {'deviceId': 'nope'},
])
def test_bad_device_is_422(push, overrides):
    resp = push.post('/v1/devices', json=device(**overrides))
    assert resp.status_code == 422


def test_absent_tokens(push, db):
    d = device()
    del d['apnsToken']
    assert push.post('/v1/devices', json=d).status_code == 200
    assert db.fetch('SELECT apns_token FROM push.devices')[0][0] is None


def test_rate_limit(push, monkeypatch):
    from brightsky.settings import settings
    monkeypatch.setitem(settings, 'PUSH_REGISTER_RATE_LIMIT', 2)
    codes = [push.post('/v1/devices', json=device()).status_code
             for _ in range(3)]
    assert codes == [200, 200, 429]


def test_activity_token_round_trip(push, db):
    d = device()
    secret = push.post('/v1/devices', json=d).json()['deviceSecret']
    path = f"/v1/devices/{d['deviceId']}/activity"
    resp = push.put(path, json={'activityId': 'A1', 'pushToken': 'CD' * 40},
                    headers=auth(secret))
    assert resp.status_code == 204
    row = db.fetch('SELECT activity_id, activity_token '
                   'FROM push.live_activities')[0]
    assert tuple(row) == ('A1', 'cd' * 40)
    resp = push.request('DELETE', path, json={'activityId': 'A1'},
                        headers=auth(secret))
    assert resp.status_code == 204
    row = db.fetch('SELECT activity_token, cooldown_until IS NOT NULL '
                   'FROM push.live_activities')[0]
    assert tuple(row) == (None, True)


def test_endpoints_need_the_secret(push):
    d = device()
    push.post('/v1/devices', json=d)
    path = f"/v1/devices/{d['deviceId']}"
    assert push.delete(path).status_code == 401
    assert push.delete(f'/v1/devices/{uuid.uuid4()}',
                       headers=auth('x')).status_code == 404


def test_delete_device(push, db):
    d = device([rule()])
    secret = push.post('/v1/devices', json=d).json()['deviceSecret']
    resp = push.delete(f"/v1/devices/{d['deviceId']}", headers=auth(secret))
    assert resp.status_code == 204
    assert db.fetch('SELECT * FROM push.rules') == []


def test_health(push):
    body = push.get('/health').json()
    assert body['status'] == 'ok'
    assert body['database'] is True
    assert body['sources']['warnings']['lastSuccessAgeSeconds'] is None
