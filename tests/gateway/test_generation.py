"""Contract tests for opt-in generation isolation primitives."""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import stat
import time
from pathlib import Path

import pytest

from gateway.generation import (
    GenerationCoordinator,
    GenerationIdentity,
    generation_paths,
    overlap_handover_enabled,
    remove_generation_files,
    write_generation_record,
)


def test_coordinator_registers_heartbeats_and_exposes_leased_generations(tmp_path: Path):
    coordinator = GenerationCoordinator(tmp_path)
    identity = GenerationIdentity.create(release_sha="abc123", label="ai.hermes.gateway-a", pid=123)

    coordinator.register(identity)
    epoch = coordinator.acquire_lease("telegram:token", identity.id)
    coordinator.heartbeat(identity.id, state="ready")

    rows = coordinator.generations()
    assert rows[0]["id"] == identity.id
    assert rows[0]["state"] == "ready"
    assert coordinator.leases() == [{
        "resource": "telegram:token", "epoch": epoch,
        "generation_id": identity.id, "state": "active",
    }]


def test_generation_files_are_scoped_and_cleanup_is_fenced(tmp_path: Path):
    identity = GenerationIdentity.create(release_sha="abc", label="ai.hermes.gateway-b", pid=456,
                                         start_fingerprint="456:one")
    paths = generation_paths(tmp_path, identity)
    write_generation_record(paths["state"], identity, state="standby")
    assert json.loads(paths["state"].read_text())["id"] == identity.id

    other = GenerationIdentity.create(release_sha="def", label="ai.hermes.gateway-a", pid=456,
                                      start_fingerprint="456:two")
    other_paths = generation_paths(tmp_path, other)
    write_generation_record(other_paths["state"], other)
    remove_generation_files(tmp_path, identity)
    assert not paths["state"].exists()
    assert other_paths["state"].exists()


def test_lease_cannot_be_stolen_and_release_is_fenced(tmp_path):
    coordinator = GenerationCoordinator(tmp_path)
    from gateway.status import _get_process_start_time
    first = GenerationIdentity.create(release_sha="a", label="ai.hermes.gateway-a",
                                      start_fingerprint=f"{os.getpid()}:{_get_process_start_time(os.getpid())}")
    second = GenerationIdentity.create(release_sha="b", label="ai.hermes.gateway-b")
    coordinator.register(first)
    coordinator.register(second)
    epoch = coordinator.acquire_lease("active_generation", first.id)
    with pytest.raises(RuntimeError, match="held by another"):
        coordinator.acquire_lease("active_generation", second.id)
    assert not coordinator.release_lease("active_generation", second.id, epoch)
    assert not coordinator.release_lease("active_generation", first.id, epoch + 1)
    assert coordinator.release_lease("active_generation", first.id, epoch)
    assert coordinator.acquire_lease("active_generation", second.id) > epoch


@pytest.mark.parametrize("death", ["missing_pid", "reused_pid", "different_boot"])
def test_dead_lease_holder_fails_and_new_generation_takes_higher_epoch(tmp_path, monkeypatch, death):
    from gateway import generation
    from gateway.status import _get_process_start_time
    coordinator = GenerationCoordinator(tmp_path)
    current_start = _get_process_start_time(os.getpid())
    assert current_start is not None
    first = GenerationIdentity.create(release_sha="a", label="active", pid=os.getpid(),
                                      start_fingerprint=f"{os.getpid()}:{current_start}",
                                      boot_id="old-boot" if death == "different_boot" else None)
    second = GenerationIdentity.create(release_sha="b", label="successor")
    coordinator.register(first)
    coordinator.register(second)
    epoch = coordinator.acquire_lease("active_generation", first.id)
    if death == "missing_pid":
        monkeypatch.setattr("gateway.status._pid_exists", lambda pid: False)
    elif death == "reused_pid":
        monkeypatch.setattr("gateway.status._get_process_start_time", lambda pid: current_start + 1)
    else:
        monkeypatch.setattr(generation, "_boot_id", lambda: "new-boot")
    assert coordinator.acquire_lease("active_generation", second.id) == epoch + 1
    assert coordinator.generations()[0]["state"] == "failed"
    assert not coordinator.release_lease("active_generation", first.id, epoch)


def test_stale_live_holder_becomes_suspect_without_losing_lease(tmp_path):
    from gateway.status import _get_process_start_time
    coordinator = GenerationCoordinator(tmp_path)
    first = GenerationIdentity.create(release_sha="a", label="active",
                                      start_fingerprint=f"{os.getpid()}:{_get_process_start_time(os.getpid())}")
    second = GenerationIdentity.create(release_sha="b", label="successor")
    coordinator.register(first)
    coordinator.register(second)
    epoch = coordinator.acquire_lease("active_generation", first.id)
    with coordinator.connect() as conn:
        conn.execute("UPDATE generations SET heartbeat_at=? WHERE id=?", (time.time() - 30, first.id))
    with pytest.raises(RuntimeError, match="suspect.*alive"):
        coordinator.acquire_lease("active_generation", second.id)
    assert coordinator.generations()[0]["state"] == "suspect"
    assert coordinator.leases()[0]["epoch"] == epoch


