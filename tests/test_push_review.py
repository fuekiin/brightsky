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
    radar, rain_rule, register, register_live, report_token, run, tick,
    warning_rule,
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

    async def fail(self, cells, now):
        raise RuntimeError('radar down')
    monkeypatch.setattr(sources.NowcastSource, 'fetch_all', fail)
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
    w = ev.Warning('2.49.0.0.276.0.DWD.PVW.example', 3, 'gewitter',
                   'STARKES GEWITTER', 'h', t - 30 * datetime.timedelta(
                       minutes=1), t + 150 * datetime.timedelta(minutes=1))
    update = payloads.live_update(
        live.warning_content(w, 'active', t,
                             '6f96a1c2-0000-4000-8000-000000000003',
                             escalated_from=2),
        now=t, stale=w.end, alert_title='Unwetterwarnung',
        alert_body=live.warning_headline(w, 'active', t))
    return {'alert': alert, 'digest': digest, 'live_start': start,
            'live_update_warning': update}


if __name__ == '__main__':
    FIXTURES.mkdir(exist_ok=True)
    for name, payload in golden().items():
        (FIXTURES / f'{name}.json').write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + '\n')
        print('wrote', FIXTURES / f'{name}.json')


# MARK: - Round 2 (verification of 105fe67)

def rain_rows(push_db):
    return push_db.fetch('SELECT ended_at IS NULL, state, rule_id '
                         'FROM push.live_activities')


def test_r1_orphaned_rain_activity_ends(push_db, monkeypatch):
    """R1: the live rule is deleted while its activity runs."""
    stub = StubAPNs()
    run(push_db, register_live([rain_rule()]), stub, monkeypatch)
    radar(monkeypatch, [0.2] * 24)
    run(push_db, ntick(NOW), stub, monkeypatch)
    report_token(push_db)
    run(push_db, register_live([]), stub, monkeypatch)   # rule removed
    run(push_db, ntick(NOW + 5 * M), stub, monkeypatch)
    assert bodies(stub)[-1][2]['aps']['event'] == 'end'
    assert rain_rows(push_db)[0][0] is False


def test_r1_lifetime_is_checked_before_missing_data(push_db, monkeypatch):
    stub = StubAPNs()
    run(push_db, register_live([rain_rule()]), stub, monkeypatch)
    radar(monkeypatch, [0.2] * 24)
    run(push_db, ntick(NOW), stub, monkeypatch)
    report_token(push_db)

    async def fail(self, cells, now):
        raise RuntimeError('radar down')
    monkeypatch.setattr(sources.NowcastSource, 'fetch_all', fail)
    with pytest.raises(RuntimeError):
        run(push_db, ntick(NOW + 4 * H + M), stub, monkeypatch)
    # every lookup failed, so the tick raised before any activity work;
    # with one cell still answering, the 4 h limit ends the stale one:
    radar(monkeypatch, [0.2] * 24)
    run(push_db, ntick(NOW + 4 * H + 2 * M), stub, monkeypatch)
    assert bodies(stub)[-1][2]['aps']['event'] == 'end'


def test_r2_an_update_cannot_undo_a_dismissal(push_db, monkeypatch):
    stub = StubAPNs()
    run(push_db, register_live([rain_rule()]), stub, monkeypatch)
    radar(monkeypatch, [0.2] * 24)
    run(push_db, ntick(NOW), stub, monkeypatch)
    report_token(push_db)

    async def race(worker, pool):
        from brightsky.push import livectl
        async with pool.acquire() as conn:
            row = await livectl.load(conn, DEVICE)
            await store.end_activity(conn, DEVICE, 'X', NOW + M,
                                     datetime.timedelta(minutes=60))
            # the worker had loaded the row before the dismissal landed
            rain = live.analyze_rain([live.Point(NOW + i * 5 * M, 1.0)
                                      for i in range(24)], NOW + M)
            c = live.Candidate('rain', RAIN_RULE, NOW, rain=rain,
                               cell_key=CELL)
            await livectl.update(conn, worker.client, device_of(conn),
                                 row, c, NOW + M)
    def device_of(_):
        return {'id': DEVICE, 'environment': 'sandbox',
                'push_to_start_token': None, 'live_activities_enabled': True}
    run(push_db, race, stub, monkeypatch)
    ended, state, _ = rain_rows(push_db)[0]
    assert ended is False and state['dismissed'] is True


