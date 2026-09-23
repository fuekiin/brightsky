"""Delivery with bookkeeping: audit rows, dead tokens, retries (§4.4)."""

import asyncio
import datetime
import logging
import random

from brightsky.push import apns, store
from brightsky.settings import settings


logger = logging.getLogger('brightsky.push.sender')

RETRY_DELAYS = (1.0, 4.0, 15.0)
DEFAULT_EXPIRATION = datetime.timedelta(hours=6)


def client_from_settings():
    if not (settings.PUSH_APNS_KEY_PATH and settings.PUSH_APNS_KEY_ID
            and settings.PUSH_APNS_TEAM_ID):
        raise RuntimeError(
            'APNs is not configured: set BRIGHTSKY_PUSH_APNS_KEY_PATH, '
            'BRIGHTSKY_PUSH_APNS_KEY_ID and BRIGHTSKY_PUSH_APNS_TEAM_ID')
    token = apns.ProviderToken.from_file(
        settings.PUSH_APNS_KEY_PATH, settings.PUSH_APNS_KEY_ID,
        settings.PUSH_APNS_TEAM_ID)
    return apns.APNsClient(token, settings.PUSH_APNS_TOPIC)


async def deliver(conn, client, *, device_id, environment, token,
                  token_field, payload, push_type='alert', priority=10,
                  rule_id=None, occurrence_key=None, collapse_id=None,
                  expiration=None, now, sleep=asyncio.sleep, retry=True):
    """Send, retrying 429/5xx with jitter, and record the outcome.

    `token_field` says which token this is, so a dead one is cleaned up
    the right way: a dead `apns_token` deletes the device (design §4.4),
    a dead push-to-start or activity token only forgets that token.
    """
    if expiration is None:
        # A notification that arrives a day late is worse than none.
        expiration = int((now + DEFAULT_EXPIRATION).timestamp())
    for delay in (*RETRY_DELAYS, None):
        result = await client.send(
            token, environment, payload, push_type=push_type,
            priority=priority, collapse_id=collapse_id,
            expiration=expiration)
        # `retry=False`: a Live Activity start that timed out may still
        # have started one; sending it again would start a second.
        if not result.retryable or delay is None or not retry:
            break
        await sleep(delay * (0.5 + random.random()))
    await conn.execute(
        """
        INSERT INTO push.notifications_sent (
          device_id, rule_id, occurrence_key, push_type, sent_at,
          apns_status, apns_reason, apns_id)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        """,
        device_id, rule_id, occurrence_key, push_type, now, result.status,
        result.reason, result.apns_id)
    if result.ok:
        await store.mark_source(conn, 'apns', now)
    else:
        logger.warning('APNs %s for device %s (%s): %s %s', push_type,
                       device_id, token_field, result.status, result.reason)
        if result.token_dead:
            await forget_token(conn, device_id, token_field, token, now)
        elif result.status in (0, 429) or result.status >= 500:
            await store.mark_source(
                conn, 'apns', now, f'{result.status} {result.reason}')
    return result


async def forget_token(conn, device_id, token_field, token, now):
    """Only the token that failed: the app may have reported a new one
    while this push was in flight."""
    if token_field == 'apns_token':
        logger.info('Deleting device %s: APNs token is dead', device_id)
        await conn.execute(
            'DELETE FROM push.devices WHERE id = $1 AND apns_token = $2',
            device_id, token)
    elif token_field == 'push_to_start_token':
        await conn.execute(
            'UPDATE push.devices SET push_to_start_token = NULL '
            'WHERE id = $1 AND push_to_start_token = $2', device_id, token)
    elif token_field == 'activity_token':
        # The activity is gone on the device: stop driving it.
        await conn.execute(
            'UPDATE push.live_activities SET activity_token = NULL, '
            'ended_at = COALESCE(ended_at, $3) '
            'WHERE device_id = $1 AND activity_token = $2',
            device_id, token, now)
    else:
        raise ValueError(token_field)
