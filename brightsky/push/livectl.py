"""The device's one Live Activity, against `live_activities` (design §4.5).

Decides from a device's live candidates and its row, then sends through
`sender`. A live event the activity cannot carry — no push-to-start token,
Live Activities off in iOS, APNs refusing the start, or losing precedence —
is left to the caller, which delivers it as an ordinary notification.

The warnings and nowcast loops both drive the same row, so every decision
for a device runs under that device's lock.
"""

import asyncio
import collections
import datetime
import logging
import time

from brightsky.push import live, payloads, sender


logger = logging.getLogger('brightsky.push.live')

LOCKS = collections.defaultdict(asyncio.Lock)
END_EXPIRATION = datetime.timedelta(hours=1)


def lock(device_id):
    return LOCKS[str(device_id)]


def active(row):
    return (row is not None and row['ended_at'] is None
            and row['phase'] in ('rain', 'warning'))


async def load(conn, device_id):
    return await conn.fetchrow(
        'SELECT * FROM push.live_activities WHERE device_id = $1', device_id)


async def _save(conn, device_id, *, rule_id, phase, content, state, now,
                started=False, ended=False, cooldown_until=None):
    """`started` replaces the row for a new activity — before its start is
    sent, so the app's token report (which can arrive within a second)
    lands on the new row and is never overwritten."""
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
        -- Only a new start may replace an ended row: an update racing the
        -- user's dismissal (push-api, another process) must not undo it.
        WHERE $9 OR push.live_activities.ended_at IS NULL
        """,
        device_id, rule_id, phase, now, content, state,
        now if ended else None, cooldown_until, started)


async def _push(conn, client, device, row, payload, *, start, alert, now,
                rule_id, event_key, expiration):
    if start:
        token, field = device['push_to_start_token'], 'push_to_start_token'
    else:
        token, field = row and row['activity_token'], 'activity_token'
    if not token:
        return None
    # ActivityKit drops a push older than the last one it applied: stamp it
    # when it leaves, not when the tick began.
    payload['aps']['timestamp'] = int(time.time())
    return await sender.deliver(
        conn, client, device_id=str(device['id']),
        environment=device['environment'], token=token, token_field=field,
        payload=payload, push_type='liveactivity',
        priority=10 if alert else 5, rule_id=rule_id,
        occurrence_key=event_key, now=now,
        expiration=payloads.unix(expiration),
        # A start retried after a timeout may start a second activity.
        retry=not start)


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
            'stage': live.warning_stage(c.warning, now),
            'expires': c.warning.end.isoformat()}


def _expiration(c, now):
    """How long APNs may hold the push: a rain push is worthless after
    half an hour, a warning push after the warning."""
    if c.kind == 'rain':
        return now + datetime.timedelta(minutes=30)
    return max(c.warning.end, now + datetime.timedelta(minutes=30))


def _stale(c, now):
    """The end of what the data can vouch for, not a cleanup mechanism."""
    if c.kind == 'rain':
        return now + datetime.timedelta(minutes=30)
    return min(c.warning.end, now + live.LIVE_LEAD)


async def start_or_take_over(conn, client, device, row, c, now):
    """Returns True when the activity now carries `c`."""
    title, body = _alert_text(c, now)
    sound = _sound(c, now)
    content = _content(c, now)
    if active(row) and row['activity_token']:
        # Take over the running activity by update (§17.5).
        payload = payloads.live_update(
            content, now=now, stale=_stale(c, now),
            alert_title=title if sound else None, alert_body=body)
        result = await _push(conn, client, device, row, payload,
                             start=False, alert=sound, now=now,
                             rule_id=c.rule_id, event_key=c.event_key,
                             expiration=_expiration(c, now))
        if result is not None and result.ok:
            await _save(conn, device['id'], rule_id=c.rule_id,
                        phase=c.kind, content=content, state=_state(c, now),
                        now=now)
            return True
    if not can_start(device) or active(row):
        return False
    await _save(conn, device['id'], rule_id=c.rule_id, phase=c.kind,
                content=content, state=_state(c, now), now=now, started=True)
    payload = payloads.live_start(
        content, now=now, stale=_stale(c, now), alert_title=title,
        alert_body=body, sound=sound)
    result = await _push(conn, client, device, None, payload, start=True,
                         alert=True, now=now, rule_id=c.rule_id,
                         event_key=c.event_key,
                         expiration=_expiration(c, now))
    if result is not None and result.status == 0:
        # No answer: the start may well have arrived. Wait for the app's
        # token before giving up, or the next tick starts a second one.
        await conn.execute(
            'UPDATE push.live_activities SET state = state || $2::jsonb '
            'WHERE device_id = $1',
            device['id'], {'pendingSince': now.isoformat()})
        return True
    if result is None or not result.ok:
        await conn.execute(
            'UPDATE push.live_activities SET ended_at = $2 '
            'WHERE device_id = $1 AND activity_token IS NULL',
            device['id'], now)
        return False
    logger.info('Device %s: started %s activity for %s', device['id'],
                c.kind, c.event_key)
    return True


PENDING_START = datetime.timedelta(minutes=2)


async def _abandon_unconfirmed(conn, device, row, now):
    """A start that timed out and whose token never came: after two
    minutes assume it never arrived, so the next tick may start again."""
    pending = row['state'].get('pendingSince')
    if (row['activity_token'] is None and pending
            and now - datetime.datetime.fromisoformat(pending)
            >= PENDING_START):
        await conn.execute(
            'UPDATE push.live_activities SET ended_at = $2 '
            'WHERE device_id = $1 AND activity_token IS NULL',
            device['id'], now)
        logger.info('Device %s: start never confirmed, given up',
                    device['id'])
        return True
    return False


async def update(conn, client, device, row, c, now, *, alert=False,
                 escalated_from=None, stage=None):
    content = _content(c, now, escalated_from=escalated_from, stage=stage)
    title, body = _alert_text(c, now, stage=stage)
    payload = payloads.live_update(
        content, now=now, stale=_stale(c, now),
        alert_title=title if alert else None, alert_body=body)
    result = await _push(conn, client, device, row, payload, start=False,
                         alert=alert, now=now, rule_id=c.rule_id,
                         event_key=c.event_key,
                         expiration=_expiration(c, now))
    if result is not None and result.token_dead:
        return result    # the activity is gone; the sender ended the row
    state = _state(c, now)
    if stage:
        state['stage'] = stage
    # Without a token yet the activity lives on its start content (§4.5);
    # the row still records what it should show.
    await _save(conn, device['id'], rule_id=c.rule_id, phase=c.kind,
                content=content, state=state, now=now)
    return result


async def end(conn, client, device, row, content, now, *, cooldown=True,
              mark=None):
    rule_id = row['rule_id'] and str(row['rule_id'])
    state = dict(row['state'])
    payload = payloads.live_end(content, now=now,
                                dismissal=now + live.DISMISS_AFTER)
    await _push(conn, client, device, row, payload, start=False,
                alert=False, now=now, rule_id=rule_id,
                event_key=state.get('event'),
                expiration=now + END_EXPIRATION)
    if mark:
        state[mark] = True
    await _save(conn, device['id'], rule_id=rule_id, phase=row['phase'],
                content=content, state=state, now=now, ended=True,
                cooldown_until=now + live.COOLDOWN if cooldown else None)
    logger.info('Device %s: ended %s activity (%s)', device['id'],
                row['phase'], mark or 'done')


# MARK: - Rain

def rain_due_update(row, rain, now):
    """Only when the change moved by > 5 min or the class changed, and
    at most every ~10 min unless it escalates (§17.3)."""
    s = row['state']
    escalated = rain.peak_class > s.get('class', 0)
    changed = (rain.state != s.get('state')
               or rain.peak_class != s.get('class'))
    before = s.get('changeAt')
    if rain.change_at and before:
        moved = abs(rain.change_at - datetime.datetime.fromisoformat(before))
        changed = changed or moved > live.CHANGE_MOVED
    recent = (row['last_update_at'] is not None
              and now - row['last_update_at'] < live.UPDATE_BUDGET)
    return changed and (escalated or not recent), escalated


async def rain_tick(conn, client, device, candidate, rain, running_cell,
                    now):
    """One device's rain activity per nowcast cycle.

    `candidate`: the winning live rain event that may start an activity
    (real rain within 60 min). `rain`: the nowcast at the running
    activity's cell (`running_cell`), or None when there is no data. The
    activity only ever shows its own place: a winner elsewhere ends it and
    starts its own. Returns True when the candidate is carried live (or
    held back on purpose), so no notification is needed for it.
    """
    async with lock(device['id']):
        row = await load(conn, device['id'])
        if active(row) and row['phase'] == 'warning':
            return False
        if active(row):
            if await _abandon_unconfirmed(conn, device, row, now):
                row = await load(conn, device['id'])
        if active(row):
            same_place = (candidate is not None
                          and candidate.cell_key == running_cell)
            rule_id = row['rule_id'] and str(row['rule_id'])
            too_old = now - row['started_at'] >= live.MAX_RAIN_LIFETIME
            if rule_id is None or running_cell is None or too_old:
                # Its rule is gone, or it ran its 4 h (§17.3): end it, with
                # whatever it shows now. Checked before „no data", so an
                # orphaned row can never stay active.
                await end(conn, client, device, row,
                          dict(row['last_content']), now,
                          cooldown=too_old and rule_id is not None)
                return too_old and same_place
            if rain is None:
                return False    # no data: leave it alone, notify the rest
            if rain.state == 'ended':
                # „Trocken für die nächsten 2 Stunden", then end (§4.5)
                await end(conn, client, device, row,
                          live.rain_content(rain, now, rule_id), now)
                return same_place
            if candidate is not None and not same_place:
                # Rain somewhere else wins: never switch place silently.
                await end(conn, client, device, row,
                          live.rain_content(rain, now, rule_id), now,
                          cooldown=False)
                row = await load(conn, device['id'])
            else:
                c = live.Candidate(
                    'rain', rule_id, now, rain=rain, cell_key=running_cell,
                    context=candidate.context if candidate else None)
                due, escalated = rain_due_update(row, rain, now)
                if due:
                    await update(conn, client, device, row, c, now,
                                 alert=escalated and not live.is_night(now))
                return same_place
        if candidate is None or candidate.rain.state == 'ended':
            return False
        cooling = (row is not None and row['phase'] == 'rain'
                   and row['cooldown_until'] and now < row['cooldown_until']
                   and candidate.rain.peak_class
                   <= row['state'].get('class', 0))
        if cooling:
            return True    # quiet on purpose: no notification either
        return await start_or_take_over(conn, client, device, row,
                                        candidate, now)


# MARK: - Warnings

def _cancelled(row, now):
    content = dict(row['last_content'])
    phase = dict(content['phase']['warning']['_0'])
    phase['stage'] = 'cancelled'
    phase['detail'] = live.cancelled_detail(now)
    content['phase'] = {'warning': {'_0': phase}}
    return content


async def warning_tick(conn, client, device, candidate, present, now):
    """One device's warning activity per warnings cycle.

    `candidate`: the winning live warning or None. `present`: whether the
    running activity's warning is still in DWD's snapshot. Returns True
    when the candidate is carried live (or held back on purpose)."""
    async with lock(device['id']):
        row = await load(conn, device['id'])
        if active(row) and await _abandon_unconfirmed(conn, device, row,
                                                      now):
            row = await load(conn, device['id'])
        s = row['state'] if row is not None else {}
        if active(row) and row['phase'] == 'warning':
            expires = s.get('expires')
            if expires and datetime.datetime.fromisoformat(expires) <= now:
                # Expiry is not a cancellation: end, no „aufgehoben".
                await end(conn, client, device, row,
                          dict(row['last_content']), now, cooldown=False)
                return False
            same = (candidate is not None
                    and s.get('event') == candidate.event_key)
            if not same and not present:
                await end(conn, client, device, row, _cancelled(row, now),
                          now, cooldown=False)
                return False
            if candidate is None:
                return False
            if same:
                if now - row['started_at'] >= live.LIVE_LEAD:
                    # iOS keeps an activity ~8 h; end it cleanly and do not
                    # restart the same event (§19). Tab and notification
                    # cover the rest.
                    await end(conn, client, device, row,
                              dict(row['last_content']), now,
                              cooldown=False, mark='lifetime')
                    return True
                stage = live.warning_stage(candidate.warning, now)
                escalated = candidate.level > s.get('level', 0)
                reissued = candidate.warning.end.isoformat() != expires
                if escalated or stage != s.get('stage') or reissued:
                    # Escalation alerts; a new stage or a re-issue with
                    # another expiry is a silent update (§17.4).
                    await update(
                        conn, client, device, row, candidate, now,
                        alert=escalated,
                        escalated_from=s.get('level') if escalated else None,
                        stage=stage)
                return True
        if candidate is None:
            return False
        # An event the user dismissed, or that ran its 8 hours, does not
        # come back unless it escalates.
        if (row is not None and row['ended_at'] is not None
                and s.get('event') == candidate.event_key
                and (s.get('dismissed') or s.get('lifetime'))
                and candidate.level <= s.get('level', 0)):
            return True
        return await start_or_take_over(conn, client, device, row,
                                        candidate, now)
