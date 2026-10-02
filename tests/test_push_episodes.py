"""Live Activity review 2026-10-01: one rain phase, one activity.

Production on 1 Oct: 16,147 rain activity starts on ~4,000 devices (up to
36 for one device), showers restarting the activity after every 60-min
cooldown, cards switching places, dismissals ignored, starts never counted
by the rate limit. One test per rule that replaces that behaviour."""

import datetime

from brightsky.push import live, sources, store
from brightsky.settings import settings

from .test_push_apns import StubAPNs
from .test_push_worker import (
    DEVICE, NOW, RAIN_RULE, bodies, ntick, push_db, rain_rule,
    register_live, report_token, run,
)


__all__ = ['push_db']   # the fixture, re-exported for pytest

M = datetime.timedelta(minutes=1)
H = datetime.timedelta(hours=1)
RAIN = [0.2] * 24                   # raining now, for two hours
DRY = [0] * 24
HEAVY = [1.0] * 24                  # 12 mm/h: ≥ 10 mm/h for ≥ 10 min


def events(stub):
    return [p['aps'].get('event', 'alert') for _, _, p in bodies(stub)]


def radar_at(monkeypatch, mm, at):
    """The same nowcast for every cell, its frames starting at `at`."""
    async def fetch_all(self, cells, now):
        return {k: [live.Point(at + i * 5 * M, v) for i, v in enumerate(mm)]
                for k in cells}
    monkeypatch.setattr(sources.NowcastSource, 'fetch_all', fetch_all)


def nowcast(push_db, stub, monkeypatch, mm, at):
    radar_at(monkeypatch, mm, at)
    run(push_db, ntick(at), stub, monkeypatch)


def dismiss(push_db, stub, monkeypatch, at):
    """What push-api does when the app reports the dismissal."""
    async def fn(worker, pool):
        async with pool.acquire() as conn:
            await store.end_activity(conn, DEVICE, 'X', at, 60 * M)
    run(push_db, fn, stub, monkeypatch)


# MARK: - Episodes

def test_showers_of_one_rain_phase_start_one_activity(push_db, monkeypatch):
    stub = StubAPNs()
    run(push_db, register_live([rain_rule()]), stub, monkeypatch)
    nowcast(push_db, stub, monkeypatch, RAIN, NOW)
    report_token(push_db)
    nowcast(push_db, stub, monkeypatch, DRY, NOW + 10 * M)
    assert events(stub) == ['start', 'end']
    # The next shower 40 min later: same rain phase — no second card, and
    # no notification instead.
    nowcast(push_db, stub, monkeypatch, RAIN, NOW + 40 * M)
    assert events(stub) == ['start', 'end']
    # Still showery two hours on: the phase stays open while it rains.
    nowcast(push_db, stub, monkeypatch, RAIN, NOW + 2 * H)
    assert events(stub) == ['start', 'end']
    # Dry for more than 90 minutes and 3 h after the start: a new phase.
    nowcast(push_db, stub, monkeypatch, RAIN, NOW + 2 * H + 95 * M)
    assert events(stub) == ['start', 'end', 'start']


def test_a_new_shower_within_three_hours_of_the_start_waits(push_db,
                                                            monkeypatch):
    stub = StubAPNs()
    run(push_db, register_live([rain_rule()]), stub, monkeypatch)
    nowcast(push_db, stub, monkeypatch, RAIN, NOW)
    report_token(push_db)
    nowcast(push_db, stub, monkeypatch, DRY, NOW + 10 * M)
    # dry for 100 min, but only 110 min after the start
    nowcast(push_db, stub, monkeypatch, RAIN, NOW + 110 * M)
    assert events(stub) == ['start', 'end']


def test_a_dismissal_silences_the_rain_phase_heavy_or_not(push_db,
                                                          monkeypatch):
    stub = StubAPNs()
    run(push_db, register_live([rain_rule()]), stub, monkeypatch)
    nowcast(push_db, stub, monkeypatch, RAIN, NOW)
    report_token(push_db)
    dismiss(push_db, stub, monkeypatch, NOW + 5 * M)
    nowcast(push_db, stub, monkeypatch, RAIN, NOW + 10 * M)
    nowcast(push_db, stub, monkeypatch, HEAVY, NOW + 30 * M)
    assert events(stub) == ['start']       # no card, no notification
    nowcast(push_db, stub, monkeypatch, DRY, NOW + 40 * M)
    # 3 h after the dismissal and the phase long over: rain may start again
    nowcast(push_db, stub, monkeypatch, RAIN, NOW + 3 * H + 10 * M)
    assert events(stub) == ['start', 'start']


def test_heavy_rain_starts_once_more_within_a_phase(push_db, monkeypatch):
    stub = StubAPNs()
    run(push_db, register_live([rain_rule()]), stub, monkeypatch)
    nowcast(push_db, stub, monkeypatch, RAIN, NOW)
    report_token(push_db)
    nowcast(push_db, stub, monkeypatch, DRY, NOW + 10 * M)
    nowcast(push_db, stub, monkeypatch, HEAVY, NOW + 30 * M)
    report_token(push_db, 'aa' * 32)
    nowcast(push_db, stub, monkeypatch, DRY, NOW + 40 * M)
    nowcast(push_db, stub, monkeypatch, HEAVY, NOW + 60 * M)
    assert events(stub) == ['start', 'end', 'start', 'end']


