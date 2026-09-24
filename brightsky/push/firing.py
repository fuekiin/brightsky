"""Transition detection against `rule_state` (design §4.2, rules design §3).

Pure: takes the matches and the rule's current state rows, returns what to
send and what to write. A rule fires on the transition *not all conditions
satisfied* → *all conditions satisfied*, at most once per occurrence.
"""

import datetime
import math
from dataclasses import dataclass, field

from brightsky.push.rules import parse_cell_key


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


# MARK: - One warning, one notification per device

def _warning_rank(rule):
    """Which of a device's registrations speaks for a warning: a place the
    user chose before „Mein Standort", then the broader switch (lower
    level), then the lower rule id — deterministic."""
    return (rule.at_current_location, rule.warning.min_level, rule.id)


def dedupe_warnings(decided, states):
    """A device hears about each DWD warning once, whatever number of its
    `dwd_warning` registrations match it — „Zuhause" and „Mein Standort"
    cover the same area while at home.

    `decided`: [(rule, row, Decision)] of one tick; `states`: rule id →
    {occurrence_key: row} as loaded before the tick. Within a tick the
    best-ranked registration keeps the fire; across ticks the one that
    already notified wins. The others' threads are marked `covered_by`, so
    they never notify later — escalations too follow the single thread.
    Mutates the decisions.
    """
    by_device = {}
    for item in decided:
        by_device.setdefault(str(item[1]['d_id']), []).append(item)
    for items in by_device.values():
        # alert id → the rule whose thread already told this device
        told = {}
        for rule, _, _ in items:
            for key, st in states.get(rule.id, {}).items():
                if key.startswith('dwd:') and not st['state'].get(
                        'covered_by'):
                    for aid in st['state'].get('alert_ids', ()):
                        told.setdefault(aid, rule.id)
        groups = {}
        for rule, _, decision in items:
            keep = []
            for fire in decision.fires:
                thread = decision.writes.get(fire.occurrence_key, (None,))[0]
                if thread and thread.get('covered_by'):
                    continue            # a covered thread never notifies
                groups.setdefault(fire.event_key, []).append(
                    (rule, decision, fire, thread))
                keep.append(fire)
            decision.fires = keep
        for event_key, group in groups.items():
            alert_id = event_key[len('dwd:'):]
            # The registration that already told this device keeps the
            # warning (its escalation fires); a new match elsewhere is
            # covered. Only a warning nobody has told about goes by rank.
            teller = next((told[a] for a in _thread_ids(group, alert_id)
                           if a in told), None)
            if teller is not None:
                winner = next((g for g in group if g[0].id == teller), None)
            else:
                winner = min(group, key=lambda g: _warning_rank(g[0]))
            speaker = winner[0].id if winner is not None else teller
            for rule, decision, fire, thread in group:
                if winner is not None and rule.id == winner[0].id:
                    continue
                decision.fires.remove(fire)
                if thread is not None:
                    thread['covered_by'] = speaker


def _thread_ids(group, alert_id):
    ids = {alert_id}
    for _, _, _, thread in group:
        if thread:
            ids.update(thread.get('alert_ids', ()))
    return ids


# Rain is a local event: a device's rain registrations speak for one area
# only when their cell centres are this close — the app's largest geofence
# radius (default 3 km).
RAIN_AREA_KM = 10.0


def _km(a, b):
    """Great-circle distance between two (lat, lon) in km."""
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = (math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2)
         * math.sin((lon2 - lon1) / 2) ** 2)
    return 2 * 6371.0 * math.asin(math.sqrt(h))


def rain_areas(rules):
    """A device's rain registrations grouped into areas: connected within
    RAIN_AREA_KM of each other (single link, so a chain of close places is
    one area). Returns a list of lists of rules."""
    rules = list(rules)
    parent = list(range(len(rules)))

    def root(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    for i in range(len(rules)):
        for j in range(i + 1, len(rules)):
            if _km(rules[i].lat_lon, rules[j].lat_lon) <= RAIN_AREA_KM:
                parent[root(i)] = root(j)
    areas = {}
    for i, rule in enumerate(rules):
        areas.setdefault(root(i), []).append(rule)
    return list(areas.values())


def dedupe_rain(decided, states, carried=None):
    """Rain notifications once per device and AREA (rules within
    RAIN_AREA_KM, `rain_areas`) — never across areas: rain in München is
    news even while Berlin's activity runs or Hamburg's rain was told.

    - `carried`: device id → the cell key of the rain its Live Activity
      carries. The activity is the notification for that area only: fires
      of the device's registrations within RAIN_AREA_KM of it are dropped.
    - Otherwise, per area: while one registration there has told
      (disarmed), none tells again; within a tick a chosen place wins over
      „Mein Standort", then the broader threshold, then the lower rule id.

    Mutates the decisions.
    """
    carried = carried or {}
    order = {'light': 0, 'moderate': 1, 'heavy': 2}
    by_device = {}
    for item in decided:
        by_device.setdefault(str(item[1]['d_id']), []).append(item)
    for device_id, items in by_device.items():
        decision_of = {rule.id: decision for rule, _, decision in items}
        if device_id in carried:
            here = parse_cell_key(carried[device_id])
            for rule, _, decision in items:
                if _km(rule.lat_lon, here) <= RAIN_AREA_KM:
                    decision.fires = []
        for area in rain_areas(rule for rule, _, _ in items):
            fired = [(rule, f) for rule in area
                     for f in decision_of[rule.id].fires]
            if not fired:
                continue
            told = any(
                st['state'].get('armed') is False
                for rule in area
                for key, st in states.get(rule.id, {}).items()
                if key == 'rain')
            winner = None if told else min(fired, key=lambda x: (
                x[0].at_current_location, order[x[0].rain_min], x[0].id))
            for rule, fire in fired:
                if winner is None or fire is not winner[1]:
                    decision_of[rule.id].fires.remove(fire)