@pytest.mark.asyncio
async def test_scoped_control_start_failure_is_explicit(tmp_path, monkeypatch):
    from gateway.run_generation import ActiveGeneration, GenerationControlServer
    coordinator = GenerationCoordinator(tmp_path)
    identity = GenerationIdentity.create(release_sha="a", label="active")
    coordinator.register(identity)
    active = ActiveGeneration(tmp_path, coordinator, identity, 1)
    async def refused(self):
        return False
    monkeypatch.setattr(GenerationControlServer, "start", refused)
    with pytest.raises(RuntimeError, match="generation control socket unavailable"):
        await active.start()


@pytest.mark.asyncio
async def test_active_heartbeat_io_does_not_block_event_loop(tmp_path, monkeypatch):
    import threading
    from gateway.run_generation import ActiveGeneration
    coordinator = GenerationCoordinator(tmp_path)
    identity = GenerationIdentity.create(release_sha="a", label="active")
    active = ActiveGeneration(tmp_path, coordinator, identity, 1)
    entered, released = threading.Event(), threading.Event()
    def blocked():
        entered.set()
        released.wait(2)
    monkeypatch.setattr(active, "_sync_runtime_status", blocked)
    task = asyncio.create_task(active._heartbeat())
    try:
        start = time.monotonic()
        assert await asyncio.to_thread(entered.wait, 2), "heartbeat never reached I/O"
        assert time.monotonic() - start < 1.7, "heartbeat blocked the event loop"
    finally:
        released.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.windows_only
def test_overlap_flag_rejected_on_windows():
    from gateway.config import GatewayConfig
    with pytest.raises(ValueError, match="overlap_handover.*Windows"):
        GatewayConfig.from_dict({"gateway": {"overlap_handover": {"enabled": True}}})


@pytest.mark.asyncio
async def test_disabled_standby_creates_no_coordinator_or_legacy_files(tmp_path, monkeypatch):
    from gateway.config import GatewayConfig
    from gateway.run_generation import serve_standby_generation
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    with pytest.raises(RuntimeError, match="requires gateway.overlap_handover.enabled"):
        await serve_standby_generation(GatewayConfig())
    assert not (tmp_path / "gateway-coordinator.db").exists()
    assert not (tmp_path / "gateway.pid").exists()
    assert not (tmp_path / "gateway_state.json").exists()


def test_overlap_gate_defaults_off_and_reads_nested_config(tmp_path, monkeypatch):
    from gateway.config import load_gateway_config
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    assert not load_gateway_config().overlap_handover_enabled
    assert not overlap_handover_enabled({})
    assert not overlap_handover_enabled({"gateway": None})
    (tmp_path / "config.yaml").write_text("gateway:\n  overlap_handover:\n    enabled: true\n")
    assert load_gateway_config().overlap_handover_enabled
    assert overlap_handover_enabled({"gateway": {"overlap_handover": {"enabled": True}}})
    assert not overlap_handover_enabled(object())
    assert not overlap_handover_enabled({"gateway": {"overlap_handover": None}})


def test_terminal_generations_are_bounded_and_status_is_compact(tmp_path):
    from hermes_cli.gateway_generation_status import read_generation_status
    coordinator = GenerationCoordinator(tmp_path)
    active = GenerationIdentity.create(release_sha="active", label="slot-a")
    coordinator.register(active, state="ready")
    old = GenerationIdentity.create(release_sha="old", label="slot-b",
                                    started_at=time.time() - 9 * 86400)
    coordinator.register(old, state="failed")
    for n in range(50):
        identity = GenerationIdentity.create(release_sha=str(n), label="slot-a" if n % 2 else "slot-b")
        coordinator.register(identity, state="exited" if n % 2 else "failed")
    rows = coordinator.generations()
    assert active.id in {row["id"] for row in rows}
    assert old.id not in {row["id"] for row in rows}
    assert len([row for row in rows if row["state"] in {"exited", "failed"}]) <= 20
    visible = read_generation_status(tmp_path)
    assert {row["label"] for row in visible} == {"slot-a", "slot-b"}
    assert len(visible) == 3  # live active plus the latest terminal per label


