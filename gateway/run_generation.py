"""Passive standby generation lifecycle for opt-in overlap handover."""
from __future__ import annotations

import asyncio
import os
import signal
from contextlib import suppress
from pathlib import Path

from hermes_constants import get_hermes_home

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


class ActiveGeneration:
    """Generation-scoped identity and heartbeat alongside the existing active dispatcher."""

    def __init__(self, home: Path, coordinator: GenerationCoordinator,
                 identity: GenerationIdentity, epoch: int):
        self.home, self.coordinator, self.identity, self.epoch = home, coordinator, identity, epoch
        self.paths = generation_paths(home, identity)
        self.server: asyncio.AbstractServer | None = None
        self.task: asyncio.Task | None = None
        self.socket_stat = None

    async def start(self) -> None:
        import json
        for name in ("pid", "host"):
            write_generation_record(self.paths[name], self.identity, state="serving")
        socket_path = self.paths["socket"]
        if len(os.fsencode(socket_path)) >= 100:
            raise RuntimeError("generation control socket path exceeds UNIX socket limit")

        async def identify(_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            writer.write((json.dumps({"generation_id": self.identity.id, "state": "ready",
                                     "release_sha": self.identity.release_sha,
                                     "lease_epoch": self.epoch}) + "\n").encode())
            await writer.drain()
            writer.close()
            await writer.wait_closed()

        self.server = await asyncio.start_unix_server(identify, path=str(socket_path))
        self.socket_stat = socket_path.stat()
        write_generation_record(self.paths["state"], self.identity, state="serving", socket_path=socket_path)
        self.task = asyncio.create_task(self._heartbeat())

    def mark_ready(self) -> None:
        write_generation_record(self.paths["state"], self.identity, state="ready",
                                socket_path=self.paths["socket"])
        self.coordinator.heartbeat(self.identity.id, state="ready")

    async def _heartbeat(self) -> None:
        while True:
            await asyncio.sleep(1)
            self.coordinator.heartbeat(self.identity.id)

    async def close(self) -> None:
        if self.task:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
        if self.server:
            self.server.close()
            await self.server.wait_closed()
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
            loop.add_signal_handler(sig, stop.set)
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
