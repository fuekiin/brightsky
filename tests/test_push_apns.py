import asyncio
import base64
import datetime
import json

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import (
    encode_dss_signature,
)

from brightsky.push import apns, payloads


KEY = ec.generate_private_key(ec.SECP256R1())
KEY_PEM = KEY.private_bytes(
    serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
    serialization.NoEncryption())


def unb64(s):
    return base64.urlsafe_b64decode(s + '=' * (-len(s) % 4))


def verify_jwt(token):
    header, claims, sig = token.split('.')
    raw = unb64(sig)
    assert len(raw) == 64
    der = encode_dss_signature(int.from_bytes(raw[:32], 'big'),
                               int.from_bytes(raw[32:], 'big'))
    KEY.public_key().verify(der, f'{header}.{claims}'.encode(),
                            ec.ECDSA(hashes.SHA256()))
    return json.loads(unb64(header)), json.loads(unb64(claims))


class Clock:
    def __init__(self, t=1_000_000.0):
        self.t = t

    def __call__(self):
        return self.t


def test_provider_token_is_valid_es256_and_cached():
    clock = Clock()
    pt = apns.ProviderToken(KEY_PEM, 'KEY123', 'TEAM456', clock=clock)
    token = pt.get()
    header, claims = verify_jwt(token)
    assert header == {'alg': 'ES256', 'kid': 'KEY123'}
    assert claims == {'iss': 'TEAM456', 'iat': 1_000_000}
    clock.t += 54 * 60
    assert pt.get() is token
    clock.t += 2 * 60
    assert pt.get() != token


def test_provider_token_invalidation_respects_min_age():
    clock = Clock()
    pt = apns.ProviderToken(KEY_PEM, 'K', 'T', clock=clock)
    token = pt.get()
    clock.t += 60
    pt.invalidate()
    assert pt.get() is token
    clock.t += 20 * 60
    pt.invalidate()
    assert pt.get() != token


class StubAPNs:
    def __init__(self, responses=()):
        self.requests = []
        self.responses = list(responses)

    def __call__(self, request):
        self.requests.append(request)
        status, reason = self.responses.pop(0) if self.responses else (
            200, None)
        body = json.dumps({'reason': reason}) if reason else b''
        return httpx.Response(status, content=body,
                              headers={'apns-id': 'id-1'})


def make_client(stub):
    pt = apns.ProviderToken(KEY_PEM, 'K', 'T')
    return apns.APNsClient(pt, 'de.nano-wetter.app',
                           transport=httpx.MockTransport(stub))


def test_routes_by_environment_and_sets_headers():
    stub = StubAPNs()
    client = make_client(stub)

    async def run():
        await client.send('aa11', 'sandbox', {'aps': {}})
        await client.send('bb22', 'production', {'aps': {}},
                          push_type='liveactivity', priority=5)
    asyncio.run(run())
    a, b = stub.requests
    assert str(a.url) == 'https://api.sandbox.push.apple.com/3/device/aa11'
    assert a.headers['apns-topic'] == 'de.nano-wetter.app'
    assert a.headers['apns-push-type'] == 'alert'
    assert a.headers['authorization'].startswith('bearer ')
    assert str(b.url) == 'https://api.push.apple.com/3/device/bb22'
    assert b.headers['apns-topic'] == (
        'de.nano-wetter.app.push-type.liveactivity')
    assert b.headers['apns-priority'] == '5'


def test_expired_provider_token_is_retried_once():
    stub = StubAPNs([(403, 'ExpiredProviderToken'), (200, None)])
    client = make_client(stub)
    client.provider_token._issued_at -= 30 * 60
    result = asyncio.run(client.send('aa', 'sandbox', {}))
    assert result.ok
    assert len(stub.requests) == 2


def test_result_classification():
    assert apns.Result(410, 'Unregistered').token_dead
    assert apns.Result(400, 'BadDeviceToken').token_dead
    assert not apns.Result(400, 'DeviceTokenNotForTopic').token_dead
    assert apns.Result(429, 'TooManyRequests').retryable
    assert apns.Result(0, 'ConnectError').retryable
    assert not apns.Result(400, 'BadDeviceToken').retryable


