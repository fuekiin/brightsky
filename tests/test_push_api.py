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
    kept = 'k' * 43   # what this server issues: token_urlsafe(32)
    resp = push.post('/v1/devices', json=d, headers=auth(kept))
    assert resp.status_code == 200
    assert 'deviceSecret' not in resp.json()
    assert push.post('/v1/devices', json=d,
                     headers=auth(kept)).status_code == 200


def test_short_secret_cannot_adopt(push):
    """Review #5: adopting needs a secret this server could have issued."""
    assert push.post('/v1/devices', json=device(),
                     headers=auth('short')).status_code == 401


def test_adopting_counts_against_the_rate_limit(push, monkeypatch):
    from brightsky.settings import settings
    monkeypatch.setitem(settings, 'PUSH_REGISTER_RATE_LIMIT', 2)
    codes = [push.post('/v1/devices', json=device(),
                       headers=auth('k' * 43)).status_code
             for _ in range(3)]
    assert codes == [200, 200, 429]


def test_known_device_with_secret_is_not_rate_limited(push, monkeypatch):
    from brightsky.settings import settings
    d = device()
    secret = push.post('/v1/devices', json=d).json()['deviceSecret']
    monkeypatch.setitem(settings, 'PUSH_REGISTER_RATE_LIMIT', 1)
    for _ in range(3):
        assert push.post('/v1/devices', json=d,
                         headers=auth(secret)).status_code == 200


def test_cells_outside_germany_are_rejected(push):
    r = rule(cellKey='48.86,2.35')   # Paris
    body = push.post('/v1/devices', json=device([r])).json()
    assert body['rejected'] == [{'ruleId': r['id'].lower(),
                                 'reason': 'outside_coverage'}]


def test_global_cell_cap(push, monkeypatch):
    from brightsky.settings import settings
    monkeypatch.setitem(settings, 'PUSH_MAX_CELLS', 1)
    a, b, c = rule(), rule(cellKey='52.52,13.41'), rule()
    body = push.post('/v1/devices', json=device([a, b, c])).json()
    # the first cell fits, the second does not; the third rule shares the
    # first cell and costs nothing
    assert body['accepted'] == [a['id'].lower(), c['id'].lower()]
    assert body['rejected'] == [{'ruleId': b['id'].lower(),
                                 'reason': 'capacity'}]


def test_body_and_field_limits(push):
    assert push.post('/v1/devices', json=device(
        rules=[rule() for _ in range(201)])).status_code == 422
    assert push.post('/v1/devices', json=device(
        appVersion='x' * 33)).status_code == 422
    big = device()
    big['padding'] = 'x' * (300 * 1024)
    assert push.post('/v1/devices', json=big).status_code == 413


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


def test_a_registration_without_tokens_keeps_the_stored_ones(push, db):
    # The app registers at launch before iOS hands its tokens out again;
    # that must not leave the server unable to reach the phone.
    d = device(pushToStartToken='ef' * 32)
    secret = push.post('/v1/devices', json=d).json()['deviceSecret']
    again = dict(d)
    del again['apnsToken']
    del again['pushToStartToken']
    assert push.post('/v1/devices', json=again,
                     headers=auth(secret)).status_code == 200
    row = db.fetch('SELECT apns_token, push_to_start_token FROM push.devices')[0]
    assert tuple(row) == (APNS, 'ef' * 32)


def test_a_new_token_replaces_the_stored_one(push, db):
    d = device()
    secret = push.post('/v1/devices', json=d).json()['deviceSecret']
    fresh = 'cd' * 32
    assert push.post('/v1/devices', json=dict(d, apnsToken=fresh),
                     headers=auth(secret)).status_code == 200
    assert db.fetch('SELECT apns_token FROM push.devices')[0][0] == fresh


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


def test_catalog_is_served_verbatim(push):
    import json
    from pathlib import Path
    import brightsky.push
    raw = (Path(brightsky.push.__file__).parent / 'catalog.json').read_bytes()
    resp = push.get('/v1/catalog')
    assert resp.status_code == 200
    assert resp.content == raw
    assert resp.headers['cache-control'] == 'public, max-age=86400'
    catalog = json.loads(raw)
    assert set(catalog) >= {'version', 'limits', 'templates', 'themes'}


def _wire(template):
    """A catalogue template (Swift Codable shape) → the wire rule the app's
    RuleWireMapper sends for it."""
    conditions, kind = [], 'user_rule'
    for c in template['conditions']:
        if 'officialWarning' in c:
            kind = 'dwd_warning'
            conditions.append({'warning': c['officialWarning']})
        elif 'rainApproaching' in c:
            kind = 'rain_nowcast'
            conditions.append({'rain': {}})
        else:
            v = c['value']
            conditions.append({'metric': v['metric'], 'cmp': v['comparator'],
                               'value': v['threshold']})
    w = template['window']
    if 'nextHours' in w:
        window = {'nextHours': w['nextHours']['_0']}
    else:
        [(days, spec)] = w['days']['_0'].items()
        [(part, part_spec)] = w['days']['during'].items()
        window = {'days': days,
                  'part': part_spec if part == 'hours' else part}
        if days == 'weekdays':
            window.update(weekdays=sorted(spec['_0']),
                          notice=spec['notice'],
                          together=spec.get('together', False))
        elif days == 'once':
            # resolved to dates on the device when the rule is saved
            window.update(dates=['2099-09-27'], notice=spec['_0']['notice'])
        elif days == 'nextDays':
            window.update(count=spec['_0'])
    rule = rule_(kind=kind, params={'all': conditions, 'window': window})
    delivery = template['delivery']
    if 'liveActivity' in delivery:
        rule['live'] = {'night': delivery['liveActivity']['night']}
    if 'morningDigest' in delivery:
        rule['schedule'] = {'at': '07:00', 'tz': 'Europe/Berlin'}
    return rule


def rule_(**kw):
    return rule(**kw)


def test_catalog_templates_are_rules_the_server_accepts():
    """Every preset in the catalogue must survive registration — a preset
    the server rejects would be a switch that flips back."""
    import json
    from pathlib import Path
    import brightsky.push
    from brightsky.push import rules as rulemod
    catalog = json.loads(
        (Path(brightsky.push.__file__).parent / 'catalog.json').read_text())
    assert catalog['limits']['freeRules'] > 0
    assert 0 < catalog['limits']['maxConditions'] <= 10
    templates = catalog['templates']
    assert len({t['key'] for t in templates}) == len(templates)
    for t in templates:
        if 'inAppOnly' in t['delivery']:
            continue   # never registered (rules design §18.2)
        for c in t['conditions']:
            families = c.get('officialWarning', {}).get('families', [])
            assert set(families) <= set(rulemod.FAMILIES)
        rulemod.parse_rule(_wire(t))   # raises Rejected with the reason


def test_moving_cell_keeps_state(push, db):
    """Rules at „Mein Standort" re-register on every cell change; that
    must not re-arm them (review #1)."""
    r = rule()
    d = device([r])
    secret = push.post('/v1/devices', json=d).json()['deviceSecret']
    db.insert('push.rule_state', [{
        'rule_id': r['id'].lower(), 'occurrence_key': 'k', 'state': '{}'}])
    r['cellKey'] = '53.59,10.07'
    push.post('/v1/devices', json=d, headers=auth(secret))
    assert len(db.fetch('SELECT * FROM push.rule_state')) == 1
    assert db.fetch('SELECT cell_key FROM push.rules')[0][0] == '53.59,10.07'
