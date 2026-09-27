"""One test per finding of the 2026-09-27 review of nano-push (numbers as in
docs/nano/push.md, „Second review"): rules missing from a registration are
disabled, not deleted, and what that touches."""

import asyncio
import datetime

import httpx
import pytest

from brightsky.push import livectl, sources, store
from brightsky.settings import settings

from .test_push_apns import StubAPNs
from .test_push_worker import (
    CELL, DEVICE, NOW, RAIN_RULE, WARN_CELL, WARN_RULE, WARN_RULE_2,
    add_alert, bodies, ntick, push_db, radar, rain_rule, register_live,
    report_token, run, tick, warning_rule,
)


__all__ = ['push_db']   # the fixture, re-exported for pytest

M = datetime.timedelta(minutes=1)
H = datetime.timedelta(hours=1)
OTHER = '00000000-0000-0000-0000-00000000000e'
BERLIN = '52.52,13.41'
MUNICH = '48.14,11.58'
LIVE = {'night': True}


def reg(rules, device=DEVICE, at=NOW, environment='sandbox',
        apns='ab' * 32, push_to_start='cd' * 32):
    """`store.register` → (accepted, rejected, secret, event)."""
    async def fn(worker, pool):
        async with pool.acquire() as conn:
            result = await store.register(conn, {
                'deviceId': device, 'apnsToken': apns,
                'pushToStartToken': push_to_start,
                'environment': environment, 'liveActivitiesEnabled': True,
                'rules': rules}, None, at)
            await conn.execute(
                'UPDATE push.cells SET warn_cell_id = $1, resolved_at = $2',
                WARN_CELL, NOW)
            return result
    return fn


def add_state(push_db, rule_id, key='k', state='{}'):
    push_db.insert('push.rule_state', [{
        'rule_id': rule_id, 'occurrence_key': key, 'state': state}])


def rules(push_db):
    return [(str(r[0]), str(r[1]), r[2]) for r in push_db.fetch(
        'SELECT id, device_id, enabled FROM push.rules ORDER BY id')]


def events(stub):
    return [p['aps'].get('event', 'alert') for _, _, p in bodies(stub)]


# MARK: - #1 the launch race and Live Activities

def test_1_live_warning_does_not_start_again_after_a_missing_rule(
        push_db, monkeypatch):
    stub = StubAPNs()
    live_rule = warning_rule(WARN_RULE, live=LIVE)
    run(push_db, register_live([live_rule]), stub, monkeypatch)
    add_alert(push_db, 'A', 'moderate')
    run(push_db, tick(NOW), stub, monkeypatch)
    report_token(push_db)
    run(push_db, register_live([]), stub, monkeypatch)     # the race
    run(push_db, tick(NOW + M), stub, monkeypatch)
    assert events(stub) == ['start', 'end']
    run(push_db, register_live([live_rule]), stub, monkeypatch)
    run(push_db, tick(NOW + 2 * M), stub, monkeypatch)
    assert events(stub) == ['start', 'end']       # no start, no notification
    add_alert(push_db, 'B', 'severe')               # escalation still starts
    run(push_db, tick(NOW + 3 * M), stub, monkeypatch)
    assert events(stub)[-1] == 'start'


def test_1_another_live_warning_rule_starts_at_once(push_db, monkeypatch):
    stub = StubAPNs()
    run(push_db, register_live([warning_rule(WARN_RULE, live=LIVE)]), stub,
        monkeypatch)
    add_alert(push_db, 'A', 'moderate')
    run(push_db, tick(NOW), stub, monkeypatch)
    report_token(push_db)
    run(push_db, register_live([]), stub, monkeypatch)
    run(push_db, tick(NOW + M), stub, monkeypatch)
    # The user made a new rule for it: that one is not held back.
    run(push_db, register_live([warning_rule(WARN_RULE_2, live=LIVE)]),
        stub, monkeypatch)
    run(push_db, tick(NOW + 2 * M), stub, monkeypatch)
    assert events(stub) == ['start', 'end', 'start']


