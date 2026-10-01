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