def test_r3_a_start_that_timed_out_is_not_repeated(push_db, monkeypatch):
    stub = StubAPNs([(0, None)])
    run(push_db, register_live([rain_rule()]), stub, monkeypatch)
    radar(monkeypatch, [0.2] * 24)
    run(push_db, ntick(NOW), stub, monkeypatch)
    assert len(stub.requests) == 1
    run(push_db, ntick(NOW + M), stub, monkeypatch)
    assert len(stub.requests) == 1       # still waiting for the token
    run(push_db, ntick(NOW + 3 * M), stub, monkeypatch)
    # given up after 2 min: a new start
    assert [b[2]['aps']['event'] for b in bodies(stub)] == ['start', 'start']


def test_r4_dead_activity_token_counts_as_dismissal(push_db, monkeypatch):
    stub = StubAPNs([(200, None), (410, 'Unregistered')])
    run(push_db, register_live([warning_rule(WARN_RULE,
                                             live={'night': False})]),
        stub, monkeypatch)
    add_alert(push_db, 'A', 'moderate')
    run(push_db, tick(NOW), stub, monkeypatch)
    report_token(push_db)
    add_alert(push_db, 'B', 'severe')          # escalation → update → 410
    push_db.fetch("DELETE FROM alerts WHERE alert_id = 'A' RETURNING id")
    run(push_db, tick(NOW + M), stub, monkeypatch)
    ended, state, _ = rain_rows(push_db)[0]
    assert ended is False and state['dismissed'] is True


def test_r5_rain_elsewhere_ends_and_starts_its_own(push_db, monkeypatch):
    """Another rule's rain wins: the running activity is ended and the
    winner gets its own — never another place's content on this one."""
    stub = StubAPNs()
    berlin_rule = dict(rain_rule(), id='00000000-0000-0000-0000-0000000000b2',
                       cellKey='52.52,13.41')
    run(push_db, register_live([rain_rule(), berlin_rule]), stub,
        monkeypatch)
    by_lat = {53.55: [0.2] * 24, 52.52: [0] * 24}

    async def fetch_all(self, cells, now):
        return {k: [live.Point(NOW + i * 5 * M, v)
                    for i, v in enumerate(by_lat[lat])]
                for k, (lat, lon) in cells.items()}
    monkeypatch.setattr(sources.NowcastSource, 'fetch_all', fetch_all)
    run(push_db, ntick(NOW), stub, monkeypatch)
    report_token(push_db)
    # Hamburg's rain will start again in 50 min; Berlin's rain is now.
    by_lat[53.55] = [0] * 12 + [0.2] * 12
    by_lat[52.52] = [0.2] * 24
    run(push_db, ntick(NOW + 5 * M), stub, monkeypatch)
    lives = [b[2]['aps'] for b in bodies(stub)
             if b[0] == 'liveactivity']
    assert [a['event'] for a in lives] == ['start', 'end', 'start']
    assert lives[-1]['content-state']['ruleId'] == berlin_rule['id']


def test_r5_the_same_rule_moving_follows_the_user(push_db, monkeypatch):
    """§17.5: the cell is content — a „Mein Standort" rule that moves keeps
    its activity."""
    stub = StubAPNs()
    run(push_db, register_live([rain_rule()]), stub, monkeypatch)
    radar(monkeypatch, [0.2] * 24)
    run(push_db, ntick(NOW), stub, monkeypatch)
    report_token(push_db)
    moved = dict(rain_rule(), cellKey='52.52,13.41')
    run(push_db, register_live([moved]), stub, monkeypatch)
    run(push_db, ntick(NOW + 5 * M), stub, monkeypatch)
    assert [b[2]['aps']['event'] for b in bodies(stub)] == ['start']


def test_6a_one_device_failing_delivery_does_not_stop_others(
        push_db, monkeypatch):
    from brightsky.push import dispatcher
    stub = StubAPNs()
    run(push_db, register([warning_rule(WARN_RULE)]), stub, monkeypatch)
    add_alert(push_db, 'A', 'moderate')
    original = dispatcher.apply
    calls = []

    async def flaky(conn, client, device, items, now, digest_date=None):
        calls.append(device['id'])
        raise RuntimeError('boom')
    monkeypatch.setattr(dispatcher, 'apply', flaky)
    run(push_db, tick(NOW), stub, monkeypatch)       # does not raise
    assert calls
    [(ok,)] = push_db.fetch("SELECT last_error IS NULL FROM "
                            "push.source_status WHERE source = 'warnings'")
    assert ok
    monkeypatch.setattr(dispatcher, 'apply', original)


