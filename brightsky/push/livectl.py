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
from dataclasses import dataclass

from brightsky.push import evaluator as ev, firing, live, payloads, sender
from brightsky.push.rules import parse_cell_key
from brightsky.settings import settings


logger = logging.getLogger('brightsky.push.live')

LOCKS = collections.defaultdict(asyncio.Lock)
END_EXPIRATION = datetime.timedelta(hours=1)


def lock(device_id):
    return LOCKS[str(device_id)]


def prune_locks():
    """Forget the locks of devices not in the middle of a decision. A lock
    just released to a waiter reads unlocked until the waiter runs; it is
    kept, or the waiter and the next caller would hold different locks."""
    for key in [k for k, v in LOCKS.items()
                if not v.locked() and not v._waiters]:
        del LOCKS[key]


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
          -- What outlives any one state written here (STICKY: the rain
          -- episodes, the replaced activity ids) is kept unless the new
          -- state brings its own. An update writing only its _state() wiped
          -- endedIds within minutes (second review 2026-10-01).
          state = COALESCE((
            SELECT jsonb_object_agg(key, value)
            FROM jsonb_each(push.live_activities.state)
            WHERE key = ANY($10::text[])), '{}'::jsonb) || excluded.state,
          ended_at = excluded.ended_at,
          cooldown_until = COALESCE(excluded.cooldown_until,
                                    push.live_activities.cooldown_until)
        -- Only a new start may replace an ended row: an update racing the
        -- user's dismissal (push-api, another process) must not undo it.
        WHERE $9 OR push.live_activities.ended_at IS NULL
        """,
        device_id, rule_id, phase, now, content, state,
        now if ended else None, cooldown_until, started, list(STICKY))


# State keys that outlive any one activity's state (`_save`).
STICKY = ('episodes', 'endedIds', 'warningsDone')


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


# A device that never proved its cards arrive (`devices.live_confirmed`: a
# token report or a dismissal, ever) falls back to notifications after this
# many starts in a row without either; a registration gives it this many
# again. Once confirmed it never falls back: the app reports dismissals even
# where it fails to report tokens, so a missing token alone is no evidence
# (second review 2026-10-01: a plain counter would have moved 55 % of card
# devices to notifications, half of them wrongly).
UNCONFIRMED_STARTS = 2
# Activity ids the row remembers after replacing them with a new start.
ENDED_IDS = 5


def can_start(device):
    return bool(device['live_activities_enabled']
                and device['push_to_start_token']
                and (device.get('live_confirmed', True)
                     or (device.get('live_unconfirmed') or 0)
                     < UNCONFIRMED_STARTS))


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
        r = c.rain
        headline = live.rain_headline(r, now)
        if r.heavy:
            # The episode's one extra alert says why it lights the screen
            # (it read „Regen zieht auf · Trocken in 120 Min." — review
            # 2026-10-01) — not „Starkregen", DWD's warning term from
            # 15 mm/h; heavy here is ≥ 10 mm/h (second review).
            when = ('jetzt kräftig' if r.heavy_at <= now
                    else f'kräftig ab {ev.clock(r.heavy_at)}')
            return 'Kräftiger Regen', f'{headline} · {when}'
        title = 'Es regnet' if r.state == 'raining' else 'Regen zieht auf'
        return title, headline
    from brightsky.push.evaluator import level_title
    stage = stage or live.warning_stage(c.warning, now)
    return (level_title(c.level),
            live.warning_headline(c.warning, stage, now))


def _state(c, now):
    if c.kind == 'rain':
        r = c.rain
        state = {'event': 'rain', 'state': r.state,
                 'changeAt': r.change_at and r.change_at.isoformat(),
                 'class': r.peak_class}
        if c.cell_key:
            state['cell'] = c.cell_key      # the episode it belongs to
        return state
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
    """The end of what the data can vouch for, not a cleanup mechanism —
    and never later than the moment the countdown reaches its target.

    The app counts down with the system's relative format, which turns
    into „vor 1 Minute" once the target passes; iOS only redraws at the
    stale-date, where the app then shows a static headline („Regen
    jetzt"). So the stale-date is the countdown's target when that comes
    first (seen on the phone 2026-09-24: „Regen vor 1 Minute").
    """
    if c.kind == 'rain':
        stale = now + datetime.timedelta(minutes=30)
        target = c.rain.change_at
        if target is not None and target > now:
            stale = min(stale, target)
        return stale
    w = c.warning
    stale = min(w.end, now + live.LIVE_LEAD)
    if live.warning_stage(w, now) == 'upcoming':
        stale = min(stale, w.onset)     # „Beginn in …" ends at the onset
    return stale                        # active: „Ende in …" ends at expiry


# A running activity takes over a new event by update only while it can
# still carry it for this long: iOS ends an activity ~8 h after its start
# (LIVE_LEAD), so a severe warning taking over a 7 h old rain card was cut
# off within the hour (second review 2026-10-01).
TAKE_OVER_MARGIN = datetime.timedelta(hours=2)


async def _count_unconfirmed(conn, device_id, delta):
    await conn.execute(
        'UPDATE push.devices SET live_unconfirmed = '
        'GREATEST(live_unconfirmed + $2, 0) WHERE id = $1', device_id, delta)


async def start_or_take_over(conn, client, device, row, c, now,
                             episodes=None, prior_episodes=None):
    """Returns True when the activity now carries `c`. `episodes`: the
    device's rain episodes to record with a new start (`started`);
    `prior_episodes`: what to put back when the start fails or is given
    up (`episodesBefore`)."""
    title, body = _alert_text(c, now)
    sound = _sound(c, now)
    content = _content(c, now)
    if active(row) and row['activity_token']:
        young = now - row['started_at'] < live.LIVE_LEAD - TAKE_OVER_MARGIN
        if young or not can_start(device):
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
                            phase=c.kind, content=content,
                            state=_state(c, now), now=now)
                return True
        else:
            # Too old to carry it to its end: end it, start a fresh one.
            await end(conn, client, device, row, dict(row['last_content']),
                      now, cooldown=False)
            row = await load(conn, device['id'])
    if not can_start(device) or active(row):
        return False
    state = _state(c, now)
    if episodes is not None:
        state['episodes'] = episodes
    if prior_episodes is not None:
        state['episodesBefore'] = prior_episodes
    # The ids of the activities this one replaces: a late DELETE for one of
    # them must not end this one (store.end_activity) — a stale dismissal
    # would silence the new card's area for hours (review 2026-10-01).
    ended = list(row['state'].get('endedIds') or ()) if row else []
    if row is not None and row['activity_id']:
        ended.append(row['activity_id'])
    if ended:
        state['endedIds'] = ended[-ENDED_IDS:]
    done = warnings_done(row, now)
    if done:
        state['warningsDone'] = done
    await _save(conn, device['id'], rule_id=c.rule_id, phase=c.kind,
                content=content, state=state, now=now, started=True)
    # Counted until the app proves a card arrived (UNCONFIRMED_STARTS) —
    # before the push leaves, so a token report racing its answer is not
    # overwritten; taken back when the start did not happen.
    await _count_unconfirmed(conn, device['id'], 1)
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
        await _count_unconfirmed(conn, device['id'], -1)
        # A start that never happened opens no episode: the episodes are
        # what they were before it.
        await conn.execute(
            'UPDATE push.live_activities SET ended_at = $2, state = state '
            "|| jsonb_build_object('episodes', $3::jsonb) "
            """|| '{"abandoned": true}'::jsonb """
            'WHERE device_id = $1 AND activity_token IS NULL',
            device['id'], now,
            prior_episodes if prior_episodes is not None
            else (episodes_of(row, now) if row else {}))
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
        # Most likely it never arrived: it opens no episode — the episodes
        # are what they were before it (`episodesBefore`) — and it does not
        # count as a start without proof. (A warning's cell is no rain
        # episode.)
        cell = row['state'].get('cell') if row['phase'] == 'rain' else None
        await conn.execute(
            'UPDATE push.live_activities SET ended_at = $2, state = ('
            "CASE WHEN state ? 'episodesBefore' THEN state || "
            "jsonb_build_object('episodes', state -> 'episodesBefore') "
            "ELSE state #- ARRAY['episodes', $3::text] END) "
            """|| '{"abandoned": true}'::jsonb """
            'WHERE device_id = $1 AND activity_token IS NULL',
            device['id'], now, cell or '')
        await _count_unconfirmed(conn, device['id'], -1)
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
              marks=None, episodes=None):
    rule_id = row['rule_id'] and str(row['rule_id'])
    state = dict(row['state'])
    if episodes is not None:
        state['episodes'] = episodes
    payload = payloads.live_end(content, now=now,
                                dismissal=now + live.DISMISS_AFTER)
    await _push(conn, client, device, row, payload, start=False,
                alert=False, now=now, rule_id=rule_id,
                event_key=state.get('event'),
                expiration=now + END_EXPIRATION)
    state.update(marks or {})
    await _save(conn, device['id'], rule_id=rule_id, phase=row['phase'],
                content=content, state=state, now=now, ended=True,
                cooldown_until=now + live.COOLDOWN if cooldown else None)
    logger.info('Device %s: ended %s activity (%s)', device['id'],
                row['phase'], ', '.join(marks or ()) or 'done')


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


def _at(value):
    return datetime.datetime.fromisoformat(value)


def episodes_of(row, now):
    """The device's rain episodes: {cell key: {'startedAt', 'lastWet',
    'dismissedAt'?, 'heavy'?}} (ISO times), kept in the row's state across
    activities. The row's own rain activity is folded in — its dismissal
    too, which the push-api records on the row only — and entries older
    than EPISODE_KEEP are dropped."""
    if row is None:
        return {}
    eps = {cell: dict(e)
           for cell, e in (row['state'].get('episodes') or {}).items()}
    cell = row['state'].get('cell')
    # A start that failed or was given up opened no episode.
    if (row['phase'] == 'rain' and cell
            and not row['state'].get('abandoned')):
        e = eps.setdefault(cell, {})
        e.setdefault('startedAt', row['started_at'].isoformat())
        e.setdefault('lastWet', (row['last_update_at']
                                 or row['started_at']).isoformat())
        if row['state'].get('dismissed') and row['ended_at'] is not None:
            e.setdefault('dismissedAt', row['ended_at'].isoformat())
    return {cell: e for cell, e in eps.items()
            if now - max(_at(e[k]) for k in ('startedAt', 'lastWet',
                                               'dismissedAt') if k in e)
            < live.EPISODE_KEEP}


def area_of(eps, cell_key):
    """The episode within RAIN_AREA_KM of `cell_key`, nearest first."""
    here = parse_cell_key(cell_key)
    near = [(firing._km(parse_cell_key(cell), here), cell) for cell in eps]
    near = [n for n in near if n[0] <= firing.RAIN_AREA_KM]
    return min(near)[1] if near else None


async def _store_episodes(conn, device_id, eps):
    await conn.execute(
        'UPDATE push.live_activities SET state = state || '
        "jsonb_build_object('episodes', $2::jsonb) WHERE device_id = $1",
        device_id, eps)


async def budget(conn, device_id, now):
    """(pushes that lit the screen in the last hour, rain activity starts
    in the last 24 h). A start without an answer (status 0) may have
    arrived, so it counts."""
    row = await conn.fetchrow(
        """
        SELECT
          count(*) FILTER (WHERE sent_at > $2 AND apns_status = 200
                           AND (push_type = 'alert' OR alerting)) AS hour,
          count(*) FILTER (WHERE live_event = 'start'
                           AND occurrence_key = 'rain'
                           AND apns_status IN (0, 200)) AS starts
        FROM push.notifications_sent
        WHERE device_id = $1 AND sent_at > $3
        """, device_id, now - datetime.timedelta(hours=1),
        now - datetime.timedelta(hours=24))
    return row['hour'], row['starts']


async def told_recently(conn, device_id, cell_key, now):
    """Did a rain notification for this area reach the device within
    AREA_RESTART? An ordinary notification for rain elsewhere opens no
    episode, so without this a capped episode there was told a second time
    when the card left (replay 2026-10-01). The send log, not rule_state:
    a fire dropped because the area was held also sets `fired_at`, though
    nothing was told (final review). Card starts are the episodes' part."""
    rows = await conn.fetch(
        """
        SELECT DISTINCT r.cell_key FROM push.notifications_sent n
        JOIN push.rules r ON r.id = n.rule_id
        WHERE n.device_id = $1 AND n.sent_at > $2 AND n.push_type = 'alert'
          AND n.occurrence_key = 'rain' AND n.apns_status = 200
        """, device_id, now - live.AREA_RESTART)
    return area_of({r['cell_key']: {} for r in rows}, cell_key) is not None


