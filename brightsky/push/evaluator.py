"""Rule → match, ported from the app's `RuleEvaluator` (WeatherCore,
app commit dc3bc1b). Pure: no database, no network, no ambient clock.

The app evaluates the same rules for its „Warnungen" tab; the two must agree,
so this follows the Swift line by line, including its rounding (Swift's
`rounded()` is half away from zero, Python's `round` is not) and its DST
arithmetic (see `berlin`).
"""

import datetime
import math
from dataclasses import dataclass, field

from brightsky.push import berlin


HOUR = datetime.timedelta(hours=1)


# MARK: - Inputs

@dataclass(frozen=True)
class Hour:
    """One hour of `/weather`, in Bright Sky's default (dwd) units."""
    timestamp: datetime.datetime
    temperature: float | None = None
    precipitation: float | None = None
    sunshine: float | None = None          # minutes
    wind_speed: float | None = None        # km/h
    wind_gust_speed: float | None = None   # km/h
    cloud_cover: float | None = None       # %
    relative_humidity: float | None = None
    visibility: float | None = None        # m
    condition: str | None = None

    @classmethod
    def from_brightsky(cls, record):
        ts = record['timestamp']
        if isinstance(ts, str):
            ts = datetime.datetime.fromisoformat(ts)
        return cls(
            timestamp=ts.astimezone(berlin.UTC),
            temperature=record.get('temperature'),
            precipitation=record.get('precipitation'),
            sunshine=record.get('sunshine'),
            wind_speed=record.get('wind_speed'),
            wind_gust_speed=record.get('wind_gust_speed'),
            cloud_cover=record.get('cloud_cover'),
            relative_humidity=record.get('relative_humidity'),
            visibility=record.get('visibility'),
            condition=record.get('condition'),
        )


@dataclass(frozen=True)
class Warning:
    id: str
    level: int            # 1 minor … 4 extreme
    family: str           # WarningFamily raw value
    event: str
    headline: str
    onset: datetime.datetime
    expires: datetime.datetime | None
    event_code: int | None = None

    @property
    def end(self):
        return self.expires or self.onset + 6 * HOUR


SEVERITY_LEVELS = {'minor': 1, 'moderate': 2, 'severe': 3, 'extreme': 4}


# MARK: - Wording tables (NotificationRule.swift)

LEVEL_NAMES = {
    1: 'Wetterwarnung', 2: 'Markante Warnung', 3: 'Unwetterwarnung',
    4: 'Extremes Unwetter',
}
FAMILY_LABELS = {
    'gewitter': 'Gewitter', 'sturm': 'Sturm', 'regen': 'Starkregen',
    'schnee': 'Schnee', 'glaette': 'Glätte & Frost', 'hitze': 'Hitze',
    'nebel': 'Nebel', 'kueste': 'Küste',
}
WEEKDAY_NAMES = [
    'Montag', 'Dienstag', 'Mittwoch', 'Donnerstag', 'Freitag', 'Samstag',
    'Sonntag',
]
WEEKDAY_SHORT = ['Mo', 'Di', 'Mi', 'Do', 'Fr', 'Sa', 'So']
UNITS = {
    'temp': '°C', 'apparent': '°C', 'gust': 'km/h', 'wind': 'km/h',
    'precip': 'mm', 'snow': 'cm', 'sun': 'h', 'cloud': '%', 'humidity': '%',
    'visibility': 'm',
}
# `Metric.step < 1`: only precipitation shows a decimal.
ONE_DECIMAL = {'precip'}

