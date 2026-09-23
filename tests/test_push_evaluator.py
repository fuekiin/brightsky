"""Server twins of the app's RuleEvaluatorTests / ApparentTemperatureTests
(WeatherGermany, WeatherCore/Tests/WeatherCoreTests/RuleEvaluatorTests.swift
at dc3bc1b). Same inputs, same expected evidence — if one side changes, the
other must follow."""

import datetime

import pytest

from brightsky.push import berlin, evaluator as ev
from brightsky.push.rules import parse_rule


def b(y, m, d, h, mi=0):
    return datetime.datetime(y, m, d, h, mi, tzinfo=berlin.TZ).astimezone(
        berlin.UTC)


# Wednesday, 23 September 2026, 09:41 in Berlin.
NOW = b(2026, 9, 23, 9, 41)


def rule(kind='user_rule', conditions=(), window=None, **kw):
    raw = {
        'id': '00000000-0000-0000-0000-000000000001', 'kind': kind,
        'cellKey': '53.55,10.01',
        'params': {'all': list(conditions), 'window': window}, **kw,
    }
    return parse_rule(raw)


def v(metric, cmp, value):
    return {'metric': metric, 'cmp': cmp, 'value': value}


def warn_cond(level, families=()):
    return {'warning': {'minLevel': level, 'families': list(families)}}


# The app's presets (RuleCatalog.fallback) as wire rules
PRESETS = {
    'amtlich': dict(kind='dwd_warning', conditions=[warn_cond(1)],
                    window={'nextHours': 24}),
    'unwetter': dict(kind='dwd_warning', conditions=[warn_cond(3)],
                     window={'nextHours': 6}),
    'unwetter_hier': dict(
        kind='dwd_warning',
        conditions=[warn_cond(2, ['gewitter', 'regen', 'sturm'])],
        window={'nextHours': 6}),
    'regen': dict(kind='rain_nowcast', conditions=[{'rain': {}}],
                  window={'nextHours': 1}),
    'grill': dict(
        conditions=[v('temp', 'gt', 22), v('precip', 'lt', 0.1),
                    v('gust', 'lt', 25)],
        window={'days': 'weekdays', 'weekdays': [6, 7],
                'notice': 'dayBefore', 'together': True,
                'part': {'from': 12, 'to': 20}}),
    'frost': dict(conditions=[v('temp', 'lt', 0)],
                  window={'days': 'today', 'part': 'night'}),
}


def preset(key, **overrides):
    return rule(**{**PRESETS[key], **overrides})


def warning(level, event='GEWITTER', onset_in=2.0):
    return ev.Warning(
        id=f'w{level}', level=level, family=ev.classify(event), event=event,
        headline=f'Amtliche WARNUNG vor {event}',
        onset=NOW + datetime.timedelta(hours=onset_in),
        expires=NOW + datetime.timedelta(hours=onset_in + 3))


def hour(ts, temp=None, precip=None, gust=None, **kw):
    return ev.Hour(timestamp=ts, temperature=temp, precipitation=precip,
                   wind_gust_speed=gust, **kw)


# MARK: - Warnings are rules

def test_both_default_rules_match_one_warning():
    w = warning(3)
    for key in ('amtlich', 'unwetter'):
        m = ev.warning_matches(preset(key), [w], [], NOW)
        assert [x.warning.id for x in m] == ['w3']


def test_a_minor_warning_only_matches_amtlich():
    w = warning(1, 'FROST')
    assert ev.warning_matches(preset('amtlich'), [w], [], NOW)
    assert not ev.warning_matches(preset('unwetter'), [w], [], NOW)


def test_family_and_window_are_respected():
    storm = preset('unwetter_hier')
    assert not ev.warning_matches(storm, [warning(3, 'HITZE')], [], NOW)
    later = warning(3, 'GEWITTER', onset_in=10)   # outside the 6 h window
    assert not ev.warning_matches(storm, [later], [], NOW)
    soon = warning(2, 'GEWITTER', onset_in=1)
    m = ev.warning_matches(storm, [soon], [], NOW)
    assert len(m) == 1
    assert m[0].evidence.warning == {
        'level': 2, 'family': 'gewitter', 'onset': 'ab 10:41'}
    assert ev.fallback(storm, m[0].evidence) == (
        'Markante Warnung', 'Gewitter, ab 10:41')


