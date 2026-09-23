"""The rule model, parsed from the app's wire shape.

Mirrors `RuleWireMapper` in the app
(WeatherCore/Notifications/PushBackend.swift).
Parsing is strict: anything the evaluator could not act on is rejected per
rule with a machine-readable reason, so the app can revert that rule's switch
instead of leaving an enabled rule that does nothing (push backend design §6).
"""

import datetime
import math
import re
from dataclasses import dataclass, field

from brightsky.push import berlin


KINDS = ('dwd_warning', 'rain_nowcast', 'user_rule')
# Rejected until server-side StoreKit validation exists (design §7).
HEALTH_KINDS = ('pollen', 'uv', 'biowetter', 'thermal')

METRICS = (
    'temp', 'apparent', 'gust', 'wind', 'precip', 'snow', 'sun', 'cloud',
    'humidity', 'visibility',
)
COMPARATORS = ('gt', 'lt')
FAMILIES = (
    'gewitter', 'sturm', 'regen', 'schnee', 'glaette', 'hitze', 'nebel',
    'kueste',
)
# Families whose warnings may run as a Live Activity (design §4.5).
LIVE_FAMILIES = frozenset({'gewitter', 'regen', 'sturm'})
DAY_PARTS = ('allDay', 'morning', 'midday', 'evening', 'night')
NOTICES = {'sameDay': 0, 'dayBefore': 1, 'twoDaysBefore': 2}
NEXT_HOURS_RANGE = range(1, 49)
NEXT_DAYS_RANGE = range(2, 8)
DIGEST_SCHEDULE = {'at': '07:00', 'tz': 'Europe/Berlin'}

CELL_KEY_RE = re.compile(r'^-?\d{1,2}\.\d{2},-?\d{1,3}\.\d{2}$')


class Rejected(Exception):

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class DayPart:
    """`startHour`/`endHour` as in the app's `DayPart`: hours after local
    midnight, `end` may exceed 24 for parts that run into the next day."""
    name: str
    start_hour: int
    end_hour: int


NAMED_PARTS = {
    'allDay': DayPart('allDay', 0, 24),
    'morning': DayPart('morning', 6, 12),
    'midday': DayPart('midday', 11, 15),
    'evening': DayPart('evening', 17, 22),
    'night': DayPart('night', 22, 30),
}


@dataclass(frozen=True)
class Window:
    # 'nextHours' | 'today' | 'tomorrow' | 'weekdays' | 'once' | 'nextDays'
    days: str
    part: DayPart | None = None
    hours: int | None = None           # nextHours
    count: int | None = None           # nextDays
    weekdays: frozenset = frozenset()  # 1 = Monday … 7 = Sunday
    notice: int | None = None          # days before, weekdays/once only
    together: bool = False
    dates: tuple = ()                  # once: sorted datetime.date


@dataclass(frozen=True)
class ValueCondition:
    metric: str
    cmp: str
    value: float


@dataclass(frozen=True)
class WarningCondition:
    min_level: int
    families: frozenset  # empty = all


@dataclass(frozen=True)
class Rule:
    id: str
    kind: str
    cell_key: str
    window: Window
    values: tuple = ()
    warning: WarningCondition | None = None
    rain: bool = False
    schedule: dict | None = None
    live: dict | None = None
    raw_params: dict = field(default_factory=dict, compare=False)

    @property
    def lat_lon(self):
        return parse_cell_key(self.cell_key)

    @property
    def is_live_capable(self):
        if self.rain:
            return True
        if self.warning is not None:
            fams = self.warning.families
            return not fams or bool(fams & LIVE_FAMILIES)
        return False


def parse_cell_key(cell_key):
    if not isinstance(cell_key, str) or not CELL_KEY_RE.match(cell_key):
        raise Rejected('bad_cell_key')
    lat, lon = (float(x) for x in cell_key.split(','))
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        raise Rejected('bad_cell_key')
    return lat, lon


