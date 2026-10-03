"""Forward-only release promotion. Pointers follow the lease and polling proof.

The durable intent survives updater death. Recovery observes a committed owner,
never repeats its transfer. Rollback always launches a fresh previous-release owner.
"""
from __future__ import annotations

from contextlib import closing, contextmanager
from dataclasses import asdict
import json
import os
from pathlib import Path
import plistlib
import re
import sqlite3
import subprocess
import tempfile
import time
import uuid

from gateway.generation import GenerationCoordinator, GenerationIdentity, generation_paths, forward_only_handover_enabled
from gateway import deadline as gateway_deadline
from gateway.deadline import begin_immediate, connect_sqlite, deadline_scope, with_deadline_scope
from gateway.run_generation import handover_to_generation, _generation_request, HANDOVER_ABORT_RESERVE
from hermes_cli.gateway_launchd_generation import generation_launchd_label, render_generation_launchd_plist, bootstrap_generation_plist
from hermes_cli.immutable_releases import ReleasePaths, read_pointer, _atomic_bytes, _atomic_json, _sync_dir, _release_is_ready, activate_release

STARTUP_SECONDS = 45
ROLLBACK_SECONDS = 60
# A successor's loop heartbeat is rewritten every 30 s. One missed beat plus margin,
# confirmed by the sustained silent loop-tick witness, is the wedge proof here: the
# default 90 s threshold cannot fire inside the 60 s rollback bound.
WEDGE_STALE_SECONDS = 35
# Bounded SIGTERM/SIGKILL, takeover and A-prime polling proof after a wedge proof.
WEDGE_RESERVE_SECONDS = 15
WEDGE_RECOVERY_RESERVE_SECONDS = WEDGE_RESERVE_SECONDS + 3.4
COOPERATIVE_ROLLBACK_SECONDS = 10
POLL_SECONDS = 5
# A committed successor must prove polling inside this share of the rollback
# budget so A' keeps STARTUP_SECONDS to start, take over and poll.
POLL_PROOF_SECONDS = ROLLBACK_SECONDS - STARTUP_SECONDS
_REARM_FENCES = ('poller', 'cron', 'kanban', 'goal_wakeup')


def _now():
    return gateway_deadline.now()


def _sleep(seconds):
    time.sleep(seconds)


def capable(release: Path | None) -> bool:
    """An import-free, versioned declaration shipped by the release itself."""
    if release is None:
        return False
    path = release / 'hermes_cli/release-capabilities.json'
    if not path.exists():
        return False
    data = json.loads(path.read_text(encoding='utf-8-sig'))
    return type(data.get('forward_only_handover')) is int and data['forward_only_handover'] == 1


def _config(home):
    from hermes_cli.config_effective import load_user_config_effective
    return load_user_config_effective(Path(home) / 'config.yaml', fail_closed=True)


def forward_route(home: Path, candidate: Path) -> bool:
    """A first install follows S2. Never infer N-1 support from the updater's code."""
    if not forward_only_handover_enabled(_config(home)) or not capable(candidate):
        return False
    paths = ReleasePaths.for_home(home)
    if not capable(read_pointer(paths.current)):
        return False
    if not (Path(home) / 'gateway-coordinator.db').exists():
        return False  # A route probe never creates coordinator state.
    db = GenerationCoordinator(home)
    lease = next((row for row in db.leases() if row['resource'] == 'active_generation'), None)
    if lease is None:
        return False  # A flag-off serving process has no forward coordinator claim.
    serving = _row(db, lease['generation_id'])
    intent = Path(home) / 'forward-update.json'
    pending = json.loads(intent.read_text(encoding='utf-8-sig')) if intent.exists() else {}
    unresolved = pending.get('outcome') in {'running', 'blocked'} or pending.get('bookkeeping_pending')
    if lease['state'] == 'released' and serving['state'] == 'exited' and not unresolved:
        return False  # Clean stop retained as history, not a serving handover pair.
    return capable(paths.release(serving['release_sha']))


def activate_if_forward(home, candidate, sha, *, supervisor=None):
    """Return None to preserve the existing S2 call sequence exactly."""
    if not forward_route(home, candidate):
        return None
    intent = Path(home) / 'forward-update.json'
    if intent.exists():
        record = json.loads(intent.read_text(encoding='utf-8-sig'))
        if record.get('outcome') in {'running', 'blocked'} or record.get('bookkeeping_pending'):
            # None means another updater archived the intent first: continue to the candidate.
            recovered = recover_forward(home, supervisor=supervisor)
            if recovered is not None and recovered.get('new_sha') == sha:
                return recovered  # The interrupted attempt was this candidate: its outcome answers it.
            if recovered is not None and (intent.exists() or recovered.get('outcome') not in
                                          {'success', 'rolled_back', 'refused', 'aborted'}):
                # An earlier update is still unresolved. Report it against this candidate.
                proof = {'outcome': recovered.get('outcome', 'blocked'), 'alert': True, 'new_sha': sha,
                         'failure': 'earlier forward update unresolved: ' + str(
                             recovered.get('failure') or recovered.get('outcome')),
                         'unresolved_new_sha': recovered.get('new_sha')}
                from hermes_cli.update_receipt import record_forward_generation
                record_forward_generation(proof)
                return proof
    try:
        require_forward_inventory(home)
    except RuntimeError as exc:
        proof = {'outcome': 'blocked', 'alert': True, 'failure': str(exc), 'new_sha': sha}
        _atomic_json(Path(home) / 'forward-update-last.json', proof)
        from hermes_cli.update_receipt import record_forward_generation
        record_forward_generation(proof)
        return proof
    if read_pointer(ReleasePaths.for_home(home).current) == candidate:
        return observe_current_forward(home)
    result = promote_forward(home, candidate, sha, supervisor=supervisor)
    from hermes_cli.update_receipt import record_release_transition
    if result['outcome'] == 'success':
        record_release_transition(from_sha=result['old_sha'], to_sha=sha,
            from_path=result['previous'], to_path=result['current'], kind='forward_promotion')
    return result


def require_forward_inventory(home, plan=None):
    """Qualify gateway owners. Other runtime kinds do not hold polling leases."""
    from hermes_cli.update_inventory import collect_runtime_inventory
    if plan is None:
        plan = collect_runtime_inventory(strict=True)
    runtimes = plan.get('runtimes') if isinstance(plan, dict) else getattr(plan, 'runtimes', None)
    if runtimes is None:
        raise RuntimeError('forward-only fleet inventory is unavailable')
    db = GenerationCoordinator(home)
    rows = db.generations()
    owned = {row['pid'] for row in rows if row['state'] != 'exited' and _live(row) and not db._owner_is_dead(row)}
    others = []
    for runtime in runtimes:
        kind = runtime.get('kind') if isinstance(runtime, dict) else runtime.kind
        pid = runtime.get('pid') if isinstance(runtime, dict) else runtime.pid
        if kind != 'gateway':
            others.append(dict(runtime) if isinstance(runtime, dict) else asdict(runtime))
        elif pid not in owned:
            raise RuntimeError('forward-only slice cannot qualify an additional fleet runtime')
    from hermes_cli.update_receipt import record_forward_inventory
    record_forward_inventory(others)


def observe_current_forward(home, *, supervisor=None):
    """Observe the current claimant, including a fresh cold-start claim."""
    db = GenerationCoordinator(home)
    row = _row(db, _lease(db)['generation_id'])
    paths = ReleasePaths.for_home(home)
    proof = {'outcome': 'success', 'old_id': row['id'], 'new_id': row['id'],
             'old_label': row['label'], 'new_label': row['label'],
             'old_sha': row['release_sha'], 'new_sha': row['release_sha'],
             'current': str(paths.release(row['release_sha'])),
             'previous': str(read_pointer(paths.previous)) if read_pointer(paths.previous) else None}
    try:
        return verify_forward(home, proof, supervisor=supervisor)
    except (RuntimeError, OSError) as exc:
        return {**proof, 'outcome': 'blocked', 'alert': True, 'failure': str(exc)}


def _proof_deadline(record, row):
    """Use a durable commit budget only in the boot that recorded its clock.

    When that window is gone (expired, other boot, or never recorded) this is a
    re-observation of an already-committed holder, not a new handover window:
    it gets the named POLL_SECONDS observation reserve, min-ed with any
    enclosing scope, and the record is marked so the result is never reported
    as proof inside the original bound.
    """
    from gateway.generation import _boot_id
    if record.get('commit_clock') is not None and record.get('commit_boot_id') == row['boot_id'] == _boot_id():
        rollback = row['id'] == (record.get('rollback_generation') or {}).get('id')
        deadline = record['commit_clock'] + (ROLLBACK_SECONDS if rollback else POLL_PROOF_SECONDS)
        if deadline > _now():
            return deadline
    record['proof_window'] = 'reobservation'
    deadline = _now() + POLL_SECONDS
    enclosing = gateway_deadline.current()
    return deadline if enclosing is None else min(deadline, enclosing)


