"""Everything generic between a rule's decision and APNs (design §4.3):
state writes, coalescing, rate limiting, payloads. Rules stay pure and never
touch APNs."""

import asyncio
import datetime
import logging
from dataclasses import dataclass

from brightsky.push import evaluator as ev
from brightsky.push import payloads, sender
from brightsky.settings import settings


logger = logging.getLogger('brightsky.push.dispatcher')


def delivery_strength(rule_row):
    """Live-Aktivität > Mitteilung (rules design §18.3). Digest rules never
    reach the immediate dispatcher."""
    return 3 if rule_row['live'] is not None else 2


# Pushes in flight at once. A failing send retries for up to about a
# minute; in parallel that costs the tick one minute, not one per device.
SEND_CONCURRENCY = 20


@dataclass
class Outgoing:
    """A notification ready to send: payload built, rate limit passed."""
    device_id: str
    environment: str
    token: str
    payload: dict
    rule_id: str | None
    occurrence_key: str
    collapse_id: str


async def apply(conn, device, items, now, digest_date=None, pending=None):
    """Write state and prepare one device's notifications.

    `items`: [(rule, rule_row, decision)] for this device. State is written
    before sending: at-most-once — a crash between the two loses one
    notification rather than repeating it on every restart. With
    `digest_date`, everything becomes one Morgenübersicht. Returns the
    `Outgoing` pushes for `send_all`; `pending` counts what this tick has
    already prepared per device, for the rate limit.
    """
    pending = {} if pending is None else pending
    events = {}
    async with conn.transaction():
        for rule, row, decision in items:
            for key, (state, fired_at, expires_at) in decision.writes.items():
                await conn.execute(
                    """
                    INSERT INTO push.rule_state (
                      rule_id, occurrence_key, state, fired_at, expires_at)
                    VALUES ($1, $2, $3, $4, $5)
                    ON CONFLICT (rule_id, occurrence_key) DO UPDATE SET
                      state = excluded.state,
                      fired_at = excluded.fired_at,
                      expires_at = excluded.expires_at
                    """, rule.id, key, state, fired_at, expires_at)
            for fire in decision.fires:
                events.setdefault(fire.event_key, []).append(
                    (rule, row, fire))
    if digest_date is not None:
        if not events:
            return []
        out = await prepare_digest(conn, device, events, digest_date, now)
        return [out] if out else []
    out = []
    for event_key, fires in events.items():
        o = await prepare_event(conn, device, event_key, fires, now, pending)
        if o is not None:
            out.append(o)
    return out


async def send_all(conn, client, outgoing, now, sleep=asyncio.sleep,
                   concurrency=SEND_CONCURRENCY):
    """Send a tick's notifications concurrently, then record them. One
    slow or failing device no longer holds up everyone after it."""
    if not outgoing:
        return []
    gate = asyncio.Semaphore(concurrency)

    async def one(o):
        async with gate:
            return await sender.send(
                client, token=o.token, environment=o.environment,
                payload=o.payload, priority=10, collapse_id=o.collapse_id,
                now=now, sleep=sleep)
    results = await asyncio.gather(*(one(o) for o in outgoing))
    for o, result in zip(outgoing, results):
        await sender.record(
            conn, result, device_id=o.device_id, token=o.token,
            token_field='apns_token', rule_id=o.rule_id,
            occurrence_key=o.occurrence_key, now=now)
    return results


def lead(fires):
    """Strongest delivery, then the rule the app sent first."""
    return max(fires, key=lambda f: (delivery_strength(f[1]),
                                     -f[1]['position']))


def build_payload(event_key, fires):
    rule, row, fire = lead(fires)
    title, body = ev.fallback(rule, fire.evidence)
    data = {
        'v': 1,
        'kind': rule.kind,
        'ruleId': rule.id,
        'ruleIds': [r.id for r, _, _ in sorted(
            fires, key=lambda f: f[1]['position'])],
        'cellKey': rule.cell_key,
        'occurrence': event_key,
        'evidence': fire.evidence.to_json(),
    }
    return payloads.alert(
        title, body, time_sensitive=rule.warning is not None,
        thread_id=rule.id, data=data), rule, fire


async def prepare_event(conn, device, event_key, fires, now, pending):
    payload, rule, fire = build_payload(event_key, fires)
    device_id = str(device['id'])
    if not device['apns_token']:
        await _audit(conn, device_id, rule.id, fire.occurrence_key, now,
                     'no_token')
        logger.info('Device %s has no APNs token; %s not sent', device_id,
                    event_key)
        return None
    sent = await conn.fetchval(
        """
        SELECT count(*) FROM push.notifications_sent
        WHERE device_id = $1 AND push_type = 'alert' AND apns_status = 200
          AND sent_at > $2
        """, device_id, now - datetime.timedelta(hours=1))
    sent += pending.get(device_id, 0)
    if sent >= settings.PUSH_MAX_ALERTS_PER_HOUR:
        # A looping rule must not burn the user's trust or get the topic
        # throttled by Apple.
        logger.warning('Rate limit: device %s already had %d pushes this '
                       'hour, dropping %s', device_id, sent, event_key)
        await _audit(conn, device_id, rule.id, fire.occurrence_key, now,
                     'rate_limited')
        return None
    pending[device_id] = pending.get(device_id, 0) + 1
    return Outgoing(device_id, device['environment'], device['apns_token'],
                    payload, rule.id, fire.occurrence_key, event_key)


async def _audit(conn, device_id, rule_id, occurrence_key, now, reason):
    await conn.execute(
        """
        INSERT INTO push.notifications_sent (
          device_id, rule_id, occurrence_key, push_type, sent_at,
          apns_reason)
        VALUES ($1, $2, $3, 'alert', $4, $5)
        """, device_id, rule_id, occurrence_key, now, reason)


def rule_count(n):
    """`NotificationRuleSpeech.ruleCount`."""
    return 'keine Regeln' if n == 0 else '1 Regel' if n == 1 else f'{n} Regeln'


def build_digest(events, digest_date):
    """The Morgenübersicht (`NotificationComposer.digest`). Its real text
    lists the rules' titles, which only the device knows, so the fallback
    counts and `items` carries each event for the extension to compose."""
    items = []
    for event_key, fires in events.items():
        rule, row, fire = lead(fires)
        items.append((row['position'], {
            'ruleId': rule.id,
            'ruleIds': [r.id for r, _, _ in sorted(
                fires, key=lambda f: f[1]['position'])],
            'kind': rule.kind,
            'cellKey': rule.cell_key,
            'occurrence': event_key,
            'evidence': fire.evidence.to_json(),
        }))
    items = [item for _, item in sorted(items, key=lambda i: i[0])]
    n = len(items)
    body = ('Eine deiner Regeln trifft heute zu.' if n == 1
            else f'{n} deiner Regeln treffen heute zu.')
    data = {'v': 1, 'kind': 'digest', 'date': digest_date.isoformat(),
            'ruleIds': [i['ruleId'] for i in items], 'items': items}
    return payloads.alert(f'Morgenübersicht: {rule_count(n)}', body,
                          thread_id='digest', data=data)


async def prepare_digest(conn, device, events, digest_date, now):
    device_id = str(device['id'])
    payload = build_digest(events, digest_date)
    key = f'digest:{digest_date.isoformat()}'
    if not device['apns_token']:
        await _audit(conn, device_id, None, key, now, 'no_token')
        return None
    return Outgoing(device_id, device['environment'], device['apns_token'],
                    payload, None, key, key)
