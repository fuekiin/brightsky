"""APNs over HTTP/2 with an ES256 provider token (design §4.4).

One `.p8` key serves both environments. Device tokens are not portable
between them, so every send names the environment the device reported.
"""

import base64
import json
import logging
import time
from dataclasses import dataclass

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import (
    decode_dss_signature,
)


logger = logging.getLogger('brightsky.push.apns')

HOSTS = {
    'sandbox': 'https://api.sandbox.push.apple.com',
    'production': 'https://api.push.apple.com',
}
# Apple rejects tokens older than an hour and throttles regenerating one
# more often than every 20 minutes.
TOKEN_LIFETIME = 55 * 60
TOKEN_MIN_AGE = 20 * 60

# Reasons that mean the token will never work again. Not
# DeviceTokenNotForTopic: that is our misconfiguration, and deleting every
# device over it would turn a config typo into data loss.
DEAD_TOKEN_REASONS = {'BadDeviceToken', 'Unregistered'}


def _b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode()


class ProviderToken:

    def __init__(self, key_pem, key_id, team_id, clock=time.time):
        self.key = serialization.load_pem_private_key(key_pem, password=None)
        if not isinstance(self.key, ec.EllipticCurvePrivateKey):
            raise ValueError('APNs key must be an EC (P-256) key')
        self.key_id = key_id
        self.team_id = team_id
        self.clock = clock
        self._token = None
        self._issued_at = 0

    @classmethod
    def from_file(cls, path, key_id, team_id):
        with open(path, 'rb') as f:
            return cls(f.read(), key_id, team_id)

    def get(self):
        now = self.clock()
        if self._token is None or now - self._issued_at >= TOKEN_LIFETIME:
            self._issue(now)
        return self._token

    def invalidate(self):
        """After ExpiredProviderToken — but never faster than Apple
        allows, or sends fail with TooManyProviderTokenUpdates."""
        if self.clock() - self._issued_at >= TOKEN_MIN_AGE:
            self._token = None

    def _issue(self, now):
        header = {'alg': 'ES256', 'kid': self.key_id}
        claims = {'iss': self.team_id, 'iat': int(now)}
        signing_input = (
            _b64(json.dumps(header, separators=(',', ':')).encode()) + '.'
            + _b64(json.dumps(claims, separators=(',', ':')).encode()))
        der = self.key.sign(signing_input.encode(), ec.ECDSA(hashes.SHA256()))
        r, s = decode_dss_signature(der)
        signature = r.to_bytes(32, 'big') + s.to_bytes(32, 'big')
        self._token = signing_input + '.' + _b64(signature)
        self._issued_at = now


@dataclass
class Result:
    status: int
    reason: str | None = None
    apns_id: str | None = None

    @property
    def ok(self):
        return self.status == 200

    @property
    def token_dead(self):
        return self.status == 410 or self.reason in DEAD_TOKEN_REASONS

    @property
    def retryable(self):
        return self.status in (429, 500, 503) or self.status == 0


class APNsClient:

    def __init__(self, provider_token, topic, transport=None, timeout=15):
        self.provider_token = provider_token
        self.topic = topic
        self._clients = {}
        self._transport = transport
        self._timeout = timeout

    def _client(self, environment):
        if environment not in self._clients:
            kwargs = {'timeout': self._timeout}
            if self._transport is not None:
                kwargs['transport'] = self._transport
            else:
                kwargs['http2'] = True
            self._clients[environment] = httpx.AsyncClient(
                base_url=HOSTS[environment], **kwargs)
        return self._clients[environment]

    async def aclose(self):
        for c in self._clients.values():
            await c.aclose()
        self._clients.clear()

    async def send(self, device_token, environment, payload, *,
                   push_type='alert', priority=10, expiration=None,
                   collapse_id=None):
        """Send one push. Never raises for APNs or network errors — the
        result says what happened (status 0 = no response)."""
        topic = self.topic
        if push_type == 'liveactivity':
            topic += '.push-type.liveactivity'
        headers = {
            'apns-topic': topic,
            'apns-push-type': push_type,
            'apns-priority': str(priority),
        }
        if expiration is not None:
            headers['apns-expiration'] = str(int(expiration))
        if collapse_id:
            headers['apns-collapse-id'] = collapse_id[:64]
        body = json.dumps(payload, separators=(',', ':'),
                          ensure_ascii=False).encode()
        for attempt in (1, 2):
            headers['authorization'] = f'bearer {self.provider_token.get()}'
            try:
                resp = await self._client(environment).post(
                    f'/3/device/{device_token}', content=body,
                    headers=headers)
            except httpx.HTTPError as e:
                logger.warning('APNs %s unreachable: %r', environment, e)
                return Result(0, type(e).__name__)
            reason = None
            if resp.status_code != 200:
                try:
                    reason = resp.json().get('reason')
                except ValueError:
                    reason = resp.text[:100] or None
            result = Result(resp.status_code, reason,
                            resp.headers.get('apns-id'))
            if reason == 'ExpiredProviderToken' and attempt == 1:
                self.provider_token.invalidate()
                continue
            return result
