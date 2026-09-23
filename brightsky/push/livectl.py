"""The device's one Live Activity, against `live_activities` (design §4.5).

Decides from a device's live candidates and its row, then sends through
`sender`. A live event the activity cannot carry — no push-to-start token,
Live Activities off in iOS, APNs refusing the start, or losing precedence —
is returned to the caller, which delivers it as an ordinary notification.
"""

import datetime
import logging

from brightsky.push import live, payloads, sender


logger = logging.getLogger('brightsky.push.live')


def active(row, now):
    return (row is not None and row['ended_at'] is None
            and row['phase'] in ('rain', 'warning')
            and now - row['started_at'] < live.LIVE_LEAD)


async def load(conn, device_id):
    return await conn.fetchrow(
        'SELECT * FROM push.live_activities WHERE device_id = $1', device_id)


async def _save(conn, device_id, *, rule_id, phase, content, state, now,
                started=False, ended=False, cooldown_until=None):
    await conn.execute(
        """
        INSERT INTO push.live_activities (
          device_id, rule_id, phase, started_at, last_update_at,
          last_content, state, ended_at, cooldown_until, activity_token,
          activity_id)
        VALUES ($1, $2, $3, $4, $4, $5, $6, $7, $8, NULL, NULL)
        ON CONFLICT (device_id) DO UPDATE SET
          rule_id = excluded.rule_id,
          phase = excluded.phase,
          started_at = CASE WHEN $9 THEN excluded.started_at
                            ELSE push.live_activities.started_at END,
          -- a new activity gets its own token from the app
          activity_token = CASE WHEN $9 THEN NULL
                                ELSE push.live_activities.activity_token END,
          activity_id = CASE WHEN $9 THEN NULL
                             ELSE push.live_activities.activity_id END,
          last_update_at = excluded.last_update_at,
          last_content = excluded.last_content,
          state = excluded.state,
          ended_at = excluded.ended_at,
          cooldown_until = COALESCE(excluded.cooldown_until,
                                    push.live_activities.cooldown_until)
        """,
        device_id, rule_id, phase, now, content, state,
        now if ended else None, cooldown_until, started)


async def _push(conn, client, device, row, payload, *, start, alert, now,
                rule_id, event_key):
    if start:
        token, field = device['push_to_start_token'], 'push_to_start_token'
    else:
        token, field = row and row['activity_token'], 'activity_token'
    if not token:
        return None
    return await sender.deliver(
        conn, client, device_id=str(device['id']),
        environment=device['environment'], token=token, token_field=field,
        payload=payload, push_type='liveactivity',
        priority=10 if alert else 5, rule_id=rule_id,
        occurrence_key=event_key, now=now)


def can_start(device):
    return bool(device['live_activities_enabled']
                and device['push_to_start_token'])


def _sound(candidate, now):
    """§19: between 22 and 6 an activity starts without sound; warnings
    from level 3 stay audible."""
    if candidate.kind == 'warning':
        return candidate.level >= 3 or not live.is_night(now)
    return (not live.is_night(now)
            and candidate.start - now <= live.ALERT_WITHIN)


def _content(c, now, escalated_from=None, stage=None):
    if c.kind == 'rain':
        return live.rain_content(c.rain, now, c.rule_id, c.context)
    stage = stage or live.warning_stage(c.warning, now)
    return live.warning_content(c.warning, stage, now, c.rule_id,
                                escalated_from=escalated_from,
                                context=c.context)


def _alert_text(c, now, stage=None):
    if c.kind == 'rain':
        return 'Regen zieht auf', live.rain_headline(c.rain, now)
    from brightsky.push.evaluator import LEVEL_NAMES
    stage = stage or live.warning_stage(c.warning, now)
    return (LEVEL_NAMES[c.level],
            live.warning_headline(c.warning, stage, now))


def _state(c, now):
    if c.kind == 'rain':
        r = c.rain
        return {'event': 'rain', 'state': r.state,
                'changeAt': r.change_at and r.change_at.isoformat(),
                'class': r.peak_class}
    return {'event': c.event_key, 'level': c.level,
            'family': c.warning.family,
            'stage': live.warning_stage(c.warning, now)}


async def start_or_take_over(conn, client, device, row, c, now):
    """Returns True when the activity now carries `c`."""
    title, body = _alert_text(c, now)
    sound = _sound(c, now)
    content = _content(c, now)
    if active(row, now) and row['activity_token']:
        # Take over the running activity by update (§17.5).
        payload = payloads.live_update(
            content, now=now, stale=_stale(c, now),
            alert_title=title if sound else None, alert_body=body)
        result = await _push(conn, client, device, row, payload,
                             start=False, alert=sound, now=now,
                             rule_id=c.rule_id, event_key=c.event_key)
        if result is not None and result.ok:
            await _save(conn, device['id'], rule_id=c.rule_id,
                        phase=c.kind, content=content, state=_state(c, now),
                        now=now)
            return True
    if not can_start(device) or active(row, now):
        return False
    payload = payloads.live_start(
        content, now=now, stale=_stale(c, now), alert_title=title,
        alert_body=body, sound=sound)
    result = await _push(conn, client, device, row, payload, start=True,
                         alert=True, now=now, rule_id=c.rule_id,
                         event_key=c.event_key)
    if result is None or not result.ok:
        return False
    await _save(conn, device['id'], rule_id=c.rule_id, phase=c.kind,
                content=content, state=_state(c, now), now=now, started=True)
    logger.info('Device %s: started %s activity for %s', device['id'],
                c.kind, c.event_key)
    return True


