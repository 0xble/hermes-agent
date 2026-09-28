"""Passive standby generation lifecycle for opt-in overlap handover."""
from __future__ import annotations

import asyncio
import contextlib
import os
import signal
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


async def start_active_generation(config) -> "ActiveGeneration | None":
    """Register an already singleton-claimed active gateway; never claim from standby."""
    if not overlap_handover_enabled(config):
        return None
    from gateway.status import _get_process_start_time
    home = Path(get_hermes_home())
    coordinator = GenerationCoordinator(home)
    started = _get_process_start_time(os.getpid())
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
    except BaseException:
        await active.close()
        coordinator.heartbeat(identity.id, state="failed")
        raise
    return active


class GenerationControlServer(GatewayControlServer):
    """Generation-scoped control endpoint that never touches legacy paths."""

    def __init__(self, home: Path, socket_path: Path) -> None:
        super().__init__(home)
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

    async def start(self) -> None:
        for name in ("pid", "host"):
            write_generation_record(self.paths[name], self.identity, state="serving")
        socket_path = self.paths["socket"]
        if len(os.fsencode(socket_path)) >= 100:
            raise RuntimeError("generation control socket path exceeds UNIX socket limit")
        self.server = GenerationControlServer(self.home, self.paths["socket"])
        await self.server.start()
        self.socket_stat = self.paths["socket"].stat()
        write_generation_record(self.paths["state"], self.identity, state="serving", socket_path=self.paths["socket"])
        self.task = asyncio.create_task(self._heartbeat())

    def mark_ready(self) -> None:
        self._sync_runtime_status()
        self.coordinator.heartbeat(self.identity.id, state="ready")

    def _sync_runtime_status(self) -> None:
        from gateway.status import read_runtime_status
        runtime = read_runtime_status(self.home / "gateway_state.json") or {}
        # The singleton PID and lease both belong to this process before it may
        # project the compatibility status to the generation-scoped record.
        if runtime.get("pid") != self.identity.pid:
            runtime = {}
        write_generation_record(self.paths["state"], self.identity, state="ready",
                                socket_path=self.paths["socket"], runtime=runtime)

    async def _heartbeat(self) -> None:
        while True:
            await asyncio.sleep(1)
            self._sync_runtime_status()
            self.coordinator.heartbeat(self.identity.id)

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
        self.coordinator.release_lease("active_generation", self.identity.id, self.epoch)
        self.coordinator.heartbeat(self.identity.id, state="exited")
        remove_generation_files(self.home, self.identity)


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
    identity = GenerationIdentity.create(
        release_sha=release_sha, label=label,
        start_fingerprint=f"{os.getpid()}:{started}",
    )
    coordinator.register(identity)
    paths = generation_paths(home, identity)
    write_generation_record(paths["pid"], identity, state="standby")
    write_generation_record(paths["host"], identity, state="standby")

    async def report_ready(_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        import json
        writer.write((json.dumps({"generation_id": identity.id, "state": "ready",
                                 "release_sha": identity.release_sha}) + "\n").encode())
        await writer.drain()
        writer.close()
        await writer.wait_closed()

    socket_path = paths["socket"]
    if len(os.fsencode(socket_path)) >= 100:
        raise RuntimeError("generation control socket path exceeds UNIX socket limit")
    server = await asyncio.start_unix_server(report_ready, path=str(socket_path))
    socket_stat = paths["socket"].stat()
    write_generation_record(paths["state"], identity, state="ready", socket_path=socket_path)
    coordinator.heartbeat(identity.id, state="ready")
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    installed: list[signal.Signals] = []
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)  # windows-footgun: ok — caught below
        except (NotImplementedError, RuntimeError):
            continue
        installed.append(sig)
    try:
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=1.0)
            except asyncio.TimeoutError:
                coordinator.heartbeat(identity.id)
    finally:
        server.close()
        await server.wait_closed()
        try:
            current = paths["socket"].stat()
            if (current.st_dev, current.st_ino) == (socket_stat.st_dev, socket_stat.st_ino):
                paths["socket"].unlink()
        except FileNotFoundError:
            pass
        for sig in installed:
            with suppress(Exception):
                loop.remove_signal_handler(sig)
        coordinator.heartbeat(identity.id, state="exited")
        remove_generation_files(home, identity)
    return True
