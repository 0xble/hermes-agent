"""Slow admission receipts must not hold the tick lock or queue a pass every tick."""
from concurrent.futures import Future
from contextlib import asynccontextmanager
from contextvars import copy_context
import json
import threading

import pytest

from cron import executions, jobs, scheduler, scheduler_authority as authority
from hermes_constants import (get_hermes_home, hermes_home_key, reset_hermes_home_override,
                              set_hermes_home_override)
from tests.cron.test_cron_recover_pause import _foreign_journal
from tests.waiting import wait_until


@pytest.fixture(autouse=True)
def recovery_lane(monkeypatch):
    monkeypatch.setattr(authority, '_recovery_pool', None)
    monkeypatch.setattr(authority, '_recovery_futures', {})
    yield
    if authority._recovery_pool is not None:
        authority._recovery_pool.shutdown(wait=True)


def _request(home, *, allow_connect=True):
    token = set_hermes_home_override(home)
    try:
        authority.reconcile_pending_background(allow_connect=allow_connect)
        return authority._recovery_futures[hermes_home_key(home)]
    finally:
        reset_hermes_home_override(token)


def _quiet_tick(monkeypatch):
    monkeypatch.setattr(scheduler, '_should_yield_tick_to_fresh_gateway', lambda: None)
    for name in ('_maybe_reap_dead_owners', '_maybe_run_worktree_maintenance', '_sweep_mcp_orphans'):
        monkeypatch.setattr(scheduler, name, lambda: None)
    monkeypatch.setattr(scheduler, '_sweep_stale_inflight_for_tick', lambda due: None)
    from cron import bot_chat_legacy, scheduler_admission, store_health
    from agent import estop
    monkeypatch.setattr(bot_chat_legacy, 'drain_legacy_pending', lambda: None)
    monkeypatch.setattr(scheduler_admission, 'note_due_scan', lambda started: None)
    monkeypatch.setattr(store_health, 'recheck_idle', lambda: None)
    monkeypatch.setattr(estop, 'check_paused', lambda *args: False)


def test_blocking_receipt_cannot_delay_due_scan_or_repeat_ticks(tmp_path, monkeypatch):
    from gateway import session_cron
    from hermes_cli import gateway_client

    home = tmp_path / 'home'
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setattr(session_cron, 'owner_for_home', lambda home: None)
    _quiet_tick(monkeypatch)
    entered, release, scanned = threading.Event(), threading.Event(), threading.Event()
    calls = []

    class Peer:
        async def rpc(self, method, **params):
            calls.append(method)
            entered.set()
            assert release.wait(60), 'test did not release receipt recovery'
            return {'status': 'terminal', 'job': job, 'result': [True, 'document', 'answer', None]}

    @asynccontextmanager
    async def connected():
        yield Peer()

    monkeypatch.setattr(gateway_client, 'connect_gateway', connected)

    def due_scan():
        scanned.set()
        return []

    monkeypatch.setattr(scheduler, 'get_due_jobs', due_scan)
    with jobs.use_cron_store(home):
        job = jobs.create_job(prompt='report', schedule='every 1h', deliver='local')
        fire = executions.create_execution(job['id'], source='test')['id']
        executions.mark_execution_running(fire)
        journal = _foreign_journal(job, fire)
        original = journal.read_bytes()
        tick_result = Future()

        def tick_once():
            try:
                tick_result.set_result(scheduler.tick(verbose=False, sync=False))
            except BaseException as exc:
                tick_result.set_exception(exc)

        ticker = threading.Thread(target=copy_context().run, args=(tick_once,), daemon=True)
        ticker.start()
        try:
            assert entered.wait(5), 'receipt recovery did not start'
            assert scanned.wait(3), 'blocked recovery prevented the due scan'
            assert tick_result.result(timeout=3) == 0
            pending = authority._recovery_futures[hermes_home_key(home)]
            for _ in range(12):
                scanned.clear()
                assert scheduler.tick(verbose=False, sync=False) == 0
                assert scanned.is_set()
                assert authority._recovery_futures[hermes_home_key(home)] is pending
            assert calls == ['cron.recover']
            assert not pending.done()
            assert journal.read_bytes() == original
            assert executions.get_execution(fire)['status'] == 'running'
        finally:
            release.set()
            ticker.join(5)
        assert not ticker.is_alive()
        pending.result(timeout=5)
        assert not journal.exists()
        assert executions.get_execution(fire)['status'] == 'completed'
        assert jobs.get_job(job['id'])['last_status'] == 'ok'


