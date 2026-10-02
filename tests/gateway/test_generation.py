"""Contract tests for opt-in generation isolation primitives."""
from __future__ import annotations

import asyncio
import json
import os
import socket
import sqlite3
import stat
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from unittest.mock import MagicMock
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


def test_concurrent_generation_record_writers_do_not_share_temporary_path(tmp_path):
    identity = GenerationIdentity.create(release_sha="abc", label="ai.hermes.gateway")
    record = tmp_path / "gateway_state.json"
    barrier = Barrier(2)

    def write_many():
        barrier.wait()
        for _ in range(500):
            write_generation_record(record, identity, state="serving")

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(write_many)
        second = pool.submit(write_many)
        first.result()
        second.result()
    assert json.loads(record.read_text(encoding="utf-8"))["id"] == identity.id
    assert not list(tmp_path.glob("*.tmp"))


def test_long_temp_root_still_produces_usable_unix_control_socket(tmp_path, monkeypatch):
    home = tmp_path / ("h" * 100)
    identity = GenerationIdentity.create(release_sha="a", label="a")
    original = generation_paths(home, identity)["socket"]
    monkeypatch.setenv("TMPDIR", str(tmp_path / ("t" * 100)))
    assert generation_paths(home, identity)["socket"] == original
    assert len(os.fsencode(original)) < 100


def test_long_temp_root_creates_private_control_directory(tmp_path):
    from gateway.run_generation import _ensure_generation_socket_parent
    home = tmp_path / ("h" * 100)
    identity = GenerationIdentity.create(release_sha="a", label="a")
    path = generation_paths(home, identity)["socket"]
    _ensure_generation_socket_parent(path)
    assert path.parent.is_dir()
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700


def test_generation_socket_root_cleanup_removes_owned_stale_siblings(tmp_path):
    from gateway.run_generation import _ensure_generation_socket_parent
    home = tmp_path / ("h" * 100)
    identity = GenerationIdentity.create(release_sha="a", label="a")
    path = generation_paths(home, identity)["socket"]
    stale = path.parent.parent / f"{path.parent.name}-stale"
    stale.mkdir(mode=0o700)
    stale_socket = stale / "old.sock"
    stale_socket.write_text("stale", encoding="utf-8")
    stale_socket.with_name(f".{stale_socket.name}.owner.json").write_text(
        json.dumps({"pid": 999999999, "start_time": 1}), encoding="utf-8")
    old = time.time() - 11 * 60
    os.utime(stale, (old, old))
    _ensure_generation_socket_parent(path)
    assert not stale.exists()


def test_generation_socket_root_cleanup_preserves_live_siblings(tmp_path):
    from gateway.run_generation import _ensure_generation_socket_parent
    home = tmp_path / ("h" * 100)
    identity = GenerationIdentity.create(release_sha="a", label="a")
    path = generation_paths(home, identity)["socket"]
    stale = path.parent.parent / f"{path.parent.name}-live"
    stale.mkdir(mode=0o700)
    live_socket = stale / "live.sock"
    server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    server.bind(str(live_socket))
    server.listen(1)
    old = time.time() - 11 * 60
    os.utime(stale, (old, old))
    try:
        _ensure_generation_socket_parent(path)
        assert stale.exists()
        assert live_socket.exists()
    finally:
        server.close()


@pytest.mark.platforms("macos")
def test_macos_boot_id_does_not_change_when_hostname_changes(monkeypatch):
    from gateway import generation
    import platform

    monkeypatch.setattr(platform, "node", lambda: "first-host")
    result = MagicMock(stdout="boot-session\n")
    calls = []
    def sysctl(*args, **kwargs):
        calls.append(args)
        return result
    monkeypatch.setattr(generation.subprocess, "run", sysctl)
    generation._boot_id.cache_clear()
    try:
        first = generation._boot_id()
        monkeypatch.setattr(platform, "node", lambda: "second-host")
        assert generation._boot_id() == first == "boot-session"
        assert len(calls) == 1
    finally:
        generation._boot_id.cache_clear()


@pytest.mark.platforms("macos")
def test_macos_boot_id_fallback_is_host_independent(monkeypatch):
    import platform
    import psutil
    from gateway import generation

    monkeypatch.setattr(generation.subprocess, "run", lambda *a, **kw: MagicMock(stdout=""))
    monkeypatch.setattr(psutil, "boot_time", lambda: 123456.9)
    generation._boot_id.cache_clear()
    try:
        monkeypatch.setattr(platform, "node", lambda: "first-host")
        first = generation._boot_id()
        monkeypatch.setattr(platform, "node", lambda: "second-host")
        assert generation._boot_id() == first == "darwin:123456"
    finally:
        generation._boot_id.cache_clear()


