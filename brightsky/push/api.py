"""push-api: registration, activity tokens, health (docs/nano/push.md).

A separate FastAPI app from `brightsky.web`, served by its own container on
push.nano-wetter.de, so nothing here can reach the weather API's routers.
"""

import collections
import contextlib
import datetime
import logging
import re
import time
import uuid
from pathlib import Path
from typing import Any, Literal

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import ORJSONResponse, Response
from pydantic import BaseModel, Field, field_validator

import brightsky
from brightsky.push import store
from brightsky.settings import settings


logger = logging.getLogger('brightsky.push.api')

ctx = {}

HEX_TOKEN_RE = re.compile(r'^[0-9a-f]{16,512}$')
ACTIVITY_COOLDOWN = datetime.timedelta(minutes=60)


def utcnow():
    return datetime.datetime.now(datetime.timezone.utc)


@contextlib.asynccontextmanager
async def lifespan(app):
    async with store.pool() as pool:
        ctx['pool'] = pool
        yield
        del ctx['pool']


MAX_BODY = 256 * 1024


class BodyLimit:
    """413 for bodies over MAX_BODY, counted while reading, so a
    chunked body without Content-Length cannot get around it."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope['type'] != 'http':
            return await self.app(scope, receive, send)
        length = dict(scope['headers']).get(b'content-length')
        if length is not None:
            if not length.isdigit():
                return await _reply(send, 400, b'bad content-length')
            if int(length) > MAX_BODY:
                return await _too_large(send)
        seen = 0

        async def limited():
            nonlocal seen
            message = await receive()
            if message['type'] == 'http.request':
                seen += len(message.get('body', b''))
                if seen > MAX_BODY:
                    raise _TooLarge()
            return message
        try:
            await self.app(scope, limited, send)
        except _TooLarge:
            await _too_large(send)


class _TooLarge(Exception):
    pass


async def _too_large(send):
    await _reply(send, 413, b'body too large')


async def _reply(send, status, detail):
    await send({'type': 'http.response.start', 'status': status,
                'headers': [(b'content-type', b'application/json')]})
    await send({'type': 'http.response.body',
                'body': b'{"detail":"' + detail + b'"}'})


app = FastAPI(
    lifespan=lifespan,
    title='nano push',
    version=brightsky.__version__,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    default_response_class=ORJSONResponse,
)
app.add_middleware(BodyLimit)


class DeviceIn(BaseModel):
    deviceId: uuid.UUID
    apnsToken: str | None = None
    pushToStartToken: str | None = None
    liveActivitiesEnabled: bool = True
    environment: Literal['sandbox', 'production']
    appVersion: str | None = Field(default=None, max_length=32)
    tier: str = Field(default='free', max_length=16)
    # Validated one by one in store.register, so one bad rule never
    # rejects the device. The per-device ceiling (50) rejects rules; this
    # bound only keeps a hostile body from being parsed at all.
    rules: list[Any] = Field(default=[], max_length=200)

    @field_validator('apnsToken', 'pushToStartToken')
    @classmethod
    def hex_token(cls, v):
        if v is None:
            return v
        v = v.lower()
        if not HEX_TOKEN_RE.match(v):
            raise ValueError('expected a hex push token')
        return v


class ActivityTokenIn(BaseModel):
    activityId: str = Field(max_length=128)
    pushToken: str

    @field_validator('pushToken')
    @classmethod
    def hex_token(cls, v):
        v = v.lower()
        if not HEX_TOKEN_RE.match(v):
            raise ValueError('expected a hex push token')
        return v


class ActivityEndIn(BaseModel):
    activityId: str = Field(max_length=128)


def bearer(authorization):
    if not authorization:
        return None
    scheme, _, token = authorization.partition(' ')
    if scheme.lower() != 'bearer' or not token.strip():
        return None
    return token.strip()


class RateLimiter:
    """Sliding hour window per client IP, for unauthenticated registration.
    In memory: push-api runs as one process."""

    def __init__(self):
        self.hits = collections.defaultdict(collections.deque)

    def allow(self, key, limit, now=None):
        now = time.monotonic() if now is None else now
        q = self.hits[key]
        while q and q[0] <= now - 3600:
            q.popleft()
        if len(q) >= limit:
            return False
        q.append(now)
        if len(self.hits) > 50_000:
            self.hits = collections.defaultdict(
                collections.deque,
                {k: v for k, v in self.hits.items() if v})
        return True


rate_limiter = RateLimiter()


@contextlib.asynccontextmanager
async def authed(device_id, authorization):
    async with ctx['pool'].acquire() as conn:
        try:
            await store.authenticate(conn, device_id, bearer(authorization))
        except store.UnknownDevice:
            raise HTTPException(404, 'unknown device')
        except store.AuthError:
            raise HTTPException(401, 'bad device secret')
        yield conn


@app.post('/v1/devices')
async def register(
        device: DeviceIn, request: Request,
        authorization: str | None = Header(default=None)):
    secret = bearer(authorization)
    payload = device.model_dump()
    payload['deviceId'] = str(device.deviceId)
    async with ctx['pool'].acquire() as conn:
        # Every request that can create a device — a fresh registration,
        # a takeover, or adopting an unknown id with a bearer — counts
        # against the client's hourly allowance (review #5).
        if secret is None or not await store.device_exists(
                conn, payload['deviceId']):
            ip = request.client.host if request.client else 'unknown'
            if not rate_limiter.allow(ip, settings.PUSH_REGISTER_RATE_LIMIT):
                logger.warning('Registration rate limit tripped for %s', ip)
                raise HTTPException(429, 'too many registrations')
        try:
            accepted, rejected, new_secret, event = await store.register(
                conn, payload, secret, utcnow())
        except store.AuthError:
            raise HTTPException(401, 'bad device secret')
    if event != 'updated':
        logger.info('Device %s %s', device.deviceId, event)
    if rejected:
        logger.info('Device %s: rejected %s', device.deviceId, rejected)
    body = {
        'accepted': accepted,
        'rejected': [{'ruleId': r, 'reason': why} for r, why in rejected],
    }
    if new_secret is not None:
        body['deviceSecret'] = new_secret
    return body


@app.delete('/v1/devices/{device_id}', status_code=204)
async def delete_device(
        device_id: uuid.UUID,
        authorization: str | None = Header(default=None)):
    async with authed(str(device_id), authorization) as conn:
        await store.delete_device(conn, str(device_id))
    logger.info('Device %s deleted', device_id)


@app.put('/v1/devices/{device_id}/activity', status_code=204)
async def report_activity_token(
        device_id: uuid.UUID, body: ActivityTokenIn,
        authorization: str | None = Header(default=None)):
    async with authed(str(device_id), authorization) as conn:
        await store.report_activity_token(
            conn, str(device_id), body.activityId, body.pushToken, utcnow())
    logger.info('Device %s: activity token for %s', device_id,
                body.activityId)


@app.delete('/v1/devices/{device_id}/activity', status_code=204)
async def end_activity(
        device_id: uuid.UUID, body: ActivityEndIn,
        authorization: str | None = Header(default=None)):
    async with authed(str(device_id), authorization) as conn:
        await store.end_activity(
            conn, str(device_id), body.activityId, utcnow(),
            ACTIVITY_COOLDOWN)
    logger.info('Device %s: activity %s dismissed', device_id,
                body.activityId)


# The rule catalogue (backend §6, rules design §8). Owned by the app repo:
# WeatherGermany docs/push/catalog.json, generated from RuleCatalog.fallback
# and pinned by a WeatherCore test. Copied here verbatim — never edit it in
# this repo, copy it again.
CATALOG = (Path(__file__).parent / 'catalog.json').read_bytes()


@app.get('/v1/catalog')
async def catalog():
    return Response(
        CATALOG, media_type='application/json',
        headers={'Cache-Control': 'public, max-age=86400'})


# The loops push-work runs; an entry that never succeeded reports null age.
HEALTH_SOURCES = ('warnings', 'forecast', 'nowcast', 'digest', 'apns')


def _error_summary(error):
    """Class and message only — no URLs, hosts or query strings (a
    forecast URL names a cell)."""
    if not error:
        return None
    return re.sub(r'https?://\S+', '<url>', error)[:200]


@app.get('/health')
async def health():
    now = utcnow()
    try:
        async with ctx['pool'].acquire() as conn:
            rows = await conn.fetch('SELECT * FROM push.source_status')
            cells = await conn.fetchval('SELECT count(*) FROM push.cells')
    except Exception:
        logger.exception('Health check: database unreachable')
        return ORJSONResponse(
            {'status': 'error', 'database': False}, status_code=503)
    by_source = {r['source']: r for r in rows}
    sources = {}
    for name in HEALTH_SOURCES:
        r = by_source.get(name)
        last = r and r['last_success']
        sources[name] = {
            'lastSuccessAgeSeconds': (
                round((now - last).total_seconds()) if last else None),
            'lastError': _error_summary(r and r['last_error']),
        }
    warnings = []
    if cells >= 0.8 * settings.PUSH_MAX_CELLS:
        warnings.append('cells_near_capacity')
    return {
        'status': 'ok',
        'version': brightsky.__version__,
        'database': True,
        'sources': sources,
        'warnings': warnings,
    }