def test_6b_geoserver_timeout_is_no_data_for_that_cell(push_db,
                                                        monkeypatch):
    import requests
    from brightsky import query
    stub = StubAPNs()
    run(push_db, register([warning_rule(WARN_RULE)]), stub, monkeypatch)
    with push_db.cursor() as cur:
        cur.execute('UPDATE push.cells SET warn_cell_id = NULL, '
                    'resolved_at = NULL')
    push_db.commit()

    def timeout(lat, lon):
        raise requests.Timeout('GeoServer down')
    monkeypatch.setattr(query._warn_cells, 'find', timeout)
    add_alert(push_db, 'A', 'moderate')
    run(push_db, tick(NOW), stub, monkeypatch)       # does not raise
    [(resolved,)] = push_db.fetch('SELECT resolved_at FROM push.cells')
    assert resolved is not None


def test_10_ticks_for_one_device_serialize():
    from brightsky.push import livectl
    order = []

    async def hold(name):
        async with livectl.lock('d'):
            order.append(f'{name} in')
            await asyncio.sleep(0.01)
            order.append(f'{name} out')

    async def both():
        await asyncio.gather(hold('rain'), hold('warning'))
    asyncio.run(both())
    assert order in (['rain in', 'rain out', 'warning in', 'warning out'],
                     ['warning in', 'warning out', 'rain in', 'rain out'])


def test_10_cleanup_prunes_idle_locks(push_db, monkeypatch):
    from brightsky.push import livectl
    livectl.lock('someone')

    async def clean(worker, pool):
        await worker.cleanup_tick(NOW)
    run(push_db, clean, StubAPNs(), monkeypatch)
    assert 'someone' not in livectl.LOCKS


def test_13_delete_before_the_token_ends_the_unnamed_activity(
        push_db, monkeypatch):
    stub = StubAPNs()
    run(push_db, register_live([rain_rule()]), stub, monkeypatch)
    radar(monkeypatch, [0.2] * 24)
    run(push_db, ntick(NOW), stub, monkeypatch)

    async def dismiss(worker, pool):
        async with pool.acquire() as conn:
            await store.end_activity(conn, DEVICE, 'EARLY', NOW + M,
                                     datetime.timedelta(minutes=60))
    run(push_db, dismiss, stub, monkeypatch)
    [(ended, activity_id)] = push_db.fetch(
        'SELECT ended_at IS NOT NULL, activity_id FROM push.live_activities')
    assert (ended, activity_id) == (True, 'EARLY')


def test_18_duration_counts_from_the_first_real_rain():
    # drizzle, then real rain for 3 steps: 15 min, not 25
    mm = [0] * 2 + [0.015, 0.015, 0.2, 0.2, 0.2] + [0] * 17
    r = live.analyze_rain([live.Point(NOW + i * 5 * M, v)
                           for i, v in enumerate(mm)], NOW)
    assert r.duration_minutes == 15


def test_19_migrate_waits_for_the_advisory_lock(db):
    import threading
    import psycopg2
    from brightsky import db as dbmod
    from brightsky.settings import settings
    holder = psycopg2.connect(settings.DATABASE_URL)
    with holder.cursor() as cur:
        cur.execute('SELECT pg_advisory_lock(%s)', (dbmod.MIGRATION_LOCK,))
    t = threading.Thread(target=dbmod.migrate)
    t.start()
    t.join(0.5)
    assert t.is_alive(), 'migrate() must wait for the lock'
    with holder.cursor() as cur:
        cur.execute('SELECT pg_advisory_unlock(%s)', (dbmod.MIGRATION_LOCK,))
    t.join(10)
    assert not t.is_alive()
    holder.close()


def test_24_health_shows_no_device_count(db):
    from fastapi.testclient import TestClient
    from brightsky.push import api
    with TestClient(api.app) as client:
        body = client.get('/health').json()
    assert 'devices' not in body
    assert body['warnings'] == []


def test_15_malformed_content_length_is_400(db):
    from fastapi.testclient import TestClient
    from brightsky.push import api
    with TestClient(api.app) as client:
        resp = client.post('/v1/devices', content=b'{}',
                           headers={'Content-Length': 'x',
                                    'Content-Type': 'application/json'})
    assert resp.status_code == 400