@pytest.fixture
def conn(db):
    """An asyncpg connection on the test database, run synchronously."""
    from brightsky.push import store

    class Runner:
        def __init__(self):
            self.loop = asyncio.new_event_loop()

        def __call__(self, coro_fn):
            async def wrapped():
                async with store.pool(max_size=1) as pool:
                    async with pool.acquire() as c:
                        return await coro_fn(c)
            return self.loop.run_until_complete(wrapped())
    runner = Runner()
    yield runner
    runner.loop.close()
    with db.cursor() as cur:
        cur.execute('DELETE FROM push.devices; '
                    'DELETE FROM push.notifications_sent; '
                    'DELETE FROM push.source_status;')
    db.commit()


def add_device(db, device_id='00000000-0000-0000-0000-000000000001'):
    db.insert('push.devices', [{
        'id': device_id, 'secret_hash': 'x', 'apns_token': 'aa',
        'environment': 'sandbox', 'last_seen': '2026-09-23T00:00:00Z'}])
    return device_id


NOW = datetime.datetime(2026, 9, 23, 12, tzinfo=datetime.timezone.utc)


async def _no_sleep(_):
    pass


def test_deliver_records_and_marks_apns(conn, db):
    from brightsky.push import sender
    device_id = add_device(db)
    client = make_client(StubAPNs())
    result = conn(lambda c: sender.deliver(
        c, client, device_id=device_id, environment='sandbox', token='aa',
        token_field='apns_token', payload={}, now=NOW))
    assert result.ok
    rows = db.fetch('SELECT apns_status, apns_id FROM push.notifications_sent')
    assert [tuple(r) for r in rows] == [(200, 'id-1')]
    assert db.fetch("SELECT source FROM push.source_status")[0][0] == 'apns'


def test_unregistered_token_deletes_device(conn, db):
    from brightsky.push import sender
    device_id = add_device(db)
    client = make_client(StubAPNs([(410, 'Unregistered')]))
    conn(lambda c: sender.deliver(
        c, client, device_id=device_id, environment='sandbox', token='aa',
        token_field='apns_token', payload={}, now=NOW))
    assert db.fetch('SELECT * FROM push.devices') == []


def test_dead_push_to_start_token_only_forgets_that_token(conn, db):
    from brightsky.push import sender
    device_id = add_device(db)
    client = make_client(StubAPNs([(400, 'BadDeviceToken')]))
    conn(lambda c: sender.deliver(
        c, client, device_id=device_id, environment='sandbox', token='aa',
        token_field='push_to_start_token', payload={},
        push_type='liveactivity', now=NOW))
    assert len(db.fetch('SELECT * FROM push.devices')) == 1


def test_retries_with_backoff(conn, db):
    from brightsky.push import sender
    device_id = add_device(db)
    stub = StubAPNs([(503, 'ServiceUnavailable'), (429, 'TooManyRequests'),
                     (200, None)])
    client = make_client(stub)
    result = conn(lambda c: sender.deliver(
        c, client, device_id=device_id, environment='sandbox', token='aa',
        token_field='apns_token', payload={}, now=NOW, sleep=_no_sleep))
    assert result.ok
    assert len(stub.requests) == 3


def test_content_state_uses_swift_dates():
    t = datetime.datetime(2001, 1, 1, 0, 1, tzinfo=datetime.timezone.utc)
    content = payloads.rain_content(
        state='coming', change_at=t, detail='d', bucket_start=t,
        buckets=[0.0], place_name='P', generated_at=t)
    assert content == {
        'phase': {'rain': {'_0': {
            'state': 'coming', 'changeAt': 60.0, 'detail': 'd',
            'bucketStart': 60.0, 'buckets': [0.0]}}},
        'placeName': 'P', 'generatedAt': 60.0,
    }
    start = payloads.live_start(content, now=t, stale=t, alert_title='a',
                                alert_body='b')
    assert start['aps']['attributes-type'] == 'WeatherLiveAttributes'
    assert start['aps']['event'] == 'start'