def test_terminal_prune_removes_released_lease_and_upgrades_existing_schema(tmp_path):
    coordinator = GenerationCoordinator(tmp_path)
    old = GenerationIdentity.create(release_sha="old", label="slot-a",
                                    started_at=time.time() - 9 * 86400)
    coordinator.register(old, state="ready")
    epoch = coordinator.acquire_lease("old-slot", old.id)
    coordinator.release_lease("old-slot", old.id, epoch)
    coordinator.heartbeat(old.id, state="exited")
    with coordinator.connect() as conn:
        conn.execute("ALTER TABLE generations DROP COLUMN suspect_from_state")
    coordinator = GenerationCoordinator(tmp_path)
    coordinator.register(GenerationIdentity.create(release_sha="new", label="slot-b"))
    assert old.id not in {row["id"] for row in coordinator.generations()}
    assert coordinator.leases() == []


def test_suspect_heartbeat_restores_prior_live_state(tmp_path):
    from gateway.status import _get_process_start_time
    coordinator = GenerationCoordinator(tmp_path)
    contender = GenerationIdentity.create(release_sha="next", label="next")
    coordinator.register(contender)
    for initial in ("ready", "starting"):
        holder = GenerationIdentity.create(release_sha="held", label=initial,
            start_fingerprint=f"{os.getpid()}:{_get_process_start_time(os.getpid())}")
        coordinator.register(holder, state=initial)
        epoch = coordinator.acquire_lease(f"resource-{initial}", holder.id)
        with coordinator.connect() as conn:
            conn.execute("UPDATE generations SET heartbeat_at=? WHERE id=?", (time.time() - 30, holder.id))
        with pytest.raises(RuntimeError, match="suspect"):
            coordinator.acquire_lease(f"resource-{initial}", contender.id)
        coordinator.heartbeat(holder.id)
        assert next(row for row in coordinator.generations() if row["id"] == holder.id)["state"] == initial
        assert coordinator.leases()[-1]["epoch"] == epoch


@pytest.mark.asyncio
async def test_active_heartbeat_recovers_after_one_io_failure(tmp_path, monkeypatch):
    import threading
    from gateway.run_generation import ActiveGeneration
    coordinator = GenerationCoordinator(tmp_path)
    identity = GenerationIdentity.create(release_sha="a", label="active")
    coordinator.register(identity)
    active = ActiveGeneration(tmp_path, coordinator, identity, 1)
    monkeypatch.setattr(active, "_sync_runtime_status", lambda: None)
    original = coordinator.heartbeat
    attempts = 0
    recovered = threading.Event()
    def flaky(*args, **kwargs):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise sqlite3.OperationalError("transient busy")
        original(*args, **kwargs)
        recovered.set()
    monkeypatch.setattr(coordinator, "heartbeat", flaky)
    task = asyncio.create_task(active._heartbeat())
    try:
        assert await asyncio.to_thread(recovered.wait, 4)
        assert attempts >= 2 and not task.done()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_standby_heartbeat_recovers_and_socket_is_private(tmp_path, monkeypatch):
    import threading
    from gateway.run_generation import serve_standby_generation
    from gateway.config import GatewayConfig
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    original = GenerationCoordinator.heartbeat
    attempts = 0
    recovered = threading.Event()
    def flaky(self, generation_id, *, state=None):
        nonlocal attempts
        if state is None:
            attempts += 1
            if attempts == 1:
                raise OSError("transient I/O")
            recovered.set()
        return original(self, generation_id, state=state)
    monkeypatch.setattr(GenerationCoordinator, "heartbeat", flaky)
    config = GatewayConfig.from_dict({"gateway": {"overlap_handover": {"enabled": True}}})
    task = asyncio.create_task(serve_standby_generation(config))
    try:
        deadline = time.monotonic() + 3
        while not list(tmp_path.glob("gateway_state.*.json")) and time.monotonic() < deadline:
            await asyncio.sleep(.02)
        records = list(tmp_path.glob("gateway_state.*.json"))
        assert records
        socket = Path(json.loads(records[0].read_text())["socket_path"])
        assert stat.S_IMODE(socket.stat().st_mode) == 0o600
        assert await asyncio.to_thread(recovered.wait, 4)
        assert attempts >= 2 and not task.done()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


def test_runtime_status_refreshes_only_when_changed_or_stale(tmp_path, monkeypatch):
    from gateway.run_generation import ActiveGeneration
    from gateway.status import read_runtime_status
    coordinator = GenerationCoordinator(tmp_path)
    identity = GenerationIdentity.create(release_sha="a", label="active")
    active = ActiveGeneration(tmp_path, coordinator, identity, 1)
    monkeypatch.setattr("gateway.status.read_runtime_status", lambda path: {"pid": identity.pid, "marker": 1})
    active._sync_runtime_status()
    first = active.paths["state"].stat().st_mtime_ns
    active._sync_runtime_status()
    assert active.paths["state"].stat().st_mtime_ns == first
    assert read_runtime_status(active.paths["state"])["marker"] == 1
    active._last_status_write -= 31
    active._sync_runtime_status()
    assert active.paths["state"].stat().st_mtime_ns != first