def test_a_new_phase_gets_its_own_heavy_rain_exception(push_db,
                                                       monkeypatch):
    stub = StubAPNs()
    run(push_db, register_live([rain_rule()]), stub, monkeypatch)
    nowcast(push_db, stub, monkeypatch, HEAVY, NOW)              # phase 1
    report_token(push_db)
    nowcast(push_db, stub, monkeypatch, DRY, NOW + 10 * M)
    nowcast(push_db, stub, monkeypatch, RAIN, NOW + 3 * H + 10 * M)  # 2
    report_token(push_db, 'aa' * 32)
    nowcast(push_db, stub, monkeypatch, DRY, NOW + 3 * H + 20 * M)
    nowcast(push_db, stub, monkeypatch, HEAVY, NOW + 3 * H + 40 * M)
    assert events(stub) == ['start', 'end', 'start', 'end', 'start']


def test_a_long_rain_keeps_its_card_and_then_does_not_restart(
        push_db, monkeypatch):
    """The card runs while it rains, up to MAX_RAIN_LIFETIME (7.5 h, under
    the ~8 h iOS keeps it); the same rain does not start it again."""
    stub = StubAPNs()
    run(push_db, register_live([rain_rule()]), stub, monkeypatch)
    for minutes in range(0, 9 * 60, 30):
        nowcast(push_db, stub, monkeypatch, RAIN, NOW + minutes * M)
        if minutes == 0:
            report_token(push_db)
    assert [e for e in events(stub) if e != 'update'] == ['start', 'end']


def test_an_activity_without_token_is_not_stacked(push_db, monkeypatch):
    """74 % of activities never reported their token: the server cannot
    end them, so a second start would stack a second card."""
    stub = StubAPNs()
    run(push_db, register_live([rain_rule()]), stub, monkeypatch)
    nowcast(push_db, stub, monkeypatch, RAIN, NOW)
    nowcast(push_db, stub, monkeypatch, DRY, NOW + 10 * M)   # end: no token
    nowcast(push_db, stub, monkeypatch, RAIN, NOW + 40 * M)
    assert events(stub) == ['start']


# MARK: - What may start

def test_a_one_frame_shower_starts_nothing_and_tells_nothing(push_db,
                                                             monkeypatch):
    stub = StubAPNs()
    run(push_db, register_live([rain_rule()]), stub, monkeypatch)
    coming = [0] * 4 + [0.2] * 6 + [0] * 14             # in 20 min, 30 min
    nowcast(push_db, stub, monkeypatch, coming, NOW)
    nowcast(push_db, stub, monkeypatch, DRY, NOW + 5 * M)
    assert stub.requests == []
    [(n,)] = push_db.fetch(
        "SELECT count(*) FROM push.rule_state WHERE occurrence_key = "
        "'liveSeen'")
    assert n == 0
    # the rule stayed armed: real rain later is told
    nowcast(push_db, stub, monkeypatch, coming, NOW + 10 * M)
    nowcast(push_db, stub, monkeypatch, coming, NOW + 15 * M)
    assert events(stub) == ['start']


def test_heavy_rain_starts_on_its_first_frame(push_db, monkeypatch):
    stub = StubAPNs()
    run(push_db, register_live([rain_rule()]), stub, monkeypatch)
    nowcast(push_db, stub, monkeypatch, [0] * 4 + [1.0] * 20, NOW)
    assert events(stub) == ['start']


def test_a_short_light_shower_is_not_told_by_a_live_rule(push_db,
                                                        monkeypatch):
    """Under 15 minutes of light rain is not worth a card — and a live
    rule speaks through its card only, so it is not told at all."""
    stub = StubAPNs()
    run(push_db, register_live([rain_rule()]), stub, monkeypatch)
    nowcast(push_db, stub, monkeypatch, [0] * 4 + [0.2] * 2 + [0] * 18, NOW)
    assert events(stub) == []


def test_without_cards_a_short_shower_is_a_notification(push_db,
                                                        monkeypatch):
    stub = StubAPNs()
    run(push_db, register_live([rain_rule()], push_to_start=None), stub,
        monkeypatch)
    nowcast(push_db, stub, monkeypatch, [0] * 4 + [0.2] * 2 + [0] * 18, NOW)
    assert events(stub) == ['alert']


def test_a_shower_that_grows_is_told_once_by_the_card(push_db, monkeypatch):
    """Replay 1 Oct: a notification for the short shower, then a card when
    it grew — 1,588 times. Now the card alone tells it."""
    stub = StubAPNs()
    run(push_db, register_live([rain_rule()]), stub, monkeypatch)
    nowcast(push_db, stub, monkeypatch, [0] * 4 + [0.2] * 2 + [0] * 18, NOW)
    grown = [0] * 3 + [0.2] * 6 + [0] * 15
    nowcast(push_db, stub, monkeypatch, grown, NOW + 5 * M)
    nowcast(push_db, stub, monkeypatch, grown, NOW + 10 * M)
    assert events(stub) == ['start']


def test_one_step_over_5_mm_is_not_heavy():
    one = live.analyze_rain([live.Point(NOW + i * 5 * M, 0.5 if i == 3
                                        else 0.2) for i in range(24)], NOW)
    assert one.peak_class == 2 and not one.heavy
    two = live.analyze_rain([live.Point(NOW + i * 5 * M, 0.9 if i in (3, 4)
                                        else 0.2) for i in range(24)], NOW)
    assert two.heavy