def _int(value, allowed=None):
    """Swift encodes every number as a Double; accept integral floats."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(value)
    if isinstance(value, float):
        if not value.is_integer():
            raise ValueError(value)
        value = int(value)
    if allowed is not None and value not in allowed:
        raise ValueError(value)
    return value


def parse_part(raw):
    if isinstance(raw, str):
        return NAMED_PARTS[raw]
    if isinstance(raw, dict) and set(raw) == {'from', 'to'}:
        f = _int(raw['from'], range(0, 24))
        t = _int(raw['to'], range(0, 25))
        return DayPart('hours', f, t + 24 if t <= f else t)
    raise ValueError(raw)


def parse_window(raw):
    try:
        return _parse_window(raw)
    except (KeyError, ValueError, TypeError):
        raise Rejected('unknown_window')


def _parse_window(raw):
    if not isinstance(raw, dict):
        raise ValueError(raw)
    if 'nextHours' in raw:
        if set(raw) != {'nextHours'}:
            raise ValueError(raw)
        return Window('nextHours', hours=_int(raw['nextHours'],
                                              NEXT_HOURS_RANGE))
    days = raw['days']
    part = parse_part(raw['part'])
    if days in ('today', 'tomorrow'):
        return Window(days, part=part)
    if days == 'nextDays':
        return Window(days, part=part,
                      count=_int(raw['count'], NEXT_DAYS_RANGE))
    if days == 'weekdays':
        weekdays = frozenset(_int(d, range(1, 8)) for d in raw['weekdays'])
        together = raw.get('together', False)
        if not weekdays or not isinstance(together, bool):
            raise ValueError(raw)
        return Window(days, part=part, weekdays=weekdays,
                      notice=NOTICES[raw['notice']], together=together)
    if days == 'once':
        dates = tuple(sorted(
            datetime.date.fromisoformat(d) for d in raw['dates']))
        if not dates:
            raise ValueError(raw)
        return Window(days, part=part, dates=dates,
                      notice=NOTICES[raw['notice']])
    raise ValueError(raw)


def parse_condition(raw):
    if not isinstance(raw, dict):
        raise Rejected('bad_conditions')
    if 'warning' in raw:
        w = raw['warning']
        try:
            level = _int(w['minLevel'], range(1, 5))
            families = frozenset(w.get('families', ()))
        except (KeyError, ValueError, TypeError):
            raise Rejected('bad_conditions')
        if not families <= set(FAMILIES):
            raise Rejected('bad_conditions')
        return WarningCondition(level, families)
    if 'rain' in raw:
        return 'rain'
    if 'metric' in raw:
        if raw['metric'] not in METRICS:
            raise Rejected('unknown_metric')
        value = raw.get('value')
        if (raw.get('cmp') not in COMPARATORS
                or isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)):
            raise Rejected('bad_conditions')
        return ValueCondition(raw['metric'], raw['cmp'], float(value))
    raise Rejected('bad_conditions')


def parse_rule(raw):
    """Wire rule → `Rule`. Raises `Rejected` with the reason."""
    if not isinstance(raw, dict):
        raise Rejected('bad_rule')
    kind = raw.get('kind')
    if kind in HEALTH_KINDS:
        raise Rejected('health_not_available')
    if kind not in KINDS:
        raise Rejected('unknown_kind')
    cell_key = raw.get('cellKey')
    parse_cell_key(cell_key)
    params = raw.get('params')
    if not isinstance(params, dict) or not isinstance(params.get('all'), list):
        raise Rejected('bad_conditions')
    conditions = [parse_condition(c) for c in params['all']]
    values = tuple(c for c in conditions if isinstance(c, ValueCondition))
    warnings = [c for c in conditions if isinstance(c, WarningCondition)]
    rains = [c for c in conditions if c == 'rain']
    if kind == 'dwd_warning':
        ok = len(warnings) == 1 and not rains
    elif kind == 'rain_nowcast':
        ok = len(rains) == 1 and not warnings
    else:
        ok = bool(values) and not warnings and not rains
    if not ok:
        raise Rejected('bad_conditions')
    window = parse_window(params.get('window'))
    schedule = raw.get('schedule')
    if schedule is not None and schedule != DIGEST_SCHEDULE:
        raise Rejected('unknown_schedule')
    live = raw.get('live')
    rule = Rule(
        id=str(raw['id']).lower(),
        kind=kind,
        cell_key=cell_key,
        window=window,
        values=values,
        warning=warnings[0] if warnings else None,
        rain=bool(rains),
        schedule=schedule,
        live=live,
        raw_params=params,
    )
    if live is not None:
        if (not isinstance(live, dict)
                or not isinstance(live.get('night', False), bool)
                or not rule.is_live_capable):
            raise Rejected('live_not_applicable')
    return rule


def is_expired(window, now):
    """A once-window whose last part has ended is ignored, not rejected
    (`TimeWindow.isExpired` in the app)."""
    return (window.days == 'once'
            and berlin.at_hour(window.dates[-1], window.part.end_hour) <= now)
