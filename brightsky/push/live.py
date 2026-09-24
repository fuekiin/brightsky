"""Live Activities, the server side (backend design §4.5; rules design §17,
revised by §19). Pure: the loops feed it nowcasts, warnings and the device's
`live_activities` row; it says whether to start, update or end.

One activity per device. Rain and warnings share `WeatherLiveAttributes`;
a warning takes over a running rain activity by update, never by a second
start.
"""

import datetime
from dataclasses import dataclass

from brightsky.push import berlin, evaluator as ev, payloads


STEP = datetime.timedelta(minutes=5)
BUCKETS = 24                      # 2 h of 5-minute bars
RAIN_MM_PER_5MIN = 0.025          # real rain: ≥ 0.3 mm/h (§17.3)
WET_MM_PER_5MIN = 0.01            # anything the radar calls wet
HEAVY_MM_PER_5MIN = 5 / 12        # Starkregen: ≥ 5 mm/h
START_WITHIN = datetime.timedelta(minutes=60)
ALERT_WITHIN = datetime.timedelta(minutes=15)
GAP_KEEPS_PHASE = datetime.timedelta(minutes=20)
MAX_RAIN_LIFETIME = datetime.timedelta(hours=4)
LIVE_LEAD = datetime.timedelta(hours=8)    # RuleEvaluator.liveLead
COOLDOWN = datetime.timedelta(minutes=60)
DISMISS_AFTER = datetime.timedelta(minutes=15)
UPDATE_BUDGET = datetime.timedelta(minutes=10)
CHANGE_MOVED = datetime.timedelta(minutes=5)
NUMBER_WORDS = ['null', 'ein', 'zwei', 'drei', 'vier', 'fünf', 'sechs',
                'sieben', 'acht', 'neun', 'zehn', 'elf', 'zwölf']


def intensity_label(mm_per_5min):
    """`RainOutlook.intensityLabel`."""
    if mm_per_5min < 0.02:
        return 'Sehr leicht'
    if mm_per_5min < 0.5:
        return 'Leicht'
    if mm_per_5min < 2.0:
        return 'Mäßig'
    return 'Stark'


def intensity_class(mm_per_5min):
    if mm_per_5min >= HEAVY_MM_PER_5MIN:
        return 2
    if mm_per_5min >= RAIN_MM_PER_5MIN:
        return 1
    return 0


@dataclass(frozen=True)
class Point:
    timestamp: datetime.datetime
    mm: float                    # per 5 minutes


@dataclass(frozen=True)
class RainNow:
    state: str                   # coming | raining | showers | ended
    change_at: datetime.datetime | None
    detail: str
    bucket_start: datetime.datetime
    buckets: tuple
    peak: float
    peak_at: datetime.datetime | None
    first_rain_at: datetime.datetime | None   # real rain, ≥ 10 min
    duration_minutes: int

    @property
    def peak_class(self):
        return intensity_class(self.peak)


def _runs(flags):
    """[(start index, length)] of consecutive True."""
    out, start = [], None
    for i, f in enumerate(list(flags) + [False]):
        if f and start is None:
            start = i
        elif not f and start is not None:
            out.append((start, i - start))
            start = None
    return out


def analyze_rain(points, now, in_phase=False, threshold=RAIN_MM_PER_5MIN):
    """The next change, not the next shower (§17.3).

    `in_phase`: an activity for this rain phase already runs, so a dry
    spell before more rain is „Nächster Schauer", not „Regen in".
    `threshold`: what counts as rain, in mm per 5 minutes — the rule's
    `rain.min` (`Rule.rain_threshold`). It decides the match and the
    activity's states alike.
    """
    upcoming = sorted((p for p in points if p.timestamp >= now - STEP),
                      key=lambda p: p.timestamp)[:BUCKETS]
    if not upcoming:
        return None
    start = upcoming[0].timestamp
    mm = [p.mm for p in upcoming]
    at = [p.timestamp for p in upcoming]
    real = [(i, n) for i, n in _runs(m >= threshold for m in mm)
            if n >= 2]
    wet_runs = _runs(m > WET_MM_PER_5MIN for m in mm)
    peak = max(mm)
    peak_at = at[mm.index(peak)] if peak >= threshold else None
    first_rain_at = at[real[0][0]] if real else None
    # Raining now means real rain (≥ 10 min, `RuleEvaluator.firstRealRain`)
    # that has begun: one noisy step is not a shower.
    raining = any(i <= 1 and at[i] <= now for i, _ in real)
    base = dict(bucket_start=start, buckets=tuple(mm), peak=peak,
                peak_at=peak_at, first_rain_at=first_rain_at)
    label = f'{intensity_label(peak)}er Regen'
    if raining:
        # Dry again at the first dry spell of ≥ 20 min; shorter gaps keep
        # counting to the end of the phase.
        need = GAP_KEEPS_PHASE // STEP
        dry_at = None
        for i, n in _runs(m < threshold for m in mm):
            if i > 0 and (n >= need or i + n == len(mm)):
                dry_at = at[i]
                break
        more_later = dry_at is not None and any(
            at[i] > dry_at for i, _ in real)
        detail = label + (' · danach trocken' if dry_at and not more_later
                          else ' · danach Schauer' if more_later else '')
        wet = next((n for i, n in wet_runs if i <= 1), 0)
        return RainNow(state='raining',
                       change_at=dry_at or at[-1] + STEP,
                       detail=detail, duration_minutes=wet * 5, **base)
    if not real:
        return RainNow(state='ended', change_at=None,
                       detail='Der Regen ist durch', duration_minutes=0,
                       **base)
    first_i, _ = real[0]
    # From the first real rain on, as long as it stays wet — like the
    # app's `rainMatch` (`drop { < first }.prefix { > 0.01 }`), not the
    # whole wet run around it.
    wet = 0
    for m in mm[first_i:]:
        if m <= WET_MM_PER_5MIN:
            break
        wet += 1
    duration = wet * 5
    if in_phase:
        n = len(real)
        what = ('ein Schauer' if n == 1
                else f'{NUMBER_WORDS[n] if n < len(NUMBER_WORDS) else n} '
                     'Schauer')
        return RainNow(state='showers', change_at=at[first_i],
                       detail=f'Schauerwetter · {what} in den nächsten '
                              '2 Stunden',
                       duration_minutes=duration, **base)
    detail = label + (f' · etwa {duration} Minuten' if duration else '')
    return RainNow(state='coming', change_at=at[first_i], detail=detail,
                   duration_minutes=duration, **base)