# MARK: - Rules redesign (2026-09-24)

def test_rain_min_is_parsed_and_validated():
    base = rain_rule()
    for minimum, mm_h in (('light', 0.3), ('moderate', 2.5),
                          ('heavy', 10.0)):
        r = parse_rule(dict(base, params={
            'all': [{'rain': {'min': minimum}}],
            'window': {'nextHours': 1}}))
        assert r.rain_threshold == pytest.approx(mm_h / 12)
    assert parse_rule(base).rain_min == 'light'     # absent means light
    from brightsky.push.rules import Rejected
    with pytest.raises(Rejected) as e:
        parse_rule(dict(base, params={
            'all': [{'rain': {'min': 'drizzle'}}],
            'window': {'nextHours': 1}}))
    assert e.value.reason == 'unknown_intensity'


def test_rain_min_decides_the_match_and_the_activity(push_db, monkeypatch):
    """Light rain (1.2 mm/h) is rain for „leicht", not for „mäßig"."""
    stub = StubAPNs()
    moderate = dict(rain_rule(), params={
        'all': [{'rain': {'min': 'moderate'}}], 'window': {'nextHours': 1}})
    run(push_db, register_live([moderate]), stub, monkeypatch)
    radar(monkeypatch, [0.1] * 24)          # 1.2 mm/h
    run(push_db, ntick(NOW), stub, monkeypatch)
    assert stub.requests == []
    radar(monkeypatch, [0.3] * 24)          # 3.6 mm/h
    run(push_db, ntick(NOW + 5 * M), stub, monkeypatch)
    assert bodies(stub)[-1][2]['aps']['event'] == 'start'


def test_forecast_rules_decide_at_any_hour(push_db, monkeypatch):
    """No quiet hours (decision 2026-09-24): a rolling forecast rule
    reports at night. The notice gates still decide when an occasion may
    first report."""
    gusts = {'id': WARN_RULE, 'kind': 'user_rule', 'cellKey': CELL,
             'params': {'all': [{'metric': 'gust', 'cmp': 'gt',
                                 'value': 70}],
                        'window': {'nextHours': 12}}}
    stub = StubAPNs()
    run(push_db, register([gusts]), stub, monkeypatch)

    def at(h, mi=0):
        return datetime.datetime(2026, 9, 24, h, mi, tzinfo=berlin.TZ)
    hours = [ev.Hour(at(h), wind_gust_speed=85) for h in range(3, 12)]

    async def fake_fetch(self, cell_key, lat, lon, now):
        self.hours[cell_key] = hours
        self.fetched_at[cell_key] = now
        return hours
    monkeypatch.setattr(sources.ForecastSource, 'fetch', fake_fetch)

    async def night_tick(worker, pool):
        return await worker.forecast_tick(at(2, 30))
    run(push_db, night_tick, stub, monkeypatch)
    assert len(stub.requests) == 1


def test_warnings_decide_at_night(push_db, monkeypatch):
    stub = StubAPNs()
    run(push_db, register([warning_rule(WARN_RULE)]), stub, monkeypatch)
    night = datetime.datetime(2026, 9, 24, 2, tzinfo=berlin.TZ)
    add_alert(push_db, 'A', 'moderate', onset=night + H)
    run(push_db, tick(night), stub, monkeypatch)
    assert len(stub.requests) == 1


def test_switch_shapes_from_the_app_register():
    """RuleWireMapper.registrations(for: PlaceAlerts)."""
    warnings = parse_rule({
        'id': '00000000-0000-0000-0000-0000000000d1', 'kind': 'dwd_warning',
        'cellKey': CELL, 'live': {'night': True},
        'params': {'all': [{'warning': {'minLevel': 3, 'families': []}}],
                   'window': {'nextHours': 48}}})
    assert warnings.live == {'night': True}
    rain = parse_rule({
        'id': '00000000-0000-0000-0000-0000000000d2', 'kind': 'rain_nowcast',
        'cellKey': CELL, 'live': {'night': False},
        'params': {'all': [{'rain': {'min': 'heavy'}}],
                   'window': {'nextHours': 1}}})
    assert rain.rain_min == 'heavy'


# MARK: - Load (2026-09-24): one national radar request, paced forecasts