def test_1_live_rain_does_not_start_again_after_a_missing_rule(
        push_db, monkeypatch):
    stub = StubAPNs()
    run(push_db, register_live([rain_rule()]), stub, monkeypatch)
    radar(monkeypatch, [0.2] * 24)
    run(push_db, ntick(NOW), stub, monkeypatch)
    report_token(push_db)
    run(push_db, register_live([]), stub, monkeypatch)
    run(push_db, ntick(NOW + 5 * M), stub, monkeypatch)
    assert events(stub) == ['start', 'end']
    run(push_db, register_live([rain_rule()]), stub, monkeypatch)
    run(push_db, ntick(NOW + 10 * M), stub, monkeypatch)
    assert events(stub) == ['start', 'end']       # no start, no notification


def test_1_another_rain_rule_starts_at_once(push_db, monkeypatch):
    stub = StubAPNs()
    run(push_db, register_live([rain_rule()]), stub, monkeypatch)
    radar(monkeypatch, [0.2] * 24)
    run(push_db, ntick(NOW), stub, monkeypatch)
    report_token(push_db)
    run(push_db, register_live([]), stub, monkeypatch)
    run(push_db, ntick(NOW + 5 * M), stub, monkeypatch)
    other = dict(rain_rule(), id=WARN_RULE_2)
    run(push_db, register_live([other]), stub, monkeypatch)
    run(push_db, ntick(NOW + 10 * M), stub, monkeypatch)
    assert events(stub) == ['start', 'end', 'start']


# MARK: - #2 disabled rules cannot pile up

def test_2_a_missing_rule_without_state_is_deleted_at_once(
        push_db, monkeypatch):
    run(push_db, reg([warning_rule(WARN_RULE)]), StubAPNs(), monkeypatch)
    run(push_db, reg([]), StubAPNs(), monkeypatch)
    assert rules(push_db) == []


def test_2_disabled_rules_per_device_are_capped(push_db, monkeypatch):
    monkeypatch.setattr(store, 'MAX_DISABLED_PER_DEVICE', 2)
    ids = [f'00000000-0000-0000-0000-00000000010{i}' for i in range(4)]
    run(push_db, reg([warning_rule(i) for i in ids]), StubAPNs(),
        monkeypatch)
    for i in ids:
        add_state(push_db, i)
    run(push_db, reg([]), StubAPNs(), monkeypatch)
    assert rules(push_db) == [(i, DEVICE, False) for i in ids[:2]]


# MARK: - #3 the cell cap counts enabled rules

def test_3_a_place_can_be_replaced_at_the_cap(push_db, monkeypatch):
    monkeypatch.setitem(settings, 'PUSH_MAX_CELLS', 2)
    run(push_db, reg([warning_rule(WARN_RULE)]), StubAPNs(), monkeypatch)
    add_state(push_db, WARN_RULE)
    moved = dict(warning_rule(WARN_RULE_2), cellKey=BERLIN)
    accepted, rejected, _, _ = run(push_db, reg([moved]), StubAPNs(),
                                   monkeypatch)
    assert (accepted, rejected) == ([WARN_RULE_2], [])
    # The disabled rule holds its place for REARM_AFTER, then no more.
    munich = dict(warning_rule(RAIN_RULE), cellKey=MUNICH)
    _, rejected, _, _ = run(push_db, reg([munich], device=OTHER),
                            StubAPNs(), monkeypatch)
    assert rejected == [(RAIN_RULE, 'capacity')]
    accepted, _, _, _ = run(push_db, reg([munich], device=OTHER,
                                         at=NOW + 7 * H), StubAPNs(),
                            monkeypatch)
    assert accepted == [RAIN_RULE]


def test_3_a_device_keeps_its_place_when_others_fill_the_cap(
        push_db, monkeypatch):
    run(push_db, reg([warning_rule(WARN_RULE)]), StubAPNs(), monkeypatch)
    monkeypatch.setitem(settings, 'PUSH_MAX_CELLS', 1)
    run(push_db, reg([dict(warning_rule(RAIN_RULE), cellKey=BERLIN)],
                     device=OTHER), StubAPNs(), monkeypatch)
    accepted, rejected, _, _ = run(push_db, reg([warning_rule(WARN_RULE)]),
                                   StubAPNs(), monkeypatch)
    assert (accepted, rejected) == ([WARN_RULE], [])