def rain_content(rain, now, rule_id, context=None):
    # The app's bars are mm/h (`RainLive.buckets`); the radar and every
    # threshold here are mm per 5 minutes.
    return payloads.rain_content(
        state=rain.state, change_at=rain.change_at, detail=rain.detail,
        bucket_start=rain.bucket_start,
        buckets=[round(b * 12, 2) for b in rain.buckets],
        peak_at=rain.peak_at, generated_at=now, rule_id=rule_id,
        context=context)


def rain_headline(rain, now):
    """`WeatherLiveContent.headline` — for the start alert's body."""
    if rain.change_at is None or rain.state == 'ended':
        return 'Trocken für die nächsten 2 Stunden'
    m = max(0, round((rain.change_at - now).total_seconds() / 60))
    return {
        'coming': f'Regen in {m} Min.',
        'raining': f'Trocken in {m} Min.',
        'showers': f'Nächster Schauer in {m} Min.',
    }[rain.state]


def is_night(now):
    h = berlin.local_hour(now)
    return h >= 22 or h < 6


# MARK: - Warnings

def warning_stage(w, now, present=True):
    if not present:
        return 'cancelled'
    return 'active' if w.onset <= now else 'upcoming'


def warning_detail(w, now):
    parts = [ev.LEVEL_NAMES[w.level]]
    if w.expires:
        parts.append(f'bis {ev.day_clock(w.expires, now)}')
    return ' · '.join(parts)


# „hochgestuft von markant" — the level as the app's demo says it
ESCALATED_FROM = {1: 'Stufe 1', 2: 'markant', 3: 'Unwetter', 4: 'extrem'}


def cancelled_detail(now):
    return f'Der DWD hat die Warnung um {ev.clock(now)} aufgehoben'


def warning_content(w, stage, now, rule_id, escalated_from=None,
                    context=None):
    detail = warning_detail(w, now)
    if escalated_from:
        detail += f' · hochgestuft von {ESCALATED_FROM[escalated_from]}'
    if stage == 'cancelled':
        detail = cancelled_detail(now)
    return payloads.warning_content(
        stage=stage, level=w.level, event=_event_name(w), onset=w.onset,
        expires=w.end, detail=detail, generated_at=now, rule_id=rule_id,
        escalated_from=escalated_from, context=context)


LOWER_WORDS = {'mit', 'und', 'oder', 'von', 'in', 'im', 'bis', 'an', 'am'}


def _event_name(w):
    """DWD's uppercase event names, readable: „GEWITTER" → „Gewitter",
    „STARKES GEWITTER" → „Starkes Gewitter", „GEWITTER MIT HAGEL" →
    „Gewitter mit Hagel". Mixed case is DWD's own and stays."""
    e = (w.event or '').strip()
    if not e.isupper():
        return e
    words = e.lower().split()
    return ' '.join(word if i and word in LOWER_WORDS else
                    word[:1].upper() + word[1:]
                    for i, word in enumerate(words))


def warning_headline(w, stage, now):
    name = _event_name(w)
    if stage == 'upcoming':
        return f'{name} ab {ev.day_clock(w.onset, now)}'
    if stage == 'active':
        return f'{name} bis {ev.day_clock(w.end, now)}'
    return 'Warnung aufgehoben'


# MARK: - Precedence

@dataclass(frozen=True)
class Candidate:
    """A live rule's event that could own the device's one activity."""
    kind: str                    # rain | warning
    rule_id: str
    start: datetime.datetime
    level: int = 0
    warning: ev.Warning | None = None
    rain: RainNow | None = None
    context: str | None = None
    # Warning thread key from rule_state (`dwd:<first alert id>`), stable
    # across DWD re-issues
    thread: str | None = None
    cell_key: str | None = None

    @property
    def event_key(self):
        if self.kind == 'rain':
            return 'rain'
        return self.thread or f'dwd:{self.warning.id}'


def winner(candidates, now):
    """`RuleEvaluator.liveWinner` without „here" — the server does not know
    which place is the current location: severe warnings, then warnings
    before rain, then the earlier."""
    eligible = [c for c in candidates if c.start <= now + LIVE_LEAD]
    if not eligible:
        return None
    return min(eligible, key=lambda c: (
        0 if c.level >= 3 else 1, 0 if c.kind == 'warning' else 1,
        c.start))