@pytest.mark.parametrize('event, family', [
    ('STURMBÖEN', 'sturm'),
    ('STURMFLUT', 'kueste'),
    ('SCHWERES GEWITTER mit HAGEL', 'gewitter'),
    ('GLÄTTE', 'glaette'),
    ('DAUERREGEN', 'regen'),
])
def test_classify(event, family):
    assert ev.classify(event) == family


def test_value_becoming_true_during_a_running_warning():
    """Design §3: the case the app-side review found."""
    r = preset('unwetter', conditions=[warn_cond(3), v('temp', 'lt', 2)])
    w = ev.Warning('w', 3, 'gewitter', 'GEWITTER', 'h',
                   onset=NOW - datetime.timedelta(hours=1),
                   expires=NOW + datetime.timedelta(hours=4))
    mild = [hour(NOW + datetime.timedelta(hours=i), temp=5) for i in range(4)]
    assert not ev.warning_matches(r, [w], mild, NOW)
    cold = mild[:2] + [hour(NOW + datetime.timedelta(hours=2), temp=1)]
    m = ev.warning_matches(r, [w], cold, NOW)
    assert m[0].evidence.values == {'temp': 1.0}
    assert m[0].evidence.warning['onset'] == 'jetzt'


# MARK: - Values

def test_frost_tonight_from_the_coldest_hour():
    night = ([hour(b(2026, 9, 23, h), temp=3) for h in (22, 23)]
             + [hour(b(2026, 9, 24, h), temp=-2 if h == 5 else 1)
                for h in (1, 3, 5)])
    [m] = ev.value_matches(preset('frost'), night, NOW)
    assert m.occurrence_key == '2026-09-23'
    # App: „In Hamburg werden morgen gegen 5 Uhr −2 °C erwartet."
    assert m.evidence == ev.Evidence(
        values={'temp': -2.0}, day='morgen', time='gegen 5 Uhr')
    assert m.start > NOW   # not „now"
    assert ev.fallback(preset('frost'), m.evidence) == (
        'Temperaturgrenze erreicht',
        'Die Temperatur sinkt voraussichtlich auf −2 °C.')


def test_no_frost_no_match():
    warm = [hour(b(2026, 9, 23, h), temp=5) for h in (22, 23)]
    assert ev.value_matches(preset('frost'), warm, NOW) == []


def warm_day(day):
    return [hour(b(2026, 9, day, h), temp=25, precip=0, gust=12)
            for h in range(12, 20)]


def test_weekend_rule_waits_for_its_notice():
    saturday = warm_day(26)
    assert ev.value_matches(preset('grill'), saturday, NOW) == []
    friday = b(2026, 9, 25, 8)
    [m] = ev.value_matches(preset('grill'), saturday, friday)
    # App: „Morgen 25 °C und trocken in Hamburg."
    assert m.evidence.day == 'morgen'
    assert m.evidence.values == {'temp': 25.0, 'precip': 0.0, 'gust': 12.0}
    assert m.evidence.time == 'ab 12 Uhr'


def test_notice_opens_at_seven_exactly():
    saturday = warm_day(26)
    assert ev.value_matches(preset('grill'), saturday,
                            b(2026, 9, 25, 6, 59)) == []
    assert ev.value_matches(preset('grill'), saturday, b(2026, 9, 25, 7))


def test_a_weekend_held_together_names_both_days():
    [m] = ev.value_matches(preset('grill'), warm_day(26) + warm_day(27),
                           b(2026, 9, 25, 8))
    assert m.evidence.day == 'am Wochenende'
    assert m.occurrence_key == '2026-09-26'