def test_heavy_rain_lights_a_card_that_started_on_a_spike(push_db,
                                                          monkeypatch):
    """Replay 1 Oct: most cards start in the top intensity class on one
    5-min spike, so real heavy rain later never counted as an escalation
    and was never told."""
    stub = StubAPNs()
    run(push_db, register_live([rain_rule()]), stub, monkeypatch)
    spike = [0.2, 0.5] + [0.2] * 22             # one step over 5 mm/h
    nowcast(push_db, stub, monkeypatch, spike, NOW)
    report_token(push_db)
    nowcast(push_db, stub, monkeypatch, HEAVY, NOW + 15 * M)
    nowcast(push_db, stub, monkeypatch, HEAVY, NOW + 30 * M)
    loud = [p['aps']['event'] for _, _, p in bodies(stub)
            if 'alert' in p['aps']]
    assert loud == ['start', 'update']


def test_heavy_rain_lights_a_running_card_once_per_phase(push_db,
                                                         monkeypatch):
    stub = StubAPNs()
    run(push_db, register_live([rain_rule()]), stub, monkeypatch)
    nowcast(push_db, stub, monkeypatch, RAIN, NOW)
    report_token(push_db)
    for minutes, mm in ((15, HEAVY), (30, RAIN), (45, HEAVY), (60, RAIN),
                        (75, HEAVY)):
        nowcast(push_db, stub, monkeypatch, mm, NOW + minutes * M)
    loud = [p['aps']['event'] for _, _, p in bodies(stub)
            if 'alert' in p['aps']]
    assert loud == ['start', 'update']        # one heavy alert, not three
    assert events(stub).count('update') >= 3   # the rest update quietly


def test_ties_between_places_do_not_depend_on_row_order():
    rain = live.analyze_rain([live.Point(NOW + i * 5 * M, 0.2)
                              for i in range(24)], NOW)
    a = live.Candidate('rain', 'r2', NOW, rain=rain, cell_key='52.52,13.41')
    b = live.Candidate('rain', 'r1', NOW, rain=rain, cell_key='53.55,10.01')
    assert live.winner([a, b], NOW) is live.winner([b, a], NOW) is a


# MARK: - Limits and bookkeeping

def test_the_daily_cap_holds_further_starts(push_db, monkeypatch):
    monkeypatch.setattr(settings, 'PUSH_MAX_LIVE_STARTS_PER_DAY', 1)
    stub = StubAPNs()
    berlin = dict(rain_rule(), id='00000000-0000-0000-0000-0000000000b2',
                  cellKey='52.52,13.41')
    run(push_db, register_live([rain_rule(), berlin]), stub, monkeypatch)
    by_lat = {53.55: RAIN, 52.52: DRY}

    async def fetch_all(self, cells, now):
        return {k: [live.Point(now + i * 5 * M, v)
                    for i, v in enumerate(by_lat[lat])]
                for k, (lat, lon) in cells.items()}
    monkeypatch.setattr(sources.NowcastSource, 'fetch_all', fetch_all)
    run(push_db, ntick(NOW), stub, monkeypatch)
    report_token(push_db)
    by_lat[53.55], by_lat[52.52] = DRY, RAIN
    run(push_db, ntick(NOW + 10 * M), stub, monkeypatch)
    # Hamburg's card ended; Berlin would start, but the cap allows no card:
    # Berlin's new rain is a notification instead (second review — a capped
    # device heard nothing of new rain for the rest of the day).
    assert events(stub) == ['start', 'end', 'alert']


def test_live_activity_starts_count_toward_the_hourly_limit(push_db,
                                                            monkeypatch):
    monkeypatch.setattr(settings, 'PUSH_MAX_ALERTS_PER_HOUR', 1)
    stub = StubAPNs()
    berlin = dict(rain_rule(live_=False),
                  id='00000000-0000-0000-0000-0000000000b2',
                  cellKey='52.52,13.41')
    run(push_db, register_live([rain_rule(), berlin]), stub, monkeypatch)
    nowcast(push_db, stub, monkeypatch, RAIN, NOW)
    # The start used up the hour: Berlin's notification is held back.
    assert events(stub) == ['start']
    [(reason,)] = push_db.fetch(
        "SELECT apns_reason FROM push.notifications_sent "
        "WHERE push_type = 'alert'")
    assert reason == 'rate_limited'


def test_the_send_log_says_which_live_event_it_was(push_db, monkeypatch):
    stub = StubAPNs()
    run(push_db, register_live([rain_rule()]), stub, monkeypatch)
    nowcast(push_db, stub, monkeypatch, RAIN, NOW)
    report_token(push_db)
    nowcast(push_db, stub, monkeypatch, DRY, NOW + 10 * M)
    rows = push_db.fetch(
        'SELECT push_type, live_event, alerting FROM push.notifications_sent '
        'ORDER BY id')
    assert [tuple(r) for r in rows] == [('liveactivity', 'start', True),
                                        ('liveactivity', 'end', False)]


def test_episodes_survive_a_warning_taking_over(push_db, monkeypatch):
    """The rain episodes live in the row's state, which a warning's state
    replaces; they are kept."""
    stub = StubAPNs()
    run(push_db, register_live([rain_rule()]), stub, monkeypatch)
    nowcast(push_db, stub, monkeypatch, RAIN, NOW)

    async def save(worker, pool):
        from brightsky.push import livectl
        async with pool.acquire() as conn:
            await livectl._save(conn, DEVICE, rule_id=RAIN_RULE,
                                phase='warning', content={},
                                state={'event': 'dwd:A'}, now=NOW + M)
    run(push_db, save, stub, monkeypatch)
    [(state,)] = push_db.fetch('SELECT state FROM push.live_activities')
    assert '53.55,10.01' in state['episodes']