def verify_forward(home, record, *, supervisor=None):
    db = GenerationCoordinator(home)
    supervisor = supervisor or GenerationSupervisor(home)
    row = _row(db, (record.get('superseded_by') or {}).get('id') or record['new_id'])
    proof = _poller(db, row, supervisor, deadline=_proof_deadline(record, row))
    paths = ReleasePaths.for_home(home)
    if read_pointer(paths.current) != paths.release(row['release_sha']):
        raise RuntimeError('forward pointer does not name the serving generation')
    cleanup_exited(home, supervisor=supervisor)
    return {**record, 'poller': proof}


def _identity(row):
    return GenerationIdentity(**{key: row[key] for key in GenerationIdentity.__dataclass_fields__})


def _row(db, generation_id):
    return next(row for row in db.generations() if row['id'] == generation_id)


def _lease(db):
    lease = next((row for row in db.leases() if row['resource'] == 'active_generation'), None)
    if lease is None:
        raise RuntimeError('no generation lease')
    return lease


def _live(row):
    from gateway.status import _get_process_start_time, _pid_exists
    pid = row['pid']
    start = _get_process_start_time(pid) if pid and _pid_exists(pid) else None
    from gateway.generation import _boot_id, generation_start_fingerprint_matches
    return row['boot_id'] == _boot_id() and generation_start_fingerprint_matches(row, start) is True


@contextmanager
def _update_lock(home):
    import fcntl  # macOS updater, no launchd work on other hosts.
    with (Path(home) / 'forward-update.lock').open('a+') as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            yield False
            return
        yield True


def _locked():
    return {'outcome': 'locked', 'failure': 'another updater is running'}


class GenerationSupervisor:
    """Exact-label launchd operations, with an injectable command runner."""
    def __init__(self, home, *, runner=None, directory=None, domain=None):
        self.home = Path(home).resolve()
        self.runner = runner or subprocess.run
        self.directory = directory or Path.home() / 'Library/LaunchAgents'
        self.domain = domain

    def _domain(self, label, *, timeout=None):
        from hermes_cli.gateway_guardian import _gateway_domain
        return _gateway_domain(label, self.domain, runner=self.runner,
                               timeout=10 if timeout is None else timeout)

    def _definition(self, label):
        path = self.directory / f'{label}.plist'
        data = plistlib.loads(path.read_bytes())
        if (data.get('Label') != label or
                data.get('EnvironmentVariables', {}).get('HERMES_HOME') != str(self.home)):
            raise RuntimeError('generation launch agent belongs to another installation')
        return path, data

    def install(self, row, release):
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self.directory / f"{row['label']}.plist"
        if path.exists():
            self._definition(row['label'])
            raise RuntimeError('fresh generation already has a launch agent')
        body = render_generation_launchd_plist(slot=row['id'], release_sha=row['release_sha'],
            release_root=release, interpreter=release / '.venv/bin/python', hermes_home=self.home)
        data = plistlib.loads(body.encode())
        # bootstrap is explicit. A standby must never be eligible at next login.
        data['RunAtLoad'] = False
        data['KeepAlive'] = False
        _atomic_bytes(path, plistlib.dumps(data))
        return path

    def bootstrap(self, row, path, timeout, *, before_launch=None):
        deadline = _now() + min(30, timeout)
        self._definition(row['label'])
        domain = self._domain(row['label'], timeout=_remaining(deadline, 10))
        from hermes_cli.gateway_guardian import _launch_state
        if _launch_state(domain, row['label'], runner=self.runner,
                         timeout=_remaining(deadline, 5)) != 'unloaded':
            raise RuntimeError('generation label was already loaded before bootstrap')
        _deadline_check(deadline)
        # launchd retains the loaded definition. It needs crash respawn when this
        # standby becomes holder, but SuccessfulExit implies RunAtLoad at login
        # (launchd.plist(5)). Bootstrap that definition outside LaunchAgents.
        with tempfile.TemporaryDirectory(prefix='.generation-bootstrap-', dir=self.home) as scratch:
            runtime_path = Path(scratch) / path.name
            _, data = self._definition(row['label'])
            data.update(RunAtLoad=True, KeepAlive={'SuccessfulExit': False})
            _atomic_bytes(runtime_path, plistlib.dumps(data))
            def bind_scope(scope):
                _, login = self._definition(row['label'])
                login['EnvironmentVariables']['HERMES_GENERATION_SCOPE'] = scope
                _atomic_bytes(path, plistlib.dumps(login))
                if before_launch is not None:
                    before_launch(scope)
            bootstrap_generation_plist(domain=domain, plist_path=runtime_path,
                                       label=row['label'], runner=self.runner,
                                       timeout=_remaining(deadline, 30),
                                       before_launch=bind_scope)
            if _now() >= deadline:
                raise RuntimeError('forward update deadline exceeded')

    def owns_bootstrap(self, row, scope):
        """Prove an unclaimed loaded job belongs to our interrupted bootstrap."""
        return self.bootstrap_state(row, scope) == 'owned'

    def bootstrap_state(self, row, scope, *, timeout=5):
        deadline = _now() + timeout
        domain = self._domain(row['label'], timeout=_remaining(deadline, 10))
        result = self.runner(['launchctl', 'print', f"{domain}/{row['label']}"],
                             capture_output=True, text=True, encoding='utf-8',
                             timeout=_remaining(deadline, 5))
        _deadline_check(deadline)
        if result.returncode != 0:
            from hermes_cli.gateway_guardian import _launch_state
            state = _launch_state(domain, row['label'], runner=self.runner,
                                  timeout=_remaining(deadline, 5))
            _deadline_check(deadline)
            if state == 'unloaded':
                return 'unloaded'
            raise RuntimeError('cannot establish interrupted bootstrap ownership')
        def value(name):
            match = re.search(r'^\s*' + name + r'\s*=>\s*(.*?)\s*$', result.stdout, re.MULTILINE)
            return match[1] if match else None
        return 'owned' if value('HERMES_HOME') == str(self.home) and value('HERMES_GENERATION_SCOPE') == scope else 'foreign'

    def bootout(self, row, timeout=15):
        deadline = _now() + min(15, timeout)
        if row['state'] != 'exited':
            raise RuntimeError('cannot bootout a non-exited generation')
        path = self.directory / f"{row['label']}.plist"
        if path.exists():
            self._definition(row['label'])
        domain = self._domain(row['label'], timeout=_remaining(deadline, 10))
        self.runner(['launchctl', 'bootout', f"{domain}/{row['label']}"],
                    capture_output=True, timeout=_remaining(deadline, 15))
        from hermes_cli.gateway_guardian import _launch_state
        while True:
            if _now() >= deadline:
                raise RuntimeError('generation bootout readback failed')
            if _launch_state(domain, row['label'], runner=self.runner,
                             timeout=_remaining(deadline, 5)) == 'unloaded':
                if _now() >= deadline:
                    raise RuntimeError('generation bootout readback failed')
                break
            _sleep(min(.05, _remaining(deadline, .05)))
        if path.exists():
            self._definition(row['label'])
            path.unlink()
            _sync_dir(self.directory)
        return True

    def boot_active(self, row, active):
        path, data = self._definition(row['label'])
        data['RunAtLoad'] = active
        data['KeepAlive'] = {'SuccessfulExit': False} if active else False
        if active:
            data['ProgramArguments'] = [arg for arg in data['ProgramArguments'] if arg != '--standby']
        _atomic_bytes(path, plistlib.dumps(data))
        _, readback = self._definition(row['label'])
        if readback['RunAtLoad'] is not active or readback['KeepAlive'] != data['KeepAlive']:
            raise RuntimeError('generation login policy readback failed')

    @with_deadline_scope
    def ready(self, row, *, deadline=None):
        if row['pid'] is None or not _live(row) or row['verdict'] is not None:
            return False
        if deadline is not None and _now() >= deadline:
            return False
        # This generation-scoped record is published only AFTER the startup gate.
        path = generation_paths(self.home, _identity(row))['state']
        try:
            record = json.loads(path.read_text(encoding='utf-8-sig'))
        except FileNotFoundError:
            return False
        if not (all(record.get(key) == row[key] for key in GenerationIdentity.__dataclass_fields__)
                and record.get('state') in {'standby', 'serving'} and bool(record.get('socket_path'))):
            return False
        if record['state'] == 'serving':
            # Cold takeover publishes the socket before adapters start. Finish
            # startup inside its existing budget before the short poller proof.
            try:
                request_timeout = _remaining(deadline, 2) if deadline is not None else 2
                status = self.request(row, 'polling_status', timeout=request_timeout)
            except RuntimeError:
                return False
            if deadline is not None and _now() >= deadline:
                return False
            return status.get('polling') is True and status.get('healthy') is True and bool(status.get('tokens'))
        if deadline is not None and _now() >= deadline:
            return False
        return True

    def request(self, row, verb, *, params=None, timeout=2.0):
        return _generation_request(generation_paths(self.home, _identity(row))['socket'],
                                   verb, params=params, timeout=timeout)