def test_sunday_alone_is_still_the_same_weekend():
    [m] = ev.value_matches(preset('grill'), warm_day(27), b(2026, 9, 26, 21))
    assert m.evidence.day == 'morgen'
    assert m.occurrence_key == '2026-09-26'


def test_day_by_day_sunday_is_its_own_occasion():
    grill = preset('grill', window={
        'days': 'weekdays', 'weekdays': [6, 7], 'notice': 'dayBefore',
        'part': {'from': 12, 'to': 20}})
    [m] = ev.value_matches(grill, warm_day(27), b(2026, 9, 26, 21))
    assert m.occurrence_key == '2026-09-27'


def test_dry_means_the_sum_stays_below():
    wet = [hour(b(2026, 9, 26, h), temp=25, precip=0.4 if h == 15 else 0,
                gust=12) for h in range(12, 20)]
    assert ev.value_matches(preset('grill'), wet, b(2026, 9, 25, 8)) == []


# MARK: - Occurrences and DST

def test_night_part_across_the_autumn_dst_change():
    """25 Oct 2026, 03:00 CEST → 02:00 CET. The app adds absolute seconds
    to local midnight: night (22 → 30) on the 24th ends 05:00 local."""
    r = preset('frost')
    [occ] = ev.occurrences(r.window, b(2026, 10, 24, 10))
    assert occ.spans[0].start == b(2026, 10, 24, 22)
    assert occ.spans[0].end.astimezone(berlin.TZ).hour == 5


def test_once_window_is_one_occurrence_until_its_last_date():
    r = rule(conditions=[v('temp', 'gt', 20)], window={
        'days': 'once', 'dates': ['2026-09-26', '2026-09-27'],
        'notice': 'sameDay', 'part': 'allDay'})
    assert ev.occurrences(r.window, NOW) == []
    occs = ev.occurrences(r.window, b(2026, 9, 26, 8))
    assert [o.key for o in occs] == [datetime.date(2026, 9, 26)]
    assert len(occs[0].spans) == 2
    [m] = ev.value_matches(r, warm_day(27), b(2026, 9, 26, 8))
    assert m.occurrence_key == 'once'


def test_rolling_windows_report_the_first_match_only():
    r = rule(conditions=[v('gust', 'gt', 70)], window={'nextHours': 12})
    hours = [hour(NOW + datetime.timedelta(hours=i), gust=80)
             for i in range(12)]
    [m] = ev.value_matches(r, hours, NOW)
    assert m.occurrence_key == 'rolling'
    assert ev.fallback(r, m.evidence) == (
        'Windgrenze erreicht', 'Erwartet: Böen bis 80 km/h.')


# MARK: - Numbers

def test_swift_rounding_is_half_away_from_zero():
    assert ev.swift_round(2.5) == 3
    assert ev.swift_round(-2.5) == -3
    assert ev.number(0.25, 1) == '0,3'
    assert ev.number(-0.4, 0) == '0'
    assert ev.body_value('sun', 1) == '1 Stunde'
    assert ev.body_value('precip', 1.25) == '1,3 mm'


def test_snow_counts_only_snow_hours():
    hours = [hour(NOW, precip=2, condition='snow'),
             hour(NOW + datetime.timedelta(hours=1), precip=3,
                  condition='rain')]
    assert ev.aggregate('snow', 'gt', hours)[0] == 2.0


# MARK: - ApparentTemperatureTests

def test_wind_chill():
    assert ev.apparent_temperature(0, 20, 80) == pytest.approx(-5.24, abs=0.01)
    assert ev.apparent_temperature(5, 3, 80) == 5


def test_heat_index():
    assert ev.apparent_temperature(32, 5, 60) == pytest.approx(37.07,
                                                               abs=0.01)
    assert ev.apparent_temperature(30, None, 30) == 30


def test_in_between_is_the_air_temperature():
    assert ev.apparent_temperature(18, 30, 90) == 18
    assert ev.apparent_temperature(-3, None, None) == -3
