"""Forward-only update contracts with real coordinator transactions, no live services."""
import json
import plistlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway.generation import GenerationCoordinator, GenerationIdentity, generation_paths, write_generation_record
from hermes_cli import immutable_releases as releases
from hermes_cli import gateway_forward_update as forward

REAL_FRESH_REPLY = forward._fresh_reply


def release(home, sha, capable=True):
    path = home / 'releases' / sha
    (path / '.venv/bin').mkdir(parents=True)
    (path / '.venv/bin/python').write_text('fixture', encoding='utf-8')
    for marker in ('.release-ready', '.hermes_build_sha'):
        (path / marker).write_text(sha, encoding='utf-8')
    if capable:
        (path / 'hermes_cli').mkdir()
        (path / 'hermes_cli/release-capabilities.json').write_text('{"forward_only_handover":1}', encoding='utf-8')
    return path


@pytest.fixture
def rig(tmp_path, monkeypatch):
    monkeypatch.setattr('gateway.generation._boot_id', lambda: 'fixture-boot')
    home = tmp_path / 'profile'
    home.mkdir()
    monkeypatch.setenv('HERMES_HOME', str(home))
    (home / 'config.yaml').write_text('gateway:\n  forward_only_handover:\n    enabled: true\n', encoding='utf-8')
    a, b = release(home, 'a' * 40), release(home, 'b' * 40)
    (home / 'current').symlink_to(a)
    db = GenerationCoordinator(home)
    alive = {100: 1.0}
    monkeypatch.setattr('gateway.status._pid_exists', lambda pid: pid in alive)
    monkeypatch.setattr('gateway.status._get_process_start_time', lambda pid: alive.get(pid))
    old = GenerationIdentity.create(release_sha=a.name, label='ai.hermes.gateway', pid=100,
                                    start_fingerprint='100:1.0')
    db.register(old, state='serving')
    db.acquire_lease('active_generation', old.id)
    db.record_poller_event('token-hash', old.id, 1, 'lock_acquired')
    db.record_poller_event('token-hash', old.id, 1, 'poller_started')
    clock = SimpleNamespace(value=0.)
    monkeypatch.setattr(forward, '_now', lambda: clock.value)
    monkeypatch.setattr(forward, '_sleep', lambda seconds: setattr(clock, 'value', clock.value + seconds))
    events, loaded = [], {old.label}
    directory = tmp_path / 'LaunchAgents'
    directory.mkdir()
    (directory / f'{old.label}.plist').write_bytes(plistlib.dumps({
        'Label': old.label, 'EnvironmentVariables': {'HERMES_HOME': str(home)}, 'RunAtLoad': True,
        'ProgramArguments': [],
    }))

    class Supervisor(forward.GenerationSupervisor):
        mode = 'happy'
        abort = False
        polled = set()
        def _domain(self, label):
            return 'gui/fixture'
        def bootstrap(self, row, path, timeout, *, before_launch=None):
            reserved = next(item for item in db.generations() if item['id'] == row['id'])
            assert reserved['pid'] is None
            assert releases.read_pointer(home / 'current') in {a, b}
            payload = plistlib.loads(path.read_bytes())
            if before_launch is not None:
                before_launch(payload['EnvironmentVariables']['HERMES_GENERATION_SCOPE'])
            assert not payload['RunAtLoad']
            events.append(('bootstrap', row['id'], row['release_sha']))
            loaded.add(row['label'])
            if self.mode == 'crash_unclaimed':
                raise KeyboardInterrupt('after successful bootstrap before claim')
            if self.mode == 'unclaimed':
                return
            pid = 100 + len(events)
            alive[pid] = 1.
            db.claim_generation(row['id'], pid, f'{pid}:1.0', scope_nonce=payload['EnvironmentVariables']['HERMES_GENERATION_SCOPE'])
            claimed = next(item for item in db.generations() if item['id'] == row['id'])
            if self.mode == 'gate_failed':
                db._record_failure(row['id'], 'startup_gate_failed')
                return
            identity = forward._identity(claimed)
            write_generation_record(generation_paths(home, identity)['state'], identity,
                                    socket_path=generation_paths(home, identity)['socket'])
        def owns_bootstrap(self, row, scope):
            return self.bootstrap_state(row, scope) == 'owned'
        def bootstrap_state(self, row, scope):
            if row['label'] not in loaded:
                return 'unloaded'
            payload = plistlib.loads((self.directory / f"{row['label']}.plist").read_bytes())
            return 'owned' if payload['EnvironmentVariables']['HERMES_GENERATION_SCOPE'] == scope else 'foreign'
        def bootout(self, row, timeout=15):
            assert next(item for item in db.generations() if item['id'] == row['id'])['state'] == 'exited'
            events.append(('bootout', row['id']))
            return super().bootout(row, timeout)
        def request(self, row, verb, *, params=None, timeout=2):
            events.append((verb, row['id']))
            if verb == 'polling_status':
                lease = db.leases()[0]
                if lease['generation_id'] != row['id']:
                    raise RuntimeError('no lease')
                if row['release_sha'] == b.name and self.mode in ('dies', 'wedged', 'blocked', 'unhealthy'):
                    if self.mode == 'dies':
                        alive.pop(row['pid'], None)
                        raise RuntimeError('unhealthy successor')
                    if self.mode == 'unhealthy':
                        return {'healthy': False}
                    raise TimeoutError('generation loop did not answer')
                if row['id'] not in self.polled and row['id'] != old.id:
                    db.record_poller_event('token-hash', row['id'], lease['epoch'], 'lock_acquired')
                    db.record_poller_event('token-hash', row['id'], lease['epoch'], 'poller_started')
                self.polled.add(row['id'])
                return {'generation_id': row['id'], 'release_sha': row['release_sha'],
                        'release_root': str(home / 'releases' / row['release_sha']),
                        'epoch': lease['epoch'], 'tokens': ['token-hash'], 'polling': True,
                        'healthy': True, 'armed': dict.fromkeys(('poller', 'cron', 'kanban', 'goal_wakeup'), True)}
            if verb == 'transfer_aborted':
                assert row['id'] == old.id, 'Never re-arm draining A after commit'
                return {'generation_id': row['id'], 'epoch': 1, 'rearmed': True,
                        'armed': dict.fromkeys(('poller', 'cron', 'kanban', 'goal_wakeup'), True)}
            raise AssertionError(verb)

    def launchctl(argv, **kwargs):
        label = argv[-1].split('/')[-1]
        if argv[1] == 'bootout':
            loaded.discard(label)
        else:
            assert argv[1] == 'print'
        present = label in loaded
        return SimpleNamespace(returncode=0 if present else 1, stdout='',
                               stderr='' if present else 'Could not find service')
    supervisor = Supervisor(home, runner=launchctl, directory=directory, domain='gui/fixture')
    def handover(home_arg, to_id, **kwargs):
        lease = db.leases()[0]
        events.append(('handover', lease['generation_id'], to_id))
        if supervisor.mode in ('blocked', 'wedged') and lease['generation_id'] != old.id:
            raise RuntimeError('no acknowledgement')
        db.request_transfer(lease['generation_id'], to_id, lease['epoch'], {'token-hash'})
        db.record_poller_stopped(lease['generation_id'], lease['epoch'], 'token-hash', 42)
        db.record_poller_event('token-hash', lease['generation_id'], lease['epoch'], 'poller_stopped')
        db.record_poller_event('token-hash', lease['generation_id'], lease['epoch'], 'lock_released')
        if supervisor.abort:
            db.abort_transfer(lease['generation_id'], to_id, lease['epoch'])
            db.record_poller_event('token-hash', lease['generation_id'], lease['epoch'], 'lock_acquired')
            db.record_poller_event('token-hash', lease['generation_id'], lease['epoch'], 'poller_started')
            raise RuntimeError('precommit failure')
        if kwargs.get('before_commit'):
            kwargs['before_commit']()
        return db.commit_transfer(lease['generation_id'], to_id, lease['epoch'])
    monkeypatch.setattr(forward, 'handover_to_generation', handover)
    monkeypatch.setattr(forward, '_fresh_reply', lambda home, row, epoch, after, tokens:
                        {'generation_id': row['id'], 'epoch': epoch, 'message_id': 'fresh-reply'})
    monkeypatch.setattr('hermes_cli.gateway.probe_gateway_loop_liveness',
                        lambda *a, **k: 'wedged' if supervisor.mode == 'wedged' else 'unknown')
    def escalate(pid, **kwargs):
        events.append(('bounded-stop', pid, kwargs))
        alive.pop(pid, None)
        return True
    monkeypatch.setattr('hermes_cli.gateway._escalate_wedged_gateway', escalate)
    monkeypatch.setattr(releases, '_live_process_pins', lambda home: set())
    monkeypatch.setattr('hermes_cli.update_inventory.collect_runtime_inventory', lambda **kwargs:
        SimpleNamespace(runtimes=[{'kind': 'gateway', 'pid': row['pid']} for row in db.generations()
                                  if row['state'] != 'exited' and row['pid'] in alive]))
    return SimpleNamespace(home=home, a=a, b=b, db=db, old=old, supervisor=supervisor,
                           events=events, clock=clock, loaded=loaded, alive=alive)


def promote(rig):
    return forward.promote_forward(rig.home, rig.b, rig.b.name, supervisor=rig.supervisor)


def promote_different_release(rig):
    candidate = release(rig.home, 'c' * 40)
    return forward.promote_forward(rig.home, candidate, candidate.name, supervisor=rig.supervisor)


def test_review_m1_guardian_waits_quietly_for_active_updater(rig, monkeypatch):
    from hermes_cli import gateway_guardian
    forward._save(rig.home, {'outcome': 'running'})
    monkeypatch.setattr(gateway_guardian, 'receipt', lambda *a, **k: pytest.fail('contention alert'))
    with forward._update_lock(rig.home):
        assert gateway_guardian.run_once(
            rig.home, rig.supervisor.directory / f'{rig.old.label}.plist', rig.old.label,
            grace=180, domain='gui/fixture',
            launchctl_runner=lambda *a, **k: pytest.fail('launchd action during contention')) == 'locked'
    assert json.loads((rig.home / 'forward-update.json').read_text(encoding='utf-8')) == {'outcome': 'running'}