# MARK: - #4 a disabled rule's activity ends, attached or not

def test_4_an_activity_on_a_disabled_rule_ends(push_db, monkeypatch):
    stub = StubAPNs()
    run(push_db, register_live([rain_rule()]), stub, monkeypatch)
    radar(monkeypatch, [0.2] * 24)
    run(push_db, ntick(NOW), stub, monkeypatch)
    report_token(push_db)
    # What an update racing the registration leaves: the rule disabled,
    # the activity still naming it.
    push_db.fetch('UPDATE push.rules SET enabled = false, disabled_at = now() '
                  'RETURNING id')
    run(push_db, ntick(NOW + 5 * M), stub, monkeypatch)
    assert events(stub) == ['start', 'end']
    run(push_db, ntick(NOW + 10 * M), stub, monkeypatch)
    assert events(stub) == ['start', 'end']


# MARK: - #5 a failed listing check is not staleness yet

def test_5_a_failed_listing_check_is_tolerated_for_a_while(
        push_db, monkeypatch):
    up = {'yes': True}

    async def in_sync(self, conn):
        if up['yes']:
            return True
        raise httpx.ConnectTimeout('opendata.dwd.de')
    monkeypatch.setattr(sources.WarningsSource, 'in_sync', in_sync)
    source = sources.WarningsSource(None)

    async def main():
        async with store.pool(max_size=1) as pool:
            async with pool.acquire() as conn:
                await source.refresh(conn, NOW)
                up['yes'] = False
                await source.refresh(conn, NOW + 9 * M)
                with pytest.raises(sources.Stale):
                    await source.refresh(conn, NOW + 11 * M)
    asyncio.run(main())


# MARK: - #6 back after hours, running events re-arm

@pytest.mark.parametrize('away, key, left', [
    (H, 'rain', ['2026-09-23', 'rain']),
    (7 * H, 'rain', ['2026-09-23']),
    (H, 'rolling', ['2026-09-23', 'rolling']),
    (7 * H, 'rolling', ['2026-09-23']),
])
def test_6_running_events_rearm_after_a_long_absence(
        push_db, monkeypatch, away, key, left):
    rule = rain_rule(live_=False)
    run(push_db, reg([rule]), StubAPNs(), monkeypatch)
    add_state(push_db, RAIN_RULE, key, '{"armed": false}')
    add_state(push_db, RAIN_RULE, '2026-09-23')
    run(push_db, reg([]), StubAPNs(), monkeypatch)
    run(push_db, reg([rule], at=NOW + away), StubAPNs(), monkeypatch)
    assert [r[0] for r in push_db.fetch(
        'SELECT occurrence_key FROM push.rule_state ORDER BY 1')] == left


# MARK: - #7 tokens are not kept across environments

def test_7_a_new_environment_does_not_keep_the_old_tokens(
        push_db, monkeypatch):
    run(push_db, reg([]), StubAPNs(), monkeypatch)
    run(push_db, reg([], environment='production', apns=None,
                     push_to_start=None), StubAPNs(), monkeypatch)
    assert push_db.fetch(
        'SELECT apns_token, push_to_start_token, environment '
        'FROM push.devices') == [[None, None, 'production']]


# MARK: - #8 a dropped rule id may move

def test_8_a_rule_dropped_by_one_device_moves_to_another(
        push_db, monkeypatch):
    run(push_db, reg([warning_rule(WARN_RULE)], device=OTHER), StubAPNs(),
        monkeypatch)
    add_state(push_db, WARN_RULE)
    _, rejected, _, _ = run(push_db, reg([warning_rule(WARN_RULE)]),
                            StubAPNs(), monkeypatch)
    assert rejected == [(WARN_RULE, 'duplicate_id')]
    run(push_db, reg([], device=OTHER), StubAPNs(), monkeypatch)
    accepted, _, _, _ = run(push_db, reg([warning_rule(WARN_RULE)]),
                            StubAPNs(), monkeypatch)
    assert accepted == [WARN_RULE]
    assert rules(push_db) == [(WARN_RULE, DEVICE, True)]
    assert push_db.fetch('SELECT * FROM push.rule_state') == []