def test_budget_counts_from_the_send_log(push_db, monkeypatch):
    """Rain starts in 24 h (an unanswered start may have arrived; a refused
    one did not), and pushes that lit the screen in the last hour."""
    from brightsky.push import livectl
    stub = StubAPNs()
    run(push_db, register_live([rain_rule()]), stub, monkeypatch)
    rows = [
        # (minutes ago, push_type, live_event, alerting, status, key)
        (10, 'liveactivity', 'start', True, 200, 'rain'),
        (20, 'liveactivity', 'start', True, 0, 'rain'),
        (30, 'liveactivity', 'start', True, 400, 'rain'),
        (40, 'liveactivity', 'update', False, 200, 'rain'),
        (50, 'alert', None, None, 200, 'rain:x'),
        (90, 'liveactivity', 'start', True, 200, 'rain'),
        (25 * 60, 'liveactivity', 'start', True, 200, 'rain'),
        (5, 'liveactivity', 'start', True, 200, 'dwd:A'),
    ]
    push_db.insert('push.notifications_sent', [{
        'device_id': DEVICE, 'sent_at': NOW - m * M, 'push_type': t,
        'live_event': e, 'alerting': a, 'apns_status': st,
        'occurrence_key': k} for m, t, e, a, st, k in rows])

    async def fn(worker, pool):
        async with pool.acquire() as conn:
            return await livectl.budget(conn, DEVICE, NOW)
    # hour: the 200s that lit the screen within 60 min (10, 50, 5);
    # starts: rain starts in 24 h answered 200 or 0 (10, 20, 90)
    assert run(push_db, fn, stub, monkeypatch) == (3, 3)


def test_the_schema_this_code_needs_is_there(push_db, monkeypatch):
    async def fn(worker, pool):
        async with pool.acquire() as conn:
            return await store.missing_columns(conn)
    assert run(push_db, fn, StubAPNs(), monkeypatch) == []


# MARK: - Review 2026-10-01

def test_held_showers_do_not_keep_the_phase_open_all_day(push_db,
                                                         monkeypatch):
    """A shower every hour: after the card, the next showers hold — but
    they do not extend the phase, so 3 h after the start a card starts
    again (it stayed silent until evening before the fix)."""
    stub = StubAPNs()
    run(push_db, register_live([rain_rule()]), stub, monkeypatch)
    nowcast(push_db, stub, monkeypatch, RAIN, NOW)
    report_token(push_db)
    nowcast(push_db, stub, monkeypatch, DRY, NOW + 10 * M)
    for hour in (1, 2):
        nowcast(push_db, stub, monkeypatch, RAIN, NOW + hour * H)
        nowcast(push_db, stub, monkeypatch, DRY, NOW + hour * H + 10 * M)
    assert events(stub) == ['start', 'end']
    nowcast(push_db, stub, monkeypatch, RAIN, NOW + 3 * H)
    assert events(stub) == ['start', 'end', 'start']


def test_a_late_dismissal_of_a_replaced_card_spares_the_new_one(
        push_db, monkeypatch):
    stub = StubAPNs()
    berlin = dict(rain_rule(), id='00000000-0000-0000-0000-0000000000b2',
                  cellKey='52.52,13.41')
    run(push_db, register_live([rain_rule(), berlin]), stub, monkeypatch)
    by_lat = {53.55: RAIN, 52.52: DRY}

    async def fetch_all(self, cells, now):
        return {k: [live.Point(now + i * 5 * M, v)
                    for i, v in enumerate(by_lat[lat])]
                for k, (lat, lon) in cells.items()}
    monkeypatch.setattr(sources.NowcastSource, 'fetch_all', fetch_all)
    run(push_db, ntick(NOW), stub, monkeypatch)
    report_token(push_db)                     # Hamburg's card is 'X'
    by_lat[53.55], by_lat[52.52] = DRY, RAIN
    run(push_db, ntick(NOW + 10 * M), stub, monkeypatch)
    assert events(stub) == ['start', 'end', 'start']   # Berlin, no token yet
    dismiss(push_db, stub, monkeypatch, NOW + 25 * M)  # the app: 'X' is gone
    [(ended, state)] = push_db.fetch(
        'SELECT ended_at IS NOT NULL, state FROM push.live_activities')
    assert not ended and not state.get('dismissed')
    assert state['endedIds'] == ['X']


def test_heavy_rain_says_so(push_db, monkeypatch):
    stub = StubAPNs()
    run(push_db, register_live([rain_rule()]), stub, monkeypatch)
    nowcast(push_db, stub, monkeypatch, RAIN, NOW)
    report_token(push_db)
    nowcast(push_db, stub, monkeypatch, HEAVY, NOW + 15 * M)
    alerts = [p['aps']['alert'] for _, _, p in bodies(stub)
              if 'alert' in p['aps']]
    assert alerts[0]['title'] == 'Es regnet'          # not „zieht auf"
    assert alerts[1]['title'] == 'Kräftiger Regen'    # not DWD's term
    assert alerts[1]['body'] == 'Trocken in 120 Min. · jetzt kräftig'


def test_heavy_rain_ahead_says_when():
    from brightsky.push import livectl
    rain = live.analyze_rain([live.Point(NOW + i * 5 * M,
                                         0 if i < 1 else 0.2 if i < 5
                                         else 1.0) for i in range(24)], NOW)
    c = live.Candidate('rain', 'r', NOW, rain=rain, cell_key='53.55,10.01')
    assert livectl._alert_text(c, NOW) == (
        'Kräftiger Regen', 'Regen in 5 Min. · kräftig ab 14:25')