def _deadline_check(deadline):
    if _now() >= deadline:
        raise RuntimeError('forward update deadline exceeded')


def _remaining(deadline, cap):
    remaining = deadline - _now()
    if remaining <= 0:
        raise RuntimeError('forward update deadline exceeded')
    return min(cap, remaining)


@with_deadline_scope
def _wait_ready(db, generation_id, supervisor, deadline):
    while True:
        row = _row(db, generation_id)
        _deadline_check(deadline)
        if row['verdict'] is not None or row['state'] == 'exited':
            raise RuntimeError('standby failed its startup gate')
        if supervisor.ready(row, deadline=deadline):
            _deadline_check(deadline)
            return row
        _sleep(min(.2, _remaining(deadline, .2)))


def _all_fences_armed(proof):
    return all((proof.get('armed') or {}).get(key) is True for key in _REARM_FENCES)


def _frozen_transfer_tokens(db, row, lease):
    """Return the durable roster for this committed transfer, if this is one."""
    with closing(db.connect()) as conn:
        transfer = conn.execute(
            "SELECT old_id,epoch FROM generation_transfers "
            "WHERE new_id=? AND epoch=? AND state='committed'",
            (row['id'], lease['epoch'] - 1)).fetchone()
    if transfer is None:
        return None
    return {receipt['token_hash'] for receipt in db.transfer_receipts(
        transfer['old_id'], transfer['epoch'])}


@with_deadline_scope
def _poller(db, row, supervisor, deadline):
    last_failure = ''
    clock_error = False
    while True:
        try:
            _deadline_check(deadline)
        except RuntimeError as exc:
            if last_failure:
                raise RuntimeError(f'{exc}; last poller failure: {last_failure}') from exc
            raise
        current = _row(db, row['id'])
        _deadline_check(deadline)
        lease = _lease(db)
        _deadline_check(deadline)
        if lease['generation_id'] != row['id'] or lease['state'] != 'active' or current['state'] != 'serving':
            raise RuntimeError('poller proof owner changed')
        if db._owner_is_dead(current):
            raise RuntimeError('successor died')
        _deadline_check(deadline)
        def budget(cap):
            try:
                return _remaining(deadline, cap)
            except RuntimeError as exc:
                if last_failure:  # Name the real cause, not only the clock.
                    raise RuntimeError(f'{exc}; last poller failure: {last_failure}') from exc
                raise
        wait = budget(2)
        try:
            proof = supervisor.request(current, 'polling_status', timeout=wait)
            try:
                _deadline_check(deadline)
            except OSError:
                # A transient clock read failure is not evidence that the proof was
                # late; let recovery reconcile durable pointer state first.
                clock_error = True
            if (proof.get('generation_id'), proof.get('release_sha'), proof.get('epoch'),
                    proof.get('polling'), proof.get('healthy')) == (
                    current['id'], current['release_sha'], lease['epoch'], True, True):
                if not _all_fences_armed(proof):
                    raise RuntimeError('successor has an unarmed rearm fence')
                tokens = proof.get('tokens')
                if (not isinstance(tokens, list) or any(not isinstance(token, str) for token in tokens)
                        or len(set(tokens)) != len(tokens) or not tokens):
                    raise RuntimeError('successor reported an invalid polling roster')
                actual_tokens = set(tokens)
                expected_tokens = _frozen_transfer_tokens(db, current, lease)
                _deadline_check(deadline)
                if expected_tokens is None:
                    expected_tokens = actual_tokens
                if not expected_tokens:
                    raise RuntimeError('successor transfer has an empty frozen polling roster')
                if actual_tokens != expected_tokens:
                    raise RuntimeError('successor polling roster differs from frozen transfer roster')
                if proof.get('release_root') != str(ReleasePaths.for_home(db.home).release(current['release_sha'])):
                    raise RuntimeError('successor did not acknowledge its loaded release tree')
                # Socket answers alone never authorize a pointer flip under a stale lease.
                if _lease(db) != lease or not _live(current):
                    raise RuntimeError('poller identity changed during observation')
                _deadline_check(deadline)
                starts = {event['token_hash']: event['wall_at'] for event in db.poller_journal()
                          if event['generation_id'] == row['id'] and event['epoch'] == lease['epoch']
                          and event['event'] == 'poller_started' and event['token_hash'] in expected_tokens}
                _deadline_check(deadline)
                missing = expected_tokens - starts.keys()
                if missing:
                    raise RuntimeError('successor lacks durable poller starts for: ' + ', '.join(sorted(missing)))
                proof['poller_started_at'] = max(starts.values())
                proof.update(pid=current['pid'], label=current['label'])
                if clock_error:
                    proof['_deadline_clock_error'] = True
                else:
                    _deadline_check(deadline)
                return proof
        except RuntimeError as exc:
            proof, last_failure = {}, str(exc)
        if proof.get('healthy') is False:
            raise RuntimeError('successor reports unhealthy runtime')
        _sleep(min(.2, budget(.2)))


def _save(home, record):
    _atomic_json(Path(home) / 'forward-update.json', record)


def _finish(home, record, outcome, **fields):
    info = record.get('successor') or {}
    record.update(new_id=info.get('id'), new_label=info.get('label'))
    record.update(fields, outcome=outcome, finished_at=time.time())
    if outcome == 'blocked':
        record['alert'] = True
    if outcome == 'rolled_back':
        # Separate from the last receipt: refusals and unrelated observations
        # must not erase the exact revision that failed after commitment.
        _atomic_json(Path(home) / 'forward-update-bad.json', {'failed_sha': record['new_sha']})
    _save(home, record)
    if outcome in {'success', 'rolled_back', 'refused', 'aborted'} and not record.get('bookkeeping_pending'):
        _archive(home, record)
    else:
        _atomic_json(Path(home) / 'forward-update-last.json', record)
    from hermes_cli.update_receipt import record_forward_generation
    record_forward_generation(record)
    return record


def _archive(home, record):
    _atomic_json(Path(home) / 'forward-update-last.json', record)
    (Path(home) / 'forward-update.json').unlink(missing_ok=True)
    _sync_dir(Path(home))


def _refuse(db, row, supervisor, evidence, *, bootstrapped=True, bootstrap_scope=None, installed=True,
            never_claimed_only=False):
    # Fence a racing claim/cold takeover and never retire a serving lease holder.
    with closing(db.connect()) as conn, conn:
        begin_immediate(conn)
        current = conn.execute('SELECT * FROM generations WHERE id=?', (row['id'],)).fetchone()
        if never_claimed_only and current['pid'] is not None:
            return  # A racing claim is no longer this observer's cleanup to perform.
        if conn.execute("SELECT 1 FROM leases WHERE generation_id=? AND state='active'", (row['id'],)).fetchone():
            raise RuntimeError('standby acquired a lease during refusal')
        if current['state'] == 'standby':
            db._retire_in_transaction(conn, row['id'], expected_unclaimed=never_claimed_only,
                                      evidence='unclaimed' if current['pid'] is None else evidence)
        elif current['state'] != 'exited':
            raise RuntimeError('refused generation is no longer standby')
    if installed and (supervisor.directory / f"{row['label']}.plist").exists():
        supervisor.boot_active(_row(db, row['id']), False)
    if bootstrapped or current['pid'] is not None or (
            bootstrap_scope and supervisor.owns_bootstrap(row, bootstrap_scope)):
        supervisor.bootout(_row(db, row['id']))
    else:
        # Our newly installed definition is not evidence that a pre-existing
        # loaded job is ours. Remove this unused definition, never that job.
        path = supervisor.directory / f"{row['label']}.plist"
        if installed and path.exists():
            supervisor._definition(row['label'])
            path.unlink()
            _sync_dir(supervisor.directory)