async def rain_notifications(conn, device_id, now):
    """Rain notifications that reached the device in the last 24 h."""
    return await conn.fetchval(
        """
        SELECT count(*) FROM push.notifications_sent
        WHERE device_id = $1 AND sent_at > $2 AND push_type = 'alert'
          AND occurrence_key = 'rain' AND apns_status = 200
        """, device_id, now - datetime.timedelta(hours=24))


@dataclass(frozen=True)
class NotifyInstead:
    """`rain_tick`'s answer when the daily cap allows no card for a new
    episode: the worker tells it as a notification of `rule_id`, whether or
    not that rule has re-armed (worker.force_rain)."""
    cell_key: str
    rule_id: str


async def rain_gate(conn, device, eps, c, now):
    """'start'; 'hold' — quiet on purpose, no notification either; 'cap' —
    a new episode the daily cap allows no card for, told as a notification
    instead (`NotifyInstead`); or 'limit' — the hourly limit allows no card,
    the rain rule's ordinary notification goes (and meets the same limit).

    Within its area's episode (rain seen in the last EPISODE_GAP, or a
    start in the last AREA_RESTART) a candidate holds, unless it is the
    episode's first heavy rain. After a dismissal it holds for the
    episode and at least DISMISS_HOLD, heavy or not. Beyond that a new
    episode would start, but the device's daily cap on rain starts or its
    hourly limit may not allow a card: then the rain is a notification,
    once per episode like any rain rule's (second review 2026-10-01: a
    capped device heard nothing of new rain for the rest of the day)."""
    area = area_of(eps, c.cell_key)
    if area is not None:
        e = eps[area]
        open_ = now - _at(e['lastWet']) < live.EPISODE_GAP
        dismissed = e.get('dismissedAt')
        if dismissed and (open_ or now - _at(dismissed) < live.DISMISS_HOLD):
            return 'hold'
        recent = ('startedAt' in e
                  and now - _at(e['startedAt']) < live.AREA_RESTART)
        heavy_first = c.rain.heavy and not e.get('heavy')
        if (open_ or recent) and not heavy_first:
            return 'hold'
    if not c.rain.heavy and await told_recently(conn, device['id'],
                                                c.cell_key, now):
        # Told by a notification already — while another card ran, or
        # before its rain could start one: no card on top for the same rain
        # (2026-10-02: a notification, then 10–35 min later a card for the
        # same place, 6 times overnight). Heavy rain still may start one.
        return 'hold'
    hour, starts = await budget(conn, device['id'], now)
    # The hourly limit first: past it a capped episode would be recorded
    # as notified and its notification then dropped (final review).
    if hour >= settings.PUSH_MAX_ALERTS_PER_HOUR:
        logger.info('Device %s: hourly limit reached at %s', device['id'],
                    c.cell_key)
        return 'limit'
    if starts >= settings.PUSH_MAX_LIVE_STARTS_PER_DAY:
        # Its notifications instead are capped alike: past the cap a device
        # with several places got one per area every 3 h, more than an
        # uncapped one (replay 2026-10-01). Beyond that only the rain
        # rule's own once-per-phase notification goes ('limit').
        if (await rain_notifications(conn, device['id'], now)
                >= settings.PUSH_MAX_LIVE_STARTS_PER_DAY
                or await told_recently(conn, device['id'],
                                       c.cell_key, now)):
            return 'limit'
        logger.info('Device %s: %d rain activities in 24 h, a notification '
                    'for %s instead', device['id'], starts, c.cell_key)
        return 'cap'
    return 'start'