# Keyword order matters: „Sturmflut" is coast, not storm; „Gewitter mit
# Starkregen" is a thunderstorm (`WarningFamily.classify`).
FAMILY_KEYWORDS = [
    ('STURMFLUT', 'kueste'), ('KÜSTE', 'kueste'),
    ('GEWITTER', 'gewitter'), ('HAGEL', 'gewitter'),
    ('STARKREGEN', 'regen'), ('DAUERREGEN', 'regen'), ('REGEN', 'regen'),
    ('SCHNEE', 'schnee'), ('SCHNEEVERWEHUNG', 'schnee'),
    ('GLÄTTE', 'glaette'), ('GLATTEIS', 'glaette'), ('FROST', 'glaette'),
    ('GLAETTE', 'glaette'),
    ('ORKAN', 'sturm'), ('STURM', 'sturm'), ('WIND', 'sturm'),
    ('BÖEN', 'sturm'), ('BOEEN', 'sturm'),
    ('HITZE', 'hitze'), ('UV', 'hitze'),
    ('NEBEL', 'nebel'),
]

SNOW_CM_PER_MM_WATER = 1.0


def classify(event):
    e = (event or '').upper()
    for keyword, family in FAMILY_KEYWORDS:
        if keyword in e:
            return family
    return 'sturm'


# MARK: - Numbers

def swift_round(x):
    """`Double.rounded()`: half away from zero."""
    return math.copysign(math.floor(abs(x) + 0.5), x)


def round1(x):
    return swift_round(x * 10) / 10


def number(value, decimals, minus='−'):
    rounded = swift_round(value) if decimals == 0 else round1(value)
    magnitude = abs(rounded)
    if magnitude == swift_round(magnitude):
        s = str(int(magnitude))
    else:
        s = f'{magnitude:.1f}'.replace('.', ',')
    return (minus if rounded < 0 else '') + s


def body_value(metric, value):
    n = number(value, 1 if metric in ONE_DECIMAL else 0)
    if metric == 'sun':
        return '1 Stunde' if value == 1 else f'{n} Stunden'
    return f'{n} {UNITS[metric]}'


def value_phrase(metric, cmp, value):
    """„24 °C", „trocken", „Böen bis 85 km/h" — the news, not the threshold."""
    s = body_value(metric, value)
    up = cmp == 'gt'
    return {
        'temp': s,
        'apparent': f'gefühlt {s}',
        'gust': f'Böen bis {s}' if up else 'schwacher Wind',
        'wind': f'Wind mit {s}' if up else 'schwacher Wind',
        'precip': f'{s} Regen' if up else 'trocken',
        'snow': f'{s} Neuschnee',
        'sun': f'{s} Sonne',
        'cloud': f'{s} Bewölkung',
        'humidity': f'{s} Luftfeuchte',
        'visibility': f'Sicht {s}',
    }[metric]


def apparent_temperature(t, wind_kmh, humidity):
    """`ApparentTemperature.celsius`: wind chill in the cold, heat index in
    the heat, the air temperature in between."""
    if t <= 10 and wind_kmh is not None and wind_kmh > 4.8:
        p = wind_kmh ** 0.16
        return 13.12 + 0.6215 * t - 11.37 * p + 0.3965 * t * p
    if t >= 26.7 and humidity is not None and humidity >= 40:
        f = t * 9 / 5 + 32
        r = humidity
        hi = (-42.379 + 2.04901523 * f + 10.14333127 * r
              - 0.22475541 * f * r - 0.00683783 * f * f
              - 0.05481717 * r * r + 0.00122874 * f * f * r
              + 0.00085282 * f * r * r - 0.00000199 * f * f * r * r)
        return (hi - 32) * 5 / 9
    return t


# MARK: - Values

@dataclass(frozen=True)
class Held:
    metric: str
    value: float
    at: datetime.datetime


def _series_value(metric, h):
    if metric == 'temp':
        return h.temperature
    if metric == 'apparent':
        if h.temperature is None:
            return None
        return apparent_temperature(
            h.temperature, h.wind_speed, h.relative_humidity)
    if metric == 'gust':
        return h.wind_gust_speed
    if metric == 'wind':
        return h.wind_speed
    if metric == 'precip':
        return h.precipitation
    if metric == 'snow':
        if h.condition == 'snow':
            return (h.precipitation or 0) * SNOW_CM_PER_MM_WATER
        return 0
    if metric == 'sun':
        return None if h.sunshine is None else h.sunshine / 60
    if metric == 'cloud':
        return h.cloud_cover
    if metric == 'humidity':
        return h.relative_humidity
    if metric == 'visibility':
        return h.visibility
    raise ValueError(metric)


