"""Database access for schema `push` (asyncpg)."""

import contextlib
import hashlib
import json
import secrets
import uuid

import asyncpg

from brightsky.push import rules as rulemod
from brightsky.settings import settings


async def _init_connection(conn):
    for name in ('json', 'jsonb'):
        await conn.set_type_codec(
            name, encoder=json.dumps, decoder=json.loads,
            schema='pg_catalog')


@contextlib.asynccontextmanager
async def pool(min_size=1, max_size=4):
    async with asyncpg.create_pool(
        dsn=settings.DATABASE_URL, min_size=min_size, max_size=max_size,
        init=_init_connection,
    ) as p:
        yield p


def new_secret():
    return secrets.token_urlsafe(32)


def hash_secret(secret):
    # Secrets are 256 random bits, so a plain digest is enough — there is
    # nothing to brute-force that a slow hash would protect.
    return hashlib.sha256(secret.encode()).hexdigest()


def secret_matches(secret, secret_hash):
    return secrets.compare_digest(hash_secret(secret), secret_hash)


class AuthError(Exception):
    pass


class UnknownDevice(Exception):
    pass


async def authenticate(conn, device_id, bearer):
    """For every endpoint but registration: 404 unknown, 401 wrong."""
    row = await conn.fetchrow(
        'SELECT secret_hash FROM push.devices WHERE id = $1', device_id)
    if row is None:
        raise UnknownDevice()
    if bearer is None or not secret_matches(bearer, row['secret_hash']):
        raise AuthError()


async def register(conn, device, bearer, now):
    """Upsert the device and replace its rule set.

    Returns (accepted ids, rejections, new secret or None, event) where
    event is 'created' | 'adopted' | 'taken_over' | 'updated'.

    Authentication (agreed with the app, see docs/nano/push.md):
    no bearer → create, or take over a known id with a fresh secret;
    bearer on an unknown id → adopt it (the server lost the device);
    bearer on a known id must match, otherwise AuthError (401).
    """
    async with conn.transaction():
        row = await conn.fetchrow(
            'SELECT secret_hash FROM push.devices WHERE id = $1 FOR UPDATE',
            device['deviceId'])
        new_secret_value = None
        if bearer is None:
            new_secret_value = new_secret()
            secret_hash = hash_secret(new_secret_value)
            event = 'created' if row is None else 'taken_over'
        elif row is None:
            secret_hash = hash_secret(bearer)
            event = 'adopted'
        elif secret_matches(bearer, row['secret_hash']):
            secret_hash = row['secret_hash']
            event = 'updated'
        else:
            raise AuthError()

        await conn.execute(
            """
            INSERT INTO push.devices (
              id, secret_hash, apns_token, push_to_start_token,
              live_activities_enabled, environment, tier, app_version,
              last_seen)
            VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
            ON CONFLICT (id) DO UPDATE SET
              secret_hash = excluded.secret_hash,
              apns_token = excluded.apns_token,
              push_to_start_token = excluded.push_to_start_token,
              live_activities_enabled = excluded.live_activities_enabled,
              environment = excluded.environment,
              tier = excluded.tier,
              app_version = excluded.app_version,
              last_seen = excluded.last_seen
            """,
            device['deviceId'], secret_hash, device.get('apnsToken'),
            device.get('pushToStartToken'),
            device.get('liveActivitiesEnabled', True),
            device['environment'], device.get('tier') or 'free',
            device.get('appVersion'), now)

        accepted, rejected, parsed = [], [], []
        seen = set()
        for raw in device['rules']:
            rule_id = _rule_id(raw)
            if rule_id is None:
                continue
            if rule_id in seen:
                rejected.append((rule_id, 'duplicate_id'))
                continue
            seen.add(rule_id)
            try:
                rule = rulemod.parse_rule({**raw, 'id': rule_id})
            except rulemod.Rejected as e:
                rejected.append((rule_id, e.reason))
                continue
            if len(parsed) >= settings.PUSH_MAX_RULES_PER_DEVICE:
                rejected.append((rule_id, 'device_limit'))
                continue
            parsed.append((rule, raw))

        # A rule id belongs to the device that registered it first.
        foreign = {
            str(r['id']) for r in await conn.fetch(
                'SELECT id FROM push.rules '
                'WHERE id = ANY($1::uuid[]) AND device_id <> $2',
                [r.id for r, _ in parsed], device['deviceId'])
        }
        keep = []
        for rule, raw in parsed:
            if rule.id in foreign:
                rejected.append((rule.id, 'duplicate_id'))
                continue
            accepted.append(rule.id)
            # A once-window that is over is accepted and dropped (§6).
            if not rulemod.is_expired(rule.window, now):
                keep.append((rule, raw))

        await conn.execute(
            'DELETE FROM push.rules '
            'WHERE device_id = $1 AND NOT (id = ANY($2::uuid[]))',
            device['deviceId'], [r.id for r, _ in keep])
        for position, (rule, raw) in enumerate(keep):
            lat, lon = rule.lat_lon
            await conn.execute(
                'INSERT INTO push.cells (cell_key, lat, lon) '
                'VALUES ($1, $2, $3) ON CONFLICT DO NOTHING',
                rule.cell_key, lat, lon)
            previous = await conn.fetchrow(
                'SELECT kind, cell_key, params FROM push.rules WHERE id = $1',
                rule.id)
            await conn.execute(
                """
                INSERT INTO push.rules (
                  id, device_id, kind, cell_key, params, schedule, live,
                  position)
                VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
                ON CONFLICT (id) DO UPDATE SET
                  kind = excluded.kind,
                  cell_key = excluded.cell_key,
                  params = excluded.params,
                  schedule = excluded.schedule,
                  live = excluded.live,
                  position = excluded.position,
                  enabled = true
                """,
                rule.id, device['deviceId'], rule.kind, rule.cell_key,
                rule.raw_params, rule.schedule, rule.live, position)
            # An edited rule is a new rule: it re-arms (rules design §3).
            if previous is not None and (
                    previous['kind'], previous['cell_key'],
                    _semantic(previous['params'])) != (
                    rule.kind, rule.cell_key, _semantic(rule.raw_params)):
                await conn.execute(
                    'DELETE FROM push.rule_state WHERE rule_id = $1',
                    rule.id)
    return accepted, rejected, new_secret_value, event