def national_radar(values_at, frames=3):
    """A /radar?format=compressed response: `values_at` {(row, col): v} in
    1/100 mm, the same in every frame."""
    import base64
    import zlib
    import numpy as np
    grid = np.zeros((1200, 1100), dtype='<i2')
    for (row, col), v in values_at.items():
        grid[row, col] = v
    blob = base64.b64encode(zlib.compress(grid.tobytes())).decode()
    t0 = datetime.datetime(2026, 9, 23, 11, 55, tzinfo=datetime.timezone.utc)
    return {'radar': [{'timestamp': (t0 + i * 5 * M).isoformat(),
                       'source': 'x', 'precipitation_5': blob}
                      for i in range(frames)]}


def test_national_nowcast_reads_the_pixel_the_server_would_crop():
    import httpx
    import numpy as np
    from brightsky import query
    lat, lon = 53.55, 10.01
    # the server's own crop for lat/lon with distance=1 (query.radar)
    x, y = query._transformer.to_xy(lat, lon)
    row, col = int(round(y)), int(round(x))
    grid = np.zeros((1200, 1100), dtype='<i2')
    grid[row, col] = 25
    import zlib
    crop = query._load_radar(zlib.compress(grid.tobytes()),
                             (row, col, row, col))
    assert crop.tolist() == [[25]]
    requests = []

    def handler(request):
        requests.append(request)
        return httpx.Response(200, json=national_radar({(row, col): 25}))

    async def main():
        async with httpx.AsyncClient(
                transport=httpx.MockTransport(handler)) as http:
            src = sources.NowcastSource(http)
            return await src.fetch_all(
                {'hh': (lat, lon), 'b': (52.52, 13.41), 'm': (48.14, 11.58)},
                NOW)
    points = asyncio.run(main())
    assert len(requests) == 1                    # one request, three cells
    assert 'lat' not in requests[0].url.params   # national, not per cell
    assert requests[0].url.params['format'] == 'compressed'
    assert [p.mm for p in points['hh']] == [0.25] * 3
    assert [p.mm for p in points['b']] == [0.0] * 3


def test_nowcast_tick_makes_one_request_for_many_places(push_db,
                                                         monkeypatch):
    import httpx
    from brightsky.push.worker import Worker
    rules = [dict(rain_rule(live_=False),
                  id=f'00000000-0000-0000-0000-0000000001{i:02d}',
                  cellKey=key)
             for i, key in enumerate(['53.55,10.01', '52.52,13.41',
                                      '48.14,11.58', '50.94,6.96'])]
    run(push_db, register_live(rules), StubAPNs(), monkeypatch)
    calls = []

    def handler(request):
        calls.append(request.url.path)
        return httpx.Response(200, json=national_radar({}))

    async def main():
        async with store.pool(max_size=2) as pool:
            async with httpx.AsyncClient(
                    transport=httpx.MockTransport(handler)) as http:
                worker = Worker(pool, http, make_client(StubAPNs()))
                return await worker.nowcast_tick(NOW)
    assert asyncio.run(main()) == 4
    assert calls == ['/radar']


def test_forecast_lookups_are_sequential_and_spread_out(push_db,
                                                        monkeypatch):
    from brightsky.push import worker as workermod
    cells = ['53.55,10.01', '52.52,13.41', '48.14,11.58']
    rules = [{'id': f'00000000-0000-0000-0000-0000000002{i:02d}',
              'kind': 'user_rule', 'cellKey': key,
              'params': {'all': [{'metric': 'temp', 'cmp': 'lt',
                                  'value': -30}],
                         'window': {'nextHours': 6}}}
             for i, key in enumerate(cells)]
    run(push_db, register_live(rules), StubAPNs(), monkeypatch)
    active, peak, sleeps = [0], [0], []

    async def fetch(self, cell_key, lat, lon, now):
        active[0] += 1
        peak[0] = max(peak[0], active[0])
        await asyncio.sleep(0)
        active[0] -= 1
        self.hours[cell_key] = []
        self.fetched_at[cell_key] = now
        return []
    monkeypatch.setattr(sources.ForecastSource, 'fetch', fetch)

    async def record(seconds):
        sleeps.append(seconds)

    async def main():
        import httpx
        from brightsky.push.worker import Worker
        async with store.pool(max_size=2) as pool:
            async with httpx.AsyncClient() as http:
                w = Worker(pool, http, make_client(StubAPNs()),
                           sleep=record)
                await w.forecast_tick(NOW)
    asyncio.run(main())
    assert peak[0] == 1                       # never two at once
    assert len(sleeps) == 3
    # few cells: capped spacing; many cells: spread over 80 % of 15 min
    assert sleeps[0] == workermod.FORECAST_MAX_SPACING
    assert min(workermod.FORECAST_MAX_SPACING,
               workermod.FORECAST_PACE * 900 / 5000) == pytest.approx(0.144)


