"""Database access for schema `push` (asyncpg)."""

import contextlib
import datetime
import hashlib
import re
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


MIN_ADOPT_SECRET = 32


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


async def device_exists(conn, device_id):
    return await conn.fetchval(
        'SELECT true FROM push.devices WHERE id = $1', device_id) or False


# A rule the app stops sending is disabled, not deleted: it keeps its state,
# so a registration that briefly lacks it (an app launch race, 2026-09-26)
# cannot make it report the same occasion again. `worker.cleanup_tick`
# deletes it after `DISABLED_RETENTION`.
#
# Back after longer than this, its running events re-arm: whether rain
# stopped or a rolling window turned false meanwhile was not watched.
REARM_AFTER = datetime.timedelta(hours=6)
RUNNING_EVENTS = ['rain', 'rolling']
# Disabled rules kept per device; beyond, the oldest go at once.
MAX_DISABLED_PER_DEVICE = settings.PUSH_MAX_RULES_PER_DEVICE


async def register(conn, device, bearer, now):
    """Upsert the device and replace its rule set.

    Returns (accepted ids, rejections, new secret or None, event) where
    event is 'created' | 'adopted' | 'taken_over' | 'updated'.

    Authentication (agreed with the app, see docs/nano/push.md):
    no bearer → create, or take over a known id with a fresh secret;
    bearer on an unknown id → adopt it (the server lost the device);
    bearer on a known id must match, otherwise AuthError (401).
    """
    device_id = device['deviceId']
    async with conn.transaction():
        new_secret_value, secret_hash, event = await _authorize(
            conn, device_id, bearer)
        await _upsert_device(conn, device, secret_hash, now)
        parsed, rejected = _parse_rules(device['rules'])
        keep, accepted, refused = await _admit(conn, device_id, parsed, now)
        rejected += refused
        keep_ids = [r.id for r in keep]
        await _retire_missing(conn, device_id, keep_ids, now)
        # Rules another device dropped move here, without their state.
        await conn.execute(
            'DELETE FROM push.rules '
            'WHERE id = ANY($1::uuid[]) AND device_id <> $2 AND NOT enabled',
            keep_ids, device_id)
        for position, rule in enumerate(keep):
            if not await _upsert_rule(conn, device_id, rule, position, now):
                # Another device registered the id first, concurrently
                accepted.remove(rule.id)
                rejected.append((rule.id, 'duplicate_id'))
    return accepted, rejected, new_secret_value, event


async def _authorize(conn, device_id, bearer):
    """→ (new secret or None, secret hash to store, event)."""
    row = await conn.fetchrow(
        'SELECT secret_hash FROM push.devices WHERE id = $1 FOR UPDATE',
        device_id)
    if bearer is None:
        secret = new_secret()
        return (secret, hash_secret(secret),
                'created' if row is None else 'taken_over')
    if row is None:
        # Adopting is only for secrets this server once issued (256 random
        # bits); anything shorter is refused, and the app's retry without
        # it registers afresh.
        if len(bearer) < MIN_ADOPT_SECRET:
            raise AuthError()
        return None, hash_secret(bearer), 'adopted'
    if secret_matches(bearer, row['secret_hash']):
        return None, row['secret_hash'], 'updated'
    raise AuthError()