def _semantic(params):
    return {k: v for k, v in params.items() if k != 'ruleId'}


def _rule_id(raw):
    try:
        return str(uuid.UUID(str(raw['id'])))
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


async def delete_device(conn, device_id):
    await conn.execute('DELETE FROM push.devices WHERE id = $1', device_id)


async def report_activity_token(conn, device_id, activity_id, token, now):
    await conn.execute(
        """
        INSERT INTO push.live_activities (
          device_id, activity_id, phase, activity_token, started_at,
          last_content)
        VALUES ($1, $2, 'app', $3, $4, '{}'::jsonb)
        ON CONFLICT (device_id) DO UPDATE SET
          activity_id = excluded.activity_id,
          activity_token = excluded.activity_token,
          ended_at = NULL
        """,
        device_id, activity_id, token, now)


async def end_activity(conn, device_id, activity_id, now, cooldown):
    """The user dismissed the activity: stop updating it, start the
    cooldown. An `activityId` for an older activity changes nothing."""
    await conn.execute(
        """
        UPDATE push.live_activities SET
          activity_token = NULL,
          ended_at = $3,
          cooldown_until = $4
        WHERE device_id = $1
          AND (activity_id IS NULL OR activity_id = $2)
        """,
        device_id, activity_id, now, now + cooldown)


async def mark_source(conn, source, now, error=None):
    if error is None:
        await conn.execute(
            """
            INSERT INTO push.source_status (source, last_success,
                                            last_attempt, last_error)
            VALUES ($1, $2, $2, NULL)
            ON CONFLICT (source) DO UPDATE SET
              last_success = $2, last_attempt = $2, last_error = NULL
            """, source, now)
    else:
        await conn.execute(
            """
            INSERT INTO push.source_status (source, last_attempt, last_error)
            VALUES ($1, $2, $3)
            ON CONFLICT (source) DO UPDATE SET
              last_attempt = $2, last_error = $3
            """, source, now, error[:500])
