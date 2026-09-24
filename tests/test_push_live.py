import datetime

from brightsky.push import evaluator as ev, live


UTC = datetime.timezone.utc
NOW = datetime.datetime(2026, 9, 23, 7, 40, tzinfo=UTC)   # 09:40 Berlin
M5 = datetime.timedelta(minutes=5)


def series(mm):
    return [live.Point(NOW + i * M5, v) for i, v in enumerate(mm)]


def test_coming_matches_the_apps_rain_fixture():
    # RuleEvaluatorTests.testRainFromTheNowcast: 0.2 mm in buckets 4…12
    r = live.analyze_rain(series([0.2 if 4 <= i <= 12 else 0
                                  for i in range(24)]), NOW)
    assert r.state == 'coming'
    assert live.rain_headline(r, NOW) == 'Regen in 20 Min.'
    assert r.detail == 'Leichter Regen · etwa 45 Minuten'
    assert r.first_rain_at == NOW + 4 * M5


def test_drizzle_is_not_rain():
    r = live.analyze_rain(series([0.01] * 24), NOW)
    assert r.state == 'ended'
    assert r.first_rain_at is None


def test_a_single_wet_bucket_is_not_real_rain():
    r = live.analyze_rain(series([0, 0, 0.3] + [0] * 21), NOW)
    assert r.first_rain_at is None


def test_raining_counts_to_the_dry_spell():
    r = live.analyze_rain(series([1.0] * 6 + [0] * 18), NOW)
    assert r.state == 'raining'
    assert live.rain_headline(r, NOW) == 'Trocken in 30 Min.'
    assert r.detail == 'Mäßiger Regen · danach trocken'


def test_short_gap_keeps_the_phase():
    mm = [1.0] * 3 + [0] * 2 + [1.0] * 3 + [0] * 16
    r = live.analyze_rain(series(mm), NOW)
    assert r.change_at == NOW + 8 * M5


def test_showers_within_a_phase():
    mm = [0] * 3 + [0.5] * 3 + [0] * 6 + [0.6] * 2 + [0] * 10
    r = live.analyze_rain(series(mm), NOW, in_phase=True)
    assert r.state == 'showers'
    assert live.rain_headline(r, NOW) == 'Nächster Schauer in 15 Min.'
    assert r.detail == 'Schauerwetter · zwei Schauer in den nächsten ' \
        '2 Stunden'


def test_intensity_classes():
    assert live.intensity_class(0.01) == 0
    assert live.intensity_class(0.1) == 1
    assert live.intensity_class(0.5) == 2


def warning(level, onset_h=1, hours=3, id='A'):
    return ev.Warning(id, level, 'gewitter', 'GEWITTER', 'h',
                      NOW + datetime.timedelta(hours=onset_h),
                      NOW + datetime.timedelta(hours=onset_h + hours))


def test_winner_precedence():
    rain = live.Candidate('rain', 'r', NOW)
    moderate = live.Candidate('warning', 'w', NOW + M5, level=2,
                              warning=warning(2))
    severe = live.Candidate('warning', 's', NOW + 2 * M5, level=3,
                            warning=warning(3, id='S'))
    assert live.winner([rain, moderate], NOW) is moderate
    assert live.winner([rain, moderate, severe], NOW) is severe
    far = live.Candidate('warning', 'f', NOW + datetime.timedelta(hours=9),
                         level=4, warning=warning(4, onset_h=9))
    assert live.winner([far], NOW) is None


def test_warning_content_phases():
    w = warning(3)
    c = live.warning_content(w, 'upcoming', NOW, 'rule-1')
    phase = c['phase']['warning']['_0']
    assert phase['stage'] == 'upcoming'
    assert phase['event'] == 'Gewitter'
    assert c['ruleId'] == 'rule-1' and c['placeName'] == ''
    assert live.warning_headline(w, 'upcoming', NOW) == 'Gewitter ab 10:40'
    assert live.warning_headline(w, 'active', NOW) == 'Gewitter bis 13:40'