def aggregate(metric, cmp, hours):
    """In the direction the clause reads (rules design §18.4)."""
    series = [(v, h.timestamp) for h in hours
              if (v := _series_value(metric, h)) is not None]
    if not series:
        return None
    # min/max return the first extreme, like Swift's min(by:)/max(by:)
    if metric in ('temp', 'apparent'):
        pick = max if cmp == 'gt' else min
        return pick(series, key=lambda s: s[0])
    if metric in ('gust', 'wind', 'cloud', 'humidity'):
        return max(series, key=lambda s: s[0])
    if metric in ('precip', 'snow', 'sun'):
        return (sum(v for v, _ in series), series[0][1])
    return min(series, key=lambda s: s[0])  # visibility


def values_hold(conditions, hours):
    """All value conditions over the hours; None when any fails or has no
    data."""
    out = []
    for c in conditions:
        agg = aggregate(c.metric, c.cmp, hours)
        if agg is None:
            return None
        value, at = agg
        ok = value > c.value if c.cmp == 'gt' else value < c.value
        if not ok:
            return None
        out.append(Held(c.metric, round1(value), at))
    return out


# MARK: - Occurrences

@dataclass(frozen=True)
class Span:
    date: datetime.date | None
    start: datetime.datetime
    end: datetime.datetime


@dataclass(frozen=True)
class Occurrence:
    """What a rule reports at most once: a day, or a run of days held
    together. `key` is its first date (None for a rolling window)."""
    key: datetime.date | None
    spans: tuple


def _occasions(window, dates):
    if window.days == 'once':
        return [list(dates)] if dates else []
    # All seven days held together would be one endless occasion: they
    # count day by day (app 04b7648).
    if (window.days == 'weekdays' and window.together
            and len(window.weekdays) < 7):
        out = []
        for d in sorted(dates):
            if out and out[-1][-1] + datetime.timedelta(days=1) == d:
                out[-1].append(d)
            else:
                out.append([d])
        return out
    return [[d] for d in dates]


def occurrences(window, now):
    if window.days == 'nextHours':
        return [Occurrence(None, (
            Span(None, now, now + window.hours * HOUR),))]
    today = berlin.local_date(now)
    day = datetime.timedelta(days=1)
    notice = window.notice
    if window.days == 'today':
        dates = [today]
    elif window.days == 'tomorrow':
        dates = [today + day]
    elif window.days == 'weekdays':
        # Looking back far enough that a run already under way keeps its
        # true first day as its key — a week of workdays held together is
        # one occasion, not four (app 04b7648).
        lookback = 7 if window.together else 1
        dates = [today + k * day for k in range(-lookback, 9)
                 if berlin.weekday(today + k * day) in window.weekdays]
    elif window.days == 'once':
        dates = list(window.dates)
    elif window.days == 'nextDays':
        dates = [today + k * day for k in range(window.count)]
    else:
        raise ValueError(window.days)
    part = window.part
    out = []
    for occasion in _occasions(window, dates):
        first = occasion[0]
        if notice is not None:
            opens = berlin.at_hour(first - notice * day, 7)
            if now < opens:
                continue
        spans = []
        for d in occasion:
            end = berlin.at_hour(d, part.end_hour)
            if end <= now:
                continue
            start = max(berlin.at_hour(d, part.start_hour), now)
            spans.append(Span(d, start, end))
        if spans:
            out.append(Occurrence(first, tuple(spans)))
    return out


# MARK: - Wording

def day_phrase(dt, now):
    d, today = berlin.local_date(dt), berlin.local_date(now)
    if d == today:
        return 'heute'
    if d == today + datetime.timedelta(days=1):
        return 'morgen'
    return WEEKDAY_NAMES[d.isoweekday() - 1]


