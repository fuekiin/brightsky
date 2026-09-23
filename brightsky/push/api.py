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
from pydantic import BaseModel, field_validator

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


app = FastAPI(
    lifespan=lifespan,
    title='nano push',
    version=brightsky.__version__,
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
    default_response_class=ORJSONResponse,
)


class DeviceIn(BaseModel):
    deviceId: uuid.UUID
    apnsToken: str | None = None
    pushToStartToken: str | None = None
    liveActivitiesEnabled: bool = True
    environment: Literal['sandbox', 'production']
    appVersion: str | None = None
    tier: str = 'free'
    # Validated one by one in store.register, so one bad rule never
    # rejects the device.
    rules: list[Any] = []

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
    activityId: str
    pushToken: str

    @field_validator('pushToken')
    @classmethod
    def hex_token(cls, v):
        v = v.lower()
        if not HEX_TOKEN_RE.match(v):
            raise ValueError('expected a hex push token')
        return v


class ActivityEndIn(BaseModel):
    activityId: str


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
    if secret is None:
        ip = request.client.host if request.client else 'unknown'
        if not rate_limiter.allow(ip, settings.PUSH_REGISTER_RATE_LIMIT):
            logger.warning('Registration rate limit tripped for %s', ip)
            raise HTTPException(429, 'too many registrations')
    payload = device.model_dump()
    payload['deviceId'] = str(device.deviceId)
    async with ctx['pool'].acquire() as conn:
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


@app.get('/health')
async def health():
    now = utcnow()
    try:
        async with ctx['pool'].acquire() as conn:
            rows = await conn.fetch('SELECT * FROM push.source_status')
            devices = await conn.fetchval('SELECT count(*) FROM push.devices')
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
            'lastError': r and r['last_error'],
        }
    return {
        'status': 'ok',
        'version': brightsky.__version__,
        'database': True,
        'devices': devices,
        'sources': sources,
    }
