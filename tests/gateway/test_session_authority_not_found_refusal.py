"""A repeating preclaim ``not_found`` settles that head instead of pausing its FIFO for good."""
import asyncio
import threading

import pytest

from hermes_state_runtime import RuntimeStoreError, list_session_admissions
from tests.gateway.test_session_authority_cancel_settlement import ACTOR, REF, _authority, _submit


@pytest.mark.asyncio
async def test_repeated_not_found_head_settles_with_its_refusal_and_the_successor_runs(tmp_path, monkeypatch):
    from gateway import session_api_turn, session_finite
    from gateway.config import Platform

    db, authority = _authority(tmp_path, monkeypatch, platform=Platform.API_SERVER)
    executed = []

    def check_api_turn(authority, ref, payload):
        if payload['text'] == 'ghost':
            raise RuntimeStoreError('not_found')  # its target is gone, on every drain
    monkeypatch.setattr(session_api_turn, 'check_api_turn', check_api_turn)

    async def execute(authority, ref, row):
        executed.append(row['payload']['text'])
        return 'done'
    monkeypatch.setattr(session_finite, 'execute_finite_admission', execute)

    with db:
        authority._schedule = lambda ref: None
        head = await _submit(authority, 'ghost')
        await _submit(authority, 'follower')
        waiter = authority.waiters.setdefault(head.admission_id, asyncio.get_running_loop().create_future())
        del authority._schedule
        authority._schedule(REF)
        await asyncio.wait_for(authority.sessions['s'].task, 5)

        assert executed == ['follower']
        statuses = {r['request_id']: (r['status'], r['outcome'])
                    for r in list_session_admissions(db, session_id='s', pending_only=False)}
        assert statuses == {'ghost': ('terminal', 'cancelled'), 'follower': ('terminal', 'completed')}
        assert waiter.exception().reason == 'not_found'


@pytest.mark.asyncio
async def test_first_not_found_then_a_cancel_advanced_fifo_retries_instead_of_refusing(tmp_path, monkeypatch):
    """The head a not_found struck once may be cancelled before the drain re-reads it: the
    successor the cancel advanced to gets its OWN first strike retried, never refused on the
    cancelled head's identity."""
    from gateway import session_finite, session_hosted_transport, session_local_recovery
    from gateway.config import Platform

    db, authority = _authority(tmp_path, monkeypatch, platform=Platform.LOCAL)
    monkeypatch.setattr(session_local_recovery, 'restore_local_session', lambda authority, sid: None)
    monkeypatch.setattr(session_local_recovery, 'reopen_local_session', lambda authority, ref: None)
    executed, checks = [], []
    loop = asyncio.get_running_loop()
    struck, head_cancelled = asyncio.Event(), threading.Event()

    async def cancel_ghost(admission_id):
        await struck.wait()
        await authority.cancel_queued(ACTOR, REF, admission_id)
        head_cancelled.set()

    def check_remote_hosted_admission(authority, ref, row):
        checks.append(row['request_id'])
        if row['request_id'] == 'hosted:A':
            if checks.count('hosted:A') == 1:
                loop.call_soon_threadsafe(struck.set)
                raise RuntimeStoreError('not_found')
            # The cancel commits while this worker thread waits; the claim re-read then finds A gone.
            assert head_cancelled.wait(5), 'the struck head cancel never committed'
            return True
        if checks.count('hosted:B') == 1:
            raise RuntimeStoreError('not_found')  # B's own first strike: a retry, never a refusal
        return True
    monkeypatch.setattr(session_hosted_transport, 'check_remote_hosted_admission', check_remote_hosted_admission)

    async def execute(authority, ref, row):
        executed.append(row['request_id'])
        return 'done'
    monkeypatch.setattr(session_finite, 'execute_finite_admission', execute)

    with db:
        schedule, authority._schedule = authority._schedule, lambda ref: None
        head = await _submit(authority, 'hosted:A')
        await _submit(authority, 'hosted:B')
        authority._schedule = schedule
        cancel = loop.create_task(cancel_ghost(head.admission_id))
        schedule(REF)
        await asyncio.wait_for(cancel, 5)
        await asyncio.wait_for(authority.sessions['s'].task, 5)

        assert executed == ['hosted:B']
        statuses = {r['request_id']: (r['status'], r['outcome'])
                    for r in list_session_admissions(db, session_id='s', pending_only=False)}
        assert statuses == {'hosted:A': ('terminal', 'cancelled'), 'hosted:B': ('terminal', 'completed')}
        assert checks == ['hosted:A', 'hosted:A', 'hosted:B', 'hosted:B']


@pytest.mark.asyncio
async def test_admission_conflict_still_settles_only_after_two_reads_of_the_same_head(tmp_path, monkeypatch):
    from gateway import session_api_turn, session_finite
    from gateway.config import Platform

    db, authority = _authority(tmp_path, monkeypatch, platform=Platform.API_SERVER)
    executed = []

    def check_api_turn(authority, ref, payload):
        if payload['text'] == 'stale':
            raise RuntimeStoreError('admission_conflict')  # its route moved under it, on every drain
    monkeypatch.setattr(session_api_turn, 'check_api_turn', check_api_turn)

    async def execute(authority, ref, row):
        executed.append(row['payload']['text'])
        return 'done'
    monkeypatch.setattr(session_finite, 'execute_finite_admission', execute)

    with db:
        authority._schedule = lambda ref: None
        head = await _submit(authority, 'stale')
        await _submit(authority, 'follower')
        waiter = authority.waiters.setdefault(head.admission_id, asyncio.get_running_loop().create_future())
        del authority._schedule
        authority._schedule(REF)
        await asyncio.wait_for(authority.sessions['s'].task, 5)

        assert executed == ['follower']
        statuses = {r['request_id']: (r['status'], r['outcome'])
                    for r in list_session_admissions(db, session_id='s', pending_only=False)}
        assert statuses == {'stale': ('terminal', 'cancelled'), 'follower': ('terminal', 'completed')}
        assert waiter.exception().reason == 'admission_conflict'


@pytest.mark.asyncio
async def test_a_not_found_after_an_admission_conflict_still_gets_its_own_two_reads(tmp_path, monkeypatch):
    """The two reasons track their struck heads separately: one admission_conflict on a head
    must not make that head's FIRST not_found a refusal (the reasons must not cross-cancel
    each other's head identity) — only its own second not_found settles it."""
    from gateway import session_api_turn, session_finite
    from gateway.config import Platform

    db, authority = _authority(tmp_path, monkeypatch, platform=Platform.API_SERVER)
    executed, checks = [], []

    def check_api_turn(authority, ref, payload):
        if payload['text'] != 'once-each':
            return
        checks.append(1)
        if len(checks) == 1:
            raise RuntimeStoreError('admission_conflict')
        if len(checks) == 2:
            raise RuntimeStoreError('not_found')
    monkeypatch.setattr(session_api_turn, 'check_api_turn', check_api_turn)

    async def execute(authority, ref, row):
        executed.append(row['payload']['text'])
        return 'done'
    monkeypatch.setattr(session_finite, 'execute_finite_admission', execute)

    with db:
        authority._schedule = lambda ref: None
        await _submit(authority, 'once-each')
        await _submit(authority, 'follower')
        del authority._schedule
        authority._schedule(REF)
        await asyncio.wait_for(authority.sessions['s'].task, 5)

        assert executed == ['once-each', 'follower']
        statuses = {r['request_id']: (r['status'], r['outcome'])
                    for r in list_session_admissions(db, session_id='s', pending_only=False)}
        assert statuses == {'once-each': ('terminal', 'completed'), 'follower': ('terminal', 'completed')}