@pytest.mark.parametrize('caller', ['release', 'fleet'])
def test_review_m1_update_catchup_refuses_contended_lock(rig, monkeypatch, capsys, caller):
    from hermes_cli import update_cmd, update_cmd_fleet, update_receipt
    monkeypatch.setattr(update_cmd, '_immutable_release_enabled', lambda paths: True)
    monkeypatch.setattr(update_receipt, '_current', None)
    update_receipt.begin_update_receipt()
    forward._save(rig.home, {'outcome': 'running'})
    with forward._update_lock(rig.home), pytest.raises(SystemExit):
        if caller == 'release':
            update_cmd._catch_up_immutable_release(defer=False, sha=rig.b.name, source=rig.a)
        else:
            update_cmd_fleet._apply_pending_fleet_restart_catchup()
    receipt = update_receipt.read_latest_receipt()
    assert receipt['outcome'] == 'refused'
    assert 'another updater is running' in capsys.readouterr().out
    assert rig.events == []


def test_review_m2_only_holder_has_login_and_crash_respawn(rig):
    standby_id = rig.db.reserve_generation(release_sha=rig.b.name, label='standby-fixture')
    row = forward._row(rig.db, standby_id.id)
    # Rendering uses a real UUID label, matching the reservation's identity.
    row['label'] = forward.generation_launchd_label(row['id'])
    rendered = plistlib.loads(forward.render_generation_launchd_plist(
        slot=row['id'], release_sha=rig.b.name, release_root=rig.b,
        interpreter=rig.b / '.venv/bin/python', hermes_home=rig.home).encode())
    assert rendered['RunAtLoad'] is False and rendered['KeepAlive'] is False
    path = rig.supervisor.install(row, rig.b)
    standby = plistlib.loads(path.read_bytes())
    assert standby['RunAtLoad'] is False and standby['KeepAlive'] is False
    rig.supervisor.boot_active(row, True)
    holder = plistlib.loads(path.read_bytes())
    assert holder['RunAtLoad'] is True and holder['KeepAlive'] == {'SuccessfulExit': False}
    assert '--standby' not in holder['ProgramArguments']
    rig.supervisor.boot_active(row, False)
    demoted = plistlib.loads(path.read_bytes())
    assert demoted['RunAtLoad'] is False and demoted['KeepAlive'] is False


@pytest.mark.parametrize('foreign,readback_failed', [(False, False), (True, False), (False, True)])
def test_review_m2_cleanup_unlinks_only_owned_unloaded_definition(rig, monkeypatch, foreign, readback_failed):
    assert promote_different_release(rig)['outcome'] == 'success'
    path = rig.supervisor.directory / f'{rig.old.label}.plist'
    original = path.read_bytes()
    if foreign:
        data = plistlib.loads(original)
        data['EnvironmentVariables']['HERMES_HOME'] = str(rig.home / 'foreign')
        path.write_bytes(plistlib.dumps(data))
        original = path.read_bytes()
    rig.db.heartbeat(rig.old.id, state='exited')
    calls, synced = [], []
    def runner(argv, **kwargs):
        calls.append(argv)
        if argv[1] == 'bootout' and not readback_failed:
            rig.loaded.discard(rig.old.label)
        loaded = rig.old.label in rig.loaded
        return SimpleNamespace(returncode=0 if loaded else 1, stdout='', stderr='' if loaded else 'Could not find service')
    rig.supervisor.runner = runner
    monkeypatch.setattr(rig.supervisor, '_domain', lambda label: 'gui/fixture')
    monkeypatch.setattr(rig.supervisor, 'bootout', lambda row: forward.GenerationSupervisor.bootout(rig.supervisor, row))
    monkeypatch.setattr(forward, '_sync_dir', lambda directory: synced.append(directory))
    if foreign or readback_failed:
        with pytest.raises(RuntimeError):
            forward.cleanup_exited(rig.home, supervisor=rig.supervisor)
        assert path.exists()
        if foreign:
            assert path.read_bytes() == original and not calls
    else:
        forward.cleanup_exited(rig.home, supervisor=rig.supervisor)
        assert not path.exists()
        assert synced == [rig.supervisor.directory]
        calls.clear()
        forward.cleanup_exited(rig.home, supervisor=rig.supervisor)
        assert not any(argv[1] == 'bootout' for argv in calls)


def test_review_m2_bootstrap_keeps_runtime_respawn_outside_login_directory(rig, monkeypatch):
    import uuid
    from hermes_cli.gateway_launchd_generation import generation_launchd_label
    generation_id = str(uuid.uuid4())
    reserved = rig.db.reserve_generation(generation_id=generation_id, release_sha=rig.b.name,
                                         label=generation_launchd_label(generation_id))
    row = forward._row(rig.db, reserved.id)
    path = rig.supervisor.install(row, rig.b)
    scopes = []
    def runner(argv, **kwargs):
        if argv[1] == 'print':
            return SimpleNamespace(returncode=1, stdout='', stderr='Could not find service')
        assert argv[1] == 'bootstrap'
        runtime_path = Path(argv[-1])
        assert runtime_path.parent != rig.supervisor.directory
        runtime = plistlib.loads(runtime_path.read_bytes())
        login = plistlib.loads(path.read_bytes())
        assert runtime['KeepAlive'] == {'SuccessfulExit': False} and runtime['RunAtLoad'] is True
        assert login['KeepAlive'] is False and login['RunAtLoad'] is False
        assert runtime['EnvironmentVariables']['HERMES_GENERATION_SCOPE'] == login['EnvironmentVariables']['HERMES_GENERATION_SCOPE'] == scopes[0]
        return SimpleNamespace(returncode=0)
    rig.supervisor.runner = runner
    monkeypatch.setattr(rig.supervisor, '_domain', lambda label: 'gui/fixture')
    forward.GenerationSupervisor.bootstrap(rig.supervisor, row, path, 30, before_launch=scopes.append)
    assert scopes


def test_review_l1_rolled_back_sha_stays_fenced_across_receipts(rig, monkeypatch):
    rig.supervisor.mode = 'dies'
    assert promote(rig)['outcome'] == 'rolled_back'
    rig.supervisor.mode = 'happy'
    rig.events.clear()
    refused = promote(rig)
    assert refused['outcome'] == 'refused'
    assert refused['failure'] == 'release previously rolled back'
    assert rig.events == []
    assert promote(rig)['outcome'] == 'refused'  # A refusal must not erase the fence.
    from hermes_cli import update_cmd, update_receipt
    monkeypatch.setattr(update_cmd, '_immutable_release_enabled', lambda paths: True)
    monkeypatch.setattr(forward, 'GenerationSupervisor', lambda home: rig.supervisor)
    monkeypatch.setattr(update_cmd, '_require_immutable_launchd', lambda: None)
    monkeypatch.setattr(releases, 'stage_release', lambda *a, **k: (rig.b, 'reused'))
    monkeypatch.setattr(update_receipt, '_current', None)
    update_receipt.begin_update_receipt()
    with pytest.raises(SystemExit):
        update_cmd._catch_up_immutable_release(defer=False, sha=rig.b.name, source=rig.a)
    assert update_receipt.read_latest_receipt()['outcome'] == 'refused'
    candidate = release(rig.home, 'c' * 40)
    assert forward.promote_forward(rig.home, candidate, candidate.name, supervisor=rig.supervisor)['outcome'] == 'success'


def test_review_l1_catchup_failure_finalizes_partial_without_traceback(rig, monkeypatch):
    from hermes_cli import update_cmd, update_receipt
    monkeypatch.setattr(update_cmd, '_immutable_release_enabled', lambda paths: True)
    monkeypatch.setattr(forward, 'recover_forward', lambda home: (_ for _ in ()).throw(RuntimeError('recovery failed')))
    monkeypatch.setattr(update_receipt, '_current', None)
    update_receipt.begin_update_receipt()
    with pytest.raises(SystemExit) as exc:
        update_cmd._catch_up_immutable_release(defer=False, sha=rig.b.name, source=rig.a)
    assert exc.value.code == 1
    assert update_receipt.read_latest_receipt()['outcome'] == 'partial'


@pytest.mark.parametrize('phase', ['promote', 'rollback', 'recover_success', 'recover_rollback', 'verify'])
def test_review_n1_slow_first_poll_uses_original_commit_budget(rig, monkeypatch, phase):
    original = rig.supervisor.request
    waiting = {}
    def slow(row, verb, **kwargs):
        if verb == 'polling_status' and row['id'] != rig.old.id:
            start = waiting.setdefault(row['id'], rig.clock.value)
            if rig.clock.value - start < 13:
                raise RuntimeError('first poll pending')
        return original(row, verb, **kwargs)
    if phase in {'rollback', 'recover_rollback'}:
        rig.supervisor.mode = 'dies'
        def slow_rollback(row, verb, **kwargs):
            return slow(row, verb, **kwargs) if row['release_sha'] == rig.a.name else original(row, verb, **kwargs)
        delayed = slow_rollback
    else:
        delayed = slow
    if phase.startswith('recover'):
        flip = forward._flip
        def crash(*args, **kwargs):
            if phase == 'recover_success' or kwargs.get('operation') == 'rollback':
                raise KeyboardInterrupt('committed owner before pointer flip')
            return flip(*args, **kwargs)
        monkeypatch.setattr(forward, '_flip', crash)
        with pytest.raises(KeyboardInterrupt):
            promote(rig)
        monkeypatch.setattr(forward, '_flip', flip)
        monkeypatch.setattr(rig.supervisor, 'request', delayed)
        result = forward.recover_forward(rig.home, supervisor=rig.supervisor)
    elif phase == 'verify':
        result = promote(rig)
        monkeypatch.setattr(rig.supervisor, 'request', delayed)
        result = forward.verify_forward(rig.home, result, supervisor=rig.supervisor)
    else:
        monkeypatch.setattr(rig.supervisor, 'request', delayed)
        result = promote(rig)
    assert result['outcome'] == ('rolled_back' if 'rollback' in phase else 'success'), result
    assert rig.clock.value <= result['commit_clock'] + forward.ROLLBACK_SECONDS


