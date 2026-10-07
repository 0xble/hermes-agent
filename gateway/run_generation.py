"""Passive standby generation lifecycle for opt-in overlap handover."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import json
import socket
import os
import stat
import shutil
import signal
import time
from contextlib import suppress
from pathlib import Path

from hermes_constants import get_hermes_home

from gateway.control_socket import GatewayControlServer
from gateway import deadline as gateway_deadline

from gateway.generation import (
    GenerationCoordinator,
    GenerationIdentity,
    generation_paths,
    overlap_handover_enabled,
    forward_only_handover_enabled,
    remove_generation_files,
    write_generation_record,
)
from gateway.deadline import begin_immediate, deadline_scope, remaining as deadline_remaining, with_deadline_scope
from gateway.deadline import detached_context

logger = logging.getLogger(__name__)


def _now() -> float:
    return gateway_deadline.now()


HANDOVER_REQUEST_TIMEOUT = 45  # Same bound as generation control acknowledgements.
HANDOVER_ABORT_RESERVE = 2  # Reserved inside the caller's budget, never added to it.
DEFAULT_DRAIN_SECONDS = 7200  # Match the commit cap when the durable deadline is missing.


def _deadline_kwargs(deadline, **kwargs):
    if deadline is not None:
        kwargs["deadline"] = deadline
    return kwargs


class HandoverCommittedUnverified(RuntimeError):
    """Lease changed irreversibly; inspect successor health, do not retry promotion."""

    def __init__(self, generation_id: str, epoch: int):
        self.generation_id = generation_id
        self.epoch = epoch
        super().__init__(f"successor {generation_id} holds epoch {epoch} but has not proved polling progress")


def _generation_request(path: Path, verb: str, *, params: dict | None = None,
                        timeout: float = 30) -> dict:
    request = json.dumps({"protocol": 1, "verb": verb, "params": params or {}}).encode() + b"\n"
    deadline = _now() + timeout
    ambient = deadline_remaining()
    if ambient is not None:
        deadline = min(deadline, _now() + ambient)
    response: dict | None = None
    # A live generation keeps its control socket. Tolerate only brief connection
    # startup/teardown races, not disappearance for the whole request timeout.
    for attempt in range(3):
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                remaining = deadline - _now()
                if remaining <= 0:
                    raise TimeoutError("generation control deadline exceeded")
                sock.settimeout(remaining)
                sock.connect(str(path))
                remaining = deadline - _now()
                if remaining <= 0:
                    raise TimeoutError("generation control deadline exceeded")
                sock.settimeout(remaining)
                sock.sendall(request)
                chunks = bytearray()
                while b"\n" not in chunks and len(chunks) < 65536:
                    remaining = deadline - _now()
                    if remaining <= 0:
                        raise TimeoutError("generation control deadline exceeded")
                    sock.settimeout(remaining)
                    part = sock.recv(65536)
                    if not part:
                        break
                    chunks.extend(part)
            response = json.loads(bytes(chunks).partition(b"\n")[0])
            if _now() >= deadline:
                raise TimeoutError("generation control deadline exceeded")
            break
        except OSError as exc:
            if attempt == 2 or _now() >= deadline:
                raise RuntimeError(f"generation control unavailable: {type(exc).__name__}") from exc
            time.sleep(min(0.5, max(0, deadline - _now())))
        except ValueError as exc:
            raise RuntimeError(f"generation control unavailable: {type(exc).__name__}") from exc
    if not isinstance(response, dict) or response.get("ok") is not True or not isinstance(response.get("result"), dict):
        raise RuntimeError(f"generation control refused {verb}: {response.get('error') if isinstance(response, dict) else 'invalid response'}")
    return response["result"]


def handover_to_generation(home: Path, to_id: str, *, timeout: float = 45,
                           drain_seconds: float = DEFAULT_DRAIN_SECONDS,
                           before_commit=None, verify_after_commit: bool = True,
                           require_pollers: bool = False) -> int:
    """Internal updater entry point; never ask the lease holder to relinquish by force.

    ``verify_after_commit=False`` returns the committed epoch at once. A caller
    that runs its own commit-clocked polling proof uses it so that this wait
    can never spend the post-commit rollback budget.
    """
    if not 1 <= drain_seconds <= 86400:
        raise ValueError("drain_seconds must be between 1 and 86400")
    deadline = _now() + timeout
    request_deadline = deadline - min(HANDOVER_ABORT_RESERVE, timeout / 5)
    coordinator_deadline = request_deadline
    with deadline_scope(coordinator_deadline):
        coordinator = GenerationCoordinator(home)
        lease = next((row for row in coordinator.leases() if row["resource"] == "active_generation"), None)
        if not lease or lease["state"] != "active":
            raise RuntimeError("no active generation lease to transfer")
        old_id, epoch = lease["generation_id"], lease["epoch"]
        identities = {row["id"]: row for row in coordinator.generations()}
    old, successor = identities.get(old_id), identities.get(to_id)
    def remaining(*, recovery=False):
        budget = (deadline if recovery else request_deadline) - _now()
        if budget <= 0:
            raise RuntimeError('handover deadline exceeded')
        return budget
    def check_deadline():
        if _now() >= deadline:
            raise RuntimeError('handover deadline exceeded')
    if old is None or successor is None or successor["state"] != "standby":
        raise RuntimeError("successor is not ready or old generation is missing")
    old_identity = GenerationIdentity(**{key: old[key] for key in GenerationIdentity.__dataclass_fields__})
    path = generation_paths(home, old_identity)["socket"]
    roster_budget = remaining()
    roster = _generation_request(path, "polling_roster", params={"deadline": _now() + roster_budget},
                                 timeout=roster_budget)
    check_deadline()
    tokens = roster.get("tokens")
    if not isinstance(tokens, list) or any(not isinstance(token, str) for token in tokens):
        raise RuntimeError("invalid old generation polling roster")
    if require_pollers and not tokens:
        raise RuntimeError('empty polling roster cannot qualify forward-only promotion')
    coordinator.request_transfer(old_id, to_id, epoch, set(tokens), deadline=coordinator_deadline)
    check_deadline()
    nonce = coordinator.transfer_attempt_nonce(old_id, epoch, deadline=coordinator_deadline)
    check_deadline()
    try:
        ack = _generation_request(path, "transfer_requested",
                                  params={"to": to_id, "deadline": coordinator_deadline},
                                  timeout=remaining())
        check_deadline()
        if (ack.get("generation_id"), ack.get("epoch"), ack.get("poller_stopped")) != (old_id, epoch, True):
            raise RuntimeError("old generation did not prove poller stopped")
        if before_commit is not None:
            before_commit()
        check_deadline()
        with deadline_scope(coordinator_deadline):
            promoted = coordinator.commit_transfer(old_id, to_id, epoch,
                                                   drain_seconds=drain_seconds,
                                                   deadline=coordinator_deadline)
    except Exception:
        # Never hide the transfer failure with a second failure during recovery.
        # Attempt both abort and re-arm even if either operation fails.
        abort_budget = min(HANDOVER_ABORT_RESERVE, max(0.0, deadline - _now()))
        try:
            abort_budget = min(HANDOVER_ABORT_RESERVE, remaining(recovery=True))
            coordinator.abort_transfer(old_id, to_id, epoch, attempt_nonce=nonce,
                                       deadline=_now() + abort_budget)
        except Exception:
            logger.exception("transfer abort failed after pre-commit failure")
        try:
            _generation_request(path, "transfer_aborted",
                                params={"to": to_id, "nonce": nonce,
                                        "deadline": _now() + abort_budget},
                                timeout=abort_budget)
        except Exception:
            logger.exception("poller re-arm failed after pre-commit failure")
        raise
    # The lease moved irreversibly at commit. A late commit is never a plain
    # failure: callers would treat it as pre-commit and roll back a healthy
    # successor. The commit-clocked poller (or the typed outcome below) bounds it.
    if not verify_after_commit:
        return promoted
    successor_identity = GenerationIdentity(**{key: successor[key] for key in GenerationIdentity.__dataclass_fields__})
    successor_socket = generation_paths(home, successor_identity)["socket"]
    while _now() < deadline:
        claimed = next(row for row in coordinator.generations() if row['id'] == to_id)
        if coordinator._owner_is_dead(claimed):
            raise HandoverCommittedUnverified(to_id, promoted)
        try:
            socket_remaining = deadline - _now()
            if socket_remaining <= 0:
                raise HandoverCommittedUnverified(to_id, promoted)
            status = _generation_request(successor_socket, "polling_status", timeout=min(2, socket_remaining))
            if _now() >= deadline:
                raise HandoverCommittedUnverified(to_id, promoted)
            if status.get("generation_id") == to_id and status.get("polling") is True and set(status.get("tokens", [])) == set(tokens):
                return promoted
        except RuntimeError:
            pass
        time.sleep(.2)
    raise HandoverCommittedUnverified(to_id, promoted)


async def _run_generation_startup_gate(config, coordinator, identity) -> None:
    """Retire a failed private probe before either startup path can take a lease."""
    import json
    from gateway.startup_gate import run_startup_gate
    verdict = None
    try:
        verdict = await run_startup_gate(config)
        if not verdict.ready:
            raise RuntimeError("startup gate failed")
    except BaseException as exc:
        receipt = {"reason": "startup_gate_failed"}
        if verdict is not None:
            receipt["gate"] = json.loads(verdict.evidence_text)
        else:
            receipt["error"] = type(exc).__name__
        coordinator._record_failure(identity.id, json.dumps(receipt, sort_keys=True))
        raise


async def start_active_generation(config, *, claimed_generation=None) -> "ActiveGeneration | None":
    """Register an already singleton-claimed active gateway; never claim from standby."""
    if not overlap_handover_enabled(config):
        return None
    if claimed_generation is None:
        coordinator, identity = claim_active_generation(forward_only=forward_only_handover_enabled(config))
    else:
        coordinator, identity = claimed_generation
    home = Path(get_hermes_home())
    if forward_only_handover_enabled(config):
        await _run_generation_startup_gate(config, coordinator, identity)
    try:
        # Fresh legacy starts reuse the existing clean-exit takeover path
        # when shutdown retained a released lease and its epoch.
        epoch = _activate_cold_generation(coordinator, identity)
    except Exception:
        coordinator._record_failure(identity.id, "startup_failed")
        raise
    active = ActiveGeneration(home, coordinator, identity, epoch)
    try:
        await active.start()
        from gateway.status import set_generation_runtime_status
        set_generation_runtime_status(identity.id)
    except BaseException:
        coordinator._record_failure(identity.id, "startup_failed")
        await active.close()
        raise
    return active


def _bootout_retired_generation(label: str) -> bool:
    from hermes_cli.gateway_guardian import _gateway_domain, _launch_state
    import subprocess
    budget = deadline_remaining()
    timeout = 10 if budget is None else min(10.0, budget)
    if timeout <= 0:
        raise TimeoutError("gateway deadline exceeded")
    domain = _gateway_domain(label, None)
    subprocess.run(["launchctl", "bootout", f"{domain}/{label}"], capture_output=True, timeout=timeout)
    gateway_deadline.check()
    return _launch_state(domain, label) == "unloaded"


# Cold activation is part of startup; it shares the startup bound
# (hermes_cli.gateway_forward_update.STARTUP_SECONDS, asserted equal in tests).
COLD_ACTIVATION_SECONDS = 45


def _activate_cold_generation(coordinator, identity):
    """Acquire or take over the active lease within a concrete startup bound.

    An enclosing scope (the guardian's or the standby's) wins when it is
    earlier; otherwise the bound is COLD_ACTIVATION_SECONDS from now.
    """
    deadline = gateway_deadline.current()
    own = gateway_deadline.now() + COLD_ACTIVATION_SECONDS
    deadline = own if deadline is None else min(deadline, own)
    with deadline_scope(deadline):
        lease = next((row for row in coordinator.leases() if row['resource'] == 'active_generation'), None)
        if lease is None:
            epoch = coordinator.acquire_lease('active_generation', identity.id, deadline=deadline)
            coordinator.transition_state(identity.id, 'standby', 'serving')
            return epoch
        return coordinator.takeover_dead_generation('active_generation', lease['generation_id'], identity.id,
            bootout=lambda label: True if label == identity.label else _bootout_retired_generation(label),
            deadline=deadline)


def claim_active_generation(*, forward_only: bool = True) -> tuple[GenerationCoordinator, GenerationIdentity]:
    """Claim process identity before the singleton startup can acquire resources."""
    from gateway.status import _get_process_start_time
    home = Path(get_hermes_home())
    coordinator = GenerationCoordinator(home)
    started = _get_process_start_time(os.getpid())
    if started is None:
        raise RuntimeError("cannot determine process start time for generation identity")
    label = os.environ.get("HERMES_LAUNCHD_LABEL")
    if forward_only:
        service = coordinator.service_label()
        if label != service and not any(row['label'] == label for row in coordinator.generations()):
            label = service
    identity = GenerationIdentity.create(
        release_sha=os.environ.get("HERMES_RELEASE_SHA", "unknown"),
        label=label or "ai.hermes.gateway",
        start_fingerprint=f"{os.getpid()}:{started}",
    )
    claim = _claim_process_generation if forward_only else _claim_legacy_process_generation
    return coordinator, claim(coordinator, identity)


def defer_forward_launchd_restart(config) -> bool:
    """A planned launchd exit must reload the service with a new single-use scope."""
    import sys
    if (sys.platform != 'darwin' or not forward_only_handover_enabled(config)
            or not os.environ.get('HERMES_GENERATION_SCOPE')
            or not os.environ.get('HERMES_LAUNCHD_LABEL')):
        return False
    from hermes_cli.gateway_launchd import _spawn_deferred_launchd_reload
    from hermes_cli.gateway import get_launchd_plist_path, get_launchd_label, _launchd_domain
    from gateway.status import _get_process_start_time
    coordinator = GenerationCoordinator(Path(get_hermes_home()))
    label = coordinator.service_label()
    fingerprint = f'{os.getpid()}:{_get_process_start_time(os.getpid())}'
    generation_id = next((row['id'] for row in coordinator.generations()
                          if row['start_fingerprint'] == fingerprint), None)
    plist_path = get_launchd_plist_path()
    if label != get_launchd_label():
        plist_path = plist_path.with_name(f'{label}.plist')
    domain = _launchd_domain()
    if not _spawn_deferred_launchd_reload(domain=domain, label=label,
            target=f'{domain}/{label}', plist_path=plist_path, gateway_pid=os.getpid(),
            generation_id=generation_id):
        raise RuntimeError('forward-only planned restart could not submit launchd reload')
    return True


def _claim_process_generation(coordinator: GenerationCoordinator,
                              process: GenerationIdentity) -> GenerationIdentity:
    """Bind the reserved label once, before any generation resource is acquired.

    The legacy first A and direct CLI launches reserve at this boundary. Managed
    launchers reserve before bootstrap and the child finds that exact row.
    """
    import uuid
    scope_nonce = os.environ.get("HERMES_GENERATION_SCOPE") or uuid.uuid4().hex
    identity = coordinator.claim_process(process, scope_nonce)
    if identity is None:
        raise SystemExit(0)  # KeepAlive SuccessfulExit=false parks consumed scopes.
    return identity


def _claim_legacy_process_generation(coordinator, process):
    """Replace only proven-dead legacy claimants, before the same-label insert."""
    from gateway.generation import _is_unclaimed

    with contextlib.closing(coordinator.connect()) as conn, conn:
        begin_immediate(conn)
        rows = conn.execute("SELECT * FROM generations WHERE label=? AND state<>'exited'",
                            (process.label,)).fetchall()
        for row in rows:
            if (_is_unclaimed(row) or not row['start_fingerprint']
                    or not coordinator._owner_is_dead(row)):
                raise RuntimeError(f"cannot start same-label legacy generation {process.label}: "
                                   "holder is alive or identity unknown")
            if not coordinator._retire_in_transaction(conn, row['id'], expected_pid=row['pid'],
                    expected_start_fingerprint=row['start_fingerprint'],
                    evidence='boot_changed' if row['boot_id'] != process.boot_id else 'dead'):
                raise RuntimeError("same-label legacy generation retirement failed")
        # Retain the lease and its epoch for the existing legacy takeover.
        coordinator._register_in_transaction(conn, process)
        conn.commit()
    return process


def _generation_socket_owner_path(socket_path: Path) -> Path:
    return socket_path.with_name(f".{socket_path.name}.owner.json")


def _socket_accepts_connections(socket_path: Path) -> bool:
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
            probe.settimeout(0.2)
            probe.connect(str(socket_path))
        return True
    except OSError:
        return False


def _generation_owner_is_dead(socket_path: Path) -> bool:
    try:
        owner = json.loads(_generation_socket_owner_path(socket_path).read_text(encoding="utf-8-sig"))
        pid = int(owner["pid"])
        expected_start = owner.get("start_time")
        from gateway.status import _get_process_start_time, _pid_exists
        if _pid_exists(pid):
            return expected_start is not None and _get_process_start_time(pid) != expected_start
        return True
    except (OSError, ValueError, KeyError, TypeError):
        return False


def _cleanup_stale_generation_socket_roots(current: Path) -> None:
    root = current.parent
    if root.parent != Path(os.path.sep, "tmp") or not root.name.startswith("hg-"):
        return
    uid = getattr(os, "getuid", lambda: 0)()
    prefix = f"hg-{uid}-"
    grace_period = 10 * 60
    for sibling in root.parent.glob(f"{prefix}*"):
        if sibling == root or not sibling.is_dir():
            continue
        try:
            mode = stat.S_IMODE(sibling.stat().st_mode)
            if sibling.stat().st_uid != uid or mode & 0o077:
                continue
            if time.time() - sibling.stat().st_mtime < grace_period:
                continue
            sockets = list(sibling.glob("*.sock"))
            if not sockets or any(_socket_accepts_connections(path) for path in sockets):
                continue
            if not all(_generation_owner_is_dead(path) for path in sockets):
                continue
            shutil.rmtree(sibling)
        except OSError:
            continue


def _ensure_generation_socket_parent(socket_path: Path) -> None:
    parent = socket_path.parent
    if parent.parent == Path(os.path.sep, "tmp") and parent.name.startswith("hg-"):
        _cleanup_stale_generation_socket_roots(socket_path)
        parent.mkdir(mode=0o700, exist_ok=True)
        st = parent.lstat()
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != getattr(os, "getuid", lambda: 0)() or st.st_mode & 0o077:
            raise RuntimeError("generation control socket directory is not private")
    else:
        parent.mkdir(parents=True, exist_ok=True)


def _remove_empty_generation_socket_parent(socket_path: Path) -> None:
    parent = socket_path.parent
    if parent.parent == Path(os.path.sep, "tmp") and parent.name.startswith("hg-"):
        with suppress(OSError):
            parent.rmdir()  # Never remove another generation's live socket.


class GenerationControlServer(GatewayControlServer):
    """Generation-scoped control endpoint that never touches legacy paths."""

    def __init__(self, home: Path, socket_path: Path, *, verb_handlers=None) -> None:
        super().__init__(home, verb_handlers=verb_handlers)
        self._generation_socket_path = socket_path

    async def _start_posix(self) -> bool:
        bind_path = self._generation_socket_path
        bind_path.parent.mkdir(parents=True, exist_ok=True)
        if bind_path.exists():
            if _socket_accepts_connections(bind_path):
                raise RuntimeError(f"generation control socket is already live: {bind_path}")
            with contextlib.suppress(OSError):
                bind_path.unlink()
        old_umask = os.umask(0o177)
        try:
            self._server = await asyncio.start_unix_server(self._handle_connection, path=str(bind_path))
        finally:
            os.umask(old_umask)
        os.chmod(bind_path, 0o600)
        from gateway.status import _build_pid_record
        _generation_socket_owner_path(bind_path).write_text(json.dumps(_build_pid_record()), encoding="utf-8")
        self._bind_path = bind_path
        return True

    async def stop(self) -> None:
        # The base cleanup unlinks unconditionally. Retain the bind path for an
        # inode-fenced unlink by ActiveGeneration.close instead.
        self._bind_path = None
        await super().stop()

    async def _start_windows(self) -> bool:
        return False


async def take_over_legacy_gateway_resources(identity: GenerationIdentity, *, claim, start_socket, refresh):
    """After the old owner exits, claim the singleton surfaces for the promoted owner."""
    from gateway.status import get_running_pid, is_gateway_runtime_lock_active

    delay = 1.0
    failed_claims = 0
    while True:
        owner = get_running_pid()
        if owner in (None, os.getpid()) and not is_gateway_runtime_lock_active():
            try:
                if claim():
                    server = await start_socket()
                    refresh()
                    logger.info("Promoted generation %s acquired legacy gateway resources", identity.id)
                    return server
            except (RuntimeError, SystemExit):
                logger.debug("Promoted generation takeover is still fenced", exc_info=True)
                from gateway.status import (owns_gateway_runtime_lock, remove_pid_file,
                                            release_gateway_runtime_lock)
                if owns_gateway_runtime_lock():
                    remove_pid_file()
                    release_gateway_runtime_lock()
            failed_claims += 1
            if failed_claims == 5:
                logger.warning("Promoted generation could not claim legacy gateway resources; retrying slowly")
        delay = 30.0 if failed_claims >= 5 else min(delay + 1.0, 5.0)
        await asyncio.sleep(delay)


class ActiveGeneration:
    """Generation-scoped identity and heartbeat alongside the existing active dispatcher."""

    def __init__(self, home: Path, coordinator: GenerationCoordinator,
                 identity: GenerationIdentity, epoch: int):
        self.home, self.coordinator, self.identity, self.epoch = home, coordinator, identity, epoch
        self.paths = generation_paths(home, identity)
        self.server: GatewayControlServer | None = None
        self.task: asyncio.Task | None = None
        self.socket_stat = None
        self._last_runtime: dict | None = None
        self._last_status_write = 0.0
        self.runner = None
        self.cron_stop = None
        self.cron_provider = None
        self._transfer_lock = asyncio.Lock()
        self._drain_task: asyncio.Task | None = None
        self._drain_stopping = False
        self.owned_routing = None
        self._drain_stopped = False
        self._missing_deadline_warned = False
        self._local_drain_deadline: float | None = None
        self._stopped_receipts: list[tuple[object, dict]] = []
        self._pending_transfer: tuple[str, str, float] | None = None
        self._external_cron_stopped = False
        self._rearm_errors: list[str] = []

    def bind_runner(self, runner, *, cron_stop=None, cron_provider=None) -> None:
        self.runner = runner
        self.cron_stop = cron_stop
        self.cron_provider = cron_provider
        from gateway.owned_routing import OwnedRouting
        self.owned_routing = OwnedRouting(self)
        self.owned_routing.bind(runner)
        self.owned_routing._task = asyncio.create_task(self.owned_routing.drain(), context=detached_context())

    def _telegram_adapters(self) -> dict[str, object]:
        adapters = getattr(self.runner, "adapters", {}) or {}
        result = {}
        for adapter in adapters.values():
            journal = getattr(adapter, "_controlled_journal", None)
            if journal is not None:
                token = journal.token_hash
                if token in result:
                    raise RuntimeError("duplicate Telegram token in generation roster")
                result[token] = adapter
        return result

    def polling_status(self) -> dict:
        roster = self._telegram_adapters()
        return {"generation_id": self.identity.id,
                "release_sha": self.identity.release_sha, "epoch": self.epoch,
                "release_root": str(Path(__file__).resolve().parent.parent),
                "healthy": not self._rearm_errors and not (self._last_runtime or {}).get('needs_attention', False)
                           and (self._last_runtime or {}).get('gateway_state') not in {'startup_failed', 'degraded', 'stopped'},
                "armed": self.rearm_status()['armed'],
                "tokens": sorted(roster),
                "polling": self.runner is not None and all(
                    getattr(getattr(adapter, "_controlled_poller", None), "running", False)
                    and getattr(getattr(adapter, "_polling_progress_event", None), "is_set", lambda: False)()
                    for adapter in roster.values())}

    async def polling_roster(self) -> dict:
        # Wire progress precedes completed re-arm. A retry must not replace the
        # aborted nonce until recovery's final owner check has reopened dispatch.
        async with self._transfer_lock:
            return {"tokens": sorted(self._telegram_adapters())}

    @with_deadline_scope
    async def transfer_requested(self, new_id: str, *, deadline: float | None = None) -> dict:
        """Old owner alone can stop its wire and persist receipts; never stop a live turn."""
        async with self._transfer_lock:
            if self.runner is None:
                raise RuntimeError("active runner has not reached ready state")
            transfer = self.coordinator.transfer_receipts(self.identity.id, self.epoch)
            roster = self._telegram_adapters()
            if {row["token_hash"] for row in transfer} != set(roster):
                raise RuntimeError("frozen token roster differs from live adapters")
            # Invalid obligations must not pause a healthy polling/cron owner.
            # claim_live validates again after flushing newly materialized work;
            # that late failure uses the existing abort-and-rearm path.
            if self.owned_routing is not None:
                self.owned_routing.validate_live()
            stopped = []
            nonce = await asyncio.to_thread(
                self.coordinator.transfer_attempt_nonce, self.identity.id, self.epoch,
                **_deadline_kwargs(deadline))
            self.runner._overlap_draining = True
            try:
                for token, adapter in roster.items():
                    receipt = await adapter.stop_polling_for_transfer()
                    if receipt.get("token_hash") != token:
                        raise RuntimeError("poller stop token mismatch")
                    stopped.append((adapter, receipt))
                    await asyncio.to_thread(
                        self.coordinator.record_poller_stopped,
                        self.identity.id, self.epoch, token, receipt["safe_offset"],
                        attempt_nonce=nonce, deadline=deadline)
                # No more wire updates can extend a split text batch. Dispatch it
                # while A still owns the lease, before the successor can receive it.
                for adapter in roster.values():
                    for key in tuple(getattr(adapter, "_pending_text_batches", {})):
                        await adapter._flush_text_batch_now(key)
                    for key in tuple(getattr(adapter, "_pending_photo_batches", {})):
                        await adapter._flush_photo_batch_now(key)
                    for key in tuple(getattr(adapter, "_media_group_events", {})):
                        await adapter._flush_media_group_now(key)
                # Freeze A's live session obligations before the lease can move.
                if self.owned_routing is not None:
                    self.owned_routing.claim_live()
                # Keep the shared housekeeping/cron stop event alive. The built-in
                # ticker observes the overlap dispatch gate; external providers
                # are explicitly stopped and re-armed on abort.
                if self.cron_provider is not None:
                    from cron.scheduler_provider import InProcessCronScheduler
                    if not isinstance(self.cron_provider, InProcessCronScheduler):
                        from gateway.run import _stop_cron_provider
                        _stop_cron_provider(self.cron_provider)
                        self._external_cron_stopped = True
                self._stopped_receipts = stopped
                # The watchdog inherits the driver's window: it fires when that
                # window ends, never a fresh full HANDOVER_REQUEST_TIMEOUT later.
                watchdog_at = _now() + HANDOVER_REQUEST_TIMEOUT
                if deadline is not None:
                    watchdog_at = min(watchdog_at, float(deadline))
                self._pending_transfer = (new_id, nonce, watchdog_at)
                self._drain_task = asyncio.create_task(self._drain_after_transfer(), context=detached_context())
                return {"poller_stopped": True, "generation_id": self.identity.id,
                        "epoch": self.epoch, "tokens": len(stopped)}
            except Exception as original:
                # The recovery reserve is deliberately outside the caller's
                # transfer-request scope: the original deadline covers the
                # cooperative stop, while abort/re-arm has its own bounded path.
                self._stopped_receipts = stopped
                return await self._recover_failed_transfer(new_id, nonce, deadline, original)

    async def _recover_failed_transfer(self, new_id, nonce, deadline, original):
        with deadline_scope(_now() + HANDOVER_ABORT_RESERVE, inherit=False):
            try:
                await asyncio.to_thread(
                    self.coordinator.abort_transfer,
                    self.identity.id, new_id, self.epoch,
                    attempt_nonce=nonce,
                    **_deadline_kwargs(_now() + HANDOVER_ABORT_RESERVE))
            except Exception:
                message = "poller stop failed and transfer abort could not be proved"
                self._rearm_errors = [message]
                logger.exception("transfer abort failed after poller stop failure")
                await asyncio.to_thread(self._sync_runtime_status)
                raise RuntimeError(message) from original
            try:
                # The driver may already have aborted this nonce while the
                # long poll was stopping. False is not a changed attempt until
                # the fresh lease/identity/nonce read below proves it is.
                errors = await self._rearm_stopped_pollers(new_id, nonce)
            except RuntimeError:
                message = "poller stop failed and transfer attempt changed"
                self._rearm_errors = [message]
                await asyncio.to_thread(self._sync_runtime_status)
                raise RuntimeError(message) from original
            if errors:
                logger.error("poller stop failed: %s; re-arm failed for %s",
                             original, ", ".join(errors))
                raise RuntimeError(f"poller stop failed; re-arm failed for {', '.join(errors)}") from original
            raise original

    async def _check_rearm_owner(self, new_id=None, attempt_nonce=None) -> None:
        def read_owner():
            with contextlib.closing(self.coordinator.connect()) as conn:
                return conn.execute(
                    "SELECT g.*,l.generation_id AS holder,l.epoch AS lease_epoch,l.state AS lease_state,"
                    "t.new_id,t.state AS transfer_state,t.attempt_nonce FROM generations g "
                    "LEFT JOIN leases l ON l.resource='active_generation' "
                    "LEFT JOIN generation_transfers t ON t.old_id=g.id AND t.epoch=? WHERE g.id=?",
                    (self.epoch, self.identity.id)).fetchone()
        row = await asyncio.to_thread(read_owner)
        if row is None or row['state'] != 'serving' or row['verdict'] is not None:
            raise RuntimeError("only a still-serving generation can resume")
        if ((row['holder'], row['lease_epoch'], row['lease_state']) !=
                (self.identity.id, self.epoch, 'active') or
                any(row[key] != getattr(self.identity, key)
                    for key in ('pid', 'start_fingerprint', 'boot_id'))):
            raise RuntimeError("old generation no longer owns admission")
        if new_id is not None and (row['new_id'], row['transfer_state'], row['attempt_nonce']) != (
                new_id, 'aborted', attempt_nonce):
            raise RuntimeError("transfer has not been aborted for this attempt")

    async def _rearm_stopped_pollers(self, new_id=None, attempt_nonce=None) -> list[str]:
        await self._check_rearm_owner(new_id, attempt_nonce)
        errors: list[str] = []
        remaining = []
        for adapter, receipt in self._stopped_receipts:
            try:
                await adapter.start_polling_from_transfer(receipt)
            except Exception:
                name = receipt["token_hash"]
                logger.exception("poller re-arm failed for %s", name)
                errors.append(name)
                remaining.append((adapter, receipt))
        self._stopped_receipts = remaining
        if self._external_cron_stopped:
            try:
                kwargs = getattr(self.runner, "_overlap_cron_start_kwargs", None)
                if kwargs is None or self.cron_stop is None:
                    raise RuntimeError("external cron provider cannot be re-armed")
                await asyncio.to_thread(self.cron_provider.start, self.cron_stop, **kwargs)
                self._external_cron_stopped = False
            except Exception:
                logger.exception("external cron provider re-arm failed")
                errors.append("cron provider")
        self._rearm_errors = errors
        if errors:
            logger.error("generation %s remains draining: re-arm failed for %s",
                         self.identity.id, ", ".join(errors))
        else:
            await self._check_rearm_owner(new_id, attempt_nonce)
            self._pending_transfer = None
            self.runner._overlap_draining = False
        return errors

    def rearm_status(self) -> dict:
        """Read the actual dispatch fences shared by cron, kanban and wakeup loops."""
        gate = not getattr(self.runner, '_overlap_draining', False)
        cron_gate = (getattr(self.runner, '_overlap_cron_start_kwargs', None) or {}).get('can_dispatch')
        pollers = self._telegram_adapters()
        poller_armed = all(
            getattr(getattr(adapter, '_controlled_poller', None), 'running', False)
            for adapter in pollers.values())
        return {'generation_id': self.identity.id, 'epoch': self.epoch,
                'rearmed': gate and not self._rearm_errors,
                'armed': {'poller': poller_armed, 'cron': gate and not self._external_cron_stopped
                          and (cron_gate() if callable(cron_gate) else True),
                          'kanban': gate, 'goal_wakeup': gate}}

    @with_deadline_scope
    async def transfer_aborted(self, new_id: str, attempt_nonce: str | None = None,
                               *, deadline: float | None = None) -> dict:
        """Re-arm only for the same aborted attempt under the old lease."""
        async with self._transfer_lock:
            lease = next((row for row in await asyncio.to_thread(self.coordinator.leases)
                          if row["resource"] == "active_generation"), None)
            if not lease or (lease["generation_id"], lease["epoch"], lease["state"]) != (
                    self.identity.id, self.epoch, "active"):
                raise RuntimeError("old generation no longer owns admission")
            if not getattr(self.runner, "_overlap_draining", False):
                return self.rearm_status()
            def read_transfer():
                with contextlib.closing(self.coordinator.connect()) as conn:
                    return conn.execute(
                        "SELECT state,attempt_nonce FROM generation_transfers WHERE old_id=? AND new_id=? AND epoch=?",
                        (self.identity.id, new_id, self.epoch)).fetchone()

            transfer = await asyncio.to_thread(read_transfer)
            if transfer is None or transfer["state"] != "aborted" or (
                    attempt_nonce is not None and transfer["attempt_nonce"] != attempt_nonce):
                raise RuntimeError("transfer has not been aborted for this attempt")
            errors = await self._rearm_stopped_pollers(new_id, transfer['attempt_nonce'])
            if errors:
                raise RuntimeError(f"re-arm failed for {', '.join(errors)}")
            return self.rearm_status()

    async def finish_draining_once(self) -> bool:
        """Stop A only after B owns the lease and all locally owned work has settled."""
        if self.runner is None or not getattr(self.runner, "_overlap_draining", False):
            return False
        rows = await asyncio.to_thread(self.coordinator.generations)
        record = next((row for row in rows if row["id"] == self.identity.id), None)
        if record is None or record["state"] != "draining":
            return False
        from tools.process_registry import process_registry
        # A claim covers only its session. A process from an ended cron turn can
        # have a key but no claim; stopping this owner would kill it before notice.
        busy = (self.runner._active_work_count() or bool(self.runner._pending_approvals))

        def read_pending_work():
            with contextlib.closing(self.coordinator.connect()) as conn:
                queued = conn.execute("SELECT 1 FROM inbox WHERE owner_id=? AND state='pending' LIMIT 1",
                                      (self.identity.id,)).fetchone()
                claims = conn.execute("SELECT 1 FROM sessions WHERE generation_id=? LIMIT 1",
                                      (self.identity.id,)).fetchone()
                return queued, claims

        queued, claims = await asyncio.to_thread(read_pending_work)
        deadline = record["drain_deadline"]
        if deadline is None:
            if self._local_drain_deadline is None:
                self._local_drain_deadline = time.time() + DEFAULT_DRAIN_SECONDS
            deadline = self._local_drain_deadline
            if not self._missing_deadline_warned:
                logger.warning("generation missing drain deadline; using local drain cap")
                self._missing_deadline_warned = True
        if (busy or queued or claims or process_registry.has_any_active()
                or process_registry.pending_watchers) and time.time() < deadline:
            return False
        if self._drain_stopping:
            return self._drain_stopped
        self._drain_stopping = True
        try:
            if busy or queued or claims:
                await asyncio.to_thread(self.coordinator.fence_draining_generation, self.identity.id)
                # The normal shutdown path marks live turns resume_pending. The cap
                # is different: interrupted side effects must not auto-run again.
                self.runner._overlap_cap_interrupted = True
            await self.runner.stop()
        except BaseException:
            self._drain_stopping = False
            raise
        self._drain_stopped = True
        return True

    async def _drain_after_transfer(self) -> None:
        while True:
            if self.runner is None or not getattr(self.runner, "_overlap_draining", False):
                return
            pending = self._pending_transfer
            if pending and _now() >= pending[2]:
                new_id, nonce, _ = pending
                async with self._transfer_lock:
                    # Roster requests share this lock, so a retry cannot replace
                    # the aborted nonce before recovery has reopened dispatch.
                    recovery_deadline = _now() + HANDOVER_ABORT_RESERVE
                    with deadline_scope(recovery_deadline):
                        try:
                            aborted = await asyncio.to_thread(
                                self.coordinator.abort_transfer, self.identity.id, new_id,
                                self.epoch, attempt_nonce=nonce, deadline=recovery_deadline)
                        except RuntimeError as exc:
                            if str(exc) == "cannot abort a committed transfer":
                                # A committed successor owns admission; do not re-arm A.
                                self._pending_transfer = None
                            else:
                                logger.exception("transfer deadline abort failed; old gateway remains fenced")
                        except Exception:
                            logger.exception("transfer deadline abort failed; old gateway remains fenced")
                        else:
                            if not aborted:
                                def read_transfer():
                                    with contextlib.closing(self.coordinator._deadline_connect(recovery_deadline)) as conn:
                                        return conn.execute(
                                            "SELECT state,attempt_nonce FROM generation_transfers WHERE old_id=? AND epoch=?",
                                            (self.identity.id, self.epoch)).fetchone()

                                try:
                                    row = await asyncio.to_thread(read_transfer)
                                except Exception:
                                    logger.exception("transfer deadline status unavailable; old gateway remains fenced")
                                    row = None
                                if row is not None and row["attempt_nonce"] != nonce:
                                    logger.warning("transfer attempt changed; dropping stale pending recovery for %s", self.identity.id)
                                    self._pending_transfer = None
                                elif row is not None and row["state"] == "committed":
                                    self._pending_transfer = None
                                elif row is not None and row["state"] == "aborted":
                                    aborted = True
                                else:
                                    self._rearm_errors = ["transfer deadline abort could not be proved"]
                                    await asyncio.to_thread(self._sync_runtime_status)
                            if aborted:
                                logger.warning("transfer deadline expired; re-arming old gateway %s", self.identity.id)
                                try:
                                    await self._rearm_stopped_pollers(new_id, nonce)
                                except Exception:
                                    logger.exception("transfer deadline recovery failed; old gateway remains fenced")
            try:
                if await self.finish_draining_once():
                    return
            except Exception:
                logger.warning("generation drain inspection failed; retaining old gateway", exc_info=True)
            await asyncio.sleep(1)

    async def start(self) -> None:
        await asyncio.to_thread(self.coordinator.prune_history)
        for name in ("pid", "host"):
            write_generation_record(self.paths[name], self.identity, state="serving")
        socket_path = self.paths["socket"]
        _ensure_generation_socket_parent(socket_path)
        if len(os.fsencode(socket_path)) >= 100:
            raise RuntimeError("generation control socket path exceeds UNIX socket limit")
        loop = asyncio.get_running_loop()

        def _transfer_handler(params: dict) -> dict:
            new_id = params.get("to")
            if not isinstance(new_id, str):
                raise RuntimeError("successor generation ID required")
            deadline = params.get("deadline")
            if deadline is not None and (isinstance(deadline, bool) or not isinstance(deadline, (int, float))):
                raise RuntimeError("transfer deadline required")
            future = asyncio.run_coroutine_threadsafe(
                self.transfer_requested(new_id, deadline=None if deadline is None else float(deadline)), loop)
            # Control-socket handlers are rare, bounded operations; waiting here keeps the
            # synchronous socket protocol simple without occupying an event-loop thread.
            wait_timeout = HANDOVER_REQUEST_TIMEOUT
            if deadline is not None:
                wait_timeout = min(wait_timeout, max(0.0, float(deadline) - _now()))
            return future.result(timeout=wait_timeout)

        def _abort_handler(params: dict) -> dict:
            new_id = params.get("to")
            if not isinstance(new_id, str):
                raise RuntimeError("successor generation ID required")
            deadline = params.get("deadline")
            if deadline is not None and (isinstance(deadline, bool) or not isinstance(deadline, (int, float))):
                raise RuntimeError("transfer deadline required")
            future = asyncio.run_coroutine_threadsafe(
                self.transfer_aborted(new_id, params.get("nonce"),
                                      deadline=None if deadline is None else float(deadline)), loop)
            wait_timeout = HANDOVER_REQUEST_TIMEOUT
            if deadline is not None:
                wait_timeout = min(wait_timeout, max(0.0, float(deadline) - _now()))
            return future.result(timeout=wait_timeout)

        def _roster_handler(params: dict) -> dict:
            deadline = params.get("deadline")
            if deadline is not None and (isinstance(deadline, bool) or not isinstance(deadline, (int, float))):
                raise RuntimeError("roster deadline must be a number")
            future = asyncio.run_coroutine_threadsafe(self.polling_roster(), loop)
            wait_timeout = HANDOVER_REQUEST_TIMEOUT
            if deadline is not None:
                wait_timeout = min(wait_timeout, max(0.0, float(deadline) - _now()))
            try:
                return future.result(timeout=wait_timeout)
            except BaseException:
                # The caller's bound is over: never leave the roster waiter queued
                # on _transfer_lock behind abort/re-arm.
                future.cancel()
                raise

        self.server = GenerationControlServer(
            self.home, self.paths["socket"],
            verb_handlers={"transfer_requested": _transfer_handler,
                           "transfer_aborted": _abort_handler,
                           "polling_roster": _roster_handler,
                           "polling_status": self.polling_status})
        if not await self.server.start():
            raise RuntimeError("generation control socket unavailable")
        self.socket_stat = self.paths["socket"].stat()
        write_generation_record(self.paths["state"], self.identity, state="serving", socket_path=self.paths["socket"])
        self.task = asyncio.create_task(self._heartbeat(), context=detached_context())

    async def mark_ready(self) -> None:
        await asyncio.to_thread(self._sync_runtime_status)
        # A handover may commit while B is starting its runner. Heartbeat without
        # changing the coordinator's authoritative serving state.
        await asyncio.to_thread(self.coordinator.heartbeat, self.identity.id)

    def _sync_runtime_status(self) -> None:
        from gateway.status import read_runtime_status
        runtime = read_runtime_status(self.home / f"gateway_runtime.{self.identity.id}.json") or {}
        # The singleton PID and lease both belong to this process before it may
        # project the compatibility status to the generation-scoped record.
        if runtime.get("pid") != self.identity.pid:
            runtime = {}
        if self._rearm_errors:
            runtime = {**runtime, "needs_attention": True, "polling": False,
                       "error_message": f"poller re-arm failed for {', '.join(self._rearm_errors)}"}
        now = _now()
        if runtime == self._last_runtime and now - self._last_status_write < 30:
            return
        row = next((row for row in self.coordinator.generations() if row["id"] == self.identity.id), None)
        state = row["state"] if row else "serving"
        write_generation_record(self.paths["state"], self.identity, state=state,
                                socket_path=self.paths["socket"], runtime=runtime)
        self.coordinator.project_active_summary(self.identity, self.epoch, runtime)
        self._last_runtime = runtime.copy()
        self._last_status_write = now

    async def _heartbeat(self) -> None:
        last_warning = 0.0
        while True:
            await asyncio.sleep(1)
            try:
                await asyncio.to_thread(self._sync_runtime_status)
                await asyncio.to_thread(self.coordinator.heartbeat, self.identity.id)
            except Exception:
                now = _now()
                if now - last_warning >= 30:
                    logger.warning("active generation heartbeat failed; retrying", exc_info=True)
                    last_warning = now

    async def close(self) -> None:
        if self.owned_routing is not None and self.owned_routing._task is not None:
            self.owned_routing._task.cancel()
            with suppress(asyncio.CancelledError):
                await self.owned_routing._task
        if self.task:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
        if self.server:
            await self.server.stop()
        if self.socket_stat:
            with suppress(FileNotFoundError):
                current = self.paths["socket"].stat()
                if (current.st_dev, current.st_ino) == (self.socket_stat.st_dev, self.socket_stat.st_ino):
                    self.paths["socket"].unlink()
        with suppress(OSError):
            _generation_socket_owner_path(self.paths["socket"]).unlink()
        _remove_empty_generation_socket_parent(self.paths["socket"])
        await asyncio.to_thread(self.coordinator.project_stopped_summary,
                                self.identity, self.epoch)
        await asyncio.to_thread(
            self.coordinator.release_lease, "active_generation", self.identity.id, self.epoch)
        await asyncio.to_thread(self.coordinator.heartbeat, self.identity.id, state="exited")
        await asyncio.to_thread(self.coordinator.release_exited_owner, self.identity.id)
        await asyncio.to_thread(remove_generation_files, self.home, self.identity)
        runtime_path = self.home / f"gateway_runtime.{self.identity.id}.json"
        from gateway.status import read_runtime_status
        runtime = read_runtime_status(runtime_path) or {}
        row = next((row for row in await asyncio.to_thread(self.coordinator.generations)
                    if row["id"] == self.identity.id), None)
        if runtime.get("pid") == self.identity.pid and row is not None and row["verdict"] is None:
            runtime_path.unlink(missing_ok=True)


async def serve_standby_generation(config=None, *, claimed_generation=None) -> bool:
    """Pass the forward-only loopback gate before readiness or cold takeover.

    A waiting standby connects no external adapter and holds no polling lock.
    Legacy flag-off startup retains its passive readiness path.
    """
    if config is None:
        from gateway.config import load_gateway_config
        config = load_gateway_config()
    if not overlap_handover_enabled(config):
        raise RuntimeError("gateway run --standby requires gateway.overlap_handover.enabled or gateway.forward_only_handover.enabled")

    home = Path(get_hermes_home())
    if claimed_generation is None:
        coordinator = GenerationCoordinator(home)
        label = os.environ.get("HERMES_LAUNCHD_LABEL", "ai.hermes.gateway-b")
        release_sha = os.environ.get("HERMES_RELEASE_SHA", "unknown")
        from gateway.status import _get_process_start_time
        started = _get_process_start_time(os.getpid())
        if started is None:
            raise RuntimeError("cannot determine process start time for generation identity")
        identity = GenerationIdentity.create(
            release_sha=release_sha, label=label,
            start_fingerprint=f"{os.getpid()}:{started}",
        )
        claim = _claim_process_generation if forward_only_handover_enabled(config) else _claim_legacy_process_generation
        identity = claim(coordinator, identity)
    else:
        coordinator, identity = claimed_generation
    if forward_only_handover_enabled(config):
        await _run_generation_startup_gate(config, coordinator, identity)
        lease = next((row for row in coordinator.leases() if row['resource'] == 'active_generation'), None)
        holder = next((row for row in coordinator.generations() if lease and row['id'] == lease['generation_id']), None)
        if lease is None or (holder and (coordinator._owner_is_dead(holder) or
                (lease['state'] == 'released' and holder['state'] == 'exited' and holder['verdict'] is None))):
            epoch = _activate_cold_generation(coordinator, identity)
            from gateway.run import start_gateway
            return await start_gateway(config, promoted_generation=(identity, epoch))
        logger.info('Standby waiting for cooperative handover: holder is live or identity unknown')
    paths = generation_paths(home, identity)

    async def report_ready(_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        import json
        writer.write((json.dumps({"generation_id": identity.id, "state": "standby",
                                 "release_sha": identity.release_sha}) + "\n").encode())
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    socket_path = paths["socket"]
    try:
        _ensure_generation_socket_parent(socket_path)
        if len(os.fsencode(socket_path)) >= 100:
            raise RuntimeError("generation control socket path exceeds UNIX socket limit")
    except BaseException:
        coordinator._record_failure(identity.id, "socket_setup_failed")
        raise
    old_umask = os.umask(0o177)
    try:
        server = await asyncio.start_unix_server(report_ready, path=str(socket_path))
    except BaseException:
        coordinator._record_failure(identity.id, "socket_bind_failed")
        raise
    finally:
        os.umask(old_umask)
    try:
        os.chmod(socket_path, 0o600)
        socket_stat = paths["socket"].stat()
    except BaseException:
        server.close()
        await server.wait_closed()
        socket_path.unlink(missing_ok=True)
        coordinator._record_failure(identity.id, "socket_setup_failed")
        raise
    try:
        write_generation_record(paths["pid"], identity, state="standby")
        write_generation_record(paths["host"], identity, state="standby")
        write_generation_record(paths["state"], identity, state="standby", socket_path=socket_path)
        # These files advertise the socket, not database runtime authority.
        # The claim supplied the initial liveness timestamp. A later refresh
        # belongs in the retry loop: a busy DB must not close this ready socket,
        # and publication must not reset a concurrently promoted generation.
    except BaseException:
        server.close()
        await server.wait_closed()
        with suppress(FileNotFoundError):
            current = socket_path.stat()
            if (current.st_dev, current.st_ino) == (socket_stat.st_dev, socket_stat.st_ino):
                socket_path.unlink()
        if any(row["id"] == identity.id for row in coordinator.generations()):
            coordinator._record_failure(identity.id, "startup_failed")
        remove_generation_files(home, identity)
        raise
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    installed: list[signal.Signals] = []
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)  # windows-footgun: ok — caught below
        except (NotImplementedError, RuntimeError):
            continue
        installed.append(sig)
    promoted_epoch = None
    try:
        last_warning = 0.0
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                try:
                    lease = next((row for row in coordinator.leases()
                                  if row["resource"] == "active_generation"), None)
                    if lease and lease["generation_id"] == identity.id and lease["state"] == "active":
                        promoted_epoch = lease["epoch"]
                        break
                    await asyncio.to_thread(coordinator.heartbeat, identity.id)
                except Exception:
                    now = _now()
                    if now - last_warning >= 30:
                        logger.warning("standby generation heartbeat failed; retrying", exc_info=True)
                        last_warning = now
    finally:
        server.close()
        await server.wait_closed()
        try:
            current = paths["socket"].stat()
            if (current.st_dev, current.st_ino) == (socket_stat.st_dev, socket_stat.st_ino):
                paths["socket"].unlink()
        except FileNotFoundError:
            pass
        _remove_empty_generation_socket_parent(socket_path)
        for sig in installed:
            with suppress(Exception):
                loop.remove_signal_handler(sig)
        if promoted_epoch is None:
            coordinator.heartbeat(identity.id, state="exited")
            remove_generation_files(home, identity)
    if promoted_epoch is not None:
        from gateway.run import start_gateway
        return await start_gateway(config, promoted_generation=(identity, promoted_epoch))
    return True