def _discard_reservation(db, info, supervisor, evidence, *, never_claimed_only=False):
    if not info:
        return
    rows = db.generations()
    row = next((item for item in rows if item['id'] == info['id']), None)
    if row and (row['started_at'], row['label'], row['release_sha']) == (
            info['reservation_at'], info['label'], info['release_sha']):
        if any(other['id'] != row['id'] and other['label'] == row['label'] and other['state'] != 'exited'
               for other in rows):
            raise RuntimeError('reservation label belongs to another generation')
        scope = info.get('bootstrap_scope')
        if scope and supervisor.bootstrap_state(row, scope) == 'foreign':
            raise RuntimeError('reservation bootstrap label belongs to another scope')
        _refuse(db, row, supervisor, evidence, bootstrapped=info.get('bootstrapped', False),
                bootstrap_scope=scope, installed=info.get('installation_started', False),
                never_claimed_only=never_claimed_only)


def cleanup_exited(home, *, supervisor=None):
    supervisor = supervisor or GenerationSupervisor(home)
    db = GenerationCoordinator(home)
    lease = next((item for item in db.leases() if item['resource'] == 'active_generation'), None)
    holder = lease['generation_id'] if lease else None
    rows = db.generations()
    live_labels = {row['label'] for row in rows if row['state'] != 'exited'}
    for row in rows:
        if row['state'] == 'exited' and row['id'] != holder:
            if row['pid'] is None:
                continue  # Unclaimed bootstrap cleanup belongs to its durable intent.
            path = supervisor.directory / f"{row['label']}.plist"
            # A cold start may have reused a historical label. Address its live row.
            if row['label'] in live_labels:
                continue
            if path.exists():
                supervisor.boot_active(row, False)
                supervisor.bootout(row)
            # Successful bootout removes our definition. Later ticks need no
            # launchd query. A custom plist belongs to its exact-path guardian.


def _boot_entries(db, supervisor):
    holder = _lease(db)['generation_id']
    rows = db.generations()
    active = _row(db, holder)
    # Disable the previous login entry before enabling the new holder.
    for row in rows:
        if row['id'] != holder and (supervisor.directory / f"{row['label']}.plist").exists():
            if row['label'] != active['label']:
                supervisor.boot_active(row, False)
    supervisor.boot_active(active, True)


def _launch(db, release, supervisor, record, key, deadline):
    with deadline_scope(deadline):
        generation_id = str(uuid.uuid4())
        label = generation_launchd_label(generation_id)
        # Persist intent before reservation: SIGKILL cannot orphan an unknown launch.
        info = {'id': generation_id, 'label': label, 'release_sha': release.name, 'reservation_at': time.time(),
                'startup_deadline_clock': deadline}
        record[key] = info
        _save(db.home, record)
        return _resume_launch(db, release, supervisor, record, key, deadline=deadline)


def _resume_launch(db, release, supervisor, record, key, deadline):
    with deadline_scope(deadline):
        return _resume_launch_bounded(db, release, supervisor, record, key, deadline=deadline)


@with_deadline_scope
def _resume_launch_bounded(db, release, supervisor, record, key, deadline):
    info = record[key]
    generation_id, label = info['id'], info['label']
    row = next((item for item in db.generations() if item['id'] == generation_id), None)
    if row is None:
        _remaining(deadline, STARTUP_SECONDS)
        db.reserve_generation(generation_id=generation_id, label=label, release_sha=release.name,
                              started_at=info['reservation_at'])
        _deadline_check(deadline)
    elif (row['started_at'], row['label'], row['release_sha']) != (info['reservation_at'], label, release.name):
        raise RuntimeError('reservation identity changed')
    info['reserved'] = True
    _save(db.home, record)
    _deadline_check(deadline)
    row = _row(db, generation_id)
    _deadline_check(deadline)
    launch_needed = not info.get('bootstrap_scope')
    if row['pid'] is None and row['state'] == 'standby' and not launch_needed:
        state = supervisor.bootstrap_state(row, info['bootstrap_scope'],
                                           timeout=_remaining(deadline, 5))
        if state == 'foreign':
            raise RuntimeError('interrupted bootstrap label belongs to another scope')
        launch_needed = state == 'unloaded'
    if row['pid'] is None and row['state'] == 'standby' and launch_needed:
        path = supervisor.directory / f'{label}.plist'
        if path.exists():
            if not info.get('installation_started'):
                raise RuntimeError('fresh generation already has a launch agent')
            _, data = supervisor._definition(label)
            if (data.get('WorkingDirectory') != str(release) or
                    data.get('ProgramArguments', [None])[0] != str(release / '.venv/bin/python')):
                raise RuntimeError('reservation plist differs from its pinned release')
        else:
            info['installation_started'] = True
            _save(db.home, record)
            path = supervisor.install(row, release)
        def before_launch(scope):
            info['bootstrap_scope'] = scope
            _save(db.home, record)
        supervisor.bootstrap(row, path, _remaining(deadline, 30), before_launch=before_launch)
        info['bootstrapped'] = True
        _save(db.home, record)
    return _wait_ready(db, generation_id, supervisor, deadline=deadline)


class _PointerActivationUncertain(RuntimeError):
    """An activation error with unreadable pointers never authorizes rollback."""


def _commit_pointer(home, record, row, proof):
    if record is not None:
        record['pointer_commit'] = {'generation_id': row['id'], 'epoch': proof['epoch'],
                                    'release_sha': row['release_sha']}
        record['bookkeeping_pending'] = True
        _save(home, record)


def _observe_pointer_commit(home, record, row, proof):
    try:
        release = ReleasePaths.for_home(home).release(row['release_sha'])
        activated = read_pointer(Path(home) / 'current') == release
    except Exception as exc:
        raise _PointerActivationUncertain(f'pointer commit readback failed: {exc}') from exc
    if activated:
        _commit_pointer(home, record, row, proof)


def _flip(home, db, row, proof, supervisor, *, operation='promote', record=None):
    paths = ReleasePaths.for_home(home)
    lease = _lease(db)
    if (lease['generation_id'], lease['epoch'], lease['state']) != (row['id'], proof['epoch'], 'active'):
        raise RuntimeError('generation changed before pointer flip')
    if not _live(_row(db, row['id'])):
        raise RuntimeError('generation died before pointer flip')
    release = paths.release(row['release_sha'])
    # activate_release is a local fsync'd pointer transaction with no blocking
    # wait in this path (no reload callback). Refuse to start it after expiry.
    gateway_deadline.check()
    try:
        result: dict = activate_release(home, release, operation=operation)
    except Exception:
        # Activation can fail in fsync/readback/archive after replacing current.
        # Reconcile that durable effect before either caller decides on rollback.
        _observe_pointer_commit(home, record, row, proof)
        raise
    # Lease + polling proof + pointer activation is the success commit. Login
    # policy and retired-file cleanup are repairable bookkeeping, not a B fault.
    _commit_pointer(home, record, row, proof)
    if read_pointer(paths.current) != release:
        raise RuntimeError('committed pointer differs from serving release')
    try:
        _boot_entries(db, supervisor)
        cleanup_exited(home, supervisor=supervisor)
    except Exception as exc:
        result.update(bookkeeping_pending=True, alert=True, bookkeeping_failure=str(exc))
    else:
        result['bookkeeping_pending'] = False
        if record is not None:
            record.pop('bookkeeping_failure', None)
    lease = _lease(db)
    if ((lease['generation_id'], lease['epoch'], lease['state']) != (row['id'], proof['epoch'], 'active')
            or not _live(_row(db, row['id']))):
        raise RuntimeError('committed owner changed during bookkeeping')
    if read_pointer(paths.current) != release:
        raise RuntimeError('committed pointer differs from serving release')
    return result


def _flip_scope(home, record, bound, *, rollback):
    """Scope for the irreversible pointer flip.

    Inside ``bound`` while a full HANDOVER_ABORT_RESERVE still fits. Otherwise a
    rollback is classified late *before* the flip (bound missed, alert) and the
    flip gets its own named reserve. The original bound is never silently
    stretched over an irreversible step, and the flip never runs unbounded.
    """
    if bound is not None and bound - _now() >= HANDOVER_ABORT_RESERVE:
        return deadline_scope(bound)
    if rollback:
        late_info = record.setdefault('late_rollback', {'started_at': time.time()})
        late_info['bound_missed'] = True
        late_info['flip_after_bound'] = True
        record['alert'] = True
        _save(home, record)
    return deadline_scope(_now() + HANDOVER_ABORT_RESERVE, inherit=False)