def test_heavy_rain_beyond_an_hour_is_not_heavy_yet():
    rain = live.analyze_rain([live.Point(NOW + i * 5 * M, 1.0 if i >= 14
                                         else 0.2) for i in range(24)], NOW)
    assert not rain.heavy
    soon = live.analyze_rain([live.Point(NOW + i * 5 * M, 1.0 if i >= 6
                                         else 0.2) for i in range(24)], NOW)
    assert soon.heavy_at == NOW + 30 * M


def test_a_quiet_short_shower_still_resets_the_rearm_clock(push_db,
                                                           monkeypatch):
    stub = StubAPNs()
    run(push_db, register_live([rain_rule()]), stub, monkeypatch)
    nowcast(push_db, stub, monkeypatch, RAIN, NOW)
    nowcast(push_db, stub, monkeypatch, DRY, NOW + 10 * M)     # clear from
    nowcast(push_db, stub, monkeypatch, [0] * 4 + [0.2] * 2 + [0] * 18,
            NOW + 40 * M)                                       # rain again
    [(state,)] = push_db.fetch(
        "SELECT state FROM push.rule_state WHERE occurrence_key = 'rain'")
    assert state == {'armed': False}                # clearSince reset


def test_a_failed_start_keeps_the_area_record(push_db, monkeypatch):
    stub = StubAPNs([(200, None), (200, None), (400, 'BadDeviceToken')])
    run(push_db, register_live([rain_rule()]), stub, monkeypatch)
    nowcast(push_db, stub, monkeypatch, RAIN, NOW)
    report_token(push_db)
    nowcast(push_db, stub, monkeypatch, DRY, NOW + 10 * M)
    nowcast(push_db, stub, monkeypatch, RAIN, NOW + 3 * H + 10 * M)
    [(state,)] = push_db.fetch('SELECT state FROM push.live_activities')
    [entry] = state['episodes'].values()
    assert entry['startedAt'] == NOW.isoformat()    # not lost, not replaced
    assert state['abandoned'] is True
    [(n,)] = push_db.fetch('SELECT live_unconfirmed FROM push.devices')
    assert n == 0                    # a refused start proves nothing


def test_cards_that_never_report_fall_back_to_notifications(push_db,
                                                            monkeypatch):
    """Two starts in a row without a token: the cards evidently do not
    arrive, so the live rule notifies until the app reports or registers."""
    stub = StubAPNs()
    run(push_db, register_live([rain_rule()]), stub, monkeypatch)
    nowcast(push_db, stub, monkeypatch, RAIN, NOW)
    nowcast(push_db, stub, monkeypatch, DRY, NOW + 10 * M)
    nowcast(push_db, stub, monkeypatch, RAIN, NOW + 3 * H + 10 * M)
    nowcast(push_db, stub, monkeypatch, DRY, NOW + 3 * H + 20 * M)
    nowcast(push_db, stub, monkeypatch, RAIN, NOW + 6 * H + 30 * M)
    assert events(stub) == ['start', 'start', 'alert']
    run(push_db, register_live([rain_rule()]), stub, monkeypatch)
    [(n,)] = push_db.fetch('SELECT live_unconfirmed FROM push.devices')
    assert n == 0                       # the app runs: cards get a chance


# MARK: - Second review 2026-10-01

def two_places(push_db, monkeypatch):
    berlin = dict(rain_rule(), id='00000000-0000-0000-0000-0000000000b2',
                  cellKey='52.52,13.41')
    by_lat = {53.55: RAIN, 52.52: DRY}

    async def fetch_all(self, cells, now):
        return {k: [live.Point(now + i * 5 * M, v)
                    for i, v in enumerate(by_lat[lat])]
                for k, (lat, lon) in cells.items()}
    monkeypatch.setattr(sources.NowcastSource, 'fetch_all', fetch_all)
    return berlin, by_lat


def test_replaced_card_ids_survive_the_new_cards_updates(push_db,
                                                         monkeypatch):
    """endedIds was wiped by the first update of the new card."""
    stub = StubAPNs()
    berlin, by_lat = two_places(push_db, monkeypatch)
    run(push_db, register_live([rain_rule(), berlin]), stub, monkeypatch)
    run(push_db, ntick(NOW), stub, monkeypatch)
    report_token(push_db)                                 # Hamburg = 'X'
    by_lat[53.55], by_lat[52.52] = DRY, RAIN
    run(push_db, ntick(NOW + 10 * M), stub, monkeypatch)  # Berlin starts
    by_lat[52.52] = [0.2] * 6 + [0] * 18                  # Berlin changes
    run(push_db, ntick(NOW + 25 * M), stub, monkeypatch)  # → an update
    dismiss(push_db, stub, monkeypatch, NOW + 26 * M)     # late 'X'
    [(ended, state)] = push_db.fetch(
        'SELECT ended_at IS NOT NULL, state FROM push.live_activities')
    assert not ended and state['endedIds'] == ['X']


def test_a_late_token_for_a_replaced_card_does_not_take_the_new_one(
        push_db, monkeypatch):
    stub = StubAPNs()
    berlin, by_lat = two_places(push_db, monkeypatch)
    run(push_db, register_live([rain_rule(), berlin]), stub, monkeypatch)
    run(push_db, ntick(NOW), stub, monkeypatch)
    report_token(push_db)                                 # Hamburg = 'X'
    by_lat[53.55], by_lat[52.52] = DRY, RAIN
    run(push_db, ntick(NOW + 10 * M), stub, monkeypatch)  # Berlin starts

    async def late(worker, pool):
        async with pool.acquire() as conn:
            await store.report_activity_token(conn, DEVICE, 'X', 'aa' * 32,
                                              NOW + 11 * M)
    run(push_db, late, stub, monkeypatch)
    [(token, activity_id)] = push_db.fetch(
        'SELECT activity_token, activity_id FROM push.live_activities')
    assert (token, activity_id) == (None, None)


