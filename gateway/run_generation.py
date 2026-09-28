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