def _fresh_reply(home, row, epoch, after, tokens):
    """Passive proof of fresh polled input answered after this owner took over.

    Synthetic startup rows and earlier/draining-session replies cannot qualify.
    No message is sent by the updater.
    """
    db = GenerationCoordinator(home)
    with closing(db.connect()) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='telegram_updates'").fetchone():
            return None
        updates = conn.execute("SELECT u.token_hash,u.update_id,i.profile_home,"
            "json_extract(CAST(i.authorized_source AS TEXT),'$.profile') AS profile "
            "FROM telegram_updates u JOIN inbox i ON i.source_event_id=CAST(u.update_id AS TEXT) "
            "AND i.owner_id=? AND i.owner_epoch=? AND i.kind='message' AND i.state='accepted' "
            "AND json_extract(CAST(i.authorized_source AS TEXT),'$.token_hash')=u.token_hash "
            "WHERE u.state='accepted' AND u.received_at>=? "
            "AND u.token_hash IN (SELECT value FROM json_each(?))",
            (row['id'], epoch, after, json.dumps(tokens))).fetchall()
    for update in updates:
        outbox = Path(update['profile_home']) / 'gateway-outbox.db'
        if not outbox.exists():
            continue
        with closing(connect_sqlite(f'file:{outbox}?mode=ro', uri=True, timeout=2)) as conn:
            delivered = conn.execute("SELECT a.turn_id,o.message_id FROM admissions a JOIN outbox o USING(turn_id) "
                "WHERE a.platform='telegram' AND a.transport_event_id=? AND a.created_at>=? "
                "AND a.profile=? AND a.event_kind='text' AND a.result='completed' "
                "AND o.state='delivered' AND o.message_id IS NOT NULL "
                "AND COALESCE(json_extract(o.payload,'$.metadata._interim_send'),0)=0 "
                "AND ((o.type='send' AND COALESCE(json_extract(o.payload,'$.metadata.expect_edits'),0)=0) "
                "OR (o.type='edit_message' AND json_extract(o.payload,'$.finalize')=1)) "
                "AND NOT EXISTS (SELECT 1 FROM outbox later WHERE later.turn_id=o.turn_id "
                "AND later.sequence>o.sequence AND later.type IN ('send','edit_message') "
                "AND COALESCE(json_extract(later.payload,'$.metadata._interim_send'),0)=0) LIMIT 1",
                (f"update:{update['update_id']}", int(after), update['profile'])).fetchone()
            if delivered:
                return {'generation_id': row['id'], 'epoch': epoch, 'source_event_id': str(update['update_id']),
                        'turn_id': delivered[0], 'message_id': delivered[1]}
    return None


def _fresh_input(home, row, epoch, after, until, tokens):
    """Whether this owner admitted fresh Telegram input in the original window."""
    with closing(GenerationCoordinator(home).connect()) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE name='telegram_updates'").fetchone():
            return False
        return conn.execute("SELECT 1 FROM telegram_updates u JOIN inbox i "
            "ON i.source_event_id=CAST(u.update_id AS TEXT) "
            "AND i.owner_id=? AND i.owner_epoch=? AND i.kind='message' AND i.state IN ('pending','accepted') "
            "AND i.transport='telegram' AND i.created_at<=? "
            "AND json_extract(CAST(i.authorized_source AS TEXT),'$.token_hash')=u.token_hash "
            "WHERE u.state='accepted' AND u.received_at>=? AND u.received_at<=? "
            "AND u.token_hash IN (SELECT value FROM json_each(?)) LIMIT 1",
            (row['id'], epoch, until, after, until, json.dumps(tokens))).fetchone() is not None


def _observe_rollback_reply(home, db, row, proof, record):
    """A quiet or missed reply bound is a timing result, never an unresolved lease."""
    rollback = record['rollback']
    deadline = record['rollback_deadline_clock']
    # A later polling observation must not move the original input window.
    after = rollback['poller']['poller_started_at']
    rollback['reply_observed'] = False
    late_reply = False
    while _now() < deadline:
        try:
            with deadline_scope(deadline, inherit=False):
                reply = _fresh_reply(home, row, proof['epoch'], after, proof['tokens'])
        except TimeoutError:
            # The observation window closed mid-read: a missed bound, not a failure.
            break
        observed = _now()
        if reply:
            if observed > deadline:
                late_reply = True
                break
            death_clock = record.get('death_observed_clock')
            rollback.update(reply=reply, reply_observed=True,
                reply_seconds=observed - (death_clock if death_clock is not None else record['commit_clock']),
                commit_to_reply_upper_bound_seconds=observed - record['commit_clock'])
            if death_clock is not None:
                rollback['death_to_reply_seconds'] = observed - death_clock
            break
        remaining = deadline - observed
        if remaining <= 0:
            break
        _sleep(min(.2, remaining))
    until = record['commit_at'] + ROLLBACK_SECONDS
    # The cooperative observation window is over, but classification and archival
    # still need a small, explicit recovery budget; never rely on an expired-scope bypass.
    with deadline_scope(_now() + HANDOVER_ABORT_RESERVE, inherit=False):
        rollback['fresh_input_observed'] = _fresh_input(home, row, proof['epoch'], after, until, proof['tokens'])
        rollback['rollback_bound_met'] = (
            rollback['commit_to_serving_upper_bound_seconds'] <= ROLLBACK_SECONDS
            and not late_reply
            and (rollback['reply_observed'] or not rollback['fresh_input_observed']))
        # Timing observation does not authorize completion under a different owner.
        lease = _lease(db)
        if ((lease['generation_id'], lease['epoch'], lease['state']) != (row['id'], proof['epoch'], 'active')
                or not _live(_row(db, row['id']))):
            raise RuntimeError('rollback owner changed during observation')
    record['alert'] = not rollback['rollback_bound_met']


def _proven_wedged(home, row):
    """Existing liveness probe only. Unknown or alive never authorizes a late rollback."""
    from hermes_cli.gateway import probe_gateway_loop_liveness, GATEWAY_LOOP_WEDGED
    return _live(row) and probe_gateway_loop_liveness(
        row['pid'], home=home, stale_after=WEDGE_STALE_SECONDS) == GATEWAY_LOOP_WEDGED


@with_deadline_scope
def _await_wedge_proof(home, db, row, deadline):
    """Probe until the successor answers, proves wedged, or the stop reserve is reached.

    A freshly wedged loop has a fresh heartbeat, so one probe at failure time is only
    UNKNOWN. Repeating it lets the heartbeat age into proof while budget remains.
    """
    from hermes_cli.gateway import probe_gateway_loop_liveness, GATEWAY_LOOP_ALIVE, GATEWAY_LOOP_WEDGED
    while True:
        if db._owner_is_dead(_row(db, row['id'])):
            return 'dead'
        if not _live(row):
            raise RuntimeError('successor identity became unknown during probe')
        if deadline - _now() < WEDGE_RECOVERY_RESERVE_SECONDS:
            return None
        verdict = probe_gateway_loop_liveness(row['pid'], home=home, stale_after=WEDGE_STALE_SECONDS)
        if verdict == GATEWAY_LOOP_WEDGED:
            return 'wedged'
        if verdict == GATEWAY_LOOP_ALIVE:
            return None
        _sleep(1)


def _sqlite_locked(exc):
    return isinstance(exc, sqlite3.OperationalError) and 'database is locked' in str(exc).lower()


@with_deadline_scope
def _terminate_proven_wedged(home, db, row, deadline, record, *, expected_lease=None):
    """Release a wedged successor's SQLite transaction before coordinator retry."""
    from hermes_cli.gateway import _escalate_wedged_gateway
    expected_lease = expected_lease or _lease(db)
    expected = (expected_lease['generation_id'], expected_lease['epoch'], expected_lease['state'])
    if expected != (row['id'], expected_lease['epoch'], 'active'):
        raise RuntimeError('rollback owner changed before wedge recovery')
    current = _row(db, row['id'])
    if not db._owner_is_dead(current):
        if not _live(current):
            raise RuntimeError('successor identity became unknown during probe')
        proof = _await_wedge_proof(home, db, current, deadline)
        if proof is None:
            raise RuntimeError('live successor neither handed over nor proved wedged')
        current = _row(db, row['id'])
        if proof == 'wedged' and not db._owner_is_dead(current):
            if not _live(current):
                raise RuntimeError('successor identity became unknown during probe')
            grace = _remaining(deadline, 5)
            lease = _lease(db)
            if (lease['generation_id'], lease['epoch'], lease['state']) != expected:
                raise RuntimeError('rollback owner changed during wedge probe')
            if not _escalate_wedged_gateway(current['pid'], term_grace=grace,
                    kill_wait=5, deadline=deadline,
                    expected_start_time=float(current['start_fingerprint'].split(':', 1)[1])):
                raise RuntimeError('wedged successor death unproved')
    record['death_observed_at'] = time.time()
    death_clock = _now()
    record['death_observed_clock'] = death_clock
    _save(home, record)
    return death_clock


def _rollback(home, db, failed, previous, supervisor, record, *, late=False):
    from gateway.generation import _boot_id
    same_boot = record.get('commit_boot_id') == _boot_id()
    started = record.get('rollback_clock', _now())
    bound = record.get('commit_clock', started) + ROLLBACK_SECONDS
    deadline = _now() + ROLLBACK_SECONDS if late or not same_boot else bound
    with deadline_scope(deadline):
        return _rollback_bounded(home, db, failed, previous, supervisor, record,
                                 late=late, deadline=deadline)


