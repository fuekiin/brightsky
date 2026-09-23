"""One test per finding of the 2026-09-24 review of nano-push (numbers as in
docs/nano/push.md, „After the review"), plus the test gaps it named."""

import asyncio
import datetime
import json

import pytest

from brightsky.push import (
    apns, berlin, evaluator as ev, firing, live, payloads, sender, sources,
    store,
)
from brightsky.push.rules import parse_rule

from .test_push_apns import StubAPNs, make_client
from .test_push_worker import (
    CELL, DEVICE, NOW, RAIN_RULE, WARN_RULE, add_alert, bodies, ntick, push_db,
    radar, rain_rule, register_live, report_token, run, tick, warning_rule,
)


__all__ = ['push_db']   # the fixture, re-exported for pytest

M = datetime.timedelta(minutes=1)
H = datetime.timedelta(hours=1)


def live_row(push_db):
    rows = push_db.fetch(
        'SELECT phase, ended_at IS NOT NULL, state, activity_id '
        'FROM push.live_activities')
    return rows[0] if rows else None


# MARK: - #2 dismissed stays dismissed

def test_dismissed_warning_activity_does_not_come_back(push_db, monkeypatch):
    stub = StubAPNs()
    run(push_db, register_live([warning_rule(WARN_RULE,
                                             live={'night': False})]),
        stub, monkeypatch)
    add_alert(push_db, 'A', 'moderate')
    run(push_db, tick(NOW), stub, monkeypatch)
    assert bodies(stub)[-1][2]['aps']['event'] == 'start'
    report_token(push_db)

    async def dismiss(worker, pool):
        async with pool.acquire() as conn:
            await store.end_activity(conn, DEVICE, 'X', NOW + M,
                                     datetime.timedelta(minutes=60))
    run(push_db, dismiss, stub, monkeypatch)
    run(push_db, tick(NOW + 2 * M), stub, monkeypatch)
    assert len(stub.requests) == 1          # no restart, no notification
    add_alert(push_db, 'B', 'severe')       # escalation brings it back
    run(push_db, tick(NOW + 3 * M), stub, monkeypatch)
    assert bodies(stub)[-1][2]['aps']['event'] == 'start'


# MARK: - #7 expiry is not a cancellation

def test_expired_warning_ends_without_aufgehoben(push_db, monkeypatch):
    stub = StubAPNs()
    run(push_db, register_live([warning_rule(WARN_RULE,
                                             live={'night': False})]),
        stub, monkeypatch)
    add_alert(push_db, 'A', 'moderate', onset=NOW - H, hours=2)
    run(push_db, tick(NOW), stub, monkeypatch)
    report_token(push_db)
    run(push_db, tick(NOW + H + M), stub, monkeypatch)   # past its expiry
    aps = bodies(stub)[-1][2]['aps']
    assert aps['event'] == 'end'
    assert aps['content-state']['phase']['warning']['_0']['stage'] \
        == 'active'


def test_cancelled_warning_says_aufgehoben_with_time(push_db, monkeypatch):
    stub = StubAPNs()
    run(push_db, register_live([warning_rule(WARN_RULE,
                                             live={'night': False})]),
        stub, monkeypatch)
    add_alert(push_db, 'A', 'moderate')
    run(push_db, tick(NOW), stub, monkeypatch)
    report_token(push_db)
    push_db.fetch("DELETE FROM alerts WHERE alert_id = 'A' RETURNING id")
    add_alert(push_db, 'N', 'minor', event='NEBEL')
    run(push_db, tick(NOW + M), stub, monkeypatch)
    w = bodies(stub)[-1][2]['aps']['content-state']['phase']['warning']['_0']
    assert w['stage'] == 'cancelled'
    assert w['detail'] == 'Der DWD hat die Warnung um 14:01 aufgehoben'


# MARK: - #16 re-issue with another expiry, #17 the 8 h limit