# MARK: - #10 a lock handed to a waiter is kept

def test_10_a_lock_handed_to_a_waiter_is_not_pruned():
    async def main():
        lock = livectl.lock('device')
        await lock.acquire()
        waiter = asyncio.create_task(lock.acquire())
        await asyncio.sleep(0)
        lock.release()                  # handed over, the waiter not run
        assert not lock.locked()
        livectl.prune_locks()
        assert livectl.lock('device') is lock
        await waiter
        lock.release()
        livectl.prune_locks()
        assert 'device' not in livectl.LOCKS
    asyncio.run(main())


# MARK: - B1 the purge is cheap: the foreign keys have indexes

def test_b1_foreign_keys_have_indexes(push_db):
    indexes = {r[0] for r in push_db.fetch(
        "SELECT indexdef FROM pg_indexes WHERE schemaname = 'push'")}
    assert any('live_activities USING btree (rule_id)' in i for i in indexes)
    assert any('rules USING btree (cell_key)' in i for i in indexes)


# MARK: - B2 the digest paces its forecasts; B3 warnings by warn cell

DIGEST = {'at': '07:00', 'tz': 'Europe/Berlin'}


def digest_rule(rule_id, cell):
    return {'id': rule_id, 'kind': 'user_rule', 'cellKey': cell,
            'schedule': DIGEST,
            'params': {'all': [{'metric': 'temp', 'cmp': 'gt', 'value': 20}],
                       'window': {'days': 'today', 'part': 'night'}}}


def test_b2_digest_forecasts_are_paced_and_a_failure_skips_only_its_cell(
        push_db, monkeypatch):
    from brightsky.push import berlin, evaluator as ev
    run(push_db, reg([digest_rule(WARN_RULE, CELL),
                      digest_rule(WARN_RULE_2, BERLIN)]), StubAPNs(),
        monkeypatch)
    night = datetime.datetime(2026, 9, 23, 23, tzinfo=berlin.TZ)

    async def fetch(self, cell_key, lat, lon, now):
        if cell_key == BERLIN:
            raise httpx.ReadTimeout('web')
        self.hours[cell_key] = [ev.Hour(night, temperature=21)]
        self.fetched_at[cell_key] = now
        return self.hours[cell_key]
    monkeypatch.setattr(sources.ForecastSource, 'fetch', fetch)
    sleeps = []

    async def dtick(worker, pool):
        async def sleep(s):
            sleeps.append(s)
        worker.sleep = sleep
        return await worker.digest_tick(
            datetime.datetime(2026, 9, 23, 7, 5, tzinfo=berlin.TZ))
    stub = StubAPNs()
    run(push_db, dtick, stub, monkeypatch)
    assert sleeps == [1.0, 1.0]
    [(_, _, payload)] = bodies(stub)
    assert payload['nano']['ruleIds'] == [WARN_RULE]


def test_b3_warning_rules_are_loaded_by_warn_cell(push_db, monkeypatch):
    stub = StubAPNs()
    run(push_db, reg([warning_rule(WARN_RULE)]), stub, monkeypatch)
    # No warning anywhere: nothing loaded, nothing evaluated.
    assert run(push_db, tick(NOW), stub, monkeypatch) == 0
    add_alert(push_db, 'A', 'moderate')
    assert run(push_db, tick(NOW + M), stub, monkeypatch) == 1
    assert events(stub) == ['alert']


# MARK: - Second pass (the double-check of the fixes above)

def test_2_the_oldest_disabled_rules_go_first(push_db, monkeypatch):
    monkeypatch.setattr(store, 'MAX_DISABLED_PER_DEVICE', 1)
    old, new = WARN_RULE, WARN_RULE_2
    run(push_db, reg([warning_rule(old), warning_rule(new)]), StubAPNs(),
        monkeypatch)
    add_state(push_db, old)
    add_state(push_db, new)
    run(push_db, reg([warning_rule(new)]), StubAPNs(), monkeypatch)
    run(push_db, reg([], at=NOW + M), StubAPNs(), monkeypatch)
    assert rules(push_db) == [(new, DEVICE, False)]