@with_deadline_scope
def _rollback_bounded(home, db, failed, previous, supervisor, record, *, late=False,
                    deadline: float = 0.0):
    from gateway.generation import _boot_id
    same_boot = record.get('commit_boot_id') == _boot_id()
    started = record.get('rollback_clock', _now())
    # Before-commit is the earliest possible death of serving B. This durable
    # upper bound includes time spent noticing failure, not a fresh recovery budget.
    bound = record.get('commit_clock', started) + ROLLBACK_SECONDS
    # A late recovery of a proven-dead successor gets a fresh operating budget. The
    # recorded bound stays the original one, so the miss is reported, never hidden.
    if late and same_boot:
        record['late_rollback'] = {'started_at': time.time(), 'bound_missed': True}
    if not capable(previous) or not _release_is_ready(previous, previous.name):
        raise RuntimeError('previous release cannot run a fresh forward-only generation')
    if not record.get('rollback_generation'):
        require_forward_inventory(home)
    dead = db._owner_is_dead(_row(db, failed['id']))
    death_clock = record.get('death_observed_clock', _now() if dead else None) if same_boot else None
    if not record.get('rollback_generation'):
        record['failure_observed_at'] = time.time()
        record['death_observed_at'] = time.time() if dead else None
        record.update(rollback_started_at=time.time(), rollback_clock=started,
                      rollback_deadline_clock=bound, rollback_boot_id=failed['boot_id'])
        record['death_observed_clock'] = death_clock
    # An A-prime startup gate has the same 45s limit, inside the total 60s budget.
    try:
        info = record.get('rollback_generation')
        if info:
            reserved = next((row for row in db.generations() if row['id'] == info['id']), None)
            if reserved and reserved['id'] == failed['id'] and dead:
                # This reservation already served and died. Preserve its identity in
                # the intent across another updater crash; takeover retires/boots it
                # out in the required order, after the fresh standby is ready.
                history = record.setdefault('rollback_history', [])
                if info not in history:
                    history.append(info)
                if record.get('rollback'):
                    # A previous serving/reply observation remains audit evidence,
                    # never polling or timing proof for the replacement process.
                    record.setdefault('rollback_attempts', []).append(record.pop('rollback'))
                info = None
            elif reserved and (reserved['state'] == 'exited' or
                               reserved['pid'] is not None and db._owner_is_dead(reserved)):
                _remaining(deadline, STARTUP_SECONDS)
                _discard_reservation(db, info, supervisor, 'rollback_startup_not_ready')
                info = None
        if info:
            fresh = _resume_launch(db, previous, supervisor, record, 'rollback_generation',
                                   deadline=deadline if late or not same_boot else min(deadline, info['startup_deadline_clock']))
        else:
            fresh = _launch(db, previous, supervisor, record, 'rollback_generation',
                            min(deadline, _now() + STARTUP_SECONDS))
    except Exception as exc:
        # A stopped successor can leave the coordinator in BEGIN IMMEDIATE.
        # Prove the wedge, terminate that owner, and retry inside the original bound.
        if not _sqlite_locked(exc):
            _discard_reservation(db, record.get('rollback_generation'), supervisor, 'rollback_startup_not_ready')
            raise
        current = _row(db, failed['id'])
        if db._owner_is_dead(current):
            _discard_reservation(db, record.get('rollback_generation'), supervisor, 'rollback_startup_not_ready')
            raise
        try:
            expected_lease = _lease(db)
            expected = (expected_lease['generation_id'], expected_lease['epoch'], expected_lease['state'])
            if expected != (failed['id'], expected_lease['epoch'], 'active'):
                raise RuntimeError('rollback owner changed before wedge recovery')
            death_clock = _terminate_proven_wedged(
                home, db, current, deadline=deadline, record=record, expected_lease=expected_lease)
            lease = _lease(db)
            if (lease['generation_id'], lease['epoch'], lease['state']) != expected:
                raise RuntimeError('rollback owner changed before retry')
            fresh = _resume_launch(db, previous, supervisor, record, 'rollback_generation',
                                   deadline=deadline if late or not same_boot else min(deadline, _now() + STARTUP_SECONDS))
        except Exception:
            _discard_reservation(db, record.get('rollback_generation'), supervisor, 'rollback_startup_not_ready')
            raise
    death_clock = record.get('death_observed_clock', death_clock)
    require_forward_inventory(home)
    if _lease(db)['generation_id'] != fresh['id']:
        if not db._owner_is_dead(_row(db, failed['id'])):
            try:
                # A responsive successor's long poll can outlive this cap. The
                # driver reserves abort notification inside it; a delayed stop
                # self-rearms only under the same still-serving lease and nonce.
                # A silent loop still needs heartbeat aging, bounded termination,
                # takeover and polling proof inside the original rollback bound.
                cooperative_budget = min(COOPERATIVE_ROLLBACK_SECONDS,
                                         deadline - _now() - WEDGE_RECOVERY_RESERVE_SECONDS)
                if cooperative_budget <= 0:
                    raise RuntimeError('cooperative handover has no reserved recovery budget')
                handover_to_generation(home, fresh['id'], timeout=cooperative_budget, verify_after_commit=False, require_pollers=True)
            except Exception:
                if _lease(db)['generation_id'] != fresh['id']:
                    from hermes_cli.gateway import _escalate_wedged_gateway
                    current = _row(db, failed['id'])
                    if not db._owner_is_dead(current):
                        if not _live(current):
                            raise RuntimeError('successor identity became unknown during probe')
                        if _await_wedge_proof(home, db, current, deadline) is None:
                            raise RuntimeError('live successor neither handed over nor proved wedged')
                        # The probe can outlive this PID incarnation. A replacement
                        # proves B dead and must never receive either signal.
                        if not db._owner_is_dead(current):
                            if not _live(current):
                                raise RuntimeError('successor identity became unknown during probe')
                            grace = _remaining(deadline, 5)
                            if not _escalate_wedged_gateway(current['pid'], term_grace=grace,
                                    kill_wait=5, deadline=deadline,
                                    expected_start_time=float(current['start_fingerprint'].split(':', 1)[1])):
                                raise RuntimeError('wedged successor death unproved')
                    record['death_observed_at'] = time.time()
                    death_clock = _now()
                    record['death_observed_clock'] = death_clock
                    _save(home, record)
        if _lease(db)['generation_id'] != fresh['id']:
            # The standby itself can cold-takeover. CAS protects either participant.
            db.takeover_dead_generation('active_generation', failed['id'], fresh['id'],
                bootout=lambda label: supervisor.bootout(_row(db, failed['id']), _remaining(deadline, 15)),
                deadline=_now() + max(0, deadline - _now()))
    proof = _poller(db, fresh, supervisor, deadline=deadline)
    # The pointer flip is irreversible. Run it inside the rollback bound when a
    # full reserve still fits; otherwise classify the rollback late *before* the
    # flip (bound missed, alert) and give the flip its own named reserve. Never
    # silently stretch the original bound over an irreversible step.
    with _flip_scope(home, record, deadline, rollback=True):
        result = _flip(home, db, fresh, proof, supervisor, operation='rollback', record=record)
        # Serving is proven only once the pointer commit has landed.
        serving_clock = _now()
        rollback = {'old_id': failed['id'], 'new_id': fresh['id'], 'old_label': failed['label'],
                    'new_label': fresh['label'], 'old_sha': failed['release_sha'], 'new_sha': fresh['release_sha'],
                    'epoch': proof['epoch'], 'poller': proof,
                    'death_observed_at': record['death_observed_at'], **result}
        if same_boot:
            rollback['serving_seconds'] = serving_clock - (death_clock if death_clock is not None else record['commit_clock'])
            rollback['commit_to_serving_upper_bound_seconds'] = serving_clock - record['commit_clock']
            if death_clock is not None:
                rollback['death_to_serving_seconds'] = serving_clock - death_clock
        else:
            rollback.update(rollback_bound_met=None, timing_unprovable='boot changed', reply_observed=False)
            record['alert'] = True
        record['rollback'] = rollback
        _save(home, record)
    if same_boot:
        # The reply window is its own named bound (ROLLBACK_SECONDS), not the
        # short abort reserve that covers the flip and its bookkeeping.
        _observe_rollback_reply(home, db, fresh, proof, record)
    with deadline_scope(_now() + HANDOVER_ABORT_RESERVE, inherit=False):
        return _finish(home, record, 'rolled_back', **result)