def _stale(c, now):
    """The end of what the data can vouch for, not a cleanup mechanism."""
    if c.kind == 'rain':
        return now + datetime.timedelta(minutes=30)
    return min(c.warning.end, now + live.LIVE_LEAD)


async def update(conn, client, device, row, c, now, *, alert=False,
                 escalated_from=None, stage=None):
    content = _content(c, now, escalated_from=escalated_from, stage=stage)
    title, body = _alert_text(c, now, stage=stage)
    payload = payloads.live_update(
        content, now=now, stale=_stale(c, now),
        alert_title=title if alert else None, alert_body=body)
    result = await _push(conn, client, device, row, payload, start=False,
                         alert=alert, now=now, rule_id=c.rule_id,
                         event_key=c.event_key)
    state = _state(c, now)
    if stage:
        state['stage'] = stage
    # Without a token yet the activity lives on its start content (§4.5);
    # the row still records what it should show.
    await _save(conn, device['id'], rule_id=c.rule_id, phase=c.kind,
                content=content, state=state, now=now)
    return result


async def end(conn, client, device, row, content, now, *, rule_id,
              event_key, cooldown=True):
    payload = payloads.live_end(content, now=now,
                                dismissal=now + live.DISMISS_AFTER)
    await _push(conn, client, device, row, payload, start=False,
                alert=False, now=now, rule_id=rule_id, event_key=event_key)
    await _save(conn, device['id'], rule_id=rule_id, phase=row['phase'],
                content=content, state=dict(row['state']), now=now,
                ended=True,
                cooldown_until=now + live.COOLDOWN if cooldown else None)
    logger.info('Device %s: ended %s activity', device['id'], row['phase'])


# MARK: - Rain

def rain_due_update(row, c, now):
    """Only when the change moved by > 5 min or the class changed, and
    at most every ~10 min unless it escalates (§17.3)."""
    s = row['state']
    r = c.rain
    escalated = r.peak_class > s.get('class', 0)
    changed = r.state != s.get('state') or r.peak_class != s.get('class')
    before = s.get('changeAt')
    if r.change_at and before:
        moved = abs(r.change_at - datetime.datetime.fromisoformat(before))
        changed = changed or moved > live.CHANGE_MOVED
    recent = (row['last_update_at'] is not None
              and now - row['last_update_at'] < live.UPDATE_BUDGET)
    return changed and (escalated or not recent), escalated


async def rain_tick(conn, client, device, candidate, now):
    """One device's rain activity per nowcast cycle. Returns True when the
    candidate is carried live (no notification needed)."""
    row = await load(conn, device['id'])
    if active(row, now) and row['phase'] == 'warning':
        return False
    if active(row, now):
        r = candidate.rain if candidate else None
        too_old = now - row['started_at'] >= live.MAX_RAIN_LIFETIME
        if r is None or r.state == 'ended' or too_old:
            content = dict(row['last_content'])
            if r is not None:
                content = live.rain_content(r, now, row['rule_id'] and
                                            str(row['rule_id']))
            await end(conn, client, device, row, content, now,
                      rule_id=row['rule_id'] and str(row['rule_id']),
                      event_key='rain')
            return True
        due, escalated = rain_due_update(row, candidate, now)
        if due:
            await update(conn, client, device, row, candidate, now,
                         alert=escalated and not live.is_night(now))
        return True
    if candidate is None or candidate.rain.state == 'ended':
        return False
    cooling = (row is not None and row['cooldown_until']
               and now < row['cooldown_until']
               and candidate.rain.peak_class <= row['state'].get('class', 0)
               and row['phase'] == 'rain')
    if cooling:
        return True    # quiet on purpose: no notification either
    return await start_or_take_over(conn, client, device, row, candidate,
                                    now)


# MARK: - Warnings

async def warning_tick(conn, client, device, candidate, present, now):
    """One device's warning activity per warnings cycle.

    `candidate`: the winning live warning or None. `present`: whether the
    running activity's alert thread is still in DWD's snapshot.
    Returns True when the candidate is carried live."""
    row = await load(conn, device['id'])
    running = active(row, now) and row['phase'] == 'warning'
    if running:
        s = row['state']
        same = candidate is not None and s.get('event') == candidate.event_key
        if not present and not same:
            # Cancelled: „Aufgehoben", then end (§17.4).
            content = dict(row['last_content'])
            content['phase']['warning']['_0']['stage'] = 'cancelled'
            content['phase']['warning']['_0']['detail'] = (
                'Der DWD hat die Warnung aufgehoben')
            await end(conn, client, device, row, content, now,
                      rule_id=row['rule_id'] and str(row['rule_id']),
                      event_key=s.get('event'), cooldown=False)
            return False
        if candidate is None:
            expires = row['last_content']['phase']['warning']['_0'][
                'expires']
            if payloads.swift_date(now) >= expires:
                await end(conn, client, device, row,
                          dict(row['last_content']), now,
                          rule_id=row['rule_id'] and str(row['rule_id']),
                          event_key=s.get('event'), cooldown=False)
            return False
        if same:
            stage = live.warning_stage(candidate.warning, now)
            escalated = candidate.level > s.get('level', 0)
            if escalated or stage != s.get('stage'):
                await update(
                    conn, client, device, row, candidate, now,
                    alert=escalated,
                    escalated_from=s.get('level') if escalated else None,
                    stage=stage)
            return True
    if candidate is None:
        return False
    return await start_or_take_over(conn, client, device, row, candidate,
                                    now)