def test_reissue_with_later_expiry_is_a_silent_update(push_db, monkeypatch):
    stub = StubAPNs()
    run(push_db, register_live([warning_rule(WARN_RULE,
                                             live={'night': False})]),
        stub, monkeypatch)
    add_alert(push_db, 'A', 'moderate')
    run(push_db, tick(NOW), stub, monkeypatch)
    report_token(push_db)
    push_db.fetch("DELETE FROM alerts WHERE alert_id = 'A' RETURNING id")
    add_alert(push_db, 'B', 'moderate', hours=5)
    run(push_db, tick(NOW + M), stub, monkeypatch)
    aps = bodies(stub)[-1][2]['aps']
    assert aps['event'] == 'update'
    assert 'alert' not in aps
    w = aps['content-state']['phase']['warning']['_0']
    assert w['expires'] == payloads.swift_date(NOW + 6 * H)


def test_long_warning_is_not_restarted_after_eight_hours(push_db,
                                                         monkeypatch):
    stub = StubAPNs()
    run(push_db, register_live([warning_rule(WARN_RULE,
                                             live={'night': False})]),
        stub, monkeypatch)
    add_alert(push_db, 'A', 'moderate', onset=NOW, hours=20)
    run(push_db, tick(NOW + M), stub, monkeypatch)
    report_token(push_db)
    run(push_db, tick(NOW + 8 * H + M), stub, monkeypatch)
    assert bodies(stub)[-1][2]['aps']['event'] == 'end'
    n = len(stub.requests)
    run(push_db, tick(NOW + 8 * H + 2 * M), stub, monkeypatch)
    assert len(stub.requests) == n


# MARK: - #3 mm/h, #18 one rain definition

def test_rain_buckets_go_out_in_mm_per_hour():
    r = live.analyze_rain([live.Point(NOW + i * 5 * M, 0.5)
                           for i in range(24)], NOW)
    content = live.rain_content(r, NOW, 'rule')
    assert content['phase']['rain']['_0']['buckets'][0] == 6.0


def test_one_wet_step_is_not_raining_now():
    r = live.analyze_rain([live.Point(NOW + i * 5 * M, 0.5 if i == 0 else 0)
                           for i in range(24)], NOW)
    assert r.state == 'ended'


# MARK: - #8 gaps and missing data, #9 winner only

def test_a_long_gap_does_not_end_a_running_rain_activity(push_db,
                                                         monkeypatch):
    stub = StubAPNs()
    run(push_db, register_live([rain_rule()]), stub, monkeypatch)
    radar(monkeypatch, [0.2] * 6 + [0] * 18)
    run(push_db, ntick(NOW), stub, monkeypatch)
    report_token(push_db)
    # dry now, the next shower in 90 minutes: still one rain phase
    radar(monkeypatch, [0] * 18 + [0.2] * 6)
    run(push_db, ntick(NOW + 30 * M), stub, monkeypatch)
    assert all(b[2]['aps']['event'] != 'end' for b in bodies(stub))


def test_no_nowcast_leaves_a_running_activity_alone(push_db, monkeypatch):
    stub = StubAPNs()
    run(push_db, register_live([rain_rule()]), stub, monkeypatch)
    radar(monkeypatch, [0.2] * 24)
    run(push_db, ntick(NOW), stub, monkeypatch)
    report_token(push_db)

    async def fail(self, lat, lon, now):
        raise RuntimeError('radar down')
    monkeypatch.setattr(sources.NowcastSource, 'fetch', fail)
    with pytest.raises(RuntimeError):
        run(push_db, ntick(NOW + 5 * M), stub, monkeypatch)
    assert len(stub.requests) == 1
    assert live_row(push_db)[1] is False      # still running


