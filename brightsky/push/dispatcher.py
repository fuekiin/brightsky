"""Everything generic between a rule's decision and APNs (design §4.3):
state writes, coalescing, rate limiting, payloads. Rules stay pure and never
touch APNs."""

import datetime
import logging

from brightsky.push import evaluator as ev
from brightsky.push import payloads, sender
from brightsky.settings import settings


logger = logging.getLogger('brightsky.push.dispatcher')


def delivery_strength(rule_row):
    """Live-Aktivität > Mitteilung (rules design §18.3). Digest rules never
    reach the immediate dispatcher."""
    return 3 if rule_row['live'] is not None else 2


async def apply(conn, client, device, items, now):
    """Write state and deliver one device's fires.

    `items`: [(rule, rule_row, decision)] for this device. State is written
    before sending: at-most-once — a crash between the two loses one
    notification rather than repeating it on every restart.
    """
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
    for event_key, fires in events.items():
        await deliver_event(conn, client, device, event_key, fires, now)


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


async def deliver_event(conn, client, device, event_key, fires, now):
    payload, rule, fire = build_payload(event_key, fires)
    device_id = str(device['id'])
    if not device['apns_token']:
        await _audit(conn, device_id, rule.id, fire.occurrence_key, now,
                     'no_token')
        logger.info('Device %s has no APNs token; %s not sent', device_id,
                    event_key)
        return
    sent = await conn.fetchval(
        """
        SELECT count(*) FROM push.notifications_sent
        WHERE device_id = $1 AND push_type = 'alert' AND apns_status = 200
          AND sent_at > $2
        """, device_id, now - datetime.timedelta(hours=1))
    if sent >= settings.PUSH_MAX_ALERTS_PER_HOUR:
        # A looping rule must not burn the user's trust or get the topic
        # throttled by Apple.
        logger.warning('Rate limit: device %s already had %d pushes this '
                       'hour, dropping %s', device_id, sent, event_key)
        await _audit(conn, device_id, rule.id, fire.occurrence_key, now,
                     'rate_limited')
        return
    await sender.deliver(
        conn, client, device_id=device_id,
        environment=device['environment'], token=device['apns_token'],
        token_field='apns_token', payload=payload,
        priority=10, rule_id=rule.id, occurrence_key=fire.occurrence_key,
        collapse_id=event_key, now=now)


async def _audit(conn, device_id, rule_id, occurrence_key, now, reason):
    await conn.execute(
        """
        INSERT INTO push.notifications_sent (
          device_id, rule_id, occurrence_key, push_type, sent_at,
          apns_reason)
        VALUES ($1, $2, $3, 'alert', $4, $5)
        """, device_id, rule_id, occurrence_key, now, reason)
