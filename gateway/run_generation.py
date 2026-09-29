"""Passive standby generation lifecycle for opt-in overlap handover."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import json
import socket
import os
import stat
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


def _generation_request(path: Path, verb: str, *, params: dict | None = None,
                        timeout: float = 30) -> dict:
    request = json.dumps({"protocol": 1, "verb": verb, "params": params or {}}).encode() + b"\n"
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(str(path))
            sock.sendall(request)
            chunks = bytearray()
            while b"\n" not in chunks and len(chunks) < 65536:
                part = sock.recv(65536)
                if not part:
                    break
                chunks.extend(part)
        response = json.loads(bytes(chunks).partition(b"\n")[0])
    except (OSError, ValueError) as exc:
        raise RuntimeError(f"generation control unavailable: {type(exc).__name__}") from exc
    if not isinstance(response, dict) or response.get("ok") is not True or not isinstance(response.get("result"), dict):
        raise RuntimeError(f"generation control refused {verb}: {response.get('error') if isinstance(response, dict) else 'invalid response'}")
    return response["result"]


def handover_to_generation(home: Path, to_id: str, *, timeout: float = 45,
                           drain_seconds: float = 7200) -> int:
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
    ack = _generation_request(path, "transfer_requested", params={"to": to_id}, timeout=timeout)
    if (ack.get("generation_id"), ack.get("epoch"), ack.get("poller_stopped")) != (old_id, epoch, True):
        raise RuntimeError("old generation did not prove poller stopped")
    promoted = coordinator.commit_transfer(old_id, to_id, epoch, drain_seconds=drain_seconds)
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
    raise RuntimeError("successor holds lease but has not proved polling progress")


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
    release_sha = os.environ.get("HERMES_RELEASE_SHA")
    if release_sha is None:
        # First overlap activation starts from the already-running S2 legacy
        # label. It has no generation-specific environment but is release-pinned.
        from hermes_cli.immutable_releases import ReleasePaths
        current = ReleasePaths.for_home(home).current.resolve()
        if current != Path.cwd().resolve() or len(current.name) != 40:
            raise RuntimeError("legacy generation is not pinned to the current release")
        release_sha = current.name
    identity = GenerationIdentity.create(
        release_sha=release_sha,
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


def _ensure_generation_socket_parent(socket_path: Path) -> None:
    parent = socket_path.parent
    if parent.parent == Path(os.path.sep, "tmp") and parent.name.startswith("hg-"):
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
        with contextlib.suppress(OSError):
            if bind_path.exists():
                bind_path.unlink()
        old_umask = os.umask(0o177)
        try:
            self._server = await asyncio.start_unix_server(self._handle_connection, path=str(bind_path))
        finally:
            os.umask(old_umask)
        os.chmod(bind_path, 0o600)
        self._bind_path = bind_path
        return True

    async def stop(self) -> None:
        # The base cleanup unlinks unconditionally. Retain the bind path for an
        # inode-fenced unlink by ActiveGeneration.close instead.
        self._bind_path = None
        await super().stop()

    async def _start_windows(self) -> bool:
        return False


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
        self._transfer_receipts: dict[str, dict] = {}
        self._poller_paused = False

    def bind_runner(self, runner, *, cron_stop=None, cron_provider=None) -> None:
        self.runner = runner
        self.cron_stop = cron_stop
        self.cron_provider = cron_provider

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
            stopped = []
            try:
                for token, adapter in roster.items():
                    receipt = await adapter.stop_polling_for_transfer()
                    if receipt.get("token_hash") != token:
                        raise RuntimeError("poller stop token mismatch")
                    stopped.append((adapter, receipt))
                    await asyncio.to_thread(self.coordinator.record_poller_stopped,
                                            self.identity.id, self.epoch, token, receipt["safe_offset"])
                # No new autonomous dispatch from A. A's existing turns and egress stay alive.
                # Keep the ticker thread alive but fence dispatch: a guarded
                # rollback can restore it without reconstructing housekeeping.
                self.runner._overlap_draining = True
                self._transfer_receipts = {receipt["token_hash"]: receipt for _adapter, receipt in stopped}
                self._poller_paused = True
                self._drain_task = asyncio.create_task(self._drain_after_transfer())
                return {"poller_stopped": True, "generation_id": self.identity.id,
                        "epoch": self.epoch, "tokens": len(stopped)}
            except Exception:
                # A still holds the lease. Invalidate every partial receipt before
                # attempting to re-arm: the driver cannot commit while rollback runs.
                await asyncio.to_thread(self.coordinator.abort_transfer,
                                        self.identity.id, new_id, self.epoch)
                for adapter, receipt in stopped:
                    await adapter.start_polling_from_transfer(receipt)
                raise

    async def resume_uncommitted_transfer(self, epoch: int) -> dict:
        """After a failed transfer, A may rearm only while it still owns the lease."""
        async with self._transfer_lock:
            lease = next((row for row in self.coordinator.leases()
                          if row["resource"] == "active_generation"), None)
            if not lease or (lease["generation_id"], lease["epoch"], lease["state"]) != (
                    self.identity.id, epoch, "active"):
                raise RuntimeError("cannot rearm an unowned generation")
            if self._poller_paused:
                if set(self._transfer_receipts) != set(self._telegram_adapters()):
                    raise RuntimeError("incomplete old poller receipts")
                for token, adapter in self._telegram_adapters().items():
                    await adapter.start_polling_from_transfer(self._transfer_receipts[token])
                self._transfer_receipts.clear()
                self._poller_paused = False
                self.runner._overlap_draining = False
                if self._drain_task:
                    self._drain_task.cancel()
                    with suppress(asyncio.CancelledError):
                        await self._drain_task
                    self._drain_task = None
            return {"generation_id": self.identity.id, "epoch": epoch, "polling": True}

    async def stop_for_rollback(self) -> dict:
        """Stop successor's wire before any old-generation lease restoration."""
        async with self._transfer_lock:
            receipts = {}
            for token, adapter in self._telegram_adapters().items():
                receipt = await adapter.stop_polling_for_transfer()
                if receipt.get("token_hash") != token:
                    raise RuntimeError("rollback poller stop token mismatch")
                receipts[token] = receipt
            self.runner._overlap_draining = True
            self._poller_paused = True
            if self._drain_task is None:
                self._drain_task = asyncio.create_task(self._drain_after_transfer())
            return {"poller_stopped": True, "generation_id": self.identity.id,
                    "epoch": self.epoch, "tokens": len(receipts)}

    async def restore_after_rollback(self, epoch: int) -> dict:
        """Rearm A only after the coordinator installed a fresh lease epoch."""
        async with self._transfer_lock:
            lease = next((row for row in self.coordinator.leases()
                          if row["resource"] == "active_generation"), None)
            if not lease or (lease["generation_id"], lease["epoch"], lease["state"]) != (
                    self.identity.id, epoch, "active"):
                raise RuntimeError("rollback restoration requires owned active lease")
            if not self._poller_paused or set(self._transfer_receipts) != set(self._telegram_adapters()):
                raise RuntimeError("rollback has no complete stopped-poller receipts")
            if self._drain_task:
                self._drain_task.cancel()
                with suppress(asyncio.CancelledError):
                    await self._drain_task
                self._drain_task = None
            self.epoch = epoch
            for token, adapter in self._telegram_adapters().items():
                await adapter.start_polling_from_transfer(self._transfer_receipts[token])
            self.runner._overlap_draining = False
            self._transfer_receipts.clear()
            self._poller_paused = False
            await asyncio.to_thread(self.coordinator.heartbeat, self.identity.id, state="serving")
            return {"generation_id": self.identity.id, "epoch": epoch, "polling": True}

    async def finish_draining_once(self) -> bool:
        """Stop A only after B owns the lease and all locally owned work has settled."""
        if self.runner is None or not getattr(self.runner, "_overlap_draining", False):
            return False
        rows = self.coordinator.generations()
        record = next((row for row in rows if row["id"] == self.identity.id), None)
        if record is None or record["state"] != "draining":
            return False
        from tools.process_registry import process_registry
        busy = (self.runner._active_work_count() or
                bool(self.runner._pending_approvals) or
                process_registry.has_any_active() or process_registry.pending_watchers)
        with contextlib.closing(self.coordinator.connect()) as conn:
            queued = conn.execute("SELECT 1 FROM inbox WHERE owner_id=? AND state='pending' LIMIT 1",
                                  (self.identity.id,)).fetchone()
        deadline = record["drain_deadline"]
        if (busy or queued) and time.time() < deadline:
            return False
        if not self._drain_stopping:
            if time.time() >= deadline:
                count = await asyncio.to_thread(self.coordinator.interrupt_at_drain_cap, self.identity.id)
                logger.warning("generation drain cap reached; fenced %s interrupted session(s)", count)
            self._drain_stopping = True
            await self.runner.stop()
        return True

    async def _drain_after_transfer(self) -> None:
        while True:
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
            return future.result(timeout=45)

        def _rollback_handler(verb: str, params: dict) -> dict:
            epoch = params.get("epoch")
            if verb != "stop_for_rollback" and type(epoch) is not int:
                raise RuntimeError("rollback lease epoch required")
            operations = {"restore_after_rollback": lambda: self.restore_after_rollback(epoch),
                          "resume_uncommitted_transfer": lambda: self.resume_uncommitted_transfer(epoch),
                          "stop_for_rollback": self.stop_for_rollback}
            return asyncio.run_coroutine_threadsafe(operations[verb](), loop).result(timeout=45)

        self.server = GenerationControlServer(
            self.home, self.paths["socket"],
            verb_handlers={"transfer_requested": _transfer_handler,
                           "polling_roster": lambda: {"tokens": sorted(self._telegram_adapters())},
                           "polling_status": self.polling_status,
                           "stop_for_rollback": lambda params: _rollback_handler("stop_for_rollback", params),
                           "restore_after_rollback": lambda params: _rollback_handler("restore_after_rollback", params),
                           "resume_uncommitted_transfer": lambda params: _rollback_handler("resume_uncommitted_transfer", params)})
        if not await self.server.start():
            raise RuntimeError("generation control socket unavailable")
        self.socket_stat = self.paths["socket"].stat()
        write_generation_record(self.paths["state"], self.identity, state="serving", socket_path=self.paths["socket"])
        self.task = asyncio.create_task(self._heartbeat())

    async def mark_ready(self) -> None:
        await asyncio.to_thread(self._sync_runtime_status)
        await asyncio.to_thread(self.coordinator.heartbeat, self.identity.id, state="ready")

    def _sync_runtime_status(self) -> None:
        from gateway.status import read_runtime_status
        runtime = read_runtime_status(self.paths["state"]) or {}
        # The singleton PID and lease both belong to this process before it may
        # project the compatibility status to the generation-scoped record.
        if runtime.get("pid") != self.identity.pid:
            runtime = {}
        now = time.monotonic()
        if runtime == self._last_runtime and now - self._last_status_write < 30:
            return
        write_generation_record(self.paths["state"], self.identity, state="ready",
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
        _remove_empty_generation_socket_parent(self.paths["socket"])
        await asyncio.to_thread(
            self.coordinator.release_lease, "active_generation", self.identity.id, self.epoch)
        await asyncio.to_thread(self.coordinator.heartbeat, self.identity.id, state="exited")
        await asyncio.to_thread(remove_generation_files, self.home, self.identity)


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