def _superseded(home, db, record, lease, supervisor):
    """A proven independent claimant makes this intent an audit record only."""
    row = _row(db, lease['generation_id'])
    proof = _poller(db, row, supervisor, deadline=_proof_deadline(record, row))
    release = ReleasePaths.for_home(home).release(row['release_sha'])
    if read_pointer(Path(home) / 'current') != release:
        raise RuntimeError('superseding holder release differs from current pointer')
    for info in (record.get('successor'), record.get('rollback_generation')):
        reserved = next((item for item in db.generations() if info and item['id'] == info['id']), None)
        if reserved and reserved['pid'] is None and reserved['state'] in {'standby', 'exited'}:
            _discard_reservation(db, info, supervisor, 'intent_superseded', never_claimed_only=True)
    require_forward_inventory(home)
    # Cleanup may outlive the claimant or a pointer repair. Observe both again.
    current = _lease(db)
    if ((current['generation_id'], current['epoch'], current['state']) != (row['id'], proof['epoch'], 'active')
            or not _live(_row(db, row['id'])) or db._owner_is_dead(_row(db, row['id']))):
        raise RuntimeError('superseding owner changed during observation')
    if read_pointer(Path(home) / 'current') != release:
        raise RuntimeError('superseding holder release differs from current pointer')
    previous = Path(record['previous']).name if record.get('previous') else record.get('old_sha')
    outcome = 'success' if row['release_sha'] == record.get('new_sha') else (
        'rolled_back' if row['release_sha'] == previous else 'refused')
    return _finish(home, record, outcome, recovered=True, poller=proof,
                   superseded_by={'id': row['id'], 'label': row['label'], 'sha': row['release_sha'],
                                  'epoch': proof['epoch']})


def recover_forward(home, *, supervisor=None):
    """Complete an interrupted intent by observation, never repeat a lease move."""
    path = Path(home) / 'forward-update.json'
    if not path.exists():
        return None
    supervisor = supervisor or GenerationSupervisor(home)
    with _update_lock(home) as acquired:
        if not acquired:
            return _locked()
        if not path.exists():
            return None  # The updater may have archived just before lock acquisition.
        record = json.loads(path.read_text(encoding='utf-8-sig'))
        db = GenerationCoordinator(home)
        try:
            cleanup_exited(home, supervisor=supervisor)
            lease = _lease(db)
        except Exception as exc:
            return _finish(home, record, 'blocked', failure=str(exc), recovered=True)
        if (record.get('outcome') in {'success', 'rolled_back', 'refused', 'aborted'}
                and not record.get('bookkeeping_pending')):
            _archive(home, record)
            return record
        intended = [record.get('rollback_generation'), record.get('successor'), *record.get('rollback_history', [])]
        intent_ids = {record.get('old_id'), *(info['id'] for info in intended if info)}
        if lease['generation_id'] not in intent_ids:
            try:
                return _superseded(home, db, record, lease, supervisor)
            except Exception as exc:
                return _finish(home, record, 'blocked', failure=str(exc), recovered=True)
        new = next((info for info in intended if info and info['id'] == lease['generation_id']), None)
        new = new or record.get('rollback_generation') or record.get('successor')
        if not new:
            try:
                require_forward_inventory(home)
                if (lease['generation_id'], lease['epoch'], lease['state']) == (
                        record['old_id'], record['old_epoch'], 'active'):
                    status = supervisor.request(_row(db, record['old_id']), 'polling_status', timeout=2)
                    if status.get('polling') is True and _all_fences_armed(status):
                        return _finish(home, record, 'refused', failure='updater exited before reservation', recovered=True)
                raise RuntimeError('reservation intent incomplete')
            except Exception as exc:
                return _finish(home, record, 'blocked', failure=str(exc), recovered=True)
        if lease['generation_id'] == new['id']:
            # Keep pointer inspection (including its failure receipt) outside the
            # health-failure rollback path. An unreadable pointer is not a B fault.
            try:
                row = _row(db, new['id'])
                if ((record.get('pointer_commit') or {}).get('generation_id') == row['id']
                        and not db._owner_is_dead(row)):
                    if read_pointer(Path(home) / 'current') != ReleasePaths.for_home(home).release(row['release_sha']):
                        raise RuntimeError('committed pointer differs from serving release')
                    record['alert'] = False  # A repeated bookkeeping failure sets it again.
            except Exception as exc:
                return _finish(home, record, 'blocked', failure=str(exc), recovered=True)
            polling_error = None
            try:
                deadline = _proof_deadline(record, row)
                try:
                    proof = _poller(db, row, supervisor, deadline=deadline)
                except (RuntimeError, TimeoutError) as health_error:
                    polling_error = health_error
                    raise
                # A prior updater can die after current moved but before the
                # forward marker was saved. Reconcile that commit before any
                # clock, reservation cleanup, inventory or receipt can fail.
                # Reconciling an already-moved pointer writes the durable commit
                # marker: bounded like the flip itself (proof window or named reserve).
                with _flip_scope(home, record, deadline, rollback=False):
                    _observe_pointer_commit(home, record, row, proof)
                if proof.pop('_deadline_clock_error', False):
                    return _finish(home, record, 'blocked', failure='deadline clock unavailable',
                                   recovered=True, alert=True)
                rollback_owner = new != record.get('successor')
                if not rollback_owner and record.get('rollback_generation'):
                    _discard_reservation(db, record['rollback_generation'], supervisor, 'rollback_abandoned')
                try:
                    require_forward_inventory(home)
                except Exception as exc:
                    return _finish(home, record, 'blocked', failure=str(exc), recovered=True)
                if rollback_owner:
                    from gateway.generation import _boot_id as _flip_boot_id
                    flip_bound = (record.get('rollback_deadline_clock')
                                  if record.get('rollback_boot_id') == _flip_boot_id() else None)
                else:
                    flip_bound = deadline
                with _flip_scope(home, record, flip_bound, rollback=rollback_owner):
                    result = _flip(home, db, row, proof, supervisor,
                                   operation='rollback' if rollback_owner else 'promote', record=record)
                    # Serving is proven only once the pointer commit has landed.
                    serving_clock = _now()
                if rollback_owner:
                    from gateway.generation import _boot_id
                    rollback = record.setdefault('rollback', {
                        'old_id': record['successor']['id'], 'new_id': row['id'],
                        'old_label': record['successor']['label'], 'new_label': row['label'],
                        'old_sha': record['successor']['release_sha'], 'new_sha': row['release_sha'],
                        'epoch': proof['epoch'], 'poller': proof,
                        'death_observed_at': record.get('death_observed_at'), **result})
                    if record.get('rollback_boot_id') != _boot_id():
                        rollback.update(rollback_bound_met=None, timing_unprovable='boot changed',
                                        reply_observed=False, poller=proof, epoch=proof['epoch'])
                        record['alert'] = True
                    else:
                        death_clock = record.get('death_observed_clock')
                        rollback.setdefault('serving_seconds', serving_clock - (
                            death_clock if death_clock is not None else record['commit_clock']))
                        rollback.setdefault('commit_to_serving_upper_bound_seconds', serving_clock - record['commit_clock'])
                        if death_clock is not None:
                            rollback.setdefault('death_to_serving_seconds', serving_clock - death_clock)
                        _observe_rollback_reply(home, db, row, proof, record)
                return _finish(home, record, 'rolled_back' if rollback_owner else 'success',
                               poller=proof, epoch=proof['epoch'], recovered=True, **result)
            except Exception as exc:
                from gateway.generation import _boot_id
                holder = _row(db, new['id'])
                same_boot = record.get('commit_boot_id') == holder['boot_id'] == _boot_id()
                # Death authorizes a fresh takeover for any intended holder, including
                # a rollback owner or a holder from an earlier boot. Never compare a
                # prior boot's monotonic deadline with the current clock.
                in_budget = (same_boot and
                             record['commit_clock'] + ROLLBACK_SECONDS - _now()
                             > WEDGE_RECOVERY_RESERVE_SECONDS)
                committed = (record.get('pointer_commit') or {}).get('generation_id') == holder['id']
                dead = db._owner_is_dead(holder)
                # Re-read the commit after _flip: recovery may have activated it
                # in this attempt. Only a new failed polling proof, death or a
                # proven wedge can condemn a pointer-committed holder.
                wedged = same_boot and not dead and _proven_wedged(home, holder)
                if not dead and not wedged and (isinstance(exc, _PointerActivationUncertain)
                        or committed and polling_error is not exc):
                    return _finish(home, record, 'blocked', failure=str(exc), recovered=True)
                if dead or same_boot and (in_budget or wedged):
                    try:
                        return _rollback(home, db, holder, Path(record['previous']), supervisor,
                                         record, late=not in_budget)
                    except Exception as rollback_error:
                        exc = rollback_error
                return _finish(home, record, 'blocked', failure=str(exc), recovered=True)
        # The old owner owns its deadline recovery. Observe all fences before
        # retiring an abandoned standby, without requesting a second transfer.
        if (lease['generation_id'], lease['epoch'], lease['state']) == (
                record['old_id'], record['old_epoch'], 'active'):
            try:
                old = _row(db, record['old_id'])
                recovery_deadline = _now() + 2
                status = supervisor.request(old, 'polling_status', timeout=_remaining(recovery_deadline, 2))
                with closing(db._deadline_connect(recovery_deadline)) as conn:
                    transfer = conn.execute('SELECT state FROM generation_transfers WHERE old_id=? AND epoch=?',
                                            (old['id'], lease['epoch'])).fetchone()
                if (status.get('polling') is not True or not _all_fences_armed(status) or
                        transfer and transfer['state'] != 'aborted'):
                    raise RuntimeError('old owner deadline has not re-armed all fences')
                _discard_reservation(db, new, supervisor, 'updater_abandoned')
                require_forward_inventory(home)
                return _finish(home, record, 'aborted' if transfer else 'refused', resume=status, recovered=True)
            except Exception as exc:
                return _finish(home, record, 'blocked', failure=str(exc), recovered=True)
        return _finish(home, record, 'blocked', failure='committed owner changed', recovered=True)


