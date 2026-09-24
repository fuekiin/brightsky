"""Transition detection against `rule_state` (design §4.2, rules design §3).

Pure: takes the matches and the rule's current state rows, returns what to
send and what to write. A rule fires on the transition *not all conditions
satisfied* → *all conditions satisfied*, at most once per occurrence.
"""

import datetime
from dataclasses import dataclass, field


ROLLING_GAP = {
    'nextHours': datetime.timedelta(hours=3),
    'nextDays': datetime.timedelta(hours=24),
}
# Two alerts of one family this close together are one warning re-issued
# under a new id, not a new event.
REISSUE_GRACE = datetime.timedelta(hours=1)


@dataclass(frozen=True)
class Fire:
    rule_id: str
    occurrence_key: str
    event_key: str        # coalescing key across a device's rules
    evidence: object      # evaluator.Evidence
    reason: str           # 'new' | 'escalation'


@dataclass
class Decision:
    fires: list = field(default_factory=list)
    # occurrence_key → (state, fired_at | None, expires_at | None)
    writes: dict = field(default_factory=dict)


def decide_values(rule, matches, states, now):
    """`states`: occurrence_key → {'state', 'fired_at', 'expires_at'}."""
    d = Decision()
    window = rule.window.days
    if window in ROLLING_GAP:
        row = states.get('rolling')
        if matches:
            m = matches[0]
            armed = row is None or (
                row['state'].get('armed', False)
                and now - row['fired_at'] >= ROLLING_GAP[window])
            if armed:
                d.fires.append(Fire(rule.id, 'rolling',
                                    f'rule:{rule.id}:rolling', m.evidence,
                                    'new'))
                d.writes['rolling'] = ({'armed': False}, now, None)
        elif row is not None and not row['state'].get('armed', False):
            # A full cycle false re-arms; the gap still applies.
            d.writes['rolling'] = ({'armed': True}, row['fired_at'], None)
        return d
    for m in matches:
        if m.occurrence_key in states:
            continue
        d.fires.append(Fire(rule.id, m.occurrence_key,
                            f'rule:{rule.id}:{m.occurrence_key}',
                            m.evidence, 'new'))
        d.writes[m.occurrence_key] = ({}, now, m.end)
    return d


def decide_warnings(rule, matches, states, now):
    """Warning threads: one per event, keyed by its first alert id.

    DWD re-issues a warning under a new alert id when anything changes —
    `expires` moving is routine. A re-issue continues the thread and fires
    only if the level escalates.
    """
    d = Decision()
    threads = {k: dict(v['state']) for k, v in states.items()
               if k.startswith('dwd:')}
    for m in matches:
        w = m.warning
        key = next((k for k, t in threads.items()
                    if w.id in t['alert_ids']), None)
        if key is None:
            key = next((
                k for k, t in threads.items()
                if t['family'] == w.family
                and datetime.datetime.fromisoformat(t['end'])
                >= w.onset - REISSUE_GRACE), None)
        end = w.end
        if key is None:
            key = f'dwd:{w.id}'
            threads[key] = {'alert_ids': [w.id], 'family': w.family,
                            'last_level': w.level, 'end': end.isoformat()}
            d.fires.append(Fire(rule.id, key, f'dwd:{w.id}', m.evidence,
                                'new'))
            d.writes[key] = (threads[key], now, end + datetime.timedelta(
                days=1))
            continue
        t = threads[key]
        changed = False
        if w.id not in t['alert_ids']:
            t['alert_ids'] = t['alert_ids'][-9:] + [w.id]
            changed = True
        if end.isoformat() > t['end']:
            t['end'] = end.isoformat()
            changed = True
        fired_at = states[key]['fired_at'] if key in states else now
        if w.level > t['last_level']:
            t['last_level'] = w.level
            d.fires.append(Fire(rule.id, key, f'dwd:{w.id}', m.evidence,
                                'escalation'))
            fired_at = now
            changed = True
        if changed:
            d.writes[key] = (t, fired_at, datetime.datetime.fromisoformat(
                t['end']) + datetime.timedelta(days=1))
    return d


def decide_rain(rule, match, states, now, clear):
    """One notification per rain event (design §4.2).

    Re-arms only when the nowcast shows no real rain at all (`clear`) — not
    whenever the rule's horizon misses the shower by five minutes, which
    would report the same shower again as it drifts across the edge. The
    event is keyed by place, so several rain rules at one place make one
    notification, as the app keys its card „rain" per place.
    """
    d = Decision()
    row = states.get('rain')
    armed = row is None or row['state'].get('armed', False)
    if match is not None:
        if armed:
            d.fires.append(Fire(rule.id, 'rain', f'rain:{rule.cell_key}',
                                match.evidence, 'new'))
            d.writes['rain'] = ({'armed': False}, now, None)
    elif clear and not armed:
        d.writes['rain'] = ({'armed': True}, row['fired_at'], None)
    return d