def test_an_unnamed_dismissal_ends_the_card_without_silencing_the_rain(
        push_db, monkeypatch):
    """The app also reports cards the system removed, with ids the server
    never learnt: for rain that ends the card, but is no dismissal."""
    from brightsky.push import livectl
    stub = StubAPNs()
    run(push_db, register_live([rain_rule()]), stub, monkeypatch)
    nowcast(push_db, stub, monkeypatch, RAIN, NOW)       # no token yet
    dismiss(push_db, stub, monkeypatch, NOW + 5 * M)     # some id: 'X'
    [(ended, state)] = push_db.fetch(
        'SELECT ended_at IS NOT NULL, state FROM push.live_activities')
    assert ended and state.get('dismissedUnnamed') is True
    assert 'dismissed' not in state
    [row] = push_db.fetch('SELECT phase, state, started_at, last_update_at, '
                          'ended_at FROM push.live_activities')
    row = dict(zip(('phase', 'state', 'started_at', 'last_update_at',
                    'ended_at'), row))
    [entry] = livectl.episodes_of(row, NOW + 10 * M).values()
    assert 'dismissedAt' not in entry


def test_a_device_that_ever_showed_a_card_does_not_fall_back(push_db,
                                                             monkeypatch):
    """The app reports dismissals even where it fails to report tokens: one
    is proof enough that the cards arrive — for good, however many
    token-less starts follow."""
    stub = StubAPNs()
    run(push_db, register_live([rain_rule()]), stub, monkeypatch)
    nowcast(push_db, stub, monkeypatch, RAIN, NOW)
    dismiss(push_db, stub, monkeypatch, NOW + 5 * M)     # proof
    [(confirmed,)] = push_db.fetch('SELECT live_confirmed FROM push.devices')
    assert confirmed is True
    with push_db.cursor() as cur:                        # many since then
        cur.execute('UPDATE push.devices SET live_unconfirmed = 9')
    push_db.commit()
    nowcast(push_db, stub, monkeypatch, RAIN, NOW + 4 * H)
    assert events(stub) == ['start', 'start']            # a card, not a note


def test_a_warning_does_not_take_over_an_old_card(push_db, monkeypatch):
    """iOS ends an activity ~8 h after its start: a warning taking over a
    6.5 h old rain card was cut off within the hour."""
    from .test_push_worker import WARN_RULE, add_alert, tick, warning_rule
    stub = StubAPNs()
    run(push_db, register_live([
        rain_rule(), warning_rule(WARN_RULE, live={'night': False})]),
        stub, monkeypatch)
    nowcast(push_db, stub, monkeypatch, RAIN, NOW)
    report_token(push_db)
    late = NOW + 6 * H + 30 * M
    add_alert(push_db, 'A', 'severe', onset=late + H)
    run(push_db, tick(late), stub, monkeypatch)
    assert events(stub) == ['start', 'end', 'start']
    start = bodies(stub)[-1][2]['aps']
    assert 'warning' in start['content-state']['phase']


def test_a_warning_takes_over_a_young_card(push_db, monkeypatch):
    from .test_push_worker import WARN_RULE, add_alert, tick, warning_rule
    stub = StubAPNs()
    run(push_db, register_live([
        rain_rule(), warning_rule(WARN_RULE, live={'night': False})]),
        stub, monkeypatch)
    nowcast(push_db, stub, monkeypatch, RAIN, NOW)
    report_token(push_db)
    add_alert(push_db, 'A', 'severe', onset=NOW + 2 * H)
    run(push_db, tick(NOW + H), stub, monkeypatch)
    assert events(stub) == ['start', 'update']


def test_a_given_up_start_restores_the_episodes(push_db, monkeypatch):
    stub = StubAPNs([(200, None), (200, None), (0, None)])
    run(push_db, register_live([rain_rule()]), stub, monkeypatch)
    nowcast(push_db, stub, monkeypatch, RAIN, NOW)
    report_token(push_db)
    nowcast(push_db, stub, monkeypatch, DRY, NOW + 10 * M)
    nowcast(push_db, stub, monkeypatch, RAIN, NOW + 3 * H + 10 * M)  # 0
    nowcast(push_db, stub, monkeypatch, DRY, NOW + 3 * H + 13 * M)  # give up
    [(state,)] = push_db.fetch('SELECT state FROM push.live_activities')
    assert state['abandoned'] is True
    [entry] = state['episodes'].values()
    assert entry['startedAt'] == NOW.isoformat()
    [(n,)] = push_db.fetch('SELECT live_unconfirmed FROM push.devices')
    assert n == 0


# MARK: - Third review 2026-10-01