def join(parts):
    if not parts:
        return ''
    if len(parts) == 1:
        return parts[0]
    return ', '.join(parts[:-1]) + ' und ' + parts[-1]


def days_phrase(dates):
    wds = [d.isoweekday() for d in dates]
    if set(wds) == {6, 7} and len(wds) == 2:
        return 'am Wochenende'
    return join([WEEKDAY_NAMES[w - 1] for w in wds])


def clock(dt):
    local = dt.astimezone(berlin.TZ)
    return f'{local.hour}:{local.minute:02d}'


def day_clock(dt, now):
    """„18:00", „morgen 6:00", „Sa 14:00"."""
    c = clock(dt)
    d, today = berlin.local_date(dt), berlin.local_date(now)
    if d == today:
        return c
    if d == today + datetime.timedelta(days=1):
        return f'morgen {c}'
    return f'{WEEKDAY_SHORT[d.isoweekday() - 1]} {c}'


# MARK: - Matches

@dataclass(frozen=True)
class Evidence:
    """`RuleEvidence` in the app, so the Notification Service Extension can
    compose from it."""
    values: dict = field(default_factory=dict)
    day: str = ''
    time: str = ''
    warning: dict | None = None
    rain: dict | None = None

    def to_json(self):
        out = {'values': self.values, 'day': self.day, 'time': self.time}
        if self.warning is not None:
            out['warning'] = self.warning
        if self.rain is not None:
            out['rain'] = self.rain
        return out


@dataclass(frozen=True)
class ValueMatch:
    occurrence_key: str   # ISO date, 'once' or 'rolling'
    start: datetime.datetime
    end: datetime.datetime  # when the occurrence closes
    evidence: Evidence


def occurrence_key(window, occ):
    if window.days in ('nextHours', 'nextDays'):
        return 'rolling'
    if window.days == 'once':
        return 'once'
    return occ.key.isoformat()


def value_matches(rule, hours, now):
    """Every occurrence of a value rule that passes, first first.

    The app's tab shows only the first (`RuleEvaluator.valueMatch`); the
    server fires each occurrence once, so it needs all of them. For a
    rolling window the first passing one is the match.
    """
    out = []
    for occ in occurrences(rule.window, now):
        passed = []
        for span in occ.spans:
            hs = [h for h in hours
                  if span.start - HOUR / 2 <= h.timestamp < span.end]
            held = values_hold(rule.values, hs) if hs else None
            if held is not None:
                passed.append((span, held))
        if not passed:
            continue
        span, held = passed[0]
        single = len(rule.values) == 1
        at = (held[0].at if single and held else span.start)
        if len(passed) == 1:
            day = day_phrase(at, now)
        else:
            day = days_phrase([s.date for s, _ in passed if s.date])
        hour_at = berlin.local_hour(at if single else span.start)
        time = f'gegen {hour_at} Uhr' if single else f'ab {hour_at} Uhr'
        evidence = Evidence(
            values={h.metric: h.value for h in held}, day=day, time=time)
        out.append(ValueMatch(
            occurrence_key=occurrence_key(rule.window, occ),
            start=span.start, end=occ.spans[-1].end, evidence=evidence))
        if rule.window.days in ('nextHours', 'nextDays'):
            break
    return out


@dataclass(frozen=True)
class WarningMatch:
    warning: Warning
    evidence: Evidence
    context: dict | None = None


def warning_onset_phrase(w, now):
    return 'jetzt' if w.onset <= now else f'ab {day_clock(w.onset, now)}'


