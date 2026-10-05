"""Abort readback covers every shared dispatch fence and refuses draining owners."""
import threading
from types import SimpleNamespace

import pytest

from gateway.generation import GenerationCoordinator, GenerationIdentity
from gateway.run_generation import ActiveGeneration


@pytest.mark.asyncio
async def test_precommit_rearm_reads_poller_cron_kanban_and_goal_wakeup(tmp_path, monkeypatch):
    monkeypatch.setattr('gateway.generation._boot_id', lambda: 'fixture-boot')
    db = GenerationCoordinator(tmp_path)
    old = GenerationIdentity.create(release_sha='a', label='old')
    new = GenerationIdentity.create(release_sha='b', label='new')
    db.register(old, state='serving')
    db.register(new)
    epoch = db.acquire_lease('active_generation', old.id)
    poller = SimpleNamespace(running=False)
    adapter = SimpleNamespace(_controlled_poller=poller,
                              _controlled_journal=SimpleNamespace(token_hash='hash'))
    async def start(receipt):
        poller.running = True
    adapter.start_polling_from_transfer = start
    runner = SimpleNamespace(adapters={'telegram': adapter}, _overlap_draining=True)
    gate = lambda: not runner._overlap_draining
    runner._overlap_cron_start_kwargs = {'can_dispatch': gate}
    cron = SimpleNamespace(armed=False)
    def start_cron(*a, **kw):
        assert kw['can_dispatch']() is False
        cron.armed = True
    cron.start = start_cron
    active = ActiveGeneration(tmp_path, db, old, epoch)
    active.runner, active.cron_provider, active.cron_stop = runner, cron, threading.Event()
    active._external_cron_stopped = True
    active._stopped_receipts = [(adapter, {'token_hash': 'hash'})]
    db.request_transfer(old.id, new.id, epoch, {'hash'})
    nonce = db.transfer_attempt_nonce(old.id, epoch)
    assert db.abort_transfer(old.id, new.id, epoch, attempt_nonce=nonce)
    reply = await active.transfer_aborted(new.id, nonce)
    assert reply['rearmed'] and reply['generation_id'] == old.id
    assert reply['armed'] == dict.fromkeys(('poller', 'cron', 'kanban', 'goal_wakeup'), True)
    assert cron.armed and gate() and poller.running
    # A subsequent commit makes this process permanently ineligible for re-arm.
    db.request_transfer(old.id, new.id, epoch, {'hash'})
    db.record_poller_stopped(old.id, epoch, 'hash', 42)
    db.commit_transfer(old.id, new.id, epoch)
    with pytest.raises(RuntimeError, match='still-serving'):
        await active._rearm_stopped_pollers()


@pytest.mark.asyncio
async def test_readback_exposes_poller_that_failed_to_arm(tmp_path, monkeypatch):
    monkeypatch.setattr('gateway.generation._boot_id', lambda: 'fixture-boot')
    db = GenerationCoordinator(tmp_path)
    old = GenerationIdentity.create(release_sha='a', label='old')
    db.register(old, state='serving')
    epoch = db.acquire_lease('active_generation', old.id)
    active = ActiveGeneration(tmp_path, db, old, epoch)
    active.runner = SimpleNamespace(adapters={'telegram': SimpleNamespace(
        _controlled_journal=SimpleNamespace(token_hash='hash'), _controlled_poller=SimpleNamespace(running=False))},
        _overlap_draining=False)
    reply = active.rearm_status()
    assert reply['armed']['poller'] is False
    assert all(reply['armed'][key] for key in ('cron', 'kanban', 'goal_wakeup'))