def test_only_the_winning_rain_rides_on_the_activity(push_db, monkeypatch):
    stub = StubAPNs()
    other = dict(rain_rule(), id='00000000-0000-0000-0000-0000000000b2',
                 cellKey='52.52,13.41')
    run(push_db, register_live([rain_rule(), other]), stub, monkeypatch)
    radar(monkeypatch, [0.2] * 24)
    run(push_db, ntick(NOW), stub, monkeypatch)
    types = sorted(r.headers['apns-push-type'] for r in stub.requests)
    assert types == ['alert', 'liveactivity']


# MARK: - #11 no flapping, #12 one rain notification per place

def rain_match(minutes):
    return ev.RainMatch(minutes, 30, ev.Evidence(rain={}))


def test_rain_rearms_only_when_the_nowcast_is_clear():
    r = parse_rule(rain_rule(live_=False))
    states = {}

    def step(match, clear, at):
        d = firing.decide_rain(r, match, states, at, clear)
        for k, (state, fired, exp) in d.writes.items():
            states[k] = {'state': state, 'fired_at': fired,
                         'expires_at': exp}
        return d.fires
    assert step(rain_match(50), False, NOW)
    # drifts past the horizon: no match, but rain still on the radar
    assert not step(None, False, NOW + 5 * M)
    assert not step(rain_match(55), False, NOW + 10 * M)
    assert not step(None, True, NOW + 15 * M)      # clear: re-arms
    assert step(rain_match(40), False, NOW + 20 * M)


def test_two_rain_rules_at_one_place_make_one_notification(push_db,
                                                           monkeypatch):
    stub = StubAPNs()
    second = dict(rain_rule(live_=False),
                  id='00000000-0000-0000-0000-0000000000b2')
    run(push_db, register_live([rain_rule(live_=False), second]), stub,
        monkeypatch)
    radar(monkeypatch, [0.2] * 24)
    run(push_db, ntick(NOW), stub, monkeypatch)
    [(push_type, _, payload)] = bodies(stub)
    assert payload['nano']['occurrence'] == f'rain:{CELL}'
    assert len(payload['nano']['ruleIds']) == 2


# MARK: - #13 token races

def test_token_reported_during_the_start_is_kept(push_db, monkeypatch):
    """The app's PUT can arrive while the start is still in flight."""
    stub = StubAPNs()
    original = stub.__call__

    def report_then_answer(request):
        if json.loads(request.content)['aps'].get('event') == 'start':
            report_token(push_db, 'aa' * 32)
        return original(request)
    stub.__call__ = report_then_answer
    run(push_db, register_live([rain_rule()]), report_then_answer,
        monkeypatch)
    radar(monkeypatch, [0.2] * 24)
    run(push_db, ntick(NOW), report_then_answer, monkeypatch)
    [(token,)] = push_db.fetch(
        'SELECT activity_token FROM push.live_activities')
    assert token == 'aa' * 32


def test_late_delete_does_not_end_a_newer_activity(push_db, monkeypatch):
    stub = StubAPNs()
    run(push_db, register_live([rain_rule()]), stub, monkeypatch)
    radar(monkeypatch, [0.2] * 24)
    run(push_db, ntick(NOW), stub, monkeypatch)
    report_token(push_db)    # activity 'X'

    async def late_delete(worker, pool):
        async with pool.acquire() as conn:
            await store.end_activity(conn, DEVICE, 'OLDER', NOW,
                                     datetime.timedelta(minutes=60))
    run(push_db, late_delete, stub, monkeypatch)
    assert live_row(push_db)[1] is False


def test_dead_activity_token_ends_the_row(push_db, monkeypatch):
    stub = StubAPNs([(200, None), (410, 'Unregistered')])
    run(push_db, register_live([rain_rule()]), stub, monkeypatch)
    radar(monkeypatch, [0.2 if i < 6 else 0 for i in range(24)])
    run(push_db, ntick(NOW), stub, monkeypatch)
    report_token(push_db)
    radar(monkeypatch, [1.0] * 24)     # heavier: an update is due
    run(push_db, ntick(NOW + 15 * M), stub, monkeypatch)
    assert live_row(push_db)[1] is True
    assert len(push_db.fetch('SELECT * FROM push.devices')) == 1


