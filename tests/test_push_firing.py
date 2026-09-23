import datetime

from brightsky.push import evaluator as ev, firing
from brightsky.push.rules import parse_rule


UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 9, 23, 12, tzinfo=UTC)
H = datetime.timedelta(hours=1)


def rule(window, kind='user_rule', conditions=None):
    return parse_rule({
        'id': '00000000-0000-0000-0000-000000000001', 'kind': kind,
        'cellKey': '53.55,10.01',
        'params': {'all': conditions or [
            {'metric': 'gust', 'cmp': 'gt', 'value': 70}],
            'window': window},
    })


def match(key='rolling', end=NOW + 6 * H):
    return ev.ValueMatch(key, NOW, end, ev.Evidence(values={'gust': 80.0}))


def as_states(decision, previous=None):
    states = dict(previous or {})
    for key, (state, fired_at, expires_at) in decision.writes.items():
        states[key] = {'state': state, 'fired_at': fired_at,
                       'expires_at': expires_at}
    return states


def test_bounded_occurrence_fires_once():
    r = rule({'days': 'today', 'part': 'allDay'})
    d = firing.decide_values(r, [match('2026-09-23')], {}, NOW)
    assert [f.occurrence_key for f in d.fires] == ['2026-09-23']
    states = as_states(d)
    d = firing.decide_values(r, [match('2026-09-23')], states, NOW + H)
    assert d.fires == []


def test_rolling_needs_a_false_cycle_and_the_gap():
    r = rule({'nextHours': 6})
    d = firing.decide_values(r, [match()], {}, NOW)
    assert len(d.fires) == 1
    states = as_states(d)
    # Still true: nothing.
    assert firing.decide_values(r, [match()], states, NOW + H).fires == []
    # One cycle false re-arms …
    d = firing.decide_values(r, [], states, NOW + H)
    states = as_states(d, states)
    assert states['rolling']['state'] == {'armed': True}
    # … but the 3 h gap still holds.
    assert firing.decide_values(r, [match()], states, NOW + 2 * H).fires == []
    assert len(firing.decide_values(r, [match()], states,
                                    NOW + 3 * H).fires) == 1


def test_next_days_gap_is_a_day():
    r = rule({'days': 'nextDays', 'count': 3, 'part': 'night'})
    states = as_states(firing.decide_values(r, [match()], {}, NOW))
    states = as_states(firing.decide_values(r, [], states, NOW + H), states)
    assert firing.decide_values(r, [match()], states,
                                NOW + 23 * H).fires == []
    assert firing.decide_values(r, [match()], states, NOW + 24 * H).fires


def warning_rule():
    return rule({'nextHours': 12}, kind='dwd_warning', conditions=[
        {'warning': {'minLevel': 2, 'families': []}}])


def wmatch(alert_id, level, onset=NOW + H, hours=3, family='gewitter'):
    w = ev.Warning(alert_id, level, family, 'GEWITTER', 'h', onset,
                   onset + hours * H)
    return ev.WarningMatch(w, ev.Evidence(warning={
        'level': level, 'family': family, 'onset': 'ab 13:00'}))


def test_warning_fires_once_then_only_on_escalation():
    r = warning_rule()
    d = firing.decide_warnings(r, [wmatch('A', 2)], {}, NOW)
    assert [(f.occurrence_key, f.reason) for f in d.fires] == [
        ('dwd:A', 'new')]
    states = as_states(d)
    # The same alert again: nothing.
    assert firing.decide_warnings(r, [wmatch('A', 2)], states, NOW).fires \
        == []
    # Re-issued under a new id with a later expiry: silent.
    d = firing.decide_warnings(r, [wmatch('B', 2, hours=5)], states, NOW)
    assert d.fires == []
    states = as_states(d, states)
    assert states['dwd:A']['state']['alert_ids'] == ['A', 'B']
    # Re-issued again, escalated: fires on the same thread.
    d = firing.decide_warnings(r, [wmatch('C', 3)], states, NOW)
    assert [(f.occurrence_key, f.reason, f.event_key) for f in d.fires] == [
        ('dwd:A', 'escalation', 'dwd:C')]


def test_another_family_is_another_event():
    r = warning_rule()
    states = as_states(firing.decide_warnings(r, [wmatch('A', 2)], {}, NOW))
    d = firing.decide_warnings(
        r, [wmatch('F', 2, family='nebel')], states, NOW)
    assert [f.occurrence_key for f in d.fires] == ['dwd:F']


def test_a_later_storm_of_the_same_family_is_a_new_event():
    r = warning_rule()
    states = as_states(firing.decide_warnings(r, [wmatch('A', 2)], {}, NOW))
    d = firing.decide_warnings(
        r, [wmatch('Z', 2, onset=NOW + 10 * H)], states, NOW)
    assert [f.occurrence_key for f in d.fires] == ['dwd:Z']