# MARK: - stale-date at the countdown's target (phone test 2026-09-24)

def unix(dt):
    return int(dt.timestamp())


def test_rain_stale_date_is_the_change_when_it_comes_first(push_db,
                                                          monkeypatch):
    """„Regen vor 1 Minute": iOS redraws only at the stale-date."""
    stub = StubAPNs()
    run(push_db, register_live([rain_rule()]), stub, monkeypatch)
    radar(monkeypatch, [0.2 if 4 <= i <= 12 else 0 for i in range(24)])
    run(push_db, ntick(NOW), stub, monkeypatch)
    aps = bodies(stub)[-1][2]['aps']
    assert aps['event'] == 'start'
    assert aps['stale-date'] == unix(NOW + 20 * M)     # „Regen in 20 Min."


def test_rain_stale_date_stays_30_min_when_the_change_is_later(
        push_db, monkeypatch):
    stub = StubAPNs()
    run(push_db, register_live([rain_rule()]), stub, monkeypatch)
    radar(monkeypatch, [0.2] * 12 + [0] * 12)   # raining, dry in 60 min
    run(push_db, ntick(NOW), stub, monkeypatch)
    aps = bodies(stub)[-1][2]['aps']
    assert aps['stale-date'] == unix(NOW + 30 * M)


def test_warning_stale_date_is_onset_then_expiry(push_db, monkeypatch):
    stub = StubAPNs()
    run(push_db, register_live([warning_rule(WARN_RULE,
                                             live={'night': False})]),
        stub, monkeypatch)
    add_alert(push_db, 'A', 'moderate', onset=NOW + H, hours=3)
    run(push_db, tick(NOW), stub, monkeypatch)
    aps = bodies(stub)[-1][2]['aps']
    assert aps['event'] == 'start'
    assert aps['stale-date'] == unix(NOW + H)          # „Beginn in …"
    report_token(push_db)
    run(push_db, tick(NOW + H + M), stub, monkeypatch) # onset passed
    aps = bodies(stub)[-1][2]['aps']
    assert aps['event'] == 'update'
    assert aps['content-state']['phase']['warning']['_0']['stage'] \
        == 'active'
    assert aps['stale-date'] == unix(NOW + 4 * H)      # „Ende in …"


# MARK: - Frame-aligned nowcast (2026-09-24)

def test_nowcast_runs_on_each_new_radar_frame(push_db, monkeypatch):
    """Checked every minute; evaluated when a new frame is in, and at
    least every 5 minutes without one."""
    import httpx
    from brightsky.push.worker import Worker
    run(push_db, register_live([rain_rule(live_=False)]), StubAPNs(),
        monkeypatch)
    fetches = []

    async def fetch_all(self, cells, now):
        fetches.append(now)
        return {k: [] for k in cells}
    monkeypatch.setattr(sources.NowcastSource, 'fetch_all', fetch_all)

    def frame(t):
        push_db.insert('radar', [{'timestamp': t, 'source': 'test',
                                  'precipitation_5': b'x'}])

    async def main():
        async with store.pool(max_size=2) as pool:
            async with httpx.AsyncClient() as http:
                w = Worker(pool, http, make_client(StubAPNs()))
                ran = []
                frame(NOW + 2 * H)
                ran.append(await w.nowcast_tick(NOW))              # new
                ran.append(await w.nowcast_tick(NOW + M))          # same
                frame(NOW + 2 * H + 5 * M)
                ran.append(await w.nowcast_tick(NOW + 2 * M))      # new
                ran.append(await w.nowcast_tick(NOW + 3 * M))      # same
                ran.append(await w.nowcast_tick(NOW + 7 * M))      # floor
                return ran
    try:
        ran = asyncio.run(main())
    finally:
        push_db.fetch('DELETE FROM radar RETURNING timestamp')
    assert [r is not None for r in ran] == [True, False, True, False, True]
    assert len(fetches) == 3


# MARK: - Short events live, level 4 always (decision 2026-09-24)