def test_coordinator_closes_connections_after_heartbeat(tmp_path, monkeypatch):
    coordinator = GenerationCoordinator(tmp_path)
    connection = MagicMock()
    monkeypatch.setattr(coordinator, "connect", lambda: connection)

    coordinator.heartbeat("generation-id")

    connection.close.assert_called_once_with()


def test_two_hundred_heartbeats_do_not_leak_descriptors(tmp_path):
    import psutil

    coordinator = GenerationCoordinator(tmp_path)
    identity = GenerationIdentity.create(release_sha="a", label="active")
    coordinator.register(identity)
    before = psutil.Process().num_fds()
    for _ in range(200):
        coordinator.heartbeat(identity.id)
    assert psutil.Process().num_fds() <= before + 2


@pytest.mark.platforms("macos")
def test_live_lease_is_not_stolen_after_hostname_change(tmp_path, monkeypatch):
    import platform
    from gateway import generation
    from gateway.status import _get_process_start_time

    generation._boot_id.cache_clear()
    monkeypatch.setattr(platform, "node", lambda: "first-host")
    try:
        coordinator = GenerationCoordinator(tmp_path)
        holder = GenerationIdentity.create(
            release_sha="a", label="active",
            start_fingerprint=f"{os.getpid()}:{_get_process_start_time(os.getpid())}")
        contender = GenerationIdentity.create(release_sha="b", label="standby")
        coordinator.register(holder)
        coordinator.register(contender)
        epoch = coordinator.acquire_lease("active_generation", holder.id)
        monkeypatch.setattr(platform, "node", lambda: "second-host")
        with pytest.raises(RuntimeError, match="held by another"):
            coordinator.acquire_lease("active_generation", contender.id)
        assert coordinator.leases()[0]["epoch"] == epoch
        assert next(row for row in coordinator.generations() if row["id"] == holder.id)["verdict"] is None
    finally:
        generation._boot_id.cache_clear()


@pytest.mark.asyncio
async def test_standby_bind_failure_does_not_register_generation(tmp_path, monkeypatch):
    from gateway.config import GatewayConfig
    from gateway import run_generation

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    async def bind_failure(*args, **kwargs):
        raise OSError("bind failed")
    monkeypatch.setattr(run_generation.asyncio, "start_unix_server", bind_failure)
    config = GatewayConfig.from_dict({"gateway": {"overlap_handover": {"enabled": True}}})

    with pytest.raises(OSError, match="bind failed"):
        await run_generation.serve_standby_generation(config)

    row = GenerationCoordinator(tmp_path).generations()[0]
    assert (row["state"], row["verdict"]) == ("exited", "failed")


@pytest.mark.asyncio
async def test_standby_chmod_failure_closes_socket_without_registration(tmp_path, monkeypatch):
    from gateway.config import GatewayConfig
    from gateway import run_generation

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    def chmod_failure(*args, **kwargs):
        raise OSError("chmod failed")
    monkeypatch.setattr(run_generation.os, "chmod", chmod_failure)
    config = GatewayConfig.from_dict({"gateway": {"overlap_handover": {"enabled": True}}})
    with pytest.raises(OSError, match="chmod failed"):
        await run_generation.serve_standby_generation(config)
    row = GenerationCoordinator(tmp_path).generations()[0]
    assert (row["state"], row["verdict"]) == ("exited", "failed")
    assert not list(tmp_path.glob("gateway.*.sock"))


@pytest.mark.asyncio
async def test_standby_record_failure_is_terminal(tmp_path, monkeypatch):
    from gateway.config import GatewayConfig
    from gateway import run_generation

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    def record_failure(*args, **kwargs):
        raise OSError("record failed")
    monkeypatch.setattr(run_generation, "write_generation_record", record_failure)
    config = GatewayConfig.from_dict({"gateway": {"overlap_handover": {"enabled": True}}})
    with pytest.raises(OSError, match="record failed"):
        await run_generation.serve_standby_generation(config)
    assert GenerationCoordinator(tmp_path).generations()[0]["verdict"] == "failed"
    assert not list(tmp_path.glob("gateway.*.sock"))


@pytest.mark.asyncio
async def test_missing_process_start_time_fails_before_registration(tmp_path, monkeypatch):
    from gateway.config import GatewayConfig
    from gateway import run_generation

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("gateway.status._get_process_start_time", lambda pid: None)
    config = GatewayConfig.from_dict({"gateway": {"overlap_handover": {"enabled": True}}})

    with pytest.raises(RuntimeError, match="cannot determine process start time"):
        await run_generation.start_active_generation(config)
    with pytest.raises(RuntimeError, match="cannot determine process start time"):
        await run_generation.serve_standby_generation(config)

    assert GenerationCoordinator(tmp_path).generations() == []