def _started(eps, c, now):
    """`eps` with the candidate's area opened by a start."""
    eps = {cell: dict(e) for cell, e in eps.items()}
    area = area_of(eps, c.cell_key)
    prior = eps.pop(area) if area is not None else {}
    # Heavy rain alerts once per episode: a start within the same episode
    # (its heavy exception) keeps the mark, a new episode starts without.
    same_episode = ('lastWet' in prior
                    and now - _at(prior['lastWet']) < live.EPISODE_GAP)
    eps[c.cell_key] = {
        'startedAt': now.isoformat(), 'lastWet': now.isoformat(),
        'heavy': c.rain.heavy
        or (same_episode and bool(prior.get('heavy')))}
    return eps


def _legacy_cooling(row, c, now):
    """Rows from before the episodes: their cooldown, without the old
    heavier-rain bypass."""
    return (row is not None and row['phase'] == 'rain'
            and row['cooldown_until'] and now < row['cooldown_until']
            and row['state'].get('gone') in (None, c.rule_id))


async def rain_tick(conn, client, device, candidate, rain, running_cell,
                    now):
    """One device's rain activity per nowcast cycle.

    `candidate`: the winning live rain event that may start an activity
    (worker: real rain within 60 min, confirmed). `rain`: the nowcast at
    the running activity's cell (`running_cell`), or None when there is no
    data. Returns the cell key of the area the activity speaks for —
    carried live, or held back on purpose — so that area gets no
    notification; None otherwise.

    One rain phase, one activity (review 2026-10-01): a running activity
    keeps its place while it rains there, even when rain elsewhere comes
    sooner, and its area's episode stays open after it ends — showers
    within EPISODE_GAP start nothing, nor does a new shower within
    AREA_RESTART of the start.
    """
    async with lock(device['id']):
        row = await load(conn, device['id'])
        if active(row) and row['phase'] == 'warning':
            return None
        if active(row):
            if await _abandon_unconfirmed(conn, device, row, now):
                row = await load(conn, device['id'])
        eps = episodes_of(row, now)
        if active(row):
            rule_id = row['rule_id'] and str(row['rule_id'])
            # No cell: its rule was deleted or disabled (worker.RUNNING_SQL)
            gone = rule_id is None or running_cell is None
            # Both checked before „no data", so an orphaned row can never
            # stay active; the end shows whatever it shows now.
            if gone:
                # The rule's return does not start it again: it may only
                # have been missing from one registration (its episode
                # holds, and its cooldown for rows from before). Another
                # rule starts at once (below).
                await end(conn, client, device, row,
                          dict(row['last_content']), now,
                          cooldown=rule_id is not None,
                          marks={'gone': rule_id} if rule_id else None)
                row = await load(conn, device['id'])
                eps = episodes_of(row, now)
            elif rain is None and (
                    now - row['started_at'] < live.MAX_RAIN_LIFETIME):
                return None     # no data: leave it alone, notify the rest
            else:
                wet = rain is not None and rain.state != 'ended'
                if wet:
                    e = eps.setdefault(running_cell, {
                        'startedAt': row['started_at'].isoformat()})
                    e['lastWet'] = now.isoformat()
                elsewhere = (candidate is not None
                             and area_of({running_cell: {}},
                                         candidate.cell_key) is None)
                if now - row['started_at'] >= live.MAX_RAIN_LIFETIME:
                    # It ran its lifetime (§17.3), checked before „no data".
                    # Its episode stays open, so it does not start again
                    # while this rain lasts.
                    await end(conn, client, device, row,
                              dict(row['last_content']), now, episodes=eps)
                elif not wet:
                    # „Trocken für die nächsten 2 Stunden", then end (§4.5)
                    await end(conn, client, device, row,
                              live.rain_content(rain, now, rule_id), now,
                              episodes=eps)
                else:
                    # Rain elsewhere, even sooner, does not take the card
                    # away from rain here; it notifies on its own.
                    c = live.Candidate(
                        'rain', rule_id, now, rain=rain,
                        cell_key=running_cell,
                        context=None if candidate is None or elsewhere
                        else candidate.context)
                    due, _ = rain_due_update(row, rain, now)
                    # The screen lights only for the episode's first heavy
                    # rain; any other change is a quiet update (a card
                    # flipping in and out of heavier rain alerted every
                    # time — replay 2026-10-01). Not tied to the intensity
                    # class rising: most cards start in the top class on a
                    # single 5-min spike, so it could never rise again.
                    heavy_now = rain.heavy and not e.get('heavy')
                    due = due or heavy_now
                    alert = heavy_now and not live.is_night(now)
                    if alert:
                        hour, _ = await budget(conn, device['id'], now)
                        alert = hour < settings.PUSH_MAX_ALERTS_PER_HOUR
                    if heavy_now:
                        # told now — quietly at night or over the limit
                        e['heavy'] = True
                    await _store_episodes(conn, device['id'], eps)
                    if due:
                        await update(conn, client, device, row, c, now,
                                     alert=alert)
                    return running_cell
                if not elsewhere:
                    return running_cell
                # It ended here; rain elsewhere may start its own (below).
                row = await load(conn, device['id'])
        if candidate is None or candidate.rain.state == 'ended':
            return None
        gone = row is not None and row['state'].get('gone')
        if (gone and gone != candidate.rule_id and row['phase'] == 'rain'
                and row['state'].get('cell')):
            # The activity ended because its rule went away: that rule's
            # return holds in its episode, but another rule starts at once.
            # Only that activity's own episode: the `gone` mark stays on the
            # row, and dropping whatever the area recorded since — a capped
            # episode's notification — told it again every tick (replay
            # 2026-10-01: 54 notifications on one device).
            area = area_of(eps, candidate.cell_key)
            if (area is not None and eps[area].get('startedAt')
                    == row['started_at'].isoformat()):
                del eps[area]
        if _legacy_cooling(row, candidate, now) and not eps:
            return candidate.cell_key
        verdict = await rain_gate(conn, device, eps, candidate, now)
        if verdict == 'hold':
            # Held rain does not keep the episode open: only a card's own
            # rain does (`lastWet`). Otherwise a showery day kept extending
            # it and nothing was told until evening (review 2026-10-01: 47 %
            # of the rain the old code told went untold).
            return candidate.cell_key
        if verdict == 'cap' and row is not None:
            # A new episode, told once as a notification — recorded like a
            # start, so AREA_RESTART and the episode hold it from now on.
            # The rule's own re-arm (90 min clear) would wait for a dry
            # spell showery weather never has (third review 2026-10-01).
            eps = _started(eps, candidate, now)
            # One notification per episode: it also uses up the heavy-rain
            # exception — a device past its cap gets no second one when
            # the rain turns heavy (replay: 75 areas told twice in 90 min).
            eps[candidate.cell_key].update(notified=True, heavy=True)
            await _store_episodes(conn, device['id'], eps)
            return NotifyInstead(candidate.cell_key, candidate.rule_id)
        if verdict in ('cap', 'limit'):
            # told as a notification (its own limits apply); 'cap' without
            # a row cannot record the episode, so it is not forced either
            return None
        if await start_or_take_over(conn, client, device, row, candidate,
                                    now, episodes=_started(eps, candidate,
                                                           now),
                                    prior_episodes=eps):
            return candidate.cell_key
        return None