def test_review_n1_current_observation_timeout_is_recheckable(rig, monkeypatch):
    request = rig.supervisor.request
    monkeypatch.setattr(rig.supervisor, 'request', lambda *a, **k: (_ for _ in ()).throw(RuntimeError('first poll pending')))
    result = forward.observe_current_forward(rig.home, supervisor=rig.supervisor)
    assert result['outcome'] == 'blocked' and result['alert']
    assert rig.clock.value == pytest.approx(forward.POLL_SECONDS)
    monkeypatch.setattr(rig.supervisor, 'request', request)
    assert forward.observe_current_forward(rig.home, supervisor=rig.supervisor)['outcome'] == 'success'


def test_happy_ordering_and_exit_cleanup(rig):
    result = promote(rig)
    assert result['outcome'] == 'success'
    assert result['new_id'] in rig.supervisor.polled
    assert releases.read_pointer(rig.home / 'current') == rig.b
    assert releases.read_pointer(rig.home / 'previous') == rig.a
    assert rig.old.label in rig.loaded
    definitions = [plistlib.loads(path.read_bytes()) for path in rig.supervisor.directory.glob('*.plist')]
    assert [item['Label'] for item in definitions if item['RunAtLoad']] == [result['new_label']]
    rig.db.heartbeat(rig.old.id, state='exited')
    forward.cleanup_exited(rig.home, supervisor=rig.supervisor)
    assert rig.old.label not in rig.loaded
    assert result['poller']['epoch'] == result['epoch']


@pytest.mark.parametrize('mode,evidence', [('gate_failed', 'startup_gate_failed'), ('unclaimed', 'unclaimed')])
def test_refused_standby_never_pauses_old_or_flips_pointers(rig, mode, evidence):
    rig.supervisor.mode = mode
    result = promote(rig)
    assert result['outcome'] == 'refused'
    new = next(row for row in rig.db.generations() if row['id'] == result['new_id'])
    assert (new['state'], new['verdict'], new['verdict_evidence']) == ('exited', 'failed', evidence)
    assert new['label'] not in rig.loaded
    assert not any(event[0] == 'handover' for event in rig.events)
    assert releases.read_pointer(rig.home / 'current') == rig.a
    assert not (rig.home / 'previous').exists()
    if mode == 'unclaimed':
        assert rig.clock.value == pytest.approx(45)


def test_precommit_abort_requires_all_fences_readback(rig):
    rig.supervisor.abort = True
    result = promote(rig)
    assert result['outcome'] == 'aborted'
    assert all(result['resume']['armed'].values())
    assert rig.db.leases()[0]['generation_id'] == rig.old.id
    assert releases.read_pointer(rig.home / 'current') == rig.a


@pytest.mark.parametrize('mode,move', [('dies', 'takeover'), ('unhealthy', 'handover'), ('wedged', 'takeover')])
def test_rollback_starts_fresh_previous_and_never_rearms_draining_a(rig, mode, move):
    rig.supervisor.mode = mode
    result = promote(rig)
    assert result['outcome'] == 'rolled_back', result
    rollback = result['rollback']
    assert rollback['new_id'] not in (rig.old.id, result['new_id'])
    assert rollback['new_sha'] == rig.a.name
    assert rollback['new_id'] in rig.supervisor.polled
    assert rig.db.leases()[0]['generation_id'] == rollback['new_id']
    assert next(row for row in rig.db.generations() if row['id'] == rig.old.id)['state'] == 'draining'
    assert releases.read_pointer(rig.home / 'current') == rig.a
    assert not any(event[0] == 'transfer_aborted' for event in rig.events)
    with rig.db.connect() as conn:
        assert conn.execute('SELECT kind FROM lease_moves ORDER BY new_epoch DESC LIMIT 1').fetchone()[0] == move
    assert rollback['serving_seconds'] <= 60
    assert rollback['reply_seconds'] <= 60
    if mode == 'wedged':
        assert any(event[0] == 'bounded-stop' for event in rig.events)


def test_live_unknown_loop_blocks_without_second_poller_or_forced_move(rig):
    rig.supervisor.mode = 'blocked'
    result = promote(rig)
    assert result['outcome'] == 'blocked' and result['alert']
    assert rig.db.leases()[0]['generation_id'] == result['new_id']
    assert not any(event[0] == 'bounded-stop' for event in rig.events)
    assert not any(row['state'] == 'serving' and row['id'] != result['new_id'] for row in rig.db.generations())


def test_crash_after_commit_recovery_observes_instead_of_handover(rig, monkeypatch):
    original = forward.handover_to_generation
    def crash(*args, **kwargs):
        original(*args, **kwargs)
        raise KeyboardInterrupt('updater SIGKILL surrogate')
    monkeypatch.setattr(forward, 'handover_to_generation', crash)
    with pytest.raises(KeyboardInterrupt):
        promote(rig)
    rig.events.clear()
    result = forward.recover_forward(rig.home, supervisor=rig.supervisor)
    assert result['outcome'] == 'success' and result['recovered']
    assert not any(event[0] in ('bootstrap', 'handover', 'transfer_aborted') for event in rig.events)
    assert releases.read_pointer(rig.home / 'current') == rig.b


def test_retention_keeps_all_nonexited_release_pins(rig):
    pinned = release(rig.home, 'c' * 40)
    unused = release(rig.home, 'd' * 40)
    rig.db.reserve_generation(release_sha=pinned.name, label='reserved-pin')
    removed = releases.retain(rig.home, rollback_count=0)
    assert pinned.exists() and unused in removed


def test_capability_requires_target_and_serving_release(rig):
    assert forward.forward_route(rig.home, rig.b)
    (rig.a / 'hermes_cli/release-capabilities.json').unlink()
    assert not forward.forward_route(rig.home, rig.b)
    (rig.home / 'config.yaml').write_text('gateway:\n  forward_only_handover:\n    enabled: false\n', encoding='utf-8')
    assert not forward.forward_route(rig.home, rig.b)


def test_flag_on_without_a_serving_generation_uses_ordinary_restart(rig):
    (rig.home / 'gateway-coordinator.db').unlink()
    assert not forward.forward_route(rig.home, rig.b)
    assert rig.events == []


def test_legacy_overlap_refuses_forward_target_even_with_new_flag_off(rig):
    (rig.home / 'config.yaml').write_text('gateway:\n  overlap_handover:\n    enabled: true\n', encoding='utf-8')
    with pytest.raises(ValueError, match='overlap_handover'):
        releases.activate_release(rig.home, rig.b)
    with pytest.raises(ValueError, match='overlap_handover'):
        releases.stage_release(rig.b, rig.home, sha=rig.b.name)


def test_noncapable_serving_release_has_identical_s2_calls_to_flag_off(rig, monkeypatch):
    from hermes_cli import update_cmd, gateway
    calls = []
    monkeypatch.setattr(update_cmd, '_require_immutable_launchd', lambda: None)
    monkeypatch.setattr(update_cmd, '_immutable_release_enabled', lambda paths: True)
    monkeypatch.setattr(update_cmd, '_finish_pending_release_transaction', lambda home: calls.append('recover'))
    monkeypatch.setattr(gateway, 'get_launchd_plist_path', lambda: rig.home / 'missing.plist')
    monkeypatch.setattr(releases, 'stage_release', lambda *args, **kwargs: (calls.append(('stage', args, kwargs)) or (rig.b, 'staged')))
    monkeypatch.setattr(releases, 'activate_release', lambda *args, **kwargs:
                        (calls.append(('activate', args, kwargs)) or {'current': str(rig.b), 'previous': str(rig.a)}))
    (rig.home / 'config.yaml').write_text('gateway:\n  forward_only_handover:\n    enabled: false\n', encoding='utf-8')
    assert update_cmd._activate_immutable_release(sha=rig.b.name, source=rig.a)
    off_calls = list(calls)
    calls.clear()
    (rig.a / 'hermes_cli/release-capabilities.json').unlink()
    (rig.home / 'config.yaml').write_text('gateway:\n  forward_only_handover:\n    enabled: true\n', encoding='utf-8')
    assert update_cmd._activate_immutable_release(sha=rig.b.name, source=rig.a)
    assert calls == off_calls


def test_rollback_ack_precedes_pointer_restore_even_if_b_pointer_already_flipped(rig, monkeypatch):
    original = forward._flip
    def flip(home, db, row, proof, supervisor, **kwargs):
        if row['release_sha'] == rig.b.name:
            original(home, db, row, proof, supervisor, **kwargs)
            assert releases.read_pointer(rig.home / 'current') == rig.b
            rig.alive.pop(row['pid'])
            raise RuntimeError('death immediately after pointer flip')
        assert row['id'] not in (rig.old.id, db.generations()[1]['id'])
        assert row['id'] in rig.supervisor.polled
        assert releases.read_pointer(rig.home / 'current') == rig.b
        return original(home, db, row, proof, supervisor, **kwargs)
    monkeypatch.setattr(forward, '_flip', flip)
    result = promote(rig)
    assert result['outcome'] == 'rolled_back', result
    assert releases.read_pointer(rig.home / 'current') == rig.a
    assert releases.read_pointer(rig.home / 'previous') == rig.b


def test_rollback_never_claims_reply_success_past_60_second_budget(rig, monkeypatch):
    rig.supervisor.mode = 'dies'
    monkeypatch.setattr(forward, '_fresh_reply', lambda *args: None)
    result = promote(rig)
    assert result['outcome'] == 'rolled_back' and not result.get('alert', False)
    assert result['rollback']['serving_seconds'] <= 60
    assert result['rollback']['reply_observed'] is False
    assert result['rollback']['rollback_bound_met'] is True
    assert 'reply_seconds' not in result['rollback']
    assert rig.clock.value == pytest.approx(60)
    assert not (rig.home / 'forward-update.json').exists()


def test_quiet_rollback_allows_a_different_release_after_fencing_failed_sha(rig, monkeypatch):
    rig.supervisor.mode = 'dies'
    monkeypatch.setattr(forward, '_fresh_reply', lambda *args: None)
    first = promote(rig)
    rig.supervisor.mode = 'happy'
    assert promote(rig)['outcome'] == 'refused'
    second = promote_different_release(rig)
    assert first['outcome'] == 'rolled_back'
    assert first['rollback']['reply_observed'] is False
    assert second['outcome'] == 'success'
    assert second['old_id'] == first['rollback']['new_id']
    assert releases.read_pointer(rig.home / 'current').name == 'c' * 40


