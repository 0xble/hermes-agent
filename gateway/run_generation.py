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

from gateway.generation import (
    GenerationCoordinator,
    GenerationIdentity,
    generation_paths,
    overlap_handover_enabled,
    remove_generation_files,
    write_generation_record,
)

logger = logging.getLogger(__name__)
HANDOVER_REQUEST_TIMEOUT = 45  # Same bound as generation control acknowledgements.
DEFAULT_DRAIN_SECONDS = 7200  # Match the commit cap when the durable deadline is missing.


class HandoverCommittedUnverified(RuntimeError):
    """Lease changed irreversibly; inspect successor health, do not retry promotion."""

    def __init__(self, generation_id: str, epoch: int):
        self.generation_id = generation_id
        self.epoch = epoch
        super().__init__(f"successor {generation_id} holds epoch {epoch} but has not proved polling progress")


def _generation_request(path: Path, verb: str, *, params: dict | None = None,
                        timeout: float = 30) -> dict:
    request = json.dumps({"protocol": 1, "verb": verb, "params": params or {}}).encode() + b"\n"
    deadline = time.monotonic() + timeout
    response: dict | None = None
    # A live generation keeps its control socket. Tolerate only brief connection
    # startup/teardown races, not disappearance for the whole request timeout.
    for attempt in range(3):
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                remaining = max(0.1, deadline - time.monotonic())
                sock.settimeout(remaining)
                sock.connect(str(path))
                sock.sendall(request)
                chunks = bytearray()
                while b"\n" not in chunks and len(chunks) < 65536:
                    part = sock.recv(65536)
                    if not part:
                        break
                    chunks.extend(part)
            response = json.loads(bytes(chunks).partition(b"\n")[0])
            break
        except OSError as exc:
            if attempt == 2 or time.monotonic() >= deadline:
                raise RuntimeError(f"generation control unavailable: {type(exc).__name__}") from exc
            time.sleep(min(0.5, max(0, deadline - time.monotonic())))
        except ValueError as exc:
            raise RuntimeError(f"generation control unavailable: {type(exc).__name__}") from exc
    if not isinstance(response, dict) or response.get("ok") is not True or not isinstance(response.get("result"), dict):
        raise RuntimeError(f"generation control refused {verb}: {response.get('error') if isinstance(response, dict) else 'invalid response'}")
    return response["result"]


def handover_to_generation(home: Path, to_id: str, *, timeout: float = 45,
                           drain_seconds: float = DEFAULT_DRAIN_SECONDS) -> int:
    """Internal updater entry point; never ask the lease holder to relinquish by force."""
    if not 1 <= drain_seconds <= 86400:
        raise ValueError("drain_seconds must be between 1 and 86400")
    coordinator = GenerationCoordinator(home)
    lease = next((row for row in coordinator.leases() if row["resource"] == "active_generation"), None)
    if not lease or lease["state"] != "active":
        raise RuntimeError("no active generation lease to transfer")
    old_id, epoch = lease["generation_id"], lease["epoch"]
    identities = {row["id"]: row for row in coordinator.generations()}
    old, successor = identities.get(old_id), identities.get(to_id)
    if old is None or successor is None or successor["state"] != "ready":
        raise RuntimeError("successor is not ready or old generation is missing")
    old_identity = GenerationIdentity(**{key: old[key] for key in GenerationIdentity.__dataclass_fields__})
    path = generation_paths(home, old_identity)["socket"]
    roster = _generation_request(path, "polling_roster", timeout=timeout)
    tokens = roster.get("tokens")
    if not isinstance(tokens, list) or any(not isinstance(token, str) for token in tokens):
        raise RuntimeError("invalid old generation polling roster")
    coordinator.request_transfer(old_id, to_id, epoch, set(tokens))
    nonce = coordinator.transfer_attempt_nonce(old_id, epoch)
    try:
        ack = _generation_request(path, "transfer_requested", params={"to": to_id}, timeout=timeout)
        if (ack.get("generation_id"), ack.get("epoch"), ack.get("poller_stopped")) != (old_id, epoch, True):
            raise RuntimeError("old generation did not prove poller stopped")
        promoted = coordinator.commit_transfer(old_id, to_id, epoch, drain_seconds=drain_seconds)
    except Exception:
        # Never hide the transfer failure with a second failure during recovery.
        # Attempt both abort and re-arm even if either operation fails.
        try:
            coordinator.abort_transfer(old_id, to_id, epoch, attempt_nonce=nonce)
        except Exception:
            logger.exception("transfer abort failed after pre-commit failure")
        try:
            _generation_request(path, "transfer_aborted", params={"to": to_id, "nonce": nonce}, timeout=timeout)
        except Exception:
            logger.exception("poller re-arm failed after pre-commit failure")
        raise
    successor_identity = GenerationIdentity(**{key: successor[key] for key in GenerationIdentity.__dataclass_fields__})
    successor_socket = generation_paths(home, successor_identity)["socket"]
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            status = _generation_request(successor_socket, "polling_status", timeout=min(2, max(.1, deadline-time.monotonic())))
            if status.get("generation_id") == to_id and status.get("polling") is True and set(status.get("tokens", [])) == set(tokens):
                return promoted
        except RuntimeError:
            pass
        time.sleep(.2)
    raise HandoverCommittedUnverified(to_id, promoted)