# MARK: - Warnings

async def expire_warning(conn, client, device, now):
    """End the device's warning activity if its warning has expired — the
    same end `warning_tick` gives it, for when warnings cannot be evaluated
    (worker.expire_warnings)."""
    async with lock(device['id']):
        row = await load(conn, device['id'])
        if not (active(row) and row['phase'] == 'warning'):
            return
        expires = row['state'].get('expires')
        if expires and datetime.datetime.fromisoformat(expires) <= now:
            # Expiry is not a cancellation: end, no „aufgehoben".
            await end(conn, client, device, row, dict(row['last_content']),
                      now, cooldown=False)


def warnings_done(row, now):
    """Warning events that do not come back unless they escalate: {event
    key: {'level', 'until', 'closed'? (dismissed or ran its 8 hours),
    'gone'? (the rule id that went away)}}, kept across activities. Only
    the row's own state knew it before, and a rain card in between
    replaced it: a dismissed warning came back with an alert, three times
    in one night (2026-10-02). The row's own warning is folded in;
    entries past their warning's end are dropped."""
    if row is None:
        return {}
    done = {key: dict(e)
            for key, e in (row['state'].get('warningsDone') or {}).items()
            if _at(e['until']) > now}
    s = row['state']
    closed = bool(s.get('dismissed') or s.get('lifetime'))
    if (row['phase'] == 'warning' and row['ended_at'] is not None
            and s.get('event') and (closed or s.get('gone'))):
        until = s.get('expires') or (
            row['ended_at'] + live.LIVE_LEAD).isoformat()
        e = {'level': s.get('level', 0), 'until': until}
        if closed:
            e['closed'] = True
        if s.get('gone'):
            e['gone'] = s['gone']
        if _at(until) > now:
            done[s['event']] = e
    return done