def test_2_returning_rules_are_not_capped_away(push_db, monkeypatch):
    # a is disabled, then b is dropped as a returns. Counting a as
    # disabled, the cap would delete it (the oldest) and a would come back
    # without its state; it only counts rules that stay disabled.
    monkeypatch.setattr(store, 'MAX_DISABLED_PER_DEVICE', 1)
    a, b = warning_rule(WARN_RULE), warning_rule(WARN_RULE_2)
    run(push_db, reg([a]), StubAPNs(), monkeypatch)
    add_state(push_db, WARN_RULE)
    run(push_db, reg([b]), StubAPNs(), monkeypatch)
    add_state(push_db, WARN_RULE_2)
    run(push_db, reg([a], at=NOW + M), StubAPNs(), monkeypatch)
    assert rules(push_db) == [(WARN_RULE, DEVICE, True),
                              (WARN_RULE_2, DEVICE, False)]
    assert sorted(str(r[0]) for r in push_db.fetch(
        'SELECT rule_id FROM push.rule_state')) == [WARN_RULE, WARN_RULE_2]


def test_3_a_rule_missing_once_keeps_its_place_at_the_cap(
        push_db, monkeypatch):
    run(push_db, reg([warning_rule(WARN_RULE)]), StubAPNs(), monkeypatch)
    add_state(push_db, WARN_RULE)
    monkeypatch.setitem(settings, 'PUSH_MAX_CELLS', 1)
    run(push_db, reg([]), StubAPNs(), monkeypatch)             # the race
    _, rejected, _, _ = run(
        push_db, reg([dict(warning_rule(RAIN_RULE), cellKey=BERLIN)],
                     device=OTHER), StubAPNs(), monkeypatch)
    assert rejected == [(RAIN_RULE, 'capacity')]
    accepted, rejected, _, _ = run(
        push_db, reg([warning_rule(WARN_RULE)], at=NOW + M), StubAPNs(),
        monkeypatch)
    assert (accepted, rejected) == ([WARN_RULE], [])


def test_4_a_rule_another_device_owns_is_not_overwritten(
        push_db, monkeypatch):
    from brightsky.push.rules import parse_rule
    run(push_db, reg([warning_rule(WARN_RULE)], device=OTHER), StubAPNs(),
        monkeypatch)
    run(push_db, reg([]), StubAPNs(), monkeypatch)

    async def race(worker, pool):
        # What a registration that passed its check before the other
        # device committed does next
        async with pool.acquire() as conn:
            rule = parse_rule(dict(warning_rule(WARN_RULE, level=3)))
            return await store._upsert_rule(conn, DEVICE, rule, 0, NOW)
    assert run(push_db, race, StubAPNs(), monkeypatch) is False
    assert rules(push_db) == [(WARN_RULE, OTHER, True)]
    assert push_db.fetch('SELECT params FROM push.rules')[0][0][
        'all'][0]['warning']['minLevel'] == 2


def test_5_a_live_warning_rule_swapped_in_one_registration(
        push_db, monkeypatch):
    stub = StubAPNs()
    run(push_db, register_live([warning_rule(WARN_RULE, live=LIVE)]), stub,
        monkeypatch)
    add_alert(push_db, 'A', 'moderate')
    run(push_db, tick(NOW), stub, monkeypatch)
    report_token(push_db)
    run(push_db, register_live([warning_rule(WARN_RULE_2, live=LIVE)]),
        stub, monkeypatch)
    run(push_db, tick(NOW + M), stub, monkeypatch)
    run(push_db, tick(NOW + 2 * M), stub, monkeypatch)
    # The new rule takes the running event over: no notification, no end
    assert events(stub) == ['start']