def test_live_switch_covers_only_short_events(push_db, monkeypatch):
    """„Kurze Unwetter live" promises Gewitter, Starkregen und Sturm."""
    stub = StubAPNs()
    run(push_db, register_live([warning_rule(WARN_RULE,
                                             live={'night': True})]),
        stub, monkeypatch)
    add_alert(push_db, 'F', 'severe', event='FROST')
    run(push_db, tick(NOW), stub, monkeypatch)
    [(push_type, _, payload)] = bodies(stub)
    assert push_type == 'alert'          # a notification, not an activity
    add_alert(push_db, 'G', 'severe', event='GEWITTER mit HAGEL')
    run(push_db, tick(NOW + M), stub, monkeypatch)
    assert bodies(stub)[-1][0] == 'liveactivity'


def test_extreme_warning_is_live_even_with_the_switch_off(push_db,
                                                          monkeypatch):
    stub = StubAPNs()
    run(push_db, register_live([warning_rule(WARN_RULE)]), stub,
        monkeypatch)                     # no `live`
    add_alert(push_db, 'H', 'extreme', event='EXTREME HITZE')
    run(push_db, tick(NOW), stub, monkeypatch)
    [(push_type, _, payload)] = bodies(stub)
    assert push_type == 'liveactivity'
    assert payload['aps']['event'] == 'start'
    assert payload['aps']['alert']['title'] == 'EXTREMES UNWETTER'


def test_extreme_warning_without_push_to_start_is_a_notification(
        push_db, monkeypatch):
    stub = StubAPNs()
    run(push_db, register_live([warning_rule(WARN_RULE)],
                               push_to_start=None), stub, monkeypatch)
    add_alert(push_db, 'O', 'extreme', event='ORKANBÖEN')
    run(push_db, tick(NOW), stub, monkeypatch)
    [(push_type, _, payload)] = bodies(stub)
    assert push_type == 'alert'
    assert payload['aps']['alert']['title'] == 'EXTREMES UNWETTER'
    assert payload['aps']['interruption-level'] == 'time-sensitive'


def test_level_titles_below_four_are_unchanged():
    assert ev.level_title(3) == 'Unwetterwarnung'
    assert ev.level_title(2) == 'Markante Warnung'
    assert ev.level_title(4) == 'EXTREMES UNWETTER'


def test_level_four_beats_level_three_and_rain():
    w3 = ev.Warning('a', 3, 'gewitter', 'GEWITTER', 'h', NOW, NOW + H)
    w4 = ev.Warning('b', 4, 'hitze', 'EXTREME HITZE', 'h', NOW + H,
                    NOW + 5 * H)
    rain = live.Candidate('rain', 'r', NOW - H)
    c3 = live.Candidate('warning', 'x', NOW, level=3, warning=w3)
    c4 = live.Candidate('warning', 'y', NOW + H, level=4, warning=w4)
    assert live.winner([rain, c3, c4], NOW) is c4   # later, still first


def test_extreme_warning_alerts_again_when_it_begins(push_db, monkeypatch):
    stub = StubAPNs()
    run(push_db, register_live([warning_rule(WARN_RULE,
                                             live={'night': True})]),
        stub, monkeypatch)
    add_alert(push_db, 'O', 'extreme', event='ORKAN', onset=NOW + H,
              hours=3)
    run(push_db, tick(NOW), stub, monkeypatch)
    report_token(push_db)
    run(push_db, tick(NOW + H + M), stub, monkeypatch)      # begins
    aps = bodies(stub)[-1][2]['aps']
    assert aps['event'] == 'update'
    assert aps['alert']['title'] == 'EXTREMES UNWETTER'
    assert aps['alert']['body'] == 'Orkan bis 18:00'
    assert aps['alert']['sound'] == 'default'


def test_severe_warning_begins_silently(push_db, monkeypatch):
    stub = StubAPNs()
    run(push_db, register_live([warning_rule(WARN_RULE,
                                             live={'night': True})]),
        stub, monkeypatch)
    add_alert(push_db, 'G', 'severe', onset=NOW + H, hours=3)
    run(push_db, tick(NOW), stub, monkeypatch)
    report_token(push_db)
    run(push_db, tick(NOW + H + M), stub, monkeypatch)
    aps = bodies(stub)[-1][2]['aps']
    assert aps['event'] == 'update'
    assert 'alert' not in aps