def _cancelled(row, now):
    content = dict(row['last_content'])
    phase = dict(content['phase']['warning']['_0'])
    phase['stage'] = 'cancelled'
    phase['detail'] = live.cancelled_detail(now)
    content['phase'] = {'warning': {'_0': phase}}
    return content


async def warning_tick(conn, client, device, candidate, status, now):
    """One device's warning activity per warnings cycle.

    `candidate`: the winning live warning or None. `status`: what became
    of the running activity's warning (`worker.warning_status`): 'here',
    'cancelled', 'elsewhere', 'gone' or 'unknown'. Returns True
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
            if not same and status == 'cancelled':
                # DWD withdrew it: „Aufgehoben", then end (§17.4).
                await end(conn, client, device, row, _cancelled(row, now),
                          now, cooldown=False)
                return False
            if not same and status in ('gone', 'elsewhere'):
                # Its rule was deleted, disabled or moved away: the warning
                # still stands, so no „aufgehoben" — end quietly. A rule
                # that was only missing from one registration does not
                # start it again; another candidate starts at once (below).
                rule_id = row['rule_id'] and str(row['rule_id'])
                gone = status == 'gone' and rule_id
                await end(conn, client, device, row,
                          dict(row['last_content']), now, cooldown=False,
                          marks={'gone': rule_id} if gone else None)
                row = await load(conn, device['id'])
                s = row['state']
            if candidate is None:
                return False
            if same:
                if now - row['started_at'] >= live.LIVE_LEAD:
                    # iOS keeps an activity ~8 h; end it cleanly and do not
                    # restart the same event (§19). Tab and notification
                    # cover the rest.
                    await end(conn, client, device, row,
                              dict(row['last_content']), now,
                              cooldown=False, marks={'lifetime': True})
                    return True
                stage = live.warning_stage(candidate.warning, now)
                escalated = candidate.level > s.get('level', 0)
                reissued = candidate.warning.end.isoformat() != expires
                # An extreme warning alerts again when it begins
                # (decision 2026-09-24); levels 1–3 begin silently.
                begins = (stage == 'active' and s.get('stage') == 'upcoming'
                          and candidate.level == ev.EXTREME)
                if escalated or stage != s.get('stage') or reissued:
                    # Escalation alerts; a new stage or a re-issue with
                    # another expiry is a silent update (§17.4).
                    await update(
                        conn, client, device, row, candidate, now,
                        alert=escalated or begins,
                        escalated_from=s.get('level') if escalated else None,
                        stage=stage)
                elif str(row['rule_id']) != candidate.rule_id:
                    # Another rule carries it now (its own was disabled or
                    # deleted): attach it, so its status follows that rule.
                    await _save(conn, device['id'], rule_id=candidate.rule_id,
                                phase='warning',
                                content=dict(row['last_content']),
                                state=dict(s), now=now)
                return True
        if candidate is None:
            return False
        # An event the user dismissed, that ran its 8 hours, or whose rule
        # went away and came back, does not come back unless it escalates —
        # also after a rain card in between (`warnings_done`).
        done = warnings_done(row, now).get(candidate.event_key)
        if (done is not None
                and (done.get('closed')
                     or done.get('gone') == candidate.rule_id)
                and candidate.level <= done['level']):
            return True
        return await start_or_take_over(conn, client, device, row,
                                        candidate, now)