def test_coordinator_registers_heartbeats_and_exposes_leased_generations(tmp_path: Path):
    coordinator = GenerationCoordinator(tmp_path)
    identity = GenerationIdentity.create(release_sha="abc123", label="ai.hermes.gateway-a", pid=123)

    coordinator.register(identity)
    epoch = coordinator.acquire_lease("telegram:token", identity.id)
    coordinator.heartbeat(identity.id, state="standby")

    rows = coordinator.generations()
    assert rows[0]["id"] == identity.id
    assert rows[0]["state"] == "standby"
    assert coordinator.leases() == [{
        "resource": "telegram:token", "epoch": epoch,
        "generation_id": identity.id, "state": "active",
    }]


def test_generation_files_are_scoped_and_cleanup_is_fenced(tmp_path: Path):
    identity = GenerationIdentity.create(release_sha="abc", label="ai.hermes.gateway-b", pid=456,
                                         start_fingerprint="456:one")
    paths = generation_paths(tmp_path, identity)
    write_generation_record(paths["state"], identity, state="standby")
    assert json.loads(paths["state"].read_text(encoding="utf-8"))["id"] == identity.id

    other = GenerationIdentity.create(release_sha="def", label="ai.hermes.gateway-a", pid=456,
                                      start_fingerprint="456:two")
    other_paths = generation_paths(tmp_path, other)
    write_generation_record(other_paths["state"], other)
    remove_generation_files(tmp_path, identity)
    assert not paths["state"].exists()
    assert other_paths["state"].exists()


@pytest.mark.asyncio
async def test_old_exit_preserves_successor_legacy_pid_projection(tmp_path):
    from gateway.run_generation import ActiveGeneration
    from gateway.status import _get_process_start_time
    db = GenerationCoordinator(tmp_path)
    fp = f"{os.getpid()}:{_get_process_start_time(os.getpid())}"
    old = GenerationIdentity.create(release_sha="a", label="a", start_fingerprint=fp)
    new = GenerationIdentity.create(release_sha="b", label="b", start_fingerprint=fp)
    db.register(old, state="serving")
    epoch = db.acquire_lease("active_generation", old.id)
    active = ActiveGeneration(tmp_path, db, old, epoch)
    await active.start()
    db.register(new, state="standby")
    db.request_transfer(old.id, new.id, epoch, set())
    promoted = db.commit_transfer(old.id, new.id, epoch)
    assert db.project_active_summary(new, promoted, {})
    projected = json.loads((tmp_path / "gateway.pid").read_text(encoding="utf-8"))
    assert projected["id"] == new.id
    await active.close()
    assert json.loads((tmp_path / "gateway.pid").read_text(encoding="utf-8")) == projected


@pytest.mark.asyncio
async def test_promoted_exit_projects_stopped_status_without_stale_pid(tmp_path):
    from gateway.run_generation import ActiveGeneration
    from gateway.status import read_runtime_status, retained_gateway_state
    db = GenerationCoordinator(tmp_path)
    identity = GenerationIdentity.create(release_sha="b", label="b")
    db.register(identity, state="serving")
    epoch = db.acquire_lease("active_generation", identity.id)
    active = ActiveGeneration(tmp_path, db, identity, epoch)
    db.project_active_summary(identity, epoch, {"gateway_state": "running"})
    await active.close()
    state = read_runtime_status(tmp_path / "gateway_state.json")
    assert state is not None
    assert state["gateway_state"] == "stopped"
    assert state["pid"] is None
    assert retained_gateway_state(state) == "stopped"
    assert db.leases()[0]["state"] == "released"
    # Exercise the real CLI path, but remove unrelated host gateway PIDs from
    # this subprocess's process probe. The home still supplies its real records.
    command = [sys.executable, "-c",
               "import sys; from hermes_cli import gateway; "
               "gateway.find_gateway_pids = lambda: []; "
               "from hermes_cli.main import main; "
               "sys.argv = ['hermes', 'gateway', 'status']; main()"]
    status_env = {**os.environ, "HERMES_HOME": str(tmp_path),
                  "HERMES_GATEWAY_LOCK_DIR": str(tmp_path / "locks"),
                  "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
                  "HERMES_LAUNCHD_LABEL": f"ai.hermes.test-{tmp_path.name}"}
    status = subprocess.run(command, env=status_env,
                            capture_output=True, text=True, timeout=20)
    assert status.returncode == 0, status.stderr
    assert "Gateway is not running" in status.stdout, status.stdout
    assert "lease=none state=exited" in status.stdout, status.stdout


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
    with pytest.raises(RuntimeError, match="explicit takeover"):
        coordinator.acquire_lease("active_generation", second.id)