def test_a_capped_new_phase_is_told_once_even_in_showery_weather(
        push_db, monkeypatch):
    """The cap allows no card for a new phase: it is a notification instead
    — also when showers in the nowcast never let the rule re-arm (its
    re-arm waits for 90 min without rain; the fallback caught 1 capped
    phase in 5)."""
    monkeypatch.setattr(settings, 'PUSH_MAX_LIVE_STARTS_PER_DAY', 1)
    stub = StubAPNs()
    run(push_db, register_live([rain_rule()]), stub, monkeypatch)
    nowcast(push_db, stub, monkeypatch, RAIN, NOW)
    report_token(push_db)
    nowcast(push_db, stub, monkeypatch, DRY, NOW + 40 * M)
    far = [0] * 16 + [0.2] * 4 + [0] * 4       # a shower 80–100 min out
    for hour in (1, 2, 3):
        nowcast(push_db, stub, monkeypatch, far, NOW + hour * H)
    [(state,)] = push_db.fetch(
        "SELECT state FROM push.rule_state WHERE occurrence_key = 'rain'")
    assert state == {'armed': False}           # never re-armed
    nowcast(push_db, stub, monkeypatch, RAIN, NOW + 3 * H + 40 * M)
    assert events(stub) == ['start', 'end', 'alert']
    # The same phase: told, not again.
    nowcast(push_db, stub, monkeypatch, RAIN, NOW + 3 * H + 45 * M)
    nowcast(push_db, stub, monkeypatch, RAIN, NOW + 4 * H + 30 * M)
    assert events(stub) == ['start', 'end', 'alert']


def test_a_replaced_rule_and_the_cap_notify_once(push_db, monkeypatch):
    """The card's rule is replaced during rain (another rule at the same
    place): the new rule may start at once — but with the cap reached it is
    a notification, and only one: the old rule's `gone` mark stays on the
    row and dropped every later episode record (54 notifications on one
    device in the replay)."""
    monkeypatch.setattr(settings, 'PUSH_MAX_LIVE_STARTS_PER_DAY', 1)
    stub = StubAPNs()
    run(push_db, register_live([rain_rule()]), stub, monkeypatch)
    nowcast(push_db, stub, monkeypatch, RAIN, NOW)
    report_token(push_db)
    other = dict(rain_rule(), id='00000000-0000-0000-0000-0000000000b9')
    run(push_db, register_live([other]), stub, monkeypatch)
    for minutes in range(5, 65, 5):
        nowcast(push_db, stub, monkeypatch, RAIN, NOW + minutes * M)
    assert events(stub) == ['start', 'end', 'alert']


def test_a_capped_phase_is_not_told_again_when_it_turns_heavy(push_db,
                                                              monkeypatch):
    monkeypatch.setattr(settings, 'PUSH_MAX_LIVE_STARTS_PER_DAY', 1)
    stub = StubAPNs()
    run(push_db, register_live([rain_rule()]), stub, monkeypatch)
    nowcast(push_db, stub, monkeypatch, RAIN, NOW)
    report_token(push_db)
    nowcast(push_db, stub, monkeypatch, DRY, NOW + 40 * M)
    nowcast(push_db, stub, monkeypatch, RAIN, NOW + 3 * H + 40 * M)
    nowcast(push_db, stub, monkeypatch, HEAVY, NOW + 4 * H + 10 * M)
    assert events(stub) == ['start', 'end', 'alert']


def test_notifications_instead_of_cards_are_capped_too(push_db,
                                                       monkeypatch):
    """Past the cap each new phase was a notification — one per area every
    3 h, more than a device below the cap gets. They are capped alike."""
    monkeypatch.setattr(settings, 'PUSH_MAX_LIVE_STARTS_PER_DAY', 1)
    stub = StubAPNs()
    run(push_db, register_live([rain_rule()]), stub, monkeypatch)
    nowcast(push_db, stub, monkeypatch, RAIN, NOW)
    report_token(push_db)
    nowcast(push_db, stub, monkeypatch, DRY, NOW + 40 * M)
    far = [0] * 16 + [0.2] * 4 + [0] * 4       # never 90 min clear
    for hour in (1, 2, 3):
        nowcast(push_db, stub, monkeypatch, far, NOW + hour * H)
    nowcast(push_db, stub, monkeypatch, RAIN, NOW + 3 * H + 40 * M)
    assert events(stub) == ['start', 'end', 'alert']      # the one instead
    for hour in (4, 5, 6):
        nowcast(push_db, stub, monkeypatch, far, NOW + hour * H + 30 * M)
    nowcast(push_db, stub, monkeypatch, RAIN, NOW + 7 * H)
    assert events(stub) == ['start', 'end', 'alert']      # no second one


def test_rain_told_elsewhere_is_not_told_again_when_the_card_ends(
        push_db, monkeypatch):
    """Rain at another place is a notification while the card stays put —
    that tells its phase: when the card ends and the cap allows no card
    there, it is not told a second time."""
    monkeypatch.setattr(settings, 'PUSH_MAX_LIVE_STARTS_PER_DAY', 2)
    stub = StubAPNs()
    berlin, by_lat = two_places(push_db, monkeypatch)
    run(push_db, register_live([rain_rule(), berlin]), stub, monkeypatch)
    run(push_db, ntick(NOW), stub, monkeypatch)            # card 1
    report_token(push_db)
    by_lat[53.55] = DRY
    run(push_db, ntick(NOW + 10 * M), stub, monkeypatch)
    by_lat[53.55] = RAIN
    run(push_db, ntick(NOW + 3 * H + 10 * M), stub, monkeypatch)  # card 2
    report_token(push_db)
    by_lat[52.52] = RAIN
    run(push_db, ntick(NOW + 3 * H + 20 * M), stub, monkeypatch)  # Berlin
    loud = [e for e in events(stub) if e != 'update']
    assert loud == ['start', 'end', 'start', 'alert']
    by_lat[53.55] = DRY                     # the card ends; Berlin: capped
    for minutes in (30, 35, 40):
        run(push_db, ntick(NOW + 3 * H + minutes * M), stub, monkeypatch)
    loud = [e for e in events(stub) if e != 'update']
    assert loud == ['start', 'end', 'start', 'alert', 'end']


