"""Hand-made pushes for `push-send`: proving delivery before any loop
exists (design §12, steps 2 and 7)."""

import datetime

from brightsky.push import payloads, sender, store


def _example_rain(now, state='coming'):
    start = now.replace(second=0, microsecond=0)
    start -= datetime.timedelta(minutes=start.minute % 5)

    def at(i):
        return start + datetime.timedelta(minutes=5 * i)
    return payloads.rain_content(
        state=state, change_at=at(4),
        detail='Mäßiger Regen · etwa 45 Minuten', bucket_start=start,
        buckets=[0, 0, 0, 0, 0.4, 1.2, 2.2, 3.1, 2.4, 1.5, 0.9, 0.4, 0.2]
        + [0] * 11,
        peak_at=at(7), place_name='Push-Test', generated_at=now,
        title='Regen (Test)')


async def send_handmade(device_id, kind, title, body, payload=None):
    now = datetime.datetime.now(datetime.timezone.utc)
    client = sender.client_from_settings()
    async with store.pool(max_size=1) as pool:
        async with pool.acquire() as conn:
            device = await conn.fetchrow(
                'SELECT * FROM push.devices WHERE id = $1', device_id)
            if device is None:
                raise SystemExit(f'Unknown device {device_id}')
            activity = await conn.fetchrow(
                'SELECT * FROM push.live_activities WHERE device_id = $1',
                device_id)
            push_type = 'liveactivity'
            if kind == 'alert':
                token_field, push_type = 'apns_token', 'alert'
                payload = payload or payloads.alert(title, body)
            elif kind == 'start':
                token_field = 'push_to_start_token'
                payload = payload or payloads.live_start(
                    _example_rain(now), now=now,
                    stale=now + datetime.timedelta(minutes=30),
                    alert_title=title, alert_body=body)
            else:
                token_field = 'activity_token'
                content = _example_rain(
                    now, 'raining' if kind == 'update' else 'ended')
                payload = payload or (
                    payloads.live_update(
                        content, now=now,
                        stale=now + datetime.timedelta(minutes=30))
                    if kind == 'update' else payloads.live_end(
                        content, now=now,
                        dismissal=now + datetime.timedelta(minutes=15)))
            if token_field == 'activity_token':
                token = activity and activity['activity_token']
            else:
                token = device[token_field]
            if not token:
                raise SystemExit(f'Device has no {token_field}')
            try:
                result = await sender.deliver(
                    conn, client, device_id=device_id,
                    environment=device['environment'], token=token,
                    token_field=token_field, payload=payload,
                    push_type=push_type, now=now)
            finally:
                await client.aclose()
    return {'status': result.status, 'reason': result.reason,
            'apnsId': result.apns_id, 'environment': device['environment'],
            'payload': payload}