@pytest.mark.parametrize("death", ["missing_pid", "reused_pid", "different_boot"])
def test_dead_lease_holder_fails_and_new_generation_takes_higher_epoch(tmp_path, monkeypatch, death):
    from gateway import generation
    from gateway.status import _get_process_start_time, START_TIME_DRIFT_TOLERANCE
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
        # Fingerprints are centiseconds, not seconds: +1 is tolerated drift,
        # not proof of a reused PID. Exercise the canonical mismatch boundary.
        monkeypatch.setattr("gateway.status._get_process_start_time",
                            lambda pid: current_start + START_TIME_DRIFT_TOLERANCE + 1)
    else:
        monkeypatch.setattr(generation, "_boot_id", lambda: "new-boot")
    assert coordinator.takeover_dead_generation("active_generation", first.id, second.id,
        bootout=lambda label: True) == epoch + 1
    assert coordinator.generations()[0]["verdict"] == "failed"
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
    assert coordinator.generations()[0]["state"] == "standby"
    assert coordinator.generations()[0]["suspect_at"] is not None
    assert coordinator.leases()[0]["epoch"] == epoch


@pytest.mark.asyncio
async def test_ready_and_close_database_work_does_not_block_loop(tmp_path, monkeypatch):
    import threading
    from gateway.run_generation import ActiveGeneration

    coordinator = GenerationCoordinator(tmp_path)
    identity = GenerationIdentity.create(release_sha="a", label="active")
    active = ActiveGeneration(tmp_path, coordinator, identity, 1)
    entered, release = threading.Event(), threading.Event()
    def blocked(*args, **kwargs):
        entered.set()
        release.wait(3)
    monkeypatch.setattr(coordinator, "heartbeat", blocked)
    monkeypatch.setattr(active, "_sync_runtime_status", lambda: None)
    task = asyncio.create_task(active.mark_ready())
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        await asyncio.wait_for(asyncio.sleep(0), 1)
        assert not task.done()
    finally:
        release.set()
        await task
    entered.clear()
    release.clear()
    monkeypatch.setattr(coordinator, "release_lease", blocked)
    # The heartbeat stub does not mark this unregistered owner exited; keep this
    # test scoped to the close path's nonblocking release-lease call.
    monkeypatch.setattr(coordinator, "release_exited_owner", lambda owner: 0)
    task = asyncio.create_task(active.close())
    try:
        assert await asyncio.to_thread(entered.wait, 2)
        await asyncio.wait_for(asyncio.sleep(0), 1)
        assert not task.done()
    finally:
        release.set()
        await task


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


@pytest.mark.platforms("windows")
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
    (tmp_path / "config.yaml").write_text("gateway:\n  overlap_handover:\n    enabled: true\n", encoding="utf-8")
    assert load_gateway_config().overlap_handover_enabled
    assert overlap_handover_enabled({"gateway": {"overlap_handover": {"enabled": True}}})
    assert not overlap_handover_enabled(object())
    assert not overlap_handover_enabled({"gateway": {"overlap_handover": None}})


def test_overlap_status_names_old_draining_pid(tmp_path, monkeypatch, capsys):
    from hermes_cli.gateway import _print_overlap_generations
    import hermes_cli.gateway as gateway_cli

    db = GenerationCoordinator(tmp_path)
    old = GenerationIdentity.create(release_sha="a", label="slot-a", pid=12345)
    new = GenerationIdentity.create(release_sha="b", label="slot-b", pid=12346)
    db.register(old, state="serving")
    db.register(new, state="standby")
    epoch = db.acquire_lease("active_generation", old.id)
    db.request_transfer(old.id, new.id, epoch, set())
    db.commit_transfer(old.id, new.id, epoch)
    monkeypatch.setattr(gateway_cli, "get_hermes_home", lambda: tmp_path)
    _print_overlap_generations()
    assert f"old generation draining pid={old.pid}" in capsys.readouterr().out.lower()