# MARK: - Final review 2026-10-01

def test_a_held_shower_does_not_count_as_told(push_db, monkeypatch):
    """A re-armed rule's fire dropped because the area was held still sets
    its fired_at — nothing was told, so a capped new phase later is."""
    monkeypatch.setattr(settings, 'PUSH_MAX_LIVE_STARTS_PER_DAY', 1)
    stub = StubAPNs()
    run(push_db, register_live([rain_rule()]), stub, monkeypatch)
    nowcast(push_db, stub, monkeypatch, RAIN, NOW)
    report_token(push_db)
    nowcast(push_db, stub, monkeypatch, DRY, NOW + 10 * M)
    nowcast(push_db, stub, monkeypatch, DRY, NOW + 105 * M)    # re-armed
    nowcast(push_db, stub, monkeypatch, RAIN, NOW + 115 * M)   # held
    nowcast(push_db, stub, monkeypatch, DRY, NOW + 125 * M)
    assert events(stub) == ['start', 'end']
    nowcast(push_db, stub, monkeypatch, RAIN, NOW + 3 * H + 5 * M)
    assert events(stub) == ['start', 'end', 'alert']


def test_the_hourly_limit_is_checked_before_the_cap(monkeypatch):
    """Past both, a 'cap' recorded the episode as notified and the
    dispatcher then dropped its notification: never told."""
    import asyncio
    from brightsky.push import livectl

    async def budget(conn, device_id, now):
        return settings.PUSH_MAX_ALERTS_PER_HOUR, 99
    monkeypatch.setattr(livectl, 'budget', budget)

    async def told_recently(conn, device_id, cell_key, now):
        return False
    monkeypatch.setattr(livectl, 'told_recently', told_recently)
    rain = live.analyze_rain([live.Point(NOW + i * 5 * M, 0.2)
                              for i in range(24)], NOW)
    c = live.Candidate('rain', 'r', NOW, rain=rain, cell_key='53.55,10.01')
    verdict = asyncio.run(livectl.rain_gate(None, {'id': DEVICE}, {}, c, NOW))
    assert verdict == 'limit'


# MARK: - Morning after the deploy, 2026-10-02

def test_a_dismissal_right_after_a_start_spares_the_new_card(push_db,
                                                             monkeypatch):
    """The app reports the card a new one replaced within seconds of the
    start; without its token the server never learnt that card's id. It
    ended the new card instead (10 of 2,237 starts)."""
    stub = StubAPNs()
    run(push_db, register_live([rain_rule()]), stub, monkeypatch)
    nowcast(push_db, stub, monkeypatch, RAIN, NOW)          # no token

    def report(activity_id, at):
        async def fn(worker, pool):
            async with pool.acquire() as conn:
                await store.end_activity(conn, DEVICE, activity_id, at,
                                         60 * M)
        run(push_db, fn, stub, monkeypatch)

    def row():
        [(ended, state)] = push_db.fetch(
            'SELECT ended_at IS NOT NULL, state FROM push.live_activities')
        return ended, state
    report('OLD', NOW + datetime.timedelta(seconds=2))
    ended, state = row()
    assert not ended
    assert state['endedIds'] == ['OLD']
    report('OLD', NOW + 5 * M)             # the same report again: still ok
    assert not row()[0]
    report('NEW', NOW + 5 * M)             # later, an unknown id: this card
    ended, state = row()
    assert ended and state['dismissedUnnamed']


def test_a_dismissed_warning_stays_away_after_a_rain_card(push_db,
                                                          monkeypatch):
    """Only the card's own row remembered the dismissal, and a rain card in
    between replaced it: the warning came back with an alert, three times
    in one night."""
    from .test_push_worker import WARN_RULE, add_alert, tick, warning_rule
    stub = StubAPNs()
    run(push_db, register_live([
        rain_rule(), warning_rule(WARN_RULE, live={'night': False})]),
        stub, monkeypatch)
    add_alert(push_db, 'A', 'moderate', onset=NOW - H, hours=6)
    run(push_db, tick(NOW), stub, monkeypatch)
    report_token(push_db)
    dismiss(push_db, stub, monkeypatch, NOW + M)
    nowcast(push_db, stub, monkeypatch, RAIN, NOW + 5 * M)
    report_token(push_db)
    nowcast(push_db, stub, monkeypatch, HEAVY, NOW + 10 * M)  # an update
    nowcast(push_db, stub, monkeypatch, DRY, NOW + 20 * M)
    told = ['start', 'start', 'update', 'end']
    assert events(stub) == told
    run(push_db, tick(NOW + 21 * M), stub, monkeypatch)
    assert events(stub) == told
    add_alert(push_db, 'B', 'severe', onset=NOW - H, hours=6)
    run(push_db, tick(NOW + 22 * M), stub, monkeypatch)    # escalation
    assert events(stub)[-1] == 'start'


def test_every_place_waiting_for_its_second_frame_is_held(push_db,
                                                          monkeypatch):
    """Only the first waiting place was held: a device with many places
    was told of the rest by notification, then got a card for one."""
    stub = StubAPNs()
    berlin = dict(rain_rule(), id='00000000-0000-0000-0000-0000000000b2',
                  cellKey='52.52,13.41')
    run(push_db, register_live([rain_rule(), berlin]), stub, monkeypatch)
    nowcast(push_db, stub, monkeypatch, [0] * 3 + [0.2] * 21, NOW)
    assert events(stub) == []