def warning_matches(rule, warnings, hours, now):
    """`RuleEvaluator.warningMatches`: level, family, window overlap, and
    the value conditions over the warning's own hours (rules design §3: a
    value that becomes true during a running warning fires then)."""
    cond = rule.warning
    spans = [s for occ in occurrences(rule.window, now) for s in occ.spans]
    out = []
    for w in warnings:
        if w.level < cond.min_level:
            continue
        if cond.families and w.family not in cond.families:
            continue
        end = w.end
        if end <= now:
            continue
        if not any(s.start < end and s.end > w.onset for s in spans):
            continue
        values = {}
        if rule.values:
            lo = max(w.onset, now) - HOUR
            hs = [h for h in hours if lo <= h.timestamp <= end]
            held = values_hold(rule.values, hs)
            if held is None:
                continue
            values = {h.metric: h.value for h in held}
        onset = warning_onset_phrase(w, now)
        evidence = Evidence(
            values=values, day=day_phrase(max(w.onset, now), now),
            time=onset,
            warning={'level': w.level, 'family': w.family, 'onset': onset})
        out.append(WarningMatch(w, evidence))
    return out


# MARK: - Fallback (NotificationComposer.fallback)

def fallback(rule, evidence):
    """What the server can say on its own — no place, no rule name."""
    if evidence.warning is not None and rule.warning is not None:
        w = evidence.warning
        return (LEVEL_NAMES[w['level']],
                f"{FAMILY_LABELS[w['family']]}, {w['onset']}")
    if rule.rain:
        return ('Regen zieht auf', 'Regen in der Nähe erwartet')
    first = rule.values[0] if rule.values else None
    if first is None or first.metric not in evidence.values:
        return ('Wetterregel erfüllt', 'Eine deiner Regeln trifft zu.')
    value = evidence.values[first.metric]
    s = body_value(first.metric, value)
    phrase = value_phrase(first.metric, first.cmp, value)
    if first.metric in ('temp', 'apparent'):
        verb = 'steigt' if first.cmp == 'gt' else 'sinkt'
        return ('Temperaturgrenze erreicht',
                f'Die Temperatur {verb} voraussichtlich auf {s}.')
    if first.metric in ('gust', 'wind'):
        return ('Windgrenze erreicht', f'Erwartet: {phrase}.')
    if first.metric in ('precip', 'snow'):
        return ('Niederschlagsgrenze erreicht', f'Erwartet: {phrase}.')
    return ('Wettergrenze erreicht', f'Erwartet: {phrase}.')


# MARK: - Rain (RuleEvaluator.rainMatch)

def context_phrase(conditions, held):
    """„1 °C", „Böen bis 70 km/h" — what the value conditions found, as a
    short tail (`RuleEvaluator.contextPhrase`)."""
    parts = [body_value(c.metric, h.value) if c.metric in ('temp',
                                                           'apparent')
             else value_phrase(c.metric, c.cmp, h.value)
             for c, h in zip(conditions, held)]
    return ', '.join(parts) or None


@dataclass(frozen=True)
class RainMatch:
    starts_in_minutes: int
    duration_minutes: int
    evidence: Evidence
    context: str | None = None


def rain_match(rule, rain, hours, now):
    """A rain rule against the nowcast: real rain (≥ 0.3 mm/h for ≥ 10 min,
    §17.3) within the rule's horizon — `nextHours` capped at 2 h, else 1 h —
    with the value conditions judged over the rain's own hours."""
    if rain is None or rain.first_rain_at is None:
        return None
    if rule.window.days == 'nextHours':
        horizon = min(rule.window.hours, 2) * HOUR
    else:
        horizon = HOUR
    first = rain.first_rain_at
    if first > now + horizon:
        return None
    context = None
    values = {}
    if rule.values:
        end = first + max(HOUR, datetime.timedelta(
            minutes=rain.duration_minutes))
        hs = [h for h in hours if first - HOUR <= h.timestamp <= end]
        held = values_hold(rule.values, hs)
        if held is None:
            return None
        context = context_phrase(rule.values, held)
        values = {h.metric: h.value for h in held}
    minutes = max(0, int((first - now).total_seconds() // 60))
    evidence = Evidence(
        values=values, day='heute', time=f'in {minutes} Minuten',
        rain={'startsInMinutes': minutes,
              'durationMinutes': rain.duration_minutes})
    return RainMatch(minutes, rain.duration_minutes, evidence, context)