def promote_forward(home, candidate, sha, *, supervisor=None):
    home = Path(home)
    candidate = Path(candidate).resolve()
    if not forward_route(home, candidate) or candidate.name != sha or not _release_is_ready(candidate, sha):
        raise RuntimeError('forward-only release pair is not capable and ready')
    supervisor = supervisor or GenerationSupervisor(home)
    with _update_lock(home) as acquired:
        if not acquired:
            return _locked()
        bad = home / 'forward-update-bad.json'
        last = home / 'forward-update-last.json'
        previous_failure = json.loads(bad.read_text(encoding='utf-8-sig')) if bad.exists() else {}
        # Also recognize rollback records written before the durable fence existed.
        archived = json.loads(last.read_text(encoding='utf-8-sig')) if last.exists() else {}
        if (previous_failure.get('failed_sha') == sha or
                archived.get('outcome') == 'rolled_back' and archived.get('new_sha') == sha):
            _atomic_json(bad, {'failed_sha': sha})
            refusal = {'outcome': 'refused', 'new_sha': sha, 'failure': 'release previously rolled back'}
            from hermes_cli.update_receipt import record_forward_generation
            record_forward_generation(refusal)
            return refusal
        db = GenerationCoordinator(home)
        lease = _lease(db)
        old = _row(db, lease['generation_id'])
        previous = read_pointer(home / 'current')
        if not _live(old) or old['state'] != 'serving' or previous.name != old['release_sha']:
            raise RuntimeError('serving release identity is unproved')
        if (home / 'release-txn.json').exists():
            raise RuntimeError('single-gateway transaction must finish before forward-only promotion')
        intent = home / 'forward-update.json'
        pending = json.loads(intent.read_text(encoding='utf-8-sig')) if intent.exists() else {}
        if pending.get('outcome') in {'running', 'blocked'} or pending.get('bookkeeping_pending'):
            raise RuntimeError('unresolved forward-only intent must be observed before another promotion')
        cleanup_exited(home, supervisor=supervisor)
        record = {'version': 1, 'outcome': 'running', 'started_at': time.time(),
                  'old_id': old['id'], 'old_label': old['label'], 'old_sha': old['release_sha'],
                  'old_epoch': lease['epoch'], 'new_sha': sha, 'previous': str(previous), 'current': str(candidate)}
        _save(home, record)
        started = _now()
        try:
            successor = _launch(db, candidate, supervisor, record, 'successor', _now() + STARTUP_SECONDS)
        except Exception as exc:
            info = record.get('successor')
            if info:
                row = next((row for row in db.generations() if row['id'] == info['id']), None)
                if row and row['started_at'] == info['reservation_at']:
                    record.update(new_id=row['id'], new_label=row['label'])
                    try:
                        _discard_reservation(db, info, supervisor, 'startup_not_ready')
                    except Exception as cleanup_error:
                        return _finish(home, record, 'blocked', failure=str(cleanup_error))
            return _finish(home, record, 'refused', failure=str(exc), startup_seconds=_now() - started)
        record.update(new_id=successor['id'], new_label=successor['label'], startup_seconds=_now() - started)
        _save(home, record)
        handover_deadline = _now() + 45
        try:
            # Refuse an empty roster before transfer_requested can pause A or
            # move its lease. Standby startup by itself is side-effect isolated.
            roster = supervisor.request(old, 'polling_roster', params={'deadline': _now() + 2},
                                        timeout=2).get('tokens')
            if not isinstance(roster, list) or any(not isinstance(token, str) for token in roster):
                raise RuntimeError('invalid old generation polling roster')
            if not roster:
                raise RuntimeError('empty polling roster cannot qualify forward-only promotion')
            def record_commit_window():
                record.update(commit_clock=_now(), commit_at=time.time(), commit_boot_id=old['boot_id'])
                _save(home, record)
            # The handover helper reserves HANDOVER_ABORT_RESERVE inside its
            # original 45-second window. Re-arm may consume only that remaining
            # reserve; never start a fresh full handover interval here.
            handover_to_generation(home, successor['id'], timeout=45, before_commit=record_commit_window,
                                   verify_after_commit=False, require_pollers=True)
            # The rollback budget also starts at commit. The successor's proof
            # window leaves A' STARTUP_SECONDS to start, take over and poll.
            commit = record['commit_clock']
            proof_deadline = min(
                commit + ROLLBACK_SECONDS, max(commit + POLL_PROOF_SECONDS, _now() + POLL_SECONDS))
            proof = _poller(db, successor, supervisor, deadline=proof_deadline)
            # The pointer flip is irreversible: bounded by the proof window while a
            # full reserve fits, else by the named reserve. Never unbounded.
            with _flip_scope(home, record, proof_deadline, rollback=False):
                result = _flip(home, db, successor, proof, supervisor, record=record)
            return _finish(home, record, 'success', poller=proof, epoch=proof['epoch'],
                           promotion_seconds=_now() - started, **result)
        except Exception as exc:
            record['failure'] = str(exc)
            current = _lease(db)
            if (current['generation_id'], current['epoch'], current['state']) == (old['id'], lease['epoch'], 'active'):
                try:
                    recovery_deadline = _now() + min(
                        HANDOVER_ABORT_RESERVE, max(0, handover_deadline - _now()))
                    with deadline_scope(recovery_deadline):
                        with closing(db._deadline_connect(recovery_deadline)) as conn:
                            transfer = conn.execute('SELECT * FROM generation_transfers WHERE old_id=? AND epoch=?',
                                                    (old['id'], lease['epoch'])).fetchone()
                        resume = None
                        if transfer:
                            if transfer['new_id'] != successor['id']:
                                raise RuntimeError('abort attempt changed')
                            if transfer['state'] != 'aborted':
                                db.abort_transfer(old['id'], successor['id'], lease['epoch'],
                                                  attempt_nonce=transfer['attempt_nonce'],
                                                  deadline=recovery_deadline)
                            resume_deadline = _now() + min(
                                HANDOVER_ABORT_RESERVE, max(0, handover_deadline - _now()))
                            resume = supervisor.request(old, 'transfer_aborted',
                                params={'to': successor['id'], 'nonce': transfer['attempt_nonce'],
                                        'deadline': resume_deadline},
                                timeout=min(HANDOVER_ABORT_RESERVE,
                                            max(0, handover_deadline - _now())))
                            if (resume.get('rearmed') is not True or not _all_fences_armed(resume)):
                                raise RuntimeError('precommit fences did not read back as armed')
                        _refuse(db, successor, supervisor, 'precommit_aborted')
                        return _finish(home, record, 'aborted', resume=resume)
                except Exception as recovery_error:
                    return _finish(home, record, 'blocked', failure=str(recovery_error))
            if current['generation_id'] != successor['id']:
                return _finish(home, record, 'blocked', failure='committed owner changed')
            holder = _row(db, successor['id'])
            if ((isinstance(exc, _PointerActivationUncertain) or
                    (record.get('pointer_commit') or {}).get('generation_id') == holder['id'])
                    and not db._owner_is_dead(holder) and not _proven_wedged(home, holder)):
                return _finish(home, record, 'blocked', alert=True)
            try:
                return _rollback(home, db, _row(db, successor['id']), previous, supervisor, record)
            except Exception as recovery_error:
                return _finish(home, record, 'blocked', failure=str(recovery_error))