# MARK: - #14 sender

def test_a_live_start_is_never_retried():
    stub = StubAPNs([(0, None), (200, None)])
    client = make_client(stub)

    sends = []

    async def fake_send(*a, **kw):
        sends.append(a)
        return apns.Result(0, 'ReadTimeout')
    client.send = fake_send
    calls = []

    class Conn:
        async def execute(self, *a):
            calls.append(a)

    async def no_sleep(_):
        pass
    result = asyncio.run(sender.deliver(
        Conn(), client, device_id='d', environment='sandbox', token='t',
        token_field='push_to_start_token', payload={}, retry=False,
        push_type='liveactivity', now=NOW, sleep=no_sleep))
    assert result.status == 0
    assert len(sends) == 1
    # the same timeout on a notification is retried
    asyncio.run(sender.deliver(
        Conn(), client, device_id='d', environment='sandbox', token='t',
        token_field='apns_token', payload={}, now=NOW, sleep=no_sleep))
    assert len(sends) == 1 + 4


@pytest.mark.parametrize('status', [502, 504])
def test_gateway_errors_are_retried(status):
    assert apns.Result(status).retryable


def test_too_many_provider_token_updates_is_not_a_dead_token():
    r = apns.Result(429, 'TooManyProviderTokenUpdates')
    assert r.retryable and not r.token_dead


# MARK: - #21 forget only the token that failed

def test_forget_token_keeps_a_newer_token(push_db, monkeypatch):
    run(push_db, register_live([]), StubAPNs(), monkeypatch)

    async def forget(worker, pool):
        async with pool.acquire() as conn:
            await sender.forget_token(conn, DEVICE, 'apns_token',
                                      'stale' * 8, NOW)
    run(push_db, forget, StubAPNs(), monkeypatch)
    assert len(push_db.fetch('SELECT * FROM push.devices')) == 1


# MARK: - #22 wording

def test_warning_wording():
    w = ev.Warning('a', 3, 'gewitter', 'STARKES GEWITTER MIT HAGEL', 'h',
                   NOW + H, NOW + 3 * H)
    c = live.warning_content(w, 'active', NOW, 'r', escalated_from=2)
    phase = c['phase']['warning']['_0']
    assert phase['event'] == 'Starkes Gewitter mit Hagel'
    assert phase['detail'].endswith('· hochgestuft von markant')


# MARK: - #6 one bad rule does not stop the loop

def test_a_broken_stored_rule_is_skipped(push_db, monkeypatch):
    stub = StubAPNs()
    run(push_db, register_live([warning_rule(WARN_RULE),
                                dict(warning_rule(
                                    '00000000-0000-0000-0000-0000000000a2'))]),
        stub, monkeypatch)
    with push_db.cursor() as cur:
        cur.execute("UPDATE push.rules SET params = '{\"all\": 5}' "
                    "WHERE id = '00000000-0000-0000-0000-0000000000a2'")
    push_db.commit()
    add_alert(push_db, 'A', 'moderate')
    run(push_db, tick(NOW), stub, monkeypatch)
    assert len(stub.requests) == 1


# MARK: - #20 and cleanup_tick

def test_cleanup_removes_what_is_old(push_db, monkeypatch):
    run(push_db, register_live([rain_rule()]), StubAPNs(), monkeypatch)
    push_db.insert('push.rule_state', [{
        'rule_id': RAIN_RULE, 'occurrence_key': 'old', 'state': '{}',
        'expires_at': NOW - H}])
    push_db.insert('push.notifications_sent', [{
        'device_id': DEVICE, 'sent_at': NOW - datetime.timedelta(days=31)}])

    async def clean(worker, pool):
        await worker.cleanup_tick(NOW)
    run(push_db, clean, StubAPNs(), monkeypatch)
    assert push_db.fetch('SELECT * FROM push.rule_state') == []
    assert push_db.fetch('SELECT * FROM push.notifications_sent') == []
    assert len(push_db.fetch('SELECT * FROM push.devices')) == 1

    async def much_later(worker, pool):
        await worker.cleanup_tick(NOW + datetime.timedelta(days=91))
    run(push_db, much_later, StubAPNs(), monkeypatch)
    assert push_db.fetch('SELECT * FROM push.devices') == []
    assert push_db.fetch('SELECT * FROM push.cells') == []