def test_real_driver_breaks_promptly_on_committed_successor_death(tmp_path, monkeypatch):
    from gateway import run_generation
    monkeypatch.setattr('gateway.generation._boot_id', lambda: 'fixture-boot')
    dead = SimpleNamespace(value=False)
    monkeypatch.setattr('gateway.status._pid_exists', lambda pid: pid == 100 or not dead.value)
    monkeypatch.setattr('gateway.status._get_process_start_time', lambda pid: 1.)
    clock = SimpleNamespace(value=0.)
    monkeypatch.setattr(run_generation, 'time', SimpleNamespace(monotonic=lambda: clock.value,
                        sleep=lambda seconds: setattr(clock, 'value', clock.value + seconds)))
    db = GenerationCoordinator(tmp_path)
    old = GenerationIdentity.create(release_sha='a', label='old', pid=100, start_fingerprint='100:1.0')
    new = GenerationIdentity.create(release_sha='b', label='new', pid=200, start_fingerprint='200:1.0')
    db.register(old, state='serving')
    db.register(new)
    epoch = db.acquire_lease('active_generation', old.id)
    commits = []
    def request(path, verb, **kwargs):
        if verb == 'polling_roster':
            return {'tokens': []}
        if verb == 'transfer_requested':
            return {'generation_id': old.id, 'epoch': epoch, 'poller_stopped': True}
        dead.value = True
        raise RuntimeError('B died before answering')
    monkeypatch.setattr(run_generation, '_generation_request', request)
    def before_commit():
        assert db.leases()[0]['generation_id'] == old.id
        commits.append(clock.value)
    with pytest.raises(run_generation.HandoverCommittedUnverified):
        run_generation.handover_to_generation(tmp_path, new.id, timeout=45, before_commit=before_commit)
    assert commits == [0]
    assert clock.value < 2
    assert db.leases()[0]['generation_id'] == new.id


def test_bounded_escalation_recomputes_kill_wait_inside_original_deadline(monkeypatch):
    from hermes_cli import gateway
    clock = SimpleNamespace(value=0.)
    signals, waits = [], []
    monkeypatch.setattr(gateway, 'time', SimpleNamespace(monotonic=lambda: clock.value))
    monkeypatch.setattr('gateway.status.get_process_start_time', lambda pid: 1.)
    monkeypatch.setattr(gateway, 'terminate_pid', lambda pid, **kw: signals.append((pid, kw)))
    def wait(pid, seconds):
        waits.append(seconds)
        clock.value += seconds
        return False
    monkeypatch.setattr(gateway, '_wait_for_pid_exit', wait)
    assert not gateway._escalate_wedged_gateway(123, term_grace=5, kill_wait=5,
                                               deadline=6, expected_start_time=1.)
    assert waits == [5, 1]
    assert clock.value == 6
    assert [item[1]['force'] for item in signals] == [False, True]
    assert all(item[1]['expected_start_time'] == 1. for item in signals)


def test_soft_termination_refuses_a_recycled_pid(monkeypatch):
    from gateway import status
    signals = []
    monkeypatch.setattr(status, '_get_process_start_time', lambda pid: 2.)
    monkeypatch.setattr(status.os, 'kill', lambda *args: signals.append(args))
    with pytest.raises(OSError, match='identity changed'):
        status.terminate_pid(123, expected_start_time=1.)
    assert signals == []


def test_replaced_pid_after_term_proves_old_owner_dead_without_kill(monkeypatch):
    from hermes_cli import gateway
    start = SimpleNamespace(value=1.)
    signals = []
    monkeypatch.setattr('gateway.status.get_process_start_time', lambda pid: start.value)
    monkeypatch.setattr(gateway, 'terminate_pid', lambda pid, **kw: signals.append(kw))
    def wait(*args):
        start.value = 100_000.  # a new incarnation, far outside start-time drift tolerance
        return False
    monkeypatch.setattr(gateway, '_wait_for_pid_exit', wait)
    assert gateway._escalate_wedged_gateway(123, expected_start_time=1.)
    assert [item['force'] for item in signals] == [False]


def test_identity_change_at_term_guard_is_already_a_death_proof(monkeypatch):
    from hermes_cli import gateway
    start = SimpleNamespace(value=1.)
    monkeypatch.setattr('gateway.status.get_process_start_time', lambda pid: start.value)
    def refuse(*args, **kwargs):
        start.value = 100_000.  # a new incarnation, far outside start-time drift tolerance
        raise OSError('identity changed at signal guard')
    monkeypatch.setattr(gateway, 'terminate_pid', refuse)
    monkeypatch.setattr(gateway, '_wait_for_pid_exit', lambda *args: False)
    assert gateway._escalate_wedged_gateway(123, expected_start_time=1.)