def test_5_a_swapped_rule_with_another_warning_starts_in_the_same_tick(
        push_db, monkeypatch):
    stub = StubAPNs()
    run(push_db, register_live([warning_rule(WARN_RULE, live=LIVE)]), stub,
        monkeypatch)
    add_alert(push_db, 'A', 'moderate')
    run(push_db, tick(NOW), stub, monkeypatch)
    report_token(push_db)
    # The new rule wants severe warnings only, and one has just come in
    run(push_db, register_live([warning_rule(WARN_RULE_2, level=3,
                                             live=LIVE)]), stub, monkeypatch)
    add_alert(push_db, 'B', 'severe', onset=NOW + 2 * H)
    run(push_db, tick(NOW + M), stub, monkeypatch)
    run(push_db, tick(NOW + 2 * M), stub, monkeypatch)
    assert events(stub) == ['start', 'end', 'start']   # no notification


def test_5_a_live_rain_rule_swapped_in_one_registration(
        push_db, monkeypatch):
    stub = StubAPNs()
    run(push_db, register_live([rain_rule()]), stub, monkeypatch)
    radar(monkeypatch, [0.2] * 24)
    run(push_db, ntick(NOW), stub, monkeypatch)
    report_token(push_db)
    run(push_db, register_live([dict(rain_rule(), id=WARN_RULE_2)]), stub,
        monkeypatch)
    run(push_db, ntick(NOW + 5 * M), stub, monkeypatch)
    run(push_db, ntick(NOW + 10 * M), stub, monkeypatch)
    # Ended and started again in one tick: no notification in between,
    # no second start later
    assert events(stub) == ['start', 'end', 'start']


def test_6_the_digest_checks_warnings_after_its_fetches_and_drops_rules_gone_meanwhile(  # noqa: E501
        push_db, monkeypatch):
    from brightsky.push import berlin, evaluator as ev
    warn = dict(warning_rule(WARN_RULE_2), schedule=DIGEST)
    run(push_db, reg([digest_rule(WARN_RULE, CELL), warn,
                      digest_rule(RAIN_RULE, BERLIN)]), StubAPNs(),
        monkeypatch)
    for rule_id in (WARN_RULE, WARN_RULE_2, RAIN_RULE):
        add_state(push_db, rule_id, 'old')    # kept when disabled
    night = datetime.datetime(2026, 9, 23, 23, tzinfo=berlin.TZ)
    order = []

    async def fetch(self, cell_key, lat, lon, now):
        order.append('fetch')
        if cell_key == BERLIN:     # a registration drops this one meanwhile
            push_db.fetch("UPDATE push.rules SET enabled = false "
                          f"WHERE id = '{RAIN_RULE}' RETURNING id")
        self.hours[cell_key] = [ev.Hour(night, temperature=21)]
        self.fetched_at[cell_key] = now
        return self.hours[cell_key]
    monkeypatch.setattr(sources.ForecastSource, 'fetch', fetch)

    async def dtick(worker, pool):
        async def refresh(conn, now):
            order.append('refresh')
            return sources.WarningsObservation(fetched_at=now)
        worker.warnings.refresh = refresh
        return await worker.digest_tick(
            datetime.datetime(2026, 9, 23, 7, 5, tzinfo=berlin.TZ))
    stub = StubAPNs()
    run(push_db, dtick, stub, monkeypatch)
    assert order == ['fetch', 'fetch', 'refresh']
    [(_, _, payload)] = bodies(stub)
    assert payload['nano']['ruleIds'] == [WARN_RULE]


def test_8_the_rule_carrying_a_warning_activity_owns_it(
        push_db, monkeypatch):
    stub = StubAPNs()
    first = warning_rule(WARN_RULE, live=LIVE)
    second = warning_rule(WARN_RULE_2, live=LIVE)
    run(push_db, register_live([first, second]), stub, monkeypatch)
    add_alert(push_db, 'A', 'moderate')
    run(push_db, tick(NOW), stub, monkeypatch)
    report_token(push_db)
    run(push_db, register_live([second]), stub, monkeypatch)
    run(push_db, tick(NOW + M), stub, monkeypatch)
    assert events(stub) == ['start']
    assert [str(r[0]) for r in push_db.fetch(
        'SELECT rule_id FROM push.live_activities')] == [WARN_RULE_2]