def test_abandoned_precommit_reservation_is_retired_by_observer(rig, monkeypatch):
    def crash(*args, **kwargs):
        raise KeyboardInterrupt('SIGKILL before bootstrap surrogate')
    monkeypatch.setattr(rig.supervisor, 'bootstrap', crash)
    with pytest.raises(KeyboardInterrupt):
        promote(rig)
    result = forward.recover_forward(rig.home, supervisor=rig.supervisor)
    assert result['outcome'] == 'refused'
    row = next(row for row in rig.db.generations() if row['id'] == result['new_id'])
    assert (row['state'], row['verdict'], row['verdict_evidence']) == ('exited', 'failed', 'unclaimed')
    assert not any(event[0] == 'transfer_aborted' for event in rig.events)
    assert releases.read_pointer(rig.home / 'current') == rig.a


def test_fleet_catchup_cannot_restart_any_forward_generation(rig, monkeypatch):
    from hermes_cli import update_cmd, update_cmd_fleet
    result = promote(rig)
    monkeypatch.setattr(forward, 'recover_forward', lambda home: result)
    monkeypatch.setattr('hermes_cli.update_inventory.collect_runtime_inventory', lambda **kwargs: SimpleNamespace(runtimes=[]))
    monkeypatch.setattr(update_cmd, '_run_pending_fleet_restart', lambda *a, **k: pytest.fail('legacy fleet mutation'))
    monkeypatch.setattr(update_cmd_fleet, '_pending_fleet_restart_needed', lambda: True)
    update_cmd_fleet._apply_pending_fleet_restart_catchup()


def test_extra_fleet_runtime_is_refused_before_any_launch(rig):
    plan = {'runtimes': [{'kind': 'gateway', 'pid': rig.old.pid}, {'kind': 'gateway', 'pid': 200}]}
    with pytest.raises(RuntimeError, match='additional fleet runtime'):
        forward.require_forward_inventory(rig.home, plan)
    assert rig.events == []


def test_backend_inventory_does_not_block_forward_activation_and_is_recorded(rig, monkeypatch):
    from hermes_cli import update_inventory, update_receipt
    others = [update_inventory.RuntimeRecord(kind=kind, profile='default', pid=200 + index)
              for index, kind in enumerate(('serve', 'dashboard'))]
    plan = update_inventory.UpdatePlan(runtimes=[
        update_inventory.RuntimeRecord(kind='gateway', profile='default', pid=rig.old.pid), *others])
    monkeypatch.setattr(update_inventory, 'collect_runtime_inventory', lambda **kwargs: plan)
    receipt = SimpleNamespace(data={})
    monkeypatch.setattr(update_receipt, '_current', receipt)
    monkeypatch.setattr(forward, 'GenerationSupervisor', lambda home: rig.supervisor)
    result = forward.activate_if_forward(rig.home, rig.b, rig.b.name)
    assert result['outcome'] == 'success'
    assert receipt.data['forward_inventory']['other_runtimes'] == plan.to_dict()['runtimes'][1:]


def test_loaded_foreign_label_is_never_booted_out_even_by_later_cleanup(rig, monkeypatch):
    foreign = []
    def collision(row, path, timeout, **kwargs):
        foreign.append(row['label'])
        rig.loaded.add(row['label'])
        raise RuntimeError('generation label was already loaded before bootstrap')
    monkeypatch.setattr(rig.supervisor, 'bootstrap', collision)
    result = promote(rig)
    assert result['outcome'] == 'refused'
    forward.cleanup_exited(rig.home, supervisor=rig.supervisor)
    assert foreign[0] in rig.loaded
    assert not any(event[0] == 'bootout' for event in rig.events)


def test_completed_intent_is_audit_only_and_current_observer_accepts_cold_claimant(rig):
    result = promote(rig)
    assert not (rig.home / 'forward-update.json').exists()
    assert (rig.home / 'forward-update-last.json').exists()
    assert forward.recover_forward(rig.home, supervisor=rig.supervisor) is None
    previous = next(row for row in rig.db.generations() if row['id'] == result['new_id'])
    rig.db.release_lease('active_generation', previous['id'], result['epoch'])
    rig.db.heartbeat(previous['id'], state='exited')
    fresh = GenerationIdentity.create(release_sha=rig.b.name, label=previous['label'], pid=110,
                                     start_fingerprint='110:1.0')
    rig.alive[110] = 1.
    rig.db.register(fresh)
    rig.db.takeover_dead_generation('active_generation', previous['id'], fresh.id, bootout=lambda label: True)
    proof = forward.observe_current_forward(rig.home, supervisor=rig.supervisor)
    assert proof['new_id'] == fresh.id and proof['outcome'] == 'success'


def test_crashed_updater_with_dead_committed_b_launches_only_fresh_previous(rig, monkeypatch):
    original = forward.handover_to_generation
    def crash(*args, **kwargs):
        original(*args, **kwargs)
        new = next(row for row in rig.db.generations() if row['id'] == rig.db.leases()[0]['generation_id'])
        rig.alive.pop(new['pid'])
        raise KeyboardInterrupt('updater killed after commit and B died')
    monkeypatch.setattr(forward, 'handover_to_generation', crash)
    with pytest.raises(KeyboardInterrupt):
        promote(rig)
    monkeypatch.setattr(forward, 'handover_to_generation', original)
    rig.events.clear()
    result = forward.recover_forward(rig.home, supervisor=rig.supervisor)
    assert result['outcome'] == 'rolled_back', result
    assert all(event[2] == rig.a.name for event in rig.events if event[0] == 'bootstrap')
    assert not any(event[0] == 'transfer_aborted' for event in rig.events)


@pytest.mark.parametrize('resume_at', [0, 61])
def test_recovered_rollback_still_requires_fresh_reply_with_original_deadline(rig, monkeypatch, resume_at):
    rig.supervisor.mode = 'dies'
    def crash(*args):
        raise KeyboardInterrupt('updater died after A-prime serving')
    monkeypatch.setattr(forward, '_fresh_reply', crash)
    with pytest.raises(KeyboardInterrupt):
        promote(rig)
    intent = json.loads((rig.home / 'forward-update.json').read_text(encoding='utf-8'))
    assert 'rollback' in intent
    rig.clock.value = resume_at
    monkeypatch.setattr(forward, '_fresh_reply', lambda *args: None)
    result = forward.recover_forward(rig.home, supervisor=rig.supervisor)
    assert result['outcome'] == 'rolled_back' and not result.get('alert', False)
    assert result['rollback']['new_id'] == intent['rollback']['new_id']
    assert result['rollback']['reply_observed'] is False
    assert result['rollback']['rollback_bound_met'] is True
    assert 'reply_seconds' not in result['rollback']
    assert rig.clock.value == pytest.approx(max(60, resume_at))
    assert not (rig.home / 'forward-update.json').exists()
    rig.supervisor.mode = 'happy'
    assert promote_different_release(rig)['outcome'] == 'success'


def test_positive_owned_final_reply_proof_rejects_interim_synthetic_and_other_owner(rig):
    import asyncio
    from gateway.outbox import Outbox
    from plugins.platforms.telegram.polling_transfer import PollingJournal
    journal = PollingJournal(rig.db, 'fake-token')
    journal.record_response(b'{"ok":true,"result":[{"update_id":7,"message":{"text":"fresh"}}]}')
    assert asyncio.run(journal.claim(7))
    asyncio.run(journal.accept(7))
    source = json.dumps({'version': 1, 'authorized': True, 'sender': 'fixture', 'profile': 'default',
                         'home': str(rig.home), 'token_hash': journal.token_hash}).encode()
    admitted, _ = rig.db.enqueue_owned(str(rig.home), 'telegram', 'chat-fresh', '7', 'message',
                                       source, b'{}', rig.old.id, 1)
    assert rig.db.disposition(admitted['id'], rig.old.id, 1, 'accepted')
    store = Outbox(rig.home)
    turn, _ = store.admit('default', 'telegram', 'update:7', 'text')
    store.finish_admission(turn, 'completed')
    interim = store.enqueue(turn, 'send', {'text': 'working', 'metadata': {'_interim_send': True}})
    assert store.begin_send(interim)
    store.receipt(interim, message_id='working', success=True)
    row = next(row for row in rig.db.generations() if row['id'] == rig.old.id)
    args = (rig.home, row, 1, 0, [journal.token_hash])
    until = forward.time.time() + 60
    assert forward._fresh_input(rig.home, row, 1, 0, until, [journal.token_hash])
    assert not forward._fresh_input(rig.home, row, 1, until, until + 60, [journal.token_hash])
    assert not forward._fresh_input(rig.home, row, 1, 0, 0, [journal.token_hash])
    assert not forward._fresh_input(rig.home, {**row, 'id': 'other-owner'}, 1, 0, until, [journal.token_hash])
    assert not forward._fresh_input(rig.home, row, 2, 0, until, [journal.token_hash])
    assert not forward._fresh_input(rig.home, row, 1, 0, until, ['other-token'])
    assert REAL_FRESH_REPLY(*args) is None
    store.enqueue_synthetic('fixture-gate', {'text': 'loopback'})
    assert REAL_FRESH_REPLY(*args) is None
    preview = store.enqueue(turn, 'send', {'text': 'partial', 'metadata': {'expect_edits': True}})
    assert store.begin_send(preview)
    store.receipt(preview, message_id='preview', success=True)
    assert REAL_FRESH_REPLY(*args) is None
    failed = store.enqueue(turn, 'edit_message', {'content': 'complete answer', 'message_id': 'preview', 'finalize': True})
    assert store.begin_send(failed)
    store.receipt(failed, message_id=None, success=False)
    assert REAL_FRESH_REPLY(*args) is None
    final = store.enqueue(turn, 'send', {'text': 'final answer'})
    assert store.begin_send(final)
    store.receipt(final, message_id='final', success=True)
    assert REAL_FRESH_REPLY(*args)['message_id'] == 'final'
    assert REAL_FRESH_REPLY(rig.home, {**row, 'id': 'different-generation'}, 1, 0, [journal.token_hash]) is None
    assert REAL_FRESH_REPLY(rig.home, row, 2, 0, [journal.token_hash]) is None
    assert REAL_FRESH_REPLY(rig.home, row, 1, 0, ['other-token']) is None
    final_edit = store.enqueue(turn, 'edit_message', {'content': 'edited final', 'message_id': 'final', 'finalize': True})
    assert store.begin_send(final_edit)
    store.receipt(final_edit, message_id='final', success=True)
    assert REAL_FRESH_REPLY(*args)['message_id'] == 'final'