def test_review4_unverified_commit_returns_without_spending_post_commit_budget(tmp_path, monkeypatch):
    """The updater's commit-clocked proof is the only post-commit wait."""
    from gateway import run_generation
    monkeypatch.setattr('gateway.generation._boot_id', lambda: 'fixture-boot')
    monkeypatch.setattr('gateway.status._pid_exists', lambda pid: True)
    monkeypatch.setattr('gateway.status._get_process_start_time', lambda pid: 1.)
    clock = SimpleNamespace(value=0.)
    monkeypatch.setattr(run_generation, 'time', SimpleNamespace(monotonic=lambda: clock.value,
                        sleep=lambda seconds: setattr(clock, 'value', clock.value + seconds)))
    db = GenerationCoordinator(tmp_path)
    old = GenerationIdentity.create(release_sha='a', label='old', pid=100, start_fingerprint='100:1.0')
    new = GenerationIdentity.create(release_sha='b', label='new', pid=200, start_fingerprint='200:1.0')
    db.register(old, state='serving')
    db.register(new)
    epoch = db.acquire_lease('active_generation', old.id)
    verbs = []
    def request(path, verb, **kwargs):
        verbs.append(verb)
        if verb == 'polling_roster':
            return {'tokens': []}
        if verb == 'transfer_requested':
            return {'generation_id': old.id, 'epoch': epoch, 'poller_stopped': True}
        clock.value += 2  # a live but silent successor
        raise RuntimeError('successor loop silent')
    monkeypatch.setattr(run_generation, '_generation_request', request)
    promoted = run_generation.handover_to_generation(tmp_path, new.id, timeout=45, verify_after_commit=False)
    assert promoted == db.leases()[0]['epoch'] and db.leases()[0]['generation_id'] == new.id
    assert 'polling_status' not in verbs and clock.value == 0


@pytest.mark.asyncio
async def test_retry_waits_for_complete_rearm_before_freezing_new_attempt(tmp_path, monkeypatch):
    """Wire progress is not permission to replace an abort still reopening dispatch."""
    import asyncio
    from gateway import run_generation
    monkeypatch.setattr('gateway.generation._boot_id', lambda: 'fixture-boot')
    db = GenerationCoordinator(tmp_path)
    old = GenerationIdentity.create(release_sha='a', label='old')
    new = GenerationIdentity.create(release_sha='b', label='new')
    db.register(old, state='serving')
    db.register(new)
    epoch = db.acquire_lease('active_generation', old.id)
    wire_started, finish_start, roster_entered = asyncio.Event(), asyncio.Event(), asyncio.Event()
    poller = SimpleNamespace(running=False)
    async def start(receipt):
        poller.running = True
        wire_started.set()
        await finish_start.wait()
    async def stop():
        poller.running = False
        return {'token_hash': 'hash', 'safe_offset': 42}
    adapter = SimpleNamespace(_controlled_poller=poller,
        _controlled_journal=SimpleNamespace(token_hash='hash'), start_polling_from_transfer=start,
        stop_polling_for_transfer=stop)
    active = ActiveGeneration(tmp_path, db, old, epoch)
    active.runner = SimpleNamespace(adapters={'telegram': adapter}, _overlap_draining=True)
    active._stopped_receipts = [(adapter, {'token_hash': 'hash', 'safe_offset': 42})]
    db.request_transfer(old.id, new.id, epoch, {'hash'})
    nonce = db.transfer_attempt_nonce(old.id, epoch)
    assert db.abort_transfer(old.id, new.id, epoch, attempt_nonce=nonce)
    await active.start()
    rearm = asyncio.create_task(active.transfer_aborted(new.id, nonce))
    request = run_generation._generation_request
    loop = asyncio.get_running_loop()
    def observed(path, verb, **kwargs):
        if verb == 'polling_roster':
            loop.call_soon_threadsafe(roster_entered.set)
        return request(path, verb, **kwargs)
    monkeypatch.setattr(run_generation, '_generation_request', observed)
    retry = None
    try:
        await asyncio.wait_for(wire_started.wait(), 2)
        retry = asyncio.create_task(asyncio.to_thread(run_generation.handover_to_generation,
            tmp_path, new.id, timeout=4, verify_after_commit=False))
        await asyncio.wait_for(roster_entered.wait(), 2)
        # A driver already at the socket must not freeze another nonce while
        # the adapter's start is unfinished, even though getUpdates can progress.
        await asyncio.sleep(.1)
        assert db.transfer_attempt_nonce(old.id, epoch) == nonce
        assert not retry.done()
        finish_start.set()
        assert (await rearm)['rearmed']
        assert await retry == epoch + 1
        assert db.leases()[0]['generation_id'] == new.id
    finally:
        finish_start.set()
        await asyncio.gather(rearm, *([retry] if retry else []), return_exceptions=True)
        await active.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('budget,stop_delay', [(10, 11), (45, 46)])