# MARK: - DST spring-forward

def test_night_part_across_the_spring_dst_change():
    """28 Mar 2027, 02:00 CET → 03:00 CEST. Night on the 27th (22 → 30)
    ends 30 h after local midnight, i.e. at 07:00 CEST (the app adds
    absolute seconds, so the lost hour moves the end by one)."""
    r = parse_rule({
        'id': '00000000-0000-0000-0000-000000000001', 'kind': 'user_rule',
        'cellKey': CELL,
        'params': {'all': [{'metric': 'temp', 'cmp': 'lt', 'value': 0}],
                   'window': {'days': 'today', 'part': 'night'}}})
    now = datetime.datetime(2027, 3, 27, 12, tzinfo=berlin.TZ)
    [occ] = ev.occurrences(r.window, now)
    assert occ.spans[0].start.astimezone(berlin.TZ).hour == 22
    assert occ.spans[0].end.astimezone(berlin.TZ).hour == 7


# MARK: - golden payloads for the app

def test_golden_fixtures_are_current():
    """brightsky/push/fixtures/*.json are what the builders emit; the app
    pins them in WeatherCore tests. Regenerate with
    `python -m tests.test_push_review` after an intended change."""
    for name, payload in golden().items():
        path = FIXTURES / f'{name}.json'
        assert json.loads(path.read_text()) == payload, name


from pathlib import Path   # noqa: E402

import brightsky.push   # noqa: E402

FIXTURES = Path(brightsky.push.__file__).parent / 'fixtures'


def golden():
    from brightsky.push import dispatcher
    t = datetime.datetime(2026, 9, 23, 12, tzinfo=datetime.timezone.utc)
    frost = parse_rule({
        'id': '6f96a1c2-0000-4000-8000-000000000001', 'kind': 'user_rule',
        'cellKey': '53.55,10.01',
        'params': {'all': [{'metric': 'temp', 'cmp': 'lt', 'value': 0}],
                   'window': {'days': 'today', 'part': 'night'}}})
    evidence = ev.Evidence(values={'temp': -2.0}, day='morgen',
                           time='gegen 5 Uhr')
    fire = firing.Fire(frost.id, '2026-09-23', f'rule:{frost.id}:2026-09-23',
                       evidence, 'new')
    row = {'live': None, 'position': 0}
    alert, _, _ = dispatcher.build_payload(fire.event_key,
                                           [(frost, row, fire)])
    digest = dispatcher.build_digest({fire.event_key: [(frost, row, fire)]},
                                     t.date())
    m5 = datetime.timedelta(minutes=5)
    rain = live.analyze_rain([live.Point(t + i * m5,
                                         0.2 if 4 <= i <= 12 else 0)
                              for i in range(24)], t)
    start = payloads.live_start(
        live.rain_content(rain, t, '6f96a1c2-0000-4000-8000-000000000002'),
        now=t, stale=t + 30 * datetime.timedelta(minutes=1),
        alert_title='Regen zieht auf', alert_body=live.rain_headline(rain, t),
        sound=False)
    return {'alert': alert, 'digest': digest, 'live_start': start}


if __name__ == '__main__':
    FIXTURES.mkdir(exist_ok=True)
    for name, payload in golden().items():
        (FIXTURES / f'{name}.json').write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + '\n')
        print('wrote', FIXTURES / f'{name}.json')