async def _upsert_device(conn, device, secret_hash, now):
    await conn.execute(
        """
        INSERT INTO push.devices (
          id, secret_hash, apns_token, push_to_start_token,
          live_activities_enabled, environment, tier, app_version,
          last_seen)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
        ON CONFLICT (id) DO UPDATE SET
          secret_hash = excluded.secret_hash,
          -- A registration without a token keeps the stored one: the app
          -- registers at launch before iOS hands its tokens out again. A
          -- dead token is cleared by the sender (forget_token), not here.
          -- Not across environments: a sandbox token is dead in
          -- production (a tester moving from a dev build to TestFlight).
          apns_token = CASE
            WHEN excluded.environment = push.devices.environment
            THEN COALESCE(excluded.apns_token, push.devices.apns_token)
            ELSE excluded.apns_token END,
          push_to_start_token = CASE
            WHEN excluded.environment = push.devices.environment
            THEN COALESCE(excluded.push_to_start_token,
                          push.devices.push_to_start_token)
            ELSE excluded.push_to_start_token END,
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


def _parse_rules(raws):
    """→ ([Rule], [(rule id, reason)]), in the order the app sent them."""
    parsed, rejected, seen = [], [], set()
    for raw in raws:
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
        parsed.append(rule)
    return parsed, rejected


async def _admit(conn, device_id, parsed, now):
    """The rules this registration may keep → (rules to store, accepted
    ids, rejections)."""
    # A rule id belongs to the device that registered it first, while that
    # device still has it; one it dropped (disabled) may move.
    foreign = {str(r['id']) for r in await conn.fetch(
        'SELECT id FROM push.rules '
        'WHERE id = ANY($1::uuid[]) AND device_id <> $2 AND enabled',
        [r.id for r in parsed], device_id)}
    # A global ceiling on distinct cells: upstream load scales with cells,
    # not users (design §2). It counts the cells in use after this
    # registration — the other devices' rules plus this set — so a place
    # can be swapped at the ceiling, and a cell the device already uses
    # never costs. A rule disabled within REARM_AFTER still holds its
    # place (it may only have been missing from one registration); one
    # disabled longer holds none.
    held = '(enabled OR disabled_at > $2)'
    cells = list({r.cell_key for r in parsed})
    others = {r['cell_key'] for r in await conn.fetch(
        f'SELECT DISTINCT cell_key FROM push.rules WHERE {held} '
        'AND device_id <> $1 AND cell_key = ANY($3)',
        device_id, now - REARM_AFTER, cells)}
    own = {r['cell_key'] for r in await conn.fetch(
        f'SELECT DISTINCT cell_key FROM push.rules WHERE {held} '
        'AND device_id = $1', device_id, now - REARM_AFTER)}
    in_use = others | (own & set(cells))
    # Every cell in use has a row, so the rows bound the count from above:
    # the exact count (tens of ms at 200k rules) runs only when this set's
    # new cells may not fit.
    free_cells = settings.PUSH_MAX_CELLS - await conn.fetchval(
        'SELECT count(*) FROM push.cells')
    if free_cells < len(set(cells) - in_use):
        free_cells = settings.PUSH_MAX_CELLS - len(in_use - others) - (
            await conn.fetchval(
                f'SELECT count(DISTINCT cell_key) FROM push.rules '
                f'WHERE {held} AND device_id <> $1',
                device_id, now - REARM_AFTER))
    keep, accepted, rejected = [], [], []
    for rule in parsed:
        if rule.id in foreign:
            rejected.append((rule.id, 'duplicate_id'))
            continue
        if rule.cell_key not in in_use:
            if free_cells <= 0:
                rejected.append((rule.id, 'capacity'))
                continue
            free_cells -= 1
            in_use.add(rule.cell_key)
        accepted.append(rule.id)
        # A once-window that is over is accepted and dropped (§6).
        if not rulemod.is_expired(rule.window, now):
            keep.append(rule)
    return keep, accepted, rejected


async def _retire_missing(conn, device_id, keep_ids, now):
    """Disable the device's rules that are not in `keep_ids`."""
    missing = 'device_id = $1 AND NOT (id = ANY($2::uuid[]))'
    # Nothing to remember — no state, no running activity: gone at once,
    # so re-registering is no way to pile up rows.
    await conn.execute(
        f"""
        DELETE FROM push.rules r WHERE {missing}
          AND NOT EXISTS (SELECT 1 FROM push.rule_state s
                          WHERE s.rule_id = r.id)
          AND NOT EXISTS (SELECT 1 FROM push.live_activities la
                          WHERE la.rule_id = r.id AND la.ended_at IS NULL)
        """, device_id, keep_ids)
    await conn.execute(
        f'UPDATE push.rules SET enabled = false, disabled_at = $3 '
        f'WHERE {missing} AND enabled', device_id, keep_ids, now)
    await conn.execute(
        """
        DELETE FROM push.rules WHERE id IN (
          SELECT id FROM push.rules WHERE device_id = $1 AND NOT enabled
            AND NOT (id = ANY($3::uuid[]))     -- coming back right now
          ORDER BY disabled_at DESC, id OFFSET $2)
        """, device_id, MAX_DISABLED_PER_DEVICE, keep_ids)


async def _upsert_rule(conn, device_id, rule, position, now):
    """Returns False when the id belongs to another device."""
    lat, lon = rule.lat_lon
    await conn.execute(
        'INSERT INTO push.cells (cell_key, lat, lon) '
        'VALUES ($1, $2, $3) ON CONFLICT DO NOTHING',
        rule.cell_key, lat, lon)
    previous = await conn.fetchrow(
        'SELECT kind, params, enabled, disabled_at FROM push.rules '
        'WHERE id = $1 AND device_id = $2', rule.id, device_id)
    status = await conn.execute(
        """
        INSERT INTO push.rules (
          id, device_id, kind, cell_key, params, schedule, live, position)
        VALUES ($1, $2, $3, $4, $5, $6, $7, $8)
        ON CONFLICT (id) DO UPDATE SET
          kind = excluded.kind,
          cell_key = excluded.cell_key,
          params = excluded.params,
          schedule = excluded.schedule,
          live = excluded.live,
          position = excluded.position,
          enabled = true,
          disabled_at = NULL
        -- Never another device's rule (a concurrent registration took it)
        WHERE push.rules.device_id = excluded.device_id
        """,
        rule.id, device_id, rule.kind, rule.cell_key, rule.raw_params,
        rule.schedule, rule.live, position)
    if status == 'INSERT 0 0':
        return False
    if previous is None:
        return True
    # An edited rule is a new rule: it re-arms (rules design §3). A new
    # cell is not an edit — rules at „Mein Standort" move with the phone
    # and must not report again in every cell.
    if (previous['kind'], _semantic(previous['params'])) != (
            rule.kind, _semantic(rule.raw_params)):
        await conn.execute(
            'DELETE FROM push.rule_state WHERE rule_id = $1', rule.id)
    elif (not previous['enabled'] and previous['disabled_at'] is not None
            and now - previous['disabled_at'] >= REARM_AFTER):
        await conn.execute(
            'DELETE FROM push.rule_state '
            'WHERE rule_id = $1 AND occurrence_key = ANY($2)',
            rule.id, RUNNING_EVENTS)
    return True


# Not what a rule asks for, so no edit: the app's own id, and where the rule
# is registered from (the app started sending `origin` on user rules too).
_NOT_SEMANTIC = {'ruleId', 'origin'}


def _semantic(params):
    return {k: v for k, v in params.items() if k not in _NOT_SEMANTIC}


def _rule_id(raw):
    try:
        return str(uuid.UUID(str(raw['id'])))
    except (KeyError, TypeError, ValueError, AttributeError):
        return None


async def delete_device(conn, device_id):
    await conn.execute('DELETE FROM push.devices WHERE id = $1', device_id)


async def report_activity_token(conn, device_id, activity_id, token, now):
    """The app reports an activity's push token, and again on rotation.

    A server-started activity has no id until this report, so the first
    report names it. A report for another id replaces the row only when no
    activity is running — a late token from an older activity must not
    take over a newer one.
    """
    await conn.execute(
        """
        INSERT INTO push.live_activities (
          device_id, activity_id, phase, activity_token, started_at,
          last_content)
        VALUES ($1, $2, 'app', $3, $4, '{}'::jsonb)
        ON CONFLICT (device_id) DO UPDATE SET
          activity_id = excluded.activity_id,
          activity_token = excluded.activity_token
        WHERE push.live_activities.activity_id IS NULL
           OR push.live_activities.activity_id = excluded.activity_id
           OR push.live_activities.ended_at IS NOT NULL
        """,
        device_id, activity_id, token, now)


async def end_activity(conn, device_id, activity_id, now, cooldown):
    """The user dismissed the activity: stop updating it, start the
    cooldown, and remember it so the same event does not come back unless
    it escalates. Only the named activity — a late DELETE for an older one
    must not end a newer one. A server-started activity whose token the
    app has not reported yet has no id here; a DELETE naming an unknown id
    then ends that one (the only running activity without an id) and
    records the id."""
    status = await conn.execute(
        _END_ACTIVITY + 'WHERE device_id = $1 AND activity_id = $2 '
        'AND ended_at IS NULL', device_id, activity_id, now, now + cooldown)
    if status == 'UPDATE 0':
        await conn.execute(
            _END_ACTIVITY + 'WHERE device_id = $1 AND activity_id IS NULL '
            'AND ended_at IS NULL', device_id, activity_id, now,
            now + cooldown)


_END_ACTIVITY = """
    UPDATE push.live_activities SET
      activity_id = $2,
      activity_token = NULL,
      ended_at = $3,
      cooldown_until = $4,
      state = state || '{"dismissed": true}'::jsonb
"""


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
            """, source, now, re.sub(r'https?://\S+', '<url>', error)[:500])