async def test_socket_timeout_self_rearms_after_slow_stop(tmp_path, monkeypatch, budget, stop_delay):
    """The real driver/socket may time out while the owner is still stopping its wire."""
    import asyncio
    import contextlib
    import tempfile
    import time
    from pathlib import Path
    from gateway import run_generation
    from plugins.platforms.telegram.polling_transfer import ControlledPoller

    # Keep every socket under the selected scratch root, below AF_UNIX's path limit.
    with tempfile.TemporaryDirectory(dir=Path(tempfile.gettempdir()), prefix='r7') as short:
        original_paths = run_generation.generation_paths
        def paths(home, identity):
            return {**original_paths(home, identity), 'socket': Path(short) / (identity.id[:8] + '.sock')}
        monkeypatch.setattr(run_generation, 'generation_paths', paths)
        db = GenerationCoordinator(tmp_path)
        old = GenerationIdentity.create(release_sha='b', label='old')
        new = GenerationIdentity.create(release_sha='a', label='fresh-previous')
        db.register(old, state='serving')
        db.register(new)
        epoch = db.acquire_lease('active_generation', old.id)
        entered, wire_release, rearmed = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def get_updates(**kwargs):
            entered.set()
            await wire_release.wait()
            return []
        journal = SimpleNamespace(token_hash=old.id, lifecycle_owner=lambda: None,
                                  pending=lambda: [], safe_offset=lambda: 42)
        app = SimpleNamespace(bot=SimpleNamespace(get_updates=get_updates), update_queue=asyncio.Queue())
        poller = ControlledPoller(app, journal)  # real timeout+1 long-poll stop
        await poller.start()
        await entered.wait()
        adapter = SimpleNamespace(_controlled_poller=poller, _controlled_journal=journal)
        async def stop():
            started = time.monotonic()
            stopped = await poller.stop()
            assert stopped['stopped']
            # A promotion can exhaust 45s across multiple stops/queue flushes.
            await asyncio.sleep(max(0, stop_delay - (time.monotonic() - started)))
            return {'token_hash': old.id, 'safe_offset': 42}
        async def start(receipt):
            assert receipt['safe_offset'] == 42
            await poller.start()
            rearmed.set()
        adapter.stop_polling_for_transfer, adapter.start_polling_from_transfer = stop, start
        runner = SimpleNamespace(adapters={'telegram': adapter}, _overlap_draining=False)
        runner._overlap_cron_start_kwargs = {'can_dispatch': lambda: not runner._overlap_draining}
        active = ActiveGeneration(tmp_path, db, old, epoch)
        active.runner = runner
        await active.start()
        verbs = []
        request = run_generation._generation_request
        def observed(path, verb, **kwargs):
            verbs.append((verb, kwargs['timeout']))
            return request(path, verb, **kwargs)
        monkeypatch.setattr(run_generation, '_generation_request', observed)
        async def release_wire():
            await asyncio.sleep(11)  # responsive owner, ordinary outstanding Telegram poll
            wire_release.set()
        release = asyncio.create_task(release_wire())
        started = time.monotonic()
        try:
            with pytest.raises(RuntimeError, match='generation control unavailable'):
                await asyncio.to_thread(run_generation.handover_to_generation, tmp_path, new.id,
                                        timeout=budget, verify_after_commit=False)
            assert time.monotonic() - started < budget + 2
            assert any(verb == 'transfer_aborted' and 0 < seconds <= 2 for verb, seconds in verbs)
            await asyncio.wait_for(rearmed.wait(), timeout=stop_delay - budget + 5)
            # Re-arm starts the wire before reopening the shared dispatch gate.
            # Observe completion across the same real control-socket boundary.
            nonce = db.transfer_attempt_nonce(old.id, epoch)
            reply = await asyncio.to_thread(request, active.paths['socket'], 'transfer_aborted',
                                            params={'to': new.id, 'nonce': nonce}, timeout=5)
            assert reply['rearmed']
            assert active.rearm_status()['armed'] == dict.fromkeys(('poller', 'cron', 'kanban', 'goal_wakeup'), True)
            assert not active._rearm_errors
            assert db.leases()[0]['generation_id'] == old.id
            assert {row['id']: row['state'] for row in db.generations()} == {old.id: 'serving', new.id: 'standby'}
            with contextlib.closing(db.connect()) as conn:
                assert conn.execute('SELECT state FROM generation_transfers').fetchone()[0] == 'aborted'
                assert conn.execute('SELECT count(*) FROM lease_moves').fetchone()[0] == 0
        finally:
            wire_release.set()
            await release
            await poller.stop()
            await active.close()