def test_one_worker_coalesces_each_home_and_preserves_profile_context(tmp_path, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    calls = []
    home_a, home_b = tmp_path / 'a', tmp_path / 'b'

    def recover(*, allow_connect=True):
        calls.append((get_hermes_home(), allow_connect, threading.get_ident()))
        if get_hermes_home() == home_a:
            entered.set()
            assert release.wait(60)

    monkeypatch.setattr(authority, 'reconcile_pending', recover)
    first = _request(home_a, allow_connect=False)
    try:
        assert entered.wait(5)
        second = _request(home_b)
        for _ in range(12):
            assert _request(home_a, allow_connect=False) is first
            assert _request(home_b) is second
        assert len(authority._recovery_futures) == 2
        assert len(calls) == 1
        assert not second.done()
    finally:
        release.set()
    first.result(timeout=5)
    second.result(timeout=5)
    assert [(home, connect) for home, connect, _ in calls] == [(home_a, False), (home_b, True)]
    assert len({worker for _, _, worker in calls}) == 1


def test_failed_background_pass_is_observed_and_journal_retried(tmp_path, monkeypatch, caplog):
    home = tmp_path / 'home'
    journal = home / 'cron' / 'admissions' / 'pending.json'
    journal.parent.mkdir(parents=True)
    journal.write_text(json.dumps({'receipt': {'admission_id': 'keep-me'}}))
    original = journal.read_bytes()
    attempts = []

    def recover(*, allow_connect=True):
        attempts.append(get_hermes_home())
        if len(attempts) == 1:
            raise RuntimeError('owner storage unavailable')
        assert journal.read_bytes() == original

    monkeypatch.setattr(authority, 'reconcile_pending', recover)
    first = _request(home)
    with pytest.raises(RuntimeError, match='owner storage unavailable'):
        first.result(timeout=5)
    wait_until(lambda: 'Cron receipt recovery pass deferred' in caplog.text, what='recovery failure log')
    assert 'owner storage unavailable' in caplog.text
    assert journal.read_bytes() == original
    second = _request(home)
    assert second is not first
    second.result(timeout=5)
    assert attempts == [home, home]
    assert journal.read_bytes() == original


def test_headless_background_recovery_never_connects(tmp_path, monkeypatch):
    from gateway import session_cron
    from hermes_cli import gateway_client
    from cron import scheduler_tick

    home = tmp_path / 'home'
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setattr(session_cron, 'owner_for_home', lambda home: None)
    _quiet_tick(monkeypatch)

    def forbidden():
        pytest.fail('headless recovery attempted a gateway connection')

    monkeypatch.setattr(gateway_client, 'connect_gateway', forbidden)
    with jobs.use_cron_store(home):
        job = jobs.create_job(prompt='report', schedule='every 1h', deliver='local')
        fire = executions.create_execution(job['id'], source='test')['id']
        journal = _foreign_journal(job, fire)
        original = journal.read_bytes()
        scheduler_tick._pre_dispatch_housekeeping(scheduler, headless=True)
        authority._recovery_futures[hermes_home_key(home)].result(timeout=5)
        assert journal.read_bytes() == original
        assert executions.get_execution(fire)['status'] == 'claimed'


def test_background_recovery_keeps_live_execution_claim(tmp_path, monkeypatch):
    from gateway import session_cron
    from hermes_cli import gateway_client

    home = tmp_path / 'home'
    monkeypatch.setenv('HERMES_HOME', str(home))
    monkeypatch.setattr(session_cron, 'owner_for_home', lambda home: None)

    def forbidden():
        pytest.fail('recovery observed a fire still owned by its live firer')

    monkeypatch.setattr(gateway_client, 'connect_gateway', forbidden)
    with jobs.use_cron_store(home):
        job = jobs.create_job(prompt='report', schedule='every 1h', deliver='local')
        row = executions.create_execution(job['id'], source='test')
        fire = row['id']
        executions.mark_execution_running(fire)
        journal = authority.journal_path(job['id'], fire)
        journal.parent.mkdir(parents=True, exist_ok=True)
        journal.write_text(json.dumps({'params': {'job_id': job['id'], 'request_id': fire}, 'receipt': None}))
        original = journal.read_bytes()
        _request(home).result(timeout=5)
        assert journal.read_bytes() == original
        saved = executions.get_execution(fire)
        assert saved['status'] == 'running'
        assert saved['process_id'] == row['process_id']


@pytest.mark.platforms('posix')
def test_recovery_lock_preserves_cross_ticker_serialization(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    (home / 'cron').mkdir(parents=True)
    entered = []
    monkeypatch.setattr(authority, '_reconcile_pending', lambda **kwargs: entered.append(True))
    held = scheduler._acquire_tick_lock(home / 'cron' / '.receipt-recovery.lock')
    try:
        _request(home).result(timeout=5)
        assert entered == []
    finally:
        scheduler._release_tick_lock(held)
    _request(home).result(timeout=5)
    assert entered == [True]


@pytest.mark.parametrize('failure_point', ['create', 'start'])
@pytest.mark.parametrize('error', [OSError(11, 'Resource temporarily unavailable'),
                                 RuntimeError("can't start new thread")])
def test_recovery_launch_failure_keeps_due_scan_and_journal_retryable(
        tmp_path, monkeypatch, caplog, failure_point, error):
    home = tmp_path / 'home'
    monkeypatch.setenv('HERMES_HOME', str(home))
    _quiet_tick(monkeypatch)
    scanned = threading.Event()
    calls, pools = [], []
    journal = home / 'cron' / 'admissions' / 'pending.json'
    journal.parent.mkdir(parents=True)
    journal.write_text(json.dumps({'receipt': {'admission_id': 'keep-me'}}))
    original = journal.read_bytes()
    real_factory = authority.concurrent.futures.ThreadPoolExecutor
    real_start = threading.Thread.start

    def unavailable(*args, **kwargs):
        raise error

    def create_pool(*args, **kwargs):
        pool = real_factory(*args, **kwargs)
        pools.append(pool)
        return pool

    def due_scan():
        scanned.set()
        return []

    monkeypatch.setattr(scheduler, 'get_due_jobs', due_scan)
    monkeypatch.setattr(authority, 'reconcile_pending',
                        lambda **kwargs: calls.append(get_hermes_home()))
    monkeypatch.setattr(authority.concurrent.futures, 'ThreadPoolExecutor',
                        unavailable if failure_point == 'create' else create_pool)
    if failure_point == 'start':
        # Exercise real submit: the work item is queued before Thread.start fails.
        monkeypatch.setattr(threading.Thread, 'start', unavailable)
    with jobs.use_cron_store(home):
        assert scheduler.tick(verbose=False, sync=False) == 0
    assert scanned.is_set()
    assert 'Cron receipt recovery submission deferred' in caplog.text
    assert calls == []
    assert journal.read_bytes() == original
    assert authority._recovery_pool is None
    assert authority._recovery_futures == {}
    assert all(pool._shutdown for pool in pools)

    monkeypatch.setattr(threading.Thread, 'start', real_start)
    monkeypatch.setattr(authority.concurrent.futures, 'ThreadPoolExecutor', real_factory)
    _request(home).result(timeout=5)
    assert calls == [home]
    assert journal.read_bytes() == original


def test_failed_pool_drains_running_pass_before_replacement(tmp_path, monkeypatch):
    home_a, home_b = tmp_path / 'a', tmp_path / 'b'
    entered, release = threading.Event(), threading.Event()
    calls = []

    def recover(**kwargs):
        calls.append(get_hermes_home())
        if get_hermes_home() == home_a:
            entered.set()
            assert release.wait(60)

    monkeypatch.setattr(authority, 'reconcile_pending', recover)
    first = _request(home_a)
    try:
        assert entered.wait(5)
        failed_pool = authority._recovery_pool

        def unavailable(*args, **kwargs):
            raise OSError(11, 'Resource temporarily unavailable')

        monkeypatch.setattr(failed_pool, 'submit', unavailable)
        token = set_hermes_home_override(home_b)
        try:
            authority.reconcile_pending_background()
            assert authority._recovery_pool is None
            assert not first.done()
            for _ in range(12):
                authority.reconcile_pending_background()
                assert authority._recovery_pool is None
            assert calls == [home_a]
        finally:
            reset_hermes_home_override(token)
    finally:
        release.set()
    first.result(timeout=5)
    _request(home_b).result(timeout=5)
    assert calls == [home_a, home_b]


def test_current_arm_receipt_survives_concurrent_expiry_scan(tmp_path, monkeypatch):
    """A pending canonical result must not lose its job to partial-effects retirement.

    The replacement scheduler observes a dead owner's started one-shot off-tick.
    Until that exact receipt settles, expiry cannot delete its bookkeeping target.
    No payload or provider transport is invoked by this regression.
    """
    from datetime import UTC, datetime, timedelta

    from gateway import session_cron
    from hermes_cli import gateway_client

    home = tmp_path / 'home'
    monkeypatch.setenv('HERMES_HOME', str(home))
    clock = [datetime(2026, 11, 4, 12, tzinfo=UTC)]
    monkeypatch.setattr(jobs, '_hermes_now', lambda: clock[0])
    monkeypatch.setattr(session_cron, 'owner_for_home', lambda home: None)
    entered, release = threading.Event(), threading.Event()

    class Peer:
        async def rpc(self, method, **params):
            assert method == 'cron.recover'
            entered.set()
            assert release.wait(60), 'test did not release canonical receipt'
            return {'status': 'terminal', 'job': started,
                    'result': [True, 'captured document', 'captured answer', None]}

    @asynccontextmanager
    async def connected():
        yield Peer()

    monkeypatch.setattr(gateway_client, 'connect_gateway', connected)
    with jobs.use_cron_store(home):
        job = jobs.create_job(prompt='fixture', schedule='in 1m', deliver='local')
        clock[0] += timedelta(minutes=1)
        assert jobs.claim_dispatch(job['id'])
        started = jobs.get_job(job['id'])
        assert started is not None
        fire = executions.create_execution(job['id'], source='test',
                                            scheduled_instant=started['next_run_at'])['id']
        executions.mark_execution_running(fire)
        journal = _foreign_journal(started, fire)
        clock[0] += timedelta(minutes=10)
        future = _request(home)
        try:
            assert entered.wait(5), 'canonical observation did not start'
            assert jobs.get_due_jobs() == []
            scanned = jobs.get_job(job['id'])
            assert journal.exists()
        finally:
            release.set()
            future.result(timeout=10)
        completed = jobs.get_job(job['id'])
        result = executions.get_execution(fire)
        assert scanned is not None, 'expiry deleted the canonical receipt target'
        assert completed is not None and completed['state'] == 'completed'
        assert completed['canonical_completions'] == [fire]
        assert result is not None and result['status'] == 'completed'
        assert not journal.exists()
        assert jobs.get_due_jobs() == []