def test_frozen_738c502c_cannot_enable_forward_code(rig):
    import importlib.util
    import sys
    from pathlib import Path
    path = Path(__file__).parents[1] / 'gateway/fixtures/generation_738c502c.py'
    spec = importlib.util.spec_from_file_location('previous_forward_fixture', path)
    previous = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = previous
    spec.loader.exec_module(previous)
    assert previous.overlap_handover_enabled({'gateway': {'forward_only_handover': {'enabled': True},
                                                         'overlap_handover': {'enabled': False}}}) is False
    legacy = release(rig.home, '738c502c', capable=False)
    assert forward.capable(legacy) is False


def test_bootstrap_crash_before_claim_is_booted_out_by_scope_readback(rig):
    rig.supervisor.mode = 'crash_unclaimed'
    with pytest.raises(KeyboardInterrupt):
        promote(rig)
    intent = json.loads((rig.home / 'forward-update.json').read_text(encoding='utf-8'))
    assert intent['successor']['bootstrap_scope']
    assert not intent['successor'].get('bootstrapped')
    result = forward.recover_forward(rig.home, supervisor=rig.supervisor)
    row = forward._row(rig.db, result['new_id'])
    assert (row['state'], row['verdict_evidence']) == ('exited', 'unclaimed')
    assert row['label'] not in rig.loaded
    assert ('bootout', row['id']) in rig.events


@pytest.mark.parametrize('mode', ['unclaimed', 'gate_failed'])
def test_failed_rollback_startup_retires_and_boots_out_only_fresh_standby(rig, monkeypatch, mode):
    rig.supervisor.mode = 'dies'
    original = rig.supervisor.bootstrap
    def bootstrap(row, path, timeout, **kwargs):
        rig.supervisor.mode = mode if row['release_sha'] == rig.a.name else 'dies'
        try:
            return original(row, path, timeout, **kwargs)
        finally:
            rig.supervisor.mode = 'dies'
    monkeypatch.setattr(rig.supervisor, 'bootstrap', bootstrap)
    result = promote(rig)
    assert result['outcome'] == 'blocked'
    row = forward._row(rig.db, result['rollback_generation']['id'])
    assert (row['state'], row['verdict']) == ('exited', 'failed')
    assert row['label'] not in rig.loaded
    assert forward._row(rig.db, rig.old.id)['state'] == 'draining'
    assert not any(event[0] == 'transfer_aborted' for event in rig.events)


@pytest.mark.parametrize('boundary', ['reserve', 'install', 'bootstrap', 'before_launch'])
def test_interrupted_rollback_launch_resumes_same_intent_with_original_budget(rig, monkeypatch, boundary):
    rig.supervisor.mode = 'dies'
    owner, name = (rig.db, 'reserve_generation') if boundary == 'reserve' else (
        rig.supervisor, 'bootstrap' if boundary == 'before_launch' else boundary)
    original = getattr(owner, name)
    def crash(*args, **kwargs):
        sha = kwargs.get('release_sha') if boundary == 'reserve' else args[0]['release_sha']
        if sha == rig.a.name:
            if boundary == 'before_launch':
                payload = plistlib.loads(args[1].read_bytes())
                kwargs['before_launch'](payload['EnvironmentVariables']['HERMES_GENERATION_SCOPE'])
            raise KeyboardInterrupt('interrupted rollback launch')
        return original(*args, **kwargs)
    monkeypatch.setattr(owner, name, crash)
    # Coordinator instances share the patched class method for the reserve boundary.
    if boundary == 'reserve':
        original_class = GenerationCoordinator.reserve_generation
        def crash_reserve(self, *args, **kwargs):
            if kwargs.get('release_sha') == rig.a.name:
                raise KeyboardInterrupt('before rollback reserve')
            return original_class(self, *args, **kwargs)
        monkeypatch.setattr(GenerationCoordinator, 'reserve_generation', crash_reserve)
    with pytest.raises(KeyboardInterrupt):
        promote(rig)
    intent = json.loads((rig.home / 'forward-update.json').read_text(encoding='utf-8'))
    if boundary == 'reserve':
        monkeypatch.setattr(GenerationCoordinator, 'reserve_generation', original_class)
    monkeypatch.setattr(owner, name, original)
    result = forward.recover_forward(rig.home, supervisor=rig.supervisor)
    assert result['outcome'] == 'rolled_back', result
    assert result['rollback']['new_id'] == intent['rollback_generation']['id']
    assert result['rollback_deadline_clock'] == intent['rollback_deadline_clock']
    assert result['rollback']['commit_to_reply_upper_bound_seconds'] <= 60