@pytest.mark.asyncio
@pytest.mark.parametrize('changed', ['committed', 'identity', 'epoch', 'nonce'])
async def test_delayed_stop_never_rearms_changed_owner_or_attempt(tmp_path, monkeypatch, changed):
    import asyncio
    db = GenerationCoordinator(tmp_path)
    old = GenerationIdentity.create(release_sha='b', label='old')
    new = GenerationIdentity.create(release_sha='a', label='fresh-previous')
    db.register(old, state='serving')
    db.register(new)
    epoch = db.acquire_lease('active_generation', old.id)
    entered, release = asyncio.Event(), asyncio.Event()
    starts = []
    poller = SimpleNamespace(running=True)
    async def stop():
        entered.set()
        await release.wait()
        poller.running = False
        return {'token_hash': 'hash', 'safe_offset': 42}
    async def start(receipt):
        starts.append(receipt)
        poller.running = True
    adapter = SimpleNamespace(_controlled_poller=poller,
        _controlled_journal=SimpleNamespace(token_hash='hash'),
        stop_polling_for_transfer=stop, start_polling_from_transfer=start)
    active = ActiveGeneration(tmp_path, db, old, epoch)
    active.runner = SimpleNamespace(adapters={'telegram': adapter}, _overlap_draining=False)
    db.request_transfer(old.id, new.id, epoch, {'hash'})
    nonce = db.transfer_attempt_nonce(old.id, epoch)
    task = asyncio.create_task(active.transfer_requested(new.id))
    await entered.wait()
    assert db.abort_transfer(old.id, new.id, epoch, attempt_nonce=nonce)
    if changed in {'committed', 'nonce'}:
        db.request_transfer(old.id, new.id, epoch, {'hash'})
        if changed == 'committed':
            db.record_poller_stopped(old.id, epoch, 'hash', 42)
            db.commit_transfer(old.id, new.id, epoch)
    else:
        # A delayed callback can carry an obsolete process incarnation or epoch;
        # the durable identity/lease cannot itself be mutated outside lease moves.
        if changed == 'identity':
            from dataclasses import replace
            active.identity = replace(old, start_fingerprint='replaced:1')
        else:
            active.epoch += 1
    release.set()
    with pytest.raises(RuntimeError):
        await task
    with pytest.raises(RuntimeError):
        await active.transfer_aborted(new.id, nonce)
    assert not starts
    assert active.runner._overlap_draining and not poller.running