def test_terminal_generations_preserve_verdicts_labels_and_status(tmp_path):
    from hermes_cli.gateway_generation_status import read_generation_status
    coordinator = GenerationCoordinator(tmp_path)
    old = coordinator.reserve_generation(release_sha="old", label="reserved",
        started_at=time.time() - 9 * 86400)
    assert coordinator.retire_unclaimed(old.id)
    for n in range(25):
        coordinator.register(GenerationIdentity.create(release_sha=str(n), label=f"unique-{n}"), state="exited")
    assert next(row for row in read_generation_status(tmp_path) if row["id"] == old.id)["verdict"] == "failed"
    replacement = coordinator.reserve_generation(release_sha="new", label=old.label)
    assert replacement.id != old.id
    assert next(row for row in coordinator.generations() if row["id"] == old.id)["verdict"] == "failed"
    with pytest.raises(sqlite3.IntegrityError):
        coordinator.reserve_generation(release_sha="collision", label=old.label)


def test_terminal_transfer_audit_survives_while_successor_is_live(tmp_path):
    db = GenerationCoordinator(tmp_path)
    old = GenerationIdentity.create(release_sha="old", label="a", started_at=time.time() - 8 * 86400)
    new = GenerationIdentity.create(release_sha="new", label="b")
    db.register(old, state="serving")
    db.register(new, state="standby")
    epoch = db.acquire_lease("active_generation", old.id)
    db.request_transfer(old.id, new.id, epoch, set())
    db.commit_transfer(old.id, new.id, epoch)
    db.heartbeat(old.id, state="exited")
    db.register(GenerationIdentity.create(release_sha="third", label="c"))
    assert old.id in {row["id"] for row in db.generations()}
    with db.connect() as conn:
        assert conn.execute("SELECT state FROM generation_transfers WHERE old_id=?", (old.id,)).fetchone()[0] == "committed"


@pytest.mark.parametrize("count,backdate", [(2, True), (25, False)])
def test_terminal_transfer_history_is_retained_without_foreign_key_failure(tmp_path, count, backdate):
    db = GenerationCoordinator(tmp_path)
    old = GenerationIdentity.create(release_sha="old", label="a")
    new = GenerationIdentity.create(release_sha="new", label="b")
    db.register(old, state="serving")
    db.register(new, state="standby")
    epoch = db.acquire_lease("active_generation", old.id)
    db.request_transfer(old.id, new.id, epoch, {"token"})
    db.record_poller_stopped(old.id, epoch, "token", 1)
    db.commit_transfer(old.id, new.id, epoch)
    db.release_lease("active_generation", new.id, epoch + 1)
    db.heartbeat(old.id, state="exited")
    db.heartbeat(new.id, state="exited")
    if backdate:
        with db.connect() as conn:
            conn.execute("UPDATE generations SET started_at=? WHERE id IN (?,?)",
                         (time.time() - 8 * 86400, old.id, new.id))
    for index in range(count):
        db.register(GenerationIdentity.create(release_sha=str(index), label=f"other-{index}"), state="exited")
    with db.connect() as conn:
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert conn.execute("SELECT count(*) FROM generation_transfers WHERE old_id=?", (old.id,)).fetchone()[0] == 1
        assert conn.execute("SELECT count(*) FROM transfer_tokens WHERE old_id=?", (old.id,)).fetchone()[0] == 1
    assert old.id in {row["id"] for row in db.generations()}


def test_terminal_history_preserves_released_lease_and_upgrades_existing_schema(tmp_path):
    coordinator = GenerationCoordinator(tmp_path)
    old = GenerationIdentity.create(release_sha="old", label="slot-a",
                                    started_at=time.time() - 9 * 86400)
    coordinator.register(old, state="standby")
    epoch = coordinator.acquire_lease("old-slot", old.id)
    coordinator.release_lease("old-slot", old.id, epoch)
    coordinator.heartbeat(old.id, state="exited")
    with coordinator.connect() as conn:
        conn.execute("ALTER TABLE generations DROP COLUMN suspect_from_state")
    coordinator = GenerationCoordinator(tmp_path)
    coordinator.register(GenerationIdentity.create(release_sha="new", label="slot-b"))
    assert old.id in {row["id"] for row in coordinator.generations()}
    assert coordinator.leases()[0]["state"] == "released"


def test_suspect_heartbeat_restores_prior_live_state(tmp_path):
    from gateway.status import _get_process_start_time
    coordinator = GenerationCoordinator(tmp_path)
    contender = GenerationIdentity.create(release_sha="next", label="next")
    coordinator.register(contender)
    for initial in ("standby", "serving"):
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
        socket = Path(json.loads(records[0].read_text(encoding="utf-8"))["socket_path"])
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



@pytest.fixture(autouse=True)
def _coordinator_boot_identity(monkeypatch, request):
    if "macos_boot_id" in request.node.name or "hostname_change" in request.node.name:
        return
    from functools import lru_cache
    monkeypatch.setattr("gateway.generation._boot_id", lru_cache(maxsize=1)(lambda: "unit-test-boot"))