def test_terminal_intent_crash_finishes_archival_before_next_guardian_run(rig, monkeypatch):
    original = forward._archive
    monkeypatch.setattr(forward, '_archive', lambda *a: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        promote(rig)
    assert json.loads((rig.home / 'forward-update.json').read_text(encoding='utf-8'))['outcome'] == 'success'
    monkeypatch.setattr(forward, '_archive', original)
    result = forward.recover_forward(rig.home, supervisor=rig.supervisor)
    assert result['outcome'] == 'success'
    assert not (rig.home / 'forward-update.json').exists()
    assert forward.recover_forward(rig.home, supervisor=rig.supervisor) is None


def test_stopped_guardian_never_recovers_active_forward_intent(rig, monkeypatch):
    from hermes_cli import gateway_guardian
    (rig.home / 'forward-update.json').write_text('{}', encoding='utf-8')
    gateway_guardian.set_intent(rig.home, stopped=True)
    monkeypatch.setattr(forward, 'recover_forward', lambda *a, **k: pytest.fail('stop fence bypassed'))
    assert gateway_guardian.run_once(rig.home, rig.supervisor.directory / f'{rig.old.label}.plist',
                                    rig.old.label) == 'stopped'
    assert rig.events == []


def test_installed_guardian_follows_promoted_uuid_service_label(rig, monkeypatch):
    from hermes_cli import gateway_guardian
    result = promote(rig)
    inspected = []
    monkeypatch.setattr(gateway_guardian, '_run', lambda home, plist, label, **kwargs:
                        (inspected.append((plist, label)) or 'healthy'))
    assert gateway_guardian.run_once(rig.home, rig.supervisor.directory / f'{rig.old.label}.plist',
                                    rig.old.label) == 'healthy'
    assert inspected == [(rig.supervisor.directory / f"{result['new_label']}.plist", result['new_label'])]


def test_wedged_handover_timeout_preserves_termination_and_reply_budget(rig, monkeypatch):
    rig.supervisor.mode = 'wedged'
    original = forward.handover_to_generation
    def handover(home, to_id, **kwargs):
        if rig.db.leases()[0]['generation_id'] != rig.old.id:
            rig.clock.value += kwargs['timeout']
            raise RuntimeError('full cooperative timeout')
        return original(home, to_id, **kwargs)
    monkeypatch.setattr(forward, 'handover_to_generation', handover)
    def probe(*args, **kwargs):
        rig.clock.value += 3.4
        return 'wedged'
    monkeypatch.setattr('hermes_cli.gateway.probe_gateway_loop_liveness', probe)
    result = promote(rig)
    assert result['outcome'] == 'rolled_back', result
    assert any(event[0] == 'bounded-stop' for event in rig.events)
    assert result['rollback']['commit_to_reply_upper_bound_seconds'] <= 60


def test_pid_replaced_during_probe_is_taken_over_without_any_signal(rig, monkeypatch):
    rig.supervisor.mode = 'wedged'
    def probe(pid, **kwargs):
        rig.alive[pid] = 2.
        return 'wedged'
    monkeypatch.setattr('hermes_cli.gateway.probe_gateway_loop_liveness', probe)
    result = promote(rig)
    assert result['outcome'] == 'rolled_back', result
    assert not any(event[0] == 'bounded-stop' for event in rig.events)


def test_inventory_rejects_recycled_generation_pid_and_accepts_prompt_old_exit(rig):
    rig.alive[rig.old.pid] = 2.
    with pytest.raises(RuntimeError, match='additional fleet runtime'):
        forward.require_forward_inventory(rig.home, {'runtimes': [{'kind': 'gateway', 'pid': rig.old.pid}]})
    rig.alive[rig.old.pid] = 1.
    result = promote(rig)
    rig.db.heartbeat(rig.old.id, state='exited')
    rig.alive.pop(rig.old.pid)
    forward.require_forward_inventory(rig.home)
    assert forward.verify_forward(rig.home, result, supervisor=rig.supervisor)['outcome'] == 'success'


def test_strict_inventory_does_not_qualify_a_failed_probe_as_empty(rig, monkeypatch):
    from hermes_cli import update_inventory
    def collector(plan):
        with update_inventory._probe('fixture inventory'):
            raise OSError('process inventory unavailable')
    monkeypatch.setattr(update_inventory, '_collect_gateway_runtimes', lambda *a: collector(a[0]))
    monkeypatch.setattr(update_inventory, '_collect_install_shape', lambda *a: None)
    monkeypatch.setattr(update_inventory, '_collect_ledger_runtimes', lambda *a: None)
    # Exercise the real public collector, not the fixture's inventory seam.
    token = update_inventory._strict_inventory.set(True)
    try:
        with pytest.raises(RuntimeError, match='could not be qualified'):
            update_inventory._build_runtime_inventory()
    finally:
        update_inventory._strict_inventory.reset(token)


def test_reservation_collision_never_retires_or_boots_out_historical_owner(rig, monkeypatch):
    import uuid
    monkeypatch.setattr(forward, 'uuid', SimpleNamespace(uuid4=lambda: uuid.UUID(rig.old.id)))
    result = promote(rig)
    assert result['outcome'] == 'refused'
    assert forward._row(rig.db, rig.old.id)['state'] == 'serving'
    assert forward._row(rig.db, rig.old.id)['verdict'] is None
    assert rig.events == []


def test_launch_free_crash_intent_is_archived_after_armed_owner_observation(rig):
    forward._save(rig.home, {'outcome': 'running', 'old_id': rig.old.id, 'old_epoch': 1})
    result = forward.recover_forward(rig.home, supervisor=rig.supervisor)
    assert result['outcome'] == 'refused'
    assert not (rig.home / 'forward-update.json').exists()
    assert rig.db.leases()[0]['generation_id'] == rig.old.id


def test_recovered_rollback_ack_reconstructs_death_and_reply_timings(rig, monkeypatch):
    rig.supervisor.mode = 'dies'
    original = forward._flip
    def crash(home, db, row, proof, supervisor, **kwargs):
        if kwargs.get('operation') == 'rollback':
            raise KeyboardInterrupt('A-prime serving, before pointer and rollback receipt')
        return original(home, db, row, proof, supervisor, **kwargs)
    monkeypatch.setattr(forward, '_flip', crash)
    with pytest.raises(KeyboardInterrupt):
        promote(rig)
    monkeypatch.setattr(forward, '_flip', original)
    result = forward.recover_forward(rig.home, supervisor=rig.supervisor)
    assert result['outcome'] == 'rolled_back', result
    rollback = result['rollback']
    assert rollback['death_to_serving_seconds'] <= rollback['death_to_reply_seconds'] <= 60
    assert rollback['commit_to_serving_upper_bound_seconds'] <= rollback['commit_to_reply_upper_bound_seconds'] <= 60


@pytest.mark.parametrize('recovered', [False, True])
def test_successful_reply_read_cannot_cross_original_rollback_deadline(rig, monkeypatch, recovered):
    rig.supervisor.mode = 'unhealthy'
    def reply(*args):
        rig.clock.value = 61
        return {'message_id': 'too-late'}
    if recovered:
        monkeypatch.setattr(forward, '_fresh_reply', lambda *a: (_ for _ in ()).throw(KeyboardInterrupt()))
        with pytest.raises(KeyboardInterrupt):
            promote(rig)
        monkeypatch.setattr(forward, '_fresh_reply', reply)
        result = forward.recover_forward(rig.home, supervisor=rig.supervisor)
    else:
        monkeypatch.setattr(forward, '_fresh_reply', reply)
        result = promote(rig)
    assert result['outcome'] == 'rolled_back' and result['alert']
    assert result['rollback']['rollback_bound_met'] is False
    assert result['rollback']['reply_observed'] is False
    assert 'reply' not in result['rollback']
    assert 'reply_seconds' not in result['rollback']
    assert not (rig.home / 'forward-update.json').exists()
    rig.supervisor.mode = 'happy'
    assert promote_different_release(rig)['outcome'] == 'success'


@pytest.mark.parametrize('recovered', [False, True])
@pytest.mark.parametrize('input_state', ['accepted', 'pending'])
def test_unanswered_fresh_input_alerts_but_archives_completed_rollback(rig, monkeypatch, recovered, input_state):
    import asyncio
    from plugins.platforms.telegram.polling_transfer import PollingJournal
    rig.supervisor.mode = 'dies'
    journal = PollingJournal(rig.db, 'fake-token')
    request = rig.supervisor.request
    def polling(*args, **kwargs):
        proof = request(*args, **kwargs)
        if args[1] == 'polling_status':
            row = args[0]
            rig.db.record_poller_event(journal.token_hash, row['id'], proof['epoch'], 'poller_started')
            proof['tokens'] = [journal.token_hash]
        return proof
    monkeypatch.setattr(rig.supervisor, 'request', polling)
    flip = forward._flip
    def admit(home, db, row, proof, supervisor, **kwargs):
        result = flip(home, db, row, proof, supervisor, **kwargs)
        if kwargs.get('operation') == 'rollback':
            journal.record_response(b'{"ok":true,"result":[{"update_id":7,"message":{"text":"fresh"}}]}')
            assert asyncio.run(journal.claim(7))
            asyncio.run(journal.accept(7))
            source = json.dumps({'version': 1, 'authorized': True, 'sender': 'fixture', 'profile': 'default',
                                 'home': str(home), 'token_hash': journal.token_hash}).encode()
            inbox, _ = db.enqueue_owned(str(home), 'telegram', 'chat-fresh', '7', 'message',
                                        source, b'{}', row['id'], proof['epoch'])
            if input_state == 'accepted':
                assert db.disposition(inbox['id'], row['id'], proof['epoch'], 'accepted')
        return result
    monkeypatch.setattr(forward, '_flip', admit)
    if recovered:
        monkeypatch.setattr(forward, '_fresh_reply', lambda *a: (_ for _ in ()).throw(KeyboardInterrupt()))
        with pytest.raises(KeyboardInterrupt):
            promote(rig)
        monkeypatch.setattr(forward, '_flip', flip)
        monkeypatch.setattr(forward, '_fresh_reply', REAL_FRESH_REPLY)
        result = forward.recover_forward(rig.home, supervisor=rig.supervisor)
    else:
        monkeypatch.setattr(forward, '_fresh_reply', REAL_FRESH_REPLY)
        result = promote(rig)
    assert result['outcome'] == 'rolled_back' and result['alert']
    assert result['rollback']['fresh_input_observed'] is True
    assert result['rollback']['reply_observed'] is False
    assert result['rollback']['rollback_bound_met'] is False
    assert 'reply' not in result['rollback']
    assert rig.clock.value == pytest.approx(60)
    assert not (rig.home / 'forward-update.json').exists()
    rig.supervisor.mode = 'happy'
    assert promote_different_release(rig)['outcome'] == 'success'


@pytest.mark.parametrize('recovered', [False, True])
def test_late_rollback_polling_proof_alerts_but_does_not_block_next_promotion(rig, monkeypatch, recovered):
    rig.supervisor.mode = 'dies'
    poller = forward._poller
    def late(db, row, supervisor, deadline):
        proof = poller(db, row, supervisor, deadline)
        if row['release_sha'] == rig.a.name:
            rig.clock.value = 61
        return proof
    if recovered:
        flip = forward._flip
        def crash(*args, **kwargs):
            if kwargs.get('operation') == 'rollback':
                raise KeyboardInterrupt('rollback polling proved, pointers pending')
            return flip(*args, **kwargs)
        monkeypatch.setattr(forward, '_flip', crash)
        with pytest.raises(KeyboardInterrupt):
            promote(rig)
        monkeypatch.setattr(forward, '_flip', flip)
        monkeypatch.setattr(forward, '_poller', late)
        result = forward.recover_forward(rig.home, supervisor=rig.supervisor)
    else:
        monkeypatch.setattr(forward, '_poller', late)
        result = promote(rig)
    assert result['outcome'] == 'rolled_back' and result['alert']
    assert result['rollback']['commit_to_serving_upper_bound_seconds'] == 61
    assert result['rollback']['rollback_bound_met'] is False
    assert result['rollback']['reply_observed'] is False
    assert 'reply_seconds' not in result['rollback']
    assert not (rig.home / 'forward-update.json').exists()
    rig.supervisor.mode = 'happy'
    assert promote_different_release(rig)['outcome'] == 'success'


def cold_claimant(rig, sha):
    holder = forward._row(rig.db, rig.db.leases()[0]['generation_id'])
    rig.alive.clear()
    process = GenerationIdentity.create(release_sha=sha, label=holder['label'], pid=500,
                                        start_fingerprint='500:1.0')
    rig.alive[500] = 1.
    fresh = rig.db.claim_process(process, 'cold-start-scope')
    assert fresh and fresh.id != holder['id']
    epoch = rig.db.takeover_dead_generation('active_generation', holder['id'], fresh.id,
                                             bootout=lambda label: True)
    return fresh, epoch


@pytest.mark.parametrize('holder_sha,outcome', [('a', 'rolled_back'), ('b', 'success')])
def test_reboot_cold_claimant_supersedes_intent_and_retires_unclaimed_standby(rig, monkeypatch, holder_sha, outcome):
    rig.supervisor.mode = 'crash_unclaimed'
    with pytest.raises(KeyboardInterrupt):
        promote(rig)
    intent = json.loads((rig.home / 'forward-update.json').read_text(encoding='utf-8'))
    monkeypatch.setattr('gateway.generation._boot_id', lambda: 'reboot')
    sha = getattr(rig, holder_sha).name
    fresh, epoch = cold_claimant(rig, sha)
    # The independent cold start's release pointer is already consistent.
    (rig.home / 'current').unlink()
    (rig.home / 'current').symlink_to(getattr(rig, holder_sha))
    pointers = {name: (rig.home / name).readlink() if (rig.home / name).exists() else None
                for name in ('current', 'previous')}
    rig.supervisor.mode = 'happy'
    rig.events.clear()
    result = forward.recover_forward(rig.home, supervisor=rig.supervisor)
    assert result['outcome'] == outcome and result['recovered'], result
    assert result['superseded_by'] == {'id': fresh.id, 'label': fresh.label, 'sha': sha, 'epoch': epoch}
    assert not (rig.home / 'forward-update.json').exists()
    assert json.loads((rig.home / 'forward-update-last.json').read_text(encoding='utf-8')) == result
    assert pointers == {name: (rig.home / name).readlink() if (rig.home / name).exists() else None
                        for name in pointers}
    standby = forward._row(rig.db, intent['successor']['id'])
    assert (standby['state'], standby['verdict_evidence']) == ('exited', 'unclaimed')
    assert standby['label'] not in rig.loaded
    assert ('bootout', fresh.id) not in rig.events
    assert not any(event[0] in ('bootstrap', 'handover', 'transfer_aborted') for event in rig.events)
    verified = forward.verify_forward(rig.home, result, supervisor=rig.supervisor)
    assert verified['poller']['generation_id'] == fresh.id
    assert promote_different_release(rig)['outcome'] == 'success'


def test_rollback_holder_after_boot_change_archives_with_unprovable_timing(rig, monkeypatch):
    rig.supervisor.mode = 'dies'
    bootstrap = rig.supervisor.bootstrap
    def reboot_before_rollback_claim(row, *args, **kwargs):
        if row['release_sha'] == rig.a.name:
            monkeypatch.setattr('gateway.generation._boot_id', lambda: 'reboot')
            rig.alive.clear()
        return bootstrap(row, *args, **kwargs)
    monkeypatch.setattr(rig.supervisor, 'bootstrap', reboot_before_rollback_claim)
    monkeypatch.setattr(forward, '_fresh_reply', lambda *a: (_ for _ in ()).throw(KeyboardInterrupt()))
    with pytest.raises(KeyboardInterrupt):
        promote(rig)
    intent = json.loads((rig.home / 'forward-update.json').read_text(encoding='utf-8'))
    rollback_id = intent['rollback_generation']['id']
    # A-prime claimed an unconsumed reservation in the new boot. Its serving
    # proof is current, but the original coordinator's monotonic budget is not.
    assert forward._row(rig.db, rollback_id)['boot_id'] == 'reboot'
    rig.clock.value = .1
    monkeypatch.setattr(forward, '_fresh_reply', lambda *a: pytest.fail('cross-boot timing observation'))
    result = forward.recover_forward(rig.home, supervisor=rig.supervisor)
    assert result['outcome'] == 'rolled_back' and result['recovered'] and result['alert'], result
    assert result['rollback']['rollback_bound_met'] is None
    assert result['rollback']['timing_unprovable'] == 'boot changed'
    assert result['rollback']['reply_observed'] is False
    assert not (rig.home / 'forward-update.json').exists()
    assert rig.db.leases()[0]['generation_id'] == rollback_id
    assert releases.read_pointer(rig.home / 'current') == rig.a
    rig.supervisor.mode = 'happy'
    assert promote_different_release(rig)['outcome'] == 'success'


@pytest.mark.parametrize('reservation', [False, True])
def test_superseded_pointer_inconsistency_is_rechecked_without_flipping(rig, monkeypatch, reservation):
    if reservation:
        rig.supervisor.mode = 'crash_unclaimed'
        with pytest.raises(KeyboardInterrupt):
            promote(rig)
        intent = json.loads((rig.home / 'forward-update.json').read_text(encoding='utf-8'))
    else:
        intent = {'outcome': 'running', 'old_id': rig.old.id, 'old_epoch': 1,
                  'new_sha': rig.b.name, 'previous': str(rig.a)}
        forward._save(rig.home, intent)
    monkeypatch.setattr('gateway.generation._boot_id', lambda: 'reboot')
    fresh, epoch = cold_claimant(rig, rig.b.name)
    rig.supervisor.mode = 'happy'
    result = forward.recover_forward(rig.home, supervisor=rig.supervisor)
    assert result['outcome'] == 'blocked' and result['alert'], result
    assert 'release differs from current pointer' in result['failure']
    assert releases.read_pointer(rig.home / 'current') == rig.a
    if reservation:
        assert forward._row(rig.db, intent['successor']['id'])['state'] == 'standby'
    (rig.home / 'current').unlink()
    (rig.home / 'current').symlink_to(rig.b)
    recovered = forward.recover_forward(rig.home, supervisor=rig.supervisor)
    assert recovered['outcome'] == 'success' and recovered['recovered'], recovered
    assert recovered['superseded_by']['id'] == fresh.id
    assert recovered['superseded_by']['epoch'] == epoch
    assert not (rig.home / 'forward-update.json').exists()
    assert promote_different_release(rig)['outcome'] == 'success'


@pytest.mark.parametrize('resolution', ['healthy', 'handover', 'death'])
def test_live_successor_safety_block_clears_from_durable_state(rig, resolution):
    rig.supervisor.mode = 'blocked'
    blocked = promote(rig)
    assert blocked['outcome'] == 'blocked' and blocked['alert']
    original_deadline = blocked['rollback_deadline_clock']
    rig.supervisor.mode = 'happy'
    if resolution == 'death':
        failed = forward._row(rig.db, blocked['new_id'])
        rig.alive.pop(failed['pid'])
    elif resolution == 'handover':
        forward.handover_to_generation(rig.home, blocked['rollback_generation']['id'])
    recovered = forward.recover_forward(rig.home, supervisor=rig.supervisor)
    assert recovered['outcome'] == ('success' if resolution == 'healthy' else 'rolled_back'), recovered
    assert recovered['rollback_deadline_clock'] == original_deadline
    assert not (rig.home / 'forward-update.json').exists()
    assert not any(event[0] in ('bounded-stop', 'transfer_aborted') for event in rig.events)
    assert promote_different_release(rig)['outcome'] == 'success'


def test_failed_rollback_reservation_can_retry_inside_original_budget(rig, monkeypatch):
    rig.supervisor.mode = 'dies'
    bootstrap = rig.supervisor.bootstrap
    def fail_once(row, *args, **kwargs):
        if row['release_sha'] == rig.a.name:
            rig.supervisor.mode = 'gate_failed'
        try:
            return bootstrap(row, *args, **kwargs)
        finally:
            rig.supervisor.mode = 'dies'
    monkeypatch.setattr(rig.supervisor, 'bootstrap', fail_once)
    blocked = promote(rig)
    assert blocked['outcome'] == 'blocked' and blocked['alert']
    failed_id = blocked['rollback_generation']['id']
    assert forward._row(rig.db, failed_id)['state'] == 'exited'
    monkeypatch.setattr(rig.supervisor, 'bootstrap', bootstrap)
    rig.supervisor.mode = 'happy'
    recovered = forward.recover_forward(rig.home, supervisor=rig.supervisor)
    assert recovered['outcome'] == 'rolled_back', recovered
    assert recovered['rollback_generation']['id'] != failed_id
    assert recovered['rollback_clock'] == blocked['rollback_clock']
    assert recovered['rollback_deadline_clock'] == blocked['rollback_deadline_clock']
    assert recovered['rollback']['commit_to_reply_upper_bound_seconds'] <= 60
    assert not (rig.home / 'forward-update.json').exists()
    assert promote_different_release(rig)['outcome'] == 'success'


def test_cross_boot_dead_successor_does_not_reuse_monotonic_budget(rig, monkeypatch):
    handover = forward.handover_to_generation
    def crash(*args, **kwargs):
        handover(*args, **kwargs)
        raise KeyboardInterrupt('after commit')
    monkeypatch.setattr(forward, 'handover_to_generation', crash)
    with pytest.raises(KeyboardInterrupt):
        promote(rig)
    monkeypatch.setattr(forward, 'handover_to_generation', handover)
    monkeypatch.setattr('gateway.generation._boot_id', lambda: 'reboot')
    rig.alive.clear()
    rig.clock.value = 0
    rig.events.clear()
    result = forward.recover_forward(rig.home, supervisor=rig.supervisor)
    assert result['outcome'] == 'blocked' and result['alert'], result
    assert not any(event[0] in ('bootstrap', 'handover', 'bounded-stop') for event in rig.events)
    fresh, _ = cold_claimant(rig, rig.a.name)
    recovered = forward.recover_forward(rig.home, supervisor=rig.supervisor)
    assert recovered['outcome'] == 'rolled_back' and recovered['superseded_by']['id'] == fresh.id
    assert not (rig.home / 'forward-update.json').exists()


@pytest.mark.parametrize('condition', ['inventory', 'cleanup', 'foreign_scope'])
def test_superseded_safety_failure_is_rechecked_before_archival(rig, monkeypatch, condition):
    rig.supervisor.mode = 'crash_unclaimed'
    with pytest.raises(KeyboardInterrupt):
        promote(rig)
    intent = json.loads((rig.home / 'forward-update.json').read_text(encoding='utf-8'))
    standby = forward._row(rig.db, intent['successor']['id'])
    path = rig.supervisor.directory / f"{standby['label']}.plist"
    definition = path.read_bytes()
    monkeypatch.setattr('gateway.generation._boot_id', lambda: 'reboot')
    fresh, _ = cold_claimant(rig, rig.a.name)
    rig.supervisor.mode = 'happy'
    with monkeypatch.context() as blocked:
        if condition == 'inventory':
            blocked.setattr('hermes_cli.update_inventory.collect_runtime_inventory', lambda **kwargs:
                            SimpleNamespace(runtimes=[{'kind': 'gateway', 'pid': 999}]))
        elif condition == 'cleanup':
            blocked.setattr(rig.supervisor, 'bootout', lambda *a, **k:
                            (_ for _ in ()).throw(RuntimeError('bootout readback failed')))
        else:
            blocked.setattr(rig.supervisor, 'bootstrap_state', lambda *a: 'foreign')
        result = forward.recover_forward(rig.home, supervisor=rig.supervisor)
        assert result['outcome'] == 'blocked' and result['alert'], result
        assert (rig.home / 'forward-update.json').exists()
        assert rig.db.leases()[0]['generation_id'] == fresh.id
        if condition == 'foreign_scope':
            assert forward._row(rig.db, standby['id'])['state'] == 'standby'
            assert path.read_bytes() == definition
            assert standby['label'] in rig.loaded
        elif condition == 'inventory':
            assert 'additional fleet runtime' in result['failure']
            assert forward._row(rig.db, standby['id'])['state'] == 'exited'
            assert standby['label'] not in rig.loaded
    result = forward.recover_forward(rig.home, supervisor=rig.supervisor)
    assert result['outcome'] == 'rolled_back' and result['superseded_by']['id'] == fresh.id, result
    assert not (rig.home / 'forward-update.json').exists()
    assert standby['label'] not in rig.loaded
    assert promote_different_release(rig)['outcome'] == 'success'


def test_launch_free_recovery_probe_failure_alerts_and_rechecks(rig, monkeypatch):
    forward._save(rig.home, {'outcome': 'running', 'old_id': rig.old.id, 'old_epoch': 1})
    with monkeypatch.context() as blocked:
        blocked.setattr(rig.supervisor, 'request', lambda *a, **k:
                        (_ for _ in ()).throw(RuntimeError('polling status unavailable')))
        result = forward.recover_forward(rig.home, supervisor=rig.supervisor)
        assert result['outcome'] == 'blocked' and result['alert'], result
    result = forward.recover_forward(rig.home, supervisor=rig.supervisor)
    assert result['outcome'] == 'refused' and result['recovered'], result
    assert not (rig.home / 'forward-update.json').exists()


def test_exited_gateway_cleanup_precedes_inventory_retry(rig, monkeypatch):
    rig.supervisor.mode = 'gate_failed'
    bootout = rig.supervisor.bootout
    with monkeypatch.context() as blocked:
        blocked.setattr(rig.supervisor, 'bootout', lambda *a, **k:
                        (_ for _ in ()).throw(RuntimeError('bootout readback failed')))
        result = promote(rig)
        assert result['outcome'] == 'blocked'
    retired = forward._row(rig.db, result['successor']['id'])
    assert retired['state'] == 'exited' and retired['pid'] in rig.alive
    monkeypatch.setattr('hermes_cli.update_inventory.collect_runtime_inventory', lambda **kwargs:
                        SimpleNamespace(runtimes=[{'kind': 'gateway', 'pid': pid} for pid in rig.alive]))
    def cleaned(row, *args, **kwargs):
        result = bootout(row, *args, **kwargs)
        rig.alive.pop(row['pid'], None)
        return result
    monkeypatch.setattr(rig.supervisor, 'bootout', cleaned)
    result = forward.recover_forward(rig.home, supervisor=rig.supervisor)
    assert result['outcome'] == 'refused' and result['recovered'], result
    assert retired['label'] not in rig.loaded and retired['pid'] not in rig.alive
    assert not (rig.home / 'forward-update.json').exists()


def test_superseded_cleanup_cannot_retire_a_racing_standby_claim(rig, monkeypatch):
    rig.supervisor.mode = 'crash_unclaimed'
    with pytest.raises(KeyboardInterrupt):
        promote(rig)
    intent = json.loads((rig.home / 'forward-update.json').read_text(encoding='utf-8'))
    monkeypatch.setattr('gateway.generation._boot_id', lambda: 'reboot')
    fresh, _ = cold_claimant(rig, rig.a.name)
    rig.supervisor.mode = 'happy'
    discard = forward._discard_reservation
    def claim_before_discard(db, info, *args, **kwargs):
        rig.alive[701] = 1.
        assert db.claim_generation(info['id'], 701, '701:1.0', scope_nonce=info['bootstrap_scope'])
        return discard(db, info, *args, **kwargs)
    monkeypatch.setattr(forward, '_discard_reservation', claim_before_discard)
    result = forward.recover_forward(rig.home, supervisor=rig.supervisor)
    assert result['outcome'] == 'rolled_back' and result['superseded_by']['id'] == fresh.id, result
    row = forward._row(rig.db, intent['successor']['id'])
    assert row['state'] == 'standby' and row['verdict'] is None and row['pid'] == 701
    assert row['label'] in rig.loaded
    assert ('bootout', row['id']) not in rig.events


@pytest.mark.parametrize('reboot', [False, True])
def test_unclaimed_job_cleanup_precedes_recovery_inventory(rig, monkeypatch, reboot):
    rig.supervisor.mode = 'crash_unclaimed'
    with pytest.raises(KeyboardInterrupt):
        promote(rig)
    intent = json.loads((rig.home / 'forward-update.json').read_text(encoding='utf-8'))
    if reboot:
        monkeypatch.setattr('gateway.generation._boot_id', lambda: 'reboot')
        fresh, _ = cold_claimant(rig, rig.a.name)
    rig.supervisor.mode = 'happy'
    # A bootstrapped gateway can be in the process scan before it claims its row.
    rig.alive[701] = 1.
    monkeypatch.setattr('hermes_cli.update_inventory.collect_runtime_inventory', lambda **kwargs:
                        SimpleNamespace(runtimes=[{'kind': 'gateway', 'pid': pid} for pid in rig.alive]))
    bootout = rig.supervisor.bootout
    def cleaned(row, *args, **kwargs):
        result = bootout(row, *args, **kwargs)
        if row['id'] == intent['successor']['id']:
            rig.alive.pop(701)
        return result
    monkeypatch.setattr(rig.supervisor, 'bootout', cleaned)
    monkeypatch.setattr(forward, 'GenerationSupervisor', lambda home: rig.supervisor)
    result = forward.activate_if_forward(rig.home, rig.b, rig.b.name)
    assert result['outcome'] == ('rolled_back' if reboot else 'refused') and result['recovered'], result
    if reboot:
        assert result['superseded_by']['id'] == fresh.id
    assert not (rig.home / 'forward-update.json').exists()
    assert 701 not in rig.alive


def test_superseding_other_release_archives_refused_without_pointer_writes(rig, monkeypatch):
    rig.supervisor.mode = 'crash_unclaimed'
    with pytest.raises(KeyboardInterrupt):
        promote(rig)
    third = release(rig.home, 'c' * 40)
    monkeypatch.setattr('gateway.generation._boot_id', lambda: 'reboot')
    fresh, epoch = cold_claimant(rig, third.name)
    (rig.home / 'current').unlink()
    (rig.home / 'current').symlink_to(third)
    monkeypatch.setattr(forward, '_flip', lambda *a, **k: pytest.fail('superseded pointer write'))
    rig.supervisor.mode = 'happy'
    result = forward.recover_forward(rig.home, supervisor=rig.supervisor)
    assert result['outcome'] == 'refused' and result['recovered'], result
    assert result['superseded_by'] == {'id': fresh.id, 'label': fresh.label, 'sha': third.name, 'epoch': epoch}
    assert not (rig.home / 'forward-update.json').exists()
    assert releases.read_pointer(rig.home / 'current') == third
    assert not (rig.home / 'previous').exists()


def test_review2_route_probe_never_creates_coordinator_db(rig):
    (rig.home / 'gateway-coordinator.db').unlink()
    for suffix in ('-wal', '-shm'):
        (rig.home / f'gateway-coordinator.db{suffix}').unlink(missing_ok=True)
    assert not forward.forward_route(rig.home, rig.b)
    assert not (rig.home / 'gateway-coordinator.db').exists()


def test_review2_archived_intent_race_reobserves_instead_of_s2(rig, monkeypatch):
    intent = rig.home / 'forward-update.json'
    intent.write_text(json.dumps({'outcome': 'running'}), encoding='utf-8')
    def archived_by_other_updater(home, **kwargs):
        intent.unlink()
        return None
    monkeypatch.setattr(forward, 'recover_forward', archived_by_other_updater)
    result = forward.activate_if_forward(rig.home, rig.b, rig.b.name, supervisor=rig.supervisor)
    assert result is not None and result['outcome'] == 'success' and result['new_sha'] == rig.b.name


def test_review2_fleet_catchup_success_clears_restart_obligation(rig, monkeypatch):
    from hermes_cli import update_cmd_fleet
    result = promote(rig)
    cleared = []
    monkeypatch.setattr(forward, 'recover_forward', lambda home: result)
    monkeypatch.setattr(forward, 'require_forward_inventory', lambda home: None)
    monkeypatch.setattr(update_cmd_fleet, '_clear_fleet_restart_pending_marker', lambda: cleared.append(True))
    update_cmd_fleet._apply_pending_fleet_restart_catchup()
    assert cleared == [True]


def test_review3_successor_death_after_budget_still_starts_previous_release(rig):
    """A blocked intent must never become a permanent zero-poller outage."""
    rig.supervisor.mode = 'blocked'
    blocked = promote(rig)
    assert blocked['outcome'] == 'blocked' and blocked['alert']
    rig.supervisor.mode = 'happy'
    rig.clock.value = blocked['commit_clock'] + forward.ROLLBACK_SECONDS + 300
    failed = forward._row(rig.db, blocked['new_id'])
    rig.alive.pop(failed['pid'])
    recovered = forward.recover_forward(rig.home, supervisor=rig.supervisor)
    assert recovered['outcome'] == 'rolled_back', recovered
    assert recovered['late_rollback']['bound_missed'] is True
    assert recovered['rollback']['rollback_bound_met'] is False and recovered['alert']
    lease = rig.db.leases()[0]
    assert forward._row(rig.db, lease['generation_id'])['release_sha'] == rig.a.name
    assert not (rig.home / 'forward-update.json').exists()


def test_review3_live_successor_after_budget_stays_blocked_without_signals(rig):
    rig.supervisor.mode = 'blocked'
    blocked = promote(rig)
    rig.clock.value = blocked['commit_clock'] + forward.ROLLBACK_SECONDS + 300
    rig.events.clear()
    recovered = forward.recover_forward(rig.home, supervisor=rig.supervisor)
    assert recovered['outcome'] == 'blocked' and recovered['alert']
    assert not any(event[0] in ('bootstrap', 'bounded-stop', 'takeover') for event in rig.events)
    assert (rig.home / 'forward-update.json').exists()


def test_review3_unresolved_earlier_intent_never_reports_candidate_success(rig):
    rig.supervisor.mode = 'blocked'
    assert promote(rig)['outcome'] == 'blocked'
    candidate = release(rig.home, 'c' * 40)
    result = forward.activate_if_forward(rig.home, candidate, candidate.name, supervisor=rig.supervisor)
    assert result['outcome'] == 'blocked' and result['new_sha'] == candidate.name
    assert result['unresolved_new_sha'] == rig.b.name
    assert forward.read_pointer(rig.home / 'current') != candidate


def test_review3_recovered_earlier_intent_continues_to_candidate(rig):
    rig.supervisor.mode = 'blocked'
    assert promote(rig)['outcome'] == 'blocked'
    rig.supervisor.mode = 'happy'
    candidate = release(rig.home, 'c' * 40)
    result = forward.activate_if_forward(rig.home, candidate, candidate.name, supervisor=rig.supervisor)
    assert result['outcome'] == 'success' and result['new_sha'] == candidate.name, result
    assert forward.read_pointer(rig.home / 'current') == candidate


def test_review3_poller_deadline_names_the_real_failure(rig, monkeypatch):
    row = forward._row(rig.db, rig.db.leases()[0]['generation_id'])
    monkeypatch.setattr(rig.supervisor, 'request', lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError('successor lacks a durable poller start')))
    with pytest.raises(RuntimeError, match='last poller failure: successor lacks a durable poller start'):
        forward._poller(rig.db, row, rig.supervisor, forward._now() + 1)