async def start_active_generation(config) -> "ActiveGeneration | None":
    """Register an already singleton-claimed active gateway; never claim from standby."""
    if not overlap_handover_enabled(config):
        return None
    from gateway.status import _get_process_start_time
    home = Path(get_hermes_home())
    coordinator = GenerationCoordinator(home)
    started = _get_process_start_time(os.getpid())
    if started is None:
        raise RuntimeError("cannot determine process start time for generation identity")
    identity = GenerationIdentity.create(
        release_sha=os.environ.get("HERMES_RELEASE_SHA", "unknown"),
        label=os.environ.get("HERMES_LAUNCHD_LABEL", "ai.hermes.gateway"),
        start_fingerprint=f"{os.getpid()}:{started}",
    )
    coordinator.register(identity, state="serving")
    try:
        epoch = coordinator.acquire_lease("active_generation", identity.id)
    except Exception:
        coordinator.heartbeat(identity.id, state="failed")
        raise
    active = ActiveGeneration(home, coordinator, identity, epoch)
    try:
        await active.start()
        from gateway.status import set_generation_runtime_status
        set_generation_runtime_status(identity.id)
    except BaseException:
        await active.close()
        coordinator.heartbeat(identity.id, state="failed")
        raise
    return active


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
        self.owned_routing._task = asyncio.create_task(self.owned_routing.drain())

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
                "tokens": sorted(roster),
                "polling": self.runner is not None and all(
                    getattr(getattr(adapter, "_controlled_poller", None), "running", False)
                    and getattr(getattr(adapter, "_polling_progress_event", None), "is_set", lambda: False)()
                    for adapter in roster.values())}

    async def transfer_requested(self, new_id: str) -> dict:
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
            nonce = await asyncio.to_thread(self.coordinator.transfer_attempt_nonce,
                                            self.identity.id, self.epoch)
            self.runner._overlap_draining = True
            try:
                for token, adapter in roster.items():
                    receipt = await adapter.stop_polling_for_transfer()
                    if receipt.get("token_hash") != token:
                        raise RuntimeError("poller stop token mismatch")
                    stopped.append((adapter, receipt))
                    await asyncio.to_thread(self.coordinator.record_poller_stopped,
                                            self.identity.id, self.epoch, token, receipt["safe_offset"],
                                            attempt_nonce=nonce)
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
                self._pending_transfer = (new_id, nonce, time.monotonic() + HANDOVER_REQUEST_TIMEOUT)
                self._drain_task = asyncio.create_task(self._drain_after_transfer())
                return {"poller_stopped": True, "generation_id": self.identity.id,
                        "epoch": self.epoch, "tokens": len(stopped)}
            except Exception as original:
                # The driver cannot commit after abort; keep dispatch fenced if
                # recovery is incomplete, and do not mask the stop failure.
                self._stopped_receipts = stopped
                try:
                    aborted = await asyncio.to_thread(self.coordinator.abort_transfer,
                                                      self.identity.id, new_id, self.epoch,
                                                      attempt_nonce=nonce)
                except Exception:
                    message = "poller stop failed and transfer abort could not be proved"
                    self._rearm_errors = [message]
                    logger.exception("transfer abort failed after poller stop failure")
                    await asyncio.to_thread(self._sync_runtime_status)
                    raise RuntimeError(message) from original
                if not aborted:
                    message = "poller stop failed and transfer attempt changed"
                    self._rearm_errors = [message]
                    await asyncio.to_thread(self._sync_runtime_status)
                    raise RuntimeError(message) from original
                errors = await self._rearm_stopped_pollers()
                if errors:
                    logger.error("poller stop failed: %s; re-arm failed for %s",
                                 original, ", ".join(errors))
                    raise RuntimeError(f"poller stop failed; re-arm failed for {', '.join(errors)}") from original
                raise

    async def _rearm_stopped_pollers(self) -> list[str]:
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
            self._pending_transfer = None
            self.runner._overlap_draining = False
        return errors

    async def transfer_aborted(self, new_id: str, attempt_nonce: str | None = None) -> dict:
        """Re-arm only for the same aborted attempt under the old lease."""
        async with self._transfer_lock:
            lease = next((row for row in await asyncio.to_thread(self.coordinator.leases)
                          if row["resource"] == "active_generation"), None)
            if not lease or (lease["generation_id"], lease["epoch"], lease["state"]) != (
                    self.identity.id, self.epoch, "active"):
                raise RuntimeError("old generation no longer owns admission")
            if not getattr(self.runner, "_overlap_draining", False):
                return {"rearmed": True}
            def read_transfer():
                with contextlib.closing(self.coordinator.connect()) as conn:
                    return conn.execute(
                        "SELECT state,attempt_nonce FROM generation_transfers WHERE old_id=? AND new_id=? AND epoch=?",
                        (self.identity.id, new_id, self.epoch)).fetchone()

            transfer = await asyncio.to_thread(read_transfer)
            if transfer is None or transfer["state"] != "aborted" or (
                    attempt_nonce is not None and transfer["attempt_nonce"] != attempt_nonce):
                raise RuntimeError("transfer has not been aborted for this attempt")
            errors = await self._rearm_stopped_pollers()
            if errors:
                raise RuntimeError(f"re-arm failed for {', '.join(errors)}")
            return {"rearmed": True}

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
            if pending and time.monotonic() >= pending[2]:
                new_id, nonce, _ = pending
                async with self._transfer_lock:
                    # Hold the local stop/re-arm lock through recovery: a retry
                    # may write a fresh attempt immediately after the CAS, but
                    # cannot collect a new stop receipt before A is polling.
                    try:
                        aborted = await asyncio.to_thread(
                            self.coordinator.abort_transfer, self.identity.id, new_id,
                            self.epoch, attempt_nonce=nonce)
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
                                with contextlib.closing(self.coordinator.connect()) as conn:
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
                                await self._rearm_stopped_pollers()
                            except Exception:
                                logger.exception("transfer deadline recovery failed; old gateway remains fenced")
            try:
                if await self.finish_draining_once():
                    return
            except Exception:
                logger.warning("generation drain inspection failed; retaining old gateway", exc_info=True)
            await asyncio.sleep(1)

    async def start(self) -> None:
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
            future = asyncio.run_coroutine_threadsafe(self.transfer_requested(new_id), loop)
            # Control-socket handlers are rare, bounded operations; waiting here keeps the
            # synchronous socket protocol simple without occupying an event-loop thread.
            return future.result(timeout=45)

        def _abort_handler(params: dict) -> dict:
            new_id = params.get("to")
            if not isinstance(new_id, str):
                raise RuntimeError("successor generation ID required")
            future = asyncio.run_coroutine_threadsafe(self.transfer_aborted(new_id, params.get("nonce")), loop)
            return future.result(timeout=45)

        self.server = GenerationControlServer(
            self.home, self.paths["socket"],
            verb_handlers={"transfer_requested": _transfer_handler,
                           "transfer_aborted": _abort_handler,
                           "polling_roster": lambda: {"tokens": sorted(self._telegram_adapters())},
                           "polling_status": self.polling_status})
        if not await self.server.start():
            raise RuntimeError("generation control socket unavailable")
        self.socket_stat = self.paths["socket"].stat()
        write_generation_record(self.paths["state"], self.identity, state="serving", socket_path=self.paths["socket"])
        self.task = asyncio.create_task(self._heartbeat())

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
        now = time.monotonic()
        if runtime == self._last_runtime and now - self._last_status_write < 30:
            return
        write_generation_record(self.paths["state"], self.identity, state="serving",
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
                now = time.monotonic()
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
        if runtime.get("pid") == self.identity.pid:
            runtime_path.unlink(missing_ok=True)


async def serve_standby_generation(config=None) -> bool:
    """Report readiness without constructing a runner or connecting any adapter.

    No singleton status/PID/socket or token-scoped lock is touched. The next slice owns
    poller transfer, so this process is strictly passive throughout its lifetime.
    """
    if config is None:
        from gateway.config import load_gateway_config
        config = load_gateway_config()
    if not overlap_handover_enabled(config):
        raise RuntimeError("gateway run --standby requires gateway.overlap_handover.enabled")

    home = Path(get_hermes_home())
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
    paths = generation_paths(home, identity)

    async def report_ready(_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        import json
        writer.write((json.dumps({"generation_id": identity.id, "state": "ready",
                                 "release_sha": identity.release_sha}) + "\n").encode())
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    socket_path = paths["socket"]
    _ensure_generation_socket_parent(socket_path)
    if len(os.fsencode(socket_path)) >= 100:
        raise RuntimeError("generation control socket path exceeds UNIX socket limit")
    old_umask = os.umask(0o177)
    try:
        server = await asyncio.start_unix_server(report_ready, path=str(socket_path))
    finally:
        os.umask(old_umask)
    try:
        os.chmod(socket_path, 0o600)
        socket_stat = paths["socket"].stat()
    except BaseException:
        server.close()
        await server.wait_closed()
        socket_path.unlink(missing_ok=True)
        raise
    try:
        coordinator.register(identity)
        write_generation_record(paths["pid"], identity, state="standby")
        write_generation_record(paths["host"], identity, state="standby")
        write_generation_record(paths["state"], identity, state="ready", socket_path=socket_path)
        coordinator.heartbeat(identity.id, state="ready")
    except BaseException:
        server.close()
        await server.wait_closed()
        with suppress(FileNotFoundError):
            current = socket_path.stat()
            if (current.st_dev, current.st_ino) == (socket_stat.st_dev, socket_stat.st_ino):
                socket_path.unlink()
        if any(row["id"] == identity.id for row in coordinator.generations()):
            coordinator.heartbeat(identity.id, state="failed")
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
                    now = time.monotonic()
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
