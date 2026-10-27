"""Zero-cut handover slot isolation: a standby slot-b gateway coexists with slot a on one home.

Drives the real seams: ``acquire_gateway_runtime_lock`` + ``write_pid_file`` in separate
processes (real flock / O_EXCL), real control-socket servers and clients, and plist rendering.
"""

import asyncio
import json
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

from gateway import gateway_slot
from gateway import zero_cut_standby
from gateway.control_socket import GatewayControlServer, identify_gateway, query_gateway_control, resolve_server_socket_path


@pytest.fixture(autouse=True)
def _reset_slot_state(monkeypatch):
    gateway_slot.reset_gateway_slot_for_tests()
    zero_cut_standby.reset_for_tests()
    monkeypatch.setattr(gateway_slot, "_query_other_slot",
                        lambda home, slot, timeout: {"standby": False} if slot == "a" else None)
    yield
    gateway_slot.reset_gateway_slot_for_tests()
    zero_cut_standby.reset_for_tests()

REPO_ROOT = Path(__file__).resolve().parents[2]
FLAG_ON = "gateway:\n  zero_cut_handover:\n    enabled: true\n"

# Claims the runtime lock + PID file like _start_gateway_claim_pid_file, reports, then holds them
# until stdin closes so a sibling claim races a live holder.
_CLAIM = """
import json, sys
from gateway import status
won_lock = status.acquire_gateway_runtime_lock()
won_pid = False
if won_lock:
    try:
        status.write_pid_file(); won_pid = True
    except FileExistsError:
        pass
print(json.dumps({"lock": won_lock, "pid": won_pid,
                  "pid_path": status._get_pid_path().name,
                  "lock_path": status._get_gateway_lock_path().name}), flush=True)
sys.stdin.read()
"""


@pytest.fixture()
def home(tmp_path: Path, monkeypatch):
    # pytest's macOS tmp_path overflows sun_path; a short /tmp root keeps sockets in-home.
    try:
        root = Path(tempfile.mkdtemp(prefix="hgw-", dir="/tmp"))  # no-tmp: ok — AF_UNIX sun_path limit
    except OSError:
        pytest.skip("/tmp not writable on this host")
    d = root / "h"
    d.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(d))
    monkeypatch.setenv("HERMES_GATEWAY_LOCK_DIR", str(tmp_path / "locks"))
    monkeypatch.delenv("HERMES_GATEWAY_SLOT", raising=False)
    yield d
    shutil.rmtree(root, ignore_errors=True)


def _start_claim(home: Path, slot: str | None) -> tuple[subprocess.Popen, dict]:
    env = {k: v for k, v in os.environ.items() if k not in ("HERMES_GATEWAY_SLOT", "PYTHONPATH")}
    env["HERMES_HOME"] = str(home)
    if slot is not None:
        env["HERMES_GATEWAY_SLOT"] = slot
    proc = subprocess.Popen([sys.executable, "-c", _CLAIM], cwd=REPO_ROOT, env=env, text=True,
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    line = proc.stdout.readline()
    assert line, proc.stderr.read()
    return proc, json.loads(line)


def _finish(*procs: subprocess.Popen) -> None:
    for proc in procs:
        proc.stdin.close()
        proc.wait(timeout=30)
        proc.stdout.close()
        proc.stderr.close()


def test_slot_defaults_to_a_and_reads_env_at_call_time(monkeypatch):
    monkeypatch.delenv("HERMES_GATEWAY_SLOT", raising=False)
    assert gateway_slot.get_gateway_slot() == "a"
    for raw, expected in (("b", "b"), (" B ", "b"), ("a", "a"), ("", "a"), ("c", "a")):
        monkeypatch.setenv("HERMES_GATEWAY_SLOT", raw)
        assert gateway_slot.get_gateway_slot() == expected


def test_role_is_resolved_from_the_other_slot(monkeypatch):
    monkeypatch.setenv("HERMES_GATEWAY_SLOT", "b")
    queries = []
    monkeypatch.setattr(gateway_slot, "_query_other_slot",
                        lambda home, slot, timeout: queries.append(slot) or {"standby": False})
    assert gateway_slot.pin_gateway_slot({"gateway": {"zero_cut_handover": {"enabled": True}}}) == "b"
    assert zero_cut_standby.is_standby()
    assert queries == ["a"]


def test_start_gateway_pin_without_config_reads_the_home_flag_for_the_role(monkeypatch, tmp_path):
    """start_gateway pins with config=None; the role must read config.yaml like the slot does, or a
    slot-b process boots active and fights the live slot for the bot token (found in a real drill)."""
    (tmp_path / "config.yaml").write_text("gateway:\n  zero_cut_handover:\n    enabled: true\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_GATEWAY_SLOT", "b")
    monkeypatch.setattr(gateway_slot, "_query_other_slot", lambda home, slot, timeout: {"standby": False})
    assert gateway_slot.pin_gateway_slot(None) == "b"
    assert zero_cut_standby.is_standby()


def test_role_falls_back_to_active_when_other_slot_is_down_or_standby(monkeypatch):
    monkeypatch.setenv("HERMES_GATEWAY_SLOT", "a")
    for other_status in (None, {"standby": True}):
        gateway_slot.reset_gateway_slot_for_tests()
        monkeypatch.setattr(gateway_slot, "_query_other_slot",
                            lambda home, slot, timeout, result=other_status: result)
        gateway_slot.pin_gateway_slot({"gateway": {"zero_cut_handover": {"enabled": True}}})
        assert not zero_cut_standby.is_standby()


def test_flag_off_role_is_active_without_query(monkeypatch):
    monkeypatch.setenv("HERMES_GATEWAY_SLOT", "b")
    monkeypatch.setattr(gateway_slot, "_query_other_slot",
                        lambda *args: pytest.fail("flag-off startup must not query the other slot"))
    gateway_slot.pin_gateway_slot({"gateway": {"zero_cut_handover": {"enabled": False}}})
    assert not zero_cut_standby.is_standby()


def test_standby_readiness_is_local(monkeypatch):
    gateway_slot.reset_gateway_slot_for_tests()
    zero_cut_standby.reset_for_tests()
    monkeypatch.setenv("HERMES_GATEWAY_SLOT", "b")
    monkeypatch.setattr(gateway_slot, "_query_other_slot",
                        lambda home, slot, timeout: {"standby": False})
    gateway_slot.pin_gateway_slot({"gateway": {"zero_cut_handover": {"enabled": True}}})
    assert zero_cut_standby.readiness() == {"slot": "b", "standby": True, "ready": False, "deferred": []}
    zero_cut_standby.mark_ready()
    assert zero_cut_standby.readiness()["ready"] is True
    zero_cut_standby.reset_for_tests()
    gateway_slot.reset_gateway_slot_for_tests()

def test_slot_filenames():
    assert gateway_slot.slot_filename("gateway.pid", "a") == "gateway.pid"
    assert gateway_slot.slot_filename("gateway.pid", "b") == "gateway.b.pid"
    assert gateway_slot.slot_filename("gateway.sock.path", "b") == "gateway.b.sock.path"
    assert gateway_slot.sibling_lock_name("gateway.pid", "gateway.lock") == "gateway.lock"
    assert gateway_slot.sibling_lock_name("gateway.b.pid", "gateway.lock") == "gateway.b.lock"


def test_flag_off_slot_b_keeps_historical_paths_and_loses_second_claim(home):
    """Flag off: HERMES_GATEWAY_SLOT=b is inert, so B contends for A's files and loses, as today."""
    proc_a, a = _start_claim(home, None)
    proc_b, b = _start_claim(home, "b")
    try:
        assert a == {"lock": True, "pid": True, "pid_path": "gateway.pid", "lock_path": "gateway.lock"}
        assert b == {"lock": False, "pid": False, "pid_path": "gateway.pid", "lock_path": "gateway.lock"}
        assert not list(home.glob("gateway.b.*"))
    finally:
        _finish(proc_a, proc_b)


def test_flag_on_slot_a_second_claim_still_loses(home):
    (home / "config.yaml").write_text(FLAG_ON, encoding="utf-8")
    proc_a, a = _start_claim(home, "a")
    proc_a2, a2 = _start_claim(home, None)
    try:
        assert a["lock"] and a["pid"] and a["pid_path"] == "gateway.pid" and a["lock_path"] == "gateway.lock"
        assert a2["lock"] is False and a2["pid_path"] == "gateway.pid"
    finally:
        _finish(proc_a, proc_a2)


def test_flag_on_a_and_b_both_claim_beside_each_other(home):
    (home / "config.yaml").write_text(FLAG_ON, encoding="utf-8")
    proc_a, a = _start_claim(home, None)
    proc_b, b = _start_claim(home, "b")
    proc_b2, b2 = _start_claim(home, "b")
    try:
        assert a == {"lock": True, "pid": True, "pid_path": "gateway.pid", "lock_path": "gateway.lock"}
        assert b == {"lock": True, "pid": True, "pid_path": "gateway.b.pid", "lock_path": "gateway.b.lock"}
        assert b2["lock"] is False  # B's own slot is still a singleton
        assert json.loads((home / "gateway.pid").read_text())["pid"] == proc_a.pid
        assert json.loads((home / "gateway.b.pid").read_text())["pid"] == proc_b.pid
    finally:
        _finish(proc_a, proc_b, proc_b2)


def test_home_lookup_keeps_finding_slot_a(home, monkeypatch):
    """Readers in a slot-a process (CLI, status) resolve the historical files even when flag is on."""
    (home / "config.yaml").write_text(FLAG_ON, encoding="utf-8")
    from gateway import status
    assert status._get_pid_path() == home / "gateway.pid"
    assert status._get_gateway_lock_path() == home / "gateway.lock"
    assert status._get_gateway_lock_path(home / "gateway.pid") == home / "gateway.lock"
    monkeypatch.setenv("HERMES_GATEWAY_SLOT", "b")
    assert status._get_pid_path() == home / "gateway.b.pid"
    assert status._get_gateway_lock_path() == home / "gateway.b.lock"
    # An explicit probe of a home's gateway.pid still pairs with gateway.lock from a slot-b process.
    assert status._get_gateway_lock_path(home / "gateway.pid") == home / "gateway.lock"


def test_socket_paths_slot_a_unchanged(home):
    assert resolve_server_socket_path(home) == (home / "gateway.sock", None)
    assert resolve_server_socket_path(home, "b") == (home / "gateway.b.sock", None)


def test_long_home_fallback_sockets_and_pointers_are_per_slot(tmp_path):
    deep = tmp_path / ("x" * 120) / ".hermes"
    bind_a, pointer_a = resolve_server_socket_path(deep)
    bind_b, pointer_b = resolve_server_socket_path(deep, "b")
    assert pointer_a == deep / "gateway.sock.path" and pointer_b == deep / "gateway.b.sock.path"
    assert bind_a.name.startswith("hermes-gw-") and bind_a.name.endswith(".sock")
    assert bind_b == bind_a.with_name(bind_a.name[: -len(".sock")] + ".b.sock")


def test_both_slot_sockets_answer_identify_on_one_home(home, monkeypatch):
    (home / "config.yaml").write_text(FLAG_ON, encoding="utf-8")

    async def scenario():
        server_a = GatewayControlServer(home, verb_handlers={"identify": lambda: {"slot": "a"}})
        monkeypatch.setenv("HERMES_GATEWAY_SLOT", "b")
        server_b = GatewayControlServer(home, verb_handlers={"identify": lambda: {"slot": "b"}})
        monkeypatch.delenv("HERMES_GATEWAY_SLOT")
        assert await server_a.start()
        assert await server_b.start()  # binding B must not unlink A's live socket
        try:
            loop = asyncio.get_running_loop()
            default = await loop.run_in_executor(None, lambda: identify_gateway(home))
            standby = await loop.run_in_executor(None, lambda: identify_gateway(home, slot="b"))
            return default, standby
        finally:
            await server_b.stop()
            assert (home / "gateway.sock").exists()
            await server_a.stop()

    default, standby = asyncio.run(scenario())
    assert default == {"slot": "a"}
    assert standby == {"slot": "b"}
    assert not (home / "gateway.sock").exists() and not (home / "gateway.b.sock").exists()


def test_flag_off_slot_b_server_binds_historical_socket(home, monkeypatch):
    monkeypatch.setenv("HERMES_GATEWAY_SLOT", "b")
    server = GatewayControlServer(home)
    assert server._slot == "a"


def test_launchd_plist_slots(home):
    from hermes_cli import gateway as gateway_cli

    active_label, standby_label = gateway_cli.get_launchd_slot_labels()
    active = gateway_cli.generate_launchd_plist()
    assert gateway_cli.generate_launchd_plist(slot="a") == active
    assert "HERMES_GATEWAY_SLOT" not in active
    active_body = plistlib.loads(active.encode())
    assert active_body["Label"] == active_label

    standby_body = plistlib.loads(gateway_cli.generate_launchd_plist(slot="b").encode())
    assert standby_body["Label"] == standby_label == f"{active_label}.b"
    assert standby_body["EnvironmentVariables"]["HERMES_GATEWAY_SLOT"] == "b"
    expected_env = {**active_body["EnvironmentVariables"], "HERMES_GATEWAY_SLOT": "b"}
    assert standby_body["EnvironmentVariables"] == expected_env
    assert {k: v for k, v in standby_body.items() if k not in ("Label", "EnvironmentVariables")} == {
        k: v for k, v in active_body.items() if k not in ("Label", "EnvironmentVariables")}


def _pin_standby(monkeypatch, home: Path) -> None:
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_GATEWAY_SLOT", "b")
    gateway_slot.pin_gateway_slot({"gateway": {"zero_cut_handover": {"enabled": True}}})


def test_real_standby_control_socket_reports_readiness(home, monkeypatch):
    (home / "config.yaml").write_text(FLAG_ON, encoding="utf-8")
    _pin_standby(monkeypatch, home)

    async def scenario():
        server = GatewayControlServer(home)
        assert await server.start()
        try:
            before = await asyncio.get_running_loop().run_in_executor(
                None, lambda: identify_gateway(home, slot="b"))
            status_before = await asyncio.get_running_loop().run_in_executor(
                None, lambda: query_gateway_control(home, "status", slot="b"))
            zero_cut_standby.mark_ready()
            after = await asyncio.get_running_loop().run_in_executor(
                None, lambda: query_gateway_control(home, "status", slot="b"))
            await server.stop()

            monkeypatch.delenv("HERMES_GATEWAY_SLOT")
            gateway_slot.reset_gateway_slot_for_tests()
            active_server = GatewayControlServer(home)
            assert await active_server.start()
            try:
                active = await asyncio.get_running_loop().run_in_executor(
                    None, lambda: identify_gateway(home))
            finally:
                await active_server.stop()
            return before, status_before, after, active
        finally:
            await server.stop()

    identify, status_before, status_after, active = asyncio.run(scenario())
    assert identify["slot"] == "b" and identify["standby"] is True
    assert status_before["slot"] == "b" and status_before["standby"] is True
    assert status_before["ready"] is False and status_before["deferred"] == []
    assert status_after["ready"] is True and status_after["deferred"] == []

    assert active["slot"] == "a" and active["standby"] is False


def test_runtime_status_isolation_preserves_active_file(home, monkeypatch):
    from gateway import status

    (home / "config.yaml").write_text(FLAG_ON, encoding="utf-8")
    active = home / "gateway_state.json"
    active.write_bytes(b'{"gateway_state":"active","sentinel":"keep"}\n')
    original = active.read_bytes()
    _pin_standby(monkeypatch, home)

    assert status.write_runtime_status(gateway_state="starting", wait_timeout=2.0)
    assert status.flush_runtime_status(timeout=2.0)
    assert active.read_bytes() == original
    assert (home / "gateway_state.b.json").exists()
    assert status.read_runtime_status()["gateway_state"] == "starting"


def test_standby_paths_and_stop_marker_follows_the_target_pid(home, monkeypatch):
    from gateway import run_startup, status

    (home / "config.yaml").write_text(FLAG_ON, encoding="utf-8")
    _pin_standby(monkeypatch, home)
    transient = run_startup.GatewayStartupMixin._transient_exit_streak_path()
    assert transient == home / ".b.transient_startup_exits"
    assert status._get_planned_stop_marker_path() == home / ".b.gateway-planned-stop.json"
    assert status._get_planned_restart_marker_path() == home / ".b.gateway-planned-restart.json"

    monkeypatch.setattr(status, "_get_process_start_time", lambda _pid: None)
    (home / "gateway.pid").write_text("4242", encoding="utf-8")
    (home / "gateway.b.pid").write_text("4343", encoding="utf-8")
    # An explicit slot still wins.
    assert status.write_planned_stop_marker(os.getpid(), slot="a")
    assert (home / ".gateway-planned-stop.json").exists()
    (home / ".gateway-planned-stop.json").unlink()
    # Without one, the marker lands beside the gateway that PID belongs to, not the caller's slot.
    assert status.write_planned_stop_marker(4242)
    assert (home / ".gateway-planned-stop.json").exists()
    assert status.write_planned_stop_marker(4343)
    assert (home / ".b.gateway-planned-stop.json").exists()
    assert status.write_planned_restart_marker(4343)
    assert (home / ".b.gateway-planned-restart.json").exists()


def test_cli_stop_of_standby_from_an_unpinned_process_targets_slot_b(home, monkeypatch):
    """`hermes gateway stop`, s6, systemd and Windows callers run as slot a; stopping B must still mark B."""
    from gateway import status

    (home / "config.yaml").write_text(FLAG_ON, encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_GATEWAY_SLOT", raising=False)
    gateway_slot.pin_gateway_slot({"gateway": {"zero_cut_handover": {"enabled": True}}})
    assert gateway_slot.effective_gateway_slot() == "a"
    monkeypatch.setattr(status, "_get_process_start_time", lambda _pid: None)
    (home / "gateway.b.pid").write_text("4343", encoding="utf-8")
    assert status.write_planned_stop_marker(4343)
    assert (home / ".b.gateway-planned-stop.json").exists()
    assert not (home / ".gateway-planned-stop.json").exists()


def test_flag_off_slot_b_preserves_all_historical_paths(home, monkeypatch):
    from gateway import run_startup, status

    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_GATEWAY_SLOT", "b")
    gateway_slot.pin_gateway_slot({"gateway": {"zero_cut_handover": {"enabled": False}}})
    assert gateway_slot.effective_gateway_slot() == "a"
    assert zero_cut_standby.is_standby() is False
    assert status._get_runtime_status_path() == home / "gateway_state.json"
    assert status._get_planned_stop_marker_path() == home / ".gateway-planned-stop.json"
    assert status._get_planned_restart_marker_path() == home / ".gateway-planned-restart.json"
    assert run_startup.GatewayStartupMixin._transient_exit_streak_path() == home / ".transient_startup_exits"


@pytest.mark.asyncio
async def test_start_gateway_skips_host_seams_on_standby_and_keeps_them_active(monkeypatch, tmp_path):
    from gateway import run as gateway_run
    from gateway.config import GatewayConfig

    calls = []
    mode = {"exit_cleanly": True, "ready_at_cron": None}

    class FakeRunner:
        def __init__(self, config):
            self.config = config
            self.should_exit_cleanly = mode["exit_cleanly"]
            self.exit_reason = None
            self.exit_code = None
            self._running = True

        async def start(self):
            calls.append("runner.start")
            return True

        async def wait_for_shutdown(self):
            calls.append("wait_for_shutdown")

        async def stop(self):
            pass

        def _start_systemd_watchdog(self):
            pass

    class NoopThread:
        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

    async def host_attach(*args, **kwargs):
        calls.append("host_attach")
        return True if os.environ.get("HERMES_GATEWAY_SLOT") != "b" else None

    async def settle(*args, **kwargs):
        pass

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    _not_main = threading.Thread(name="not-main")  # real Thread, created before Thread is faked
    monkeypatch.setattr(gateway_run, "GatewayRunner", FakeRunner)
    monkeypatch.setattr(gateway_run, "_host_attach_or_none", host_attach)
    monkeypatch.setattr(gateway_run, "_claim_host_gateway_role", lambda *a, **k: calls.append("claim"))
    monkeypatch.setattr(gateway_run, "_start_gateway_claim_pid_file", lambda **kwargs: True)
    monkeypatch.setattr(gateway_run, "_start_gateway_start_control_socket", lambda runner: settle())
    monkeypatch.setattr(gateway_run, "_start_gateway_configure_logging", lambda *a, **k: None)
    monkeypatch.setattr(gateway_run, "_run_requested_state_db_compaction", lambda: calls.append("compact"))
    monkeypatch.setattr(gateway_run, "_refresh_host_gateway_record", lambda *a: calls.append("refresh"))
    monkeypatch.setattr(gateway_run, "_log_standalone_profiles_at_boot", lambda *a: None)
    monkeypatch.setattr(gateway_run, "_ensure_windows_gateway_venv_imports", lambda: None)
    monkeypatch.setattr(gateway_run, "_enable_multiplex_log_routing", lambda *a: None)
    monkeypatch.setattr(gateway_run, "_start_gateway_make_shutdown_signal_handler", lambda *a: None)
    monkeypatch.setattr(gateway_run, "_start_gateway_make_restart_signal_handler", lambda *a: None)
    monkeypatch.setattr(gateway_run, "_start_gateway_replace_existing_instance", lambda *a: True)
    monkeypatch.setattr(gateway_run, "_shutdown_gateway_health_export", lambda *a: None)
    monkeypatch.setattr(gateway_run, "_settle_and_shutdown_mcp", settle)
    monkeypatch.setattr(gateway_run, "_best_effort", lambda fn, *a, **k: fn())
    monkeypatch.setattr(gateway_run.threading, "Thread", NoopThread)
    # Not the main thread (so no signal handlers), but a real thread so logging records still build.
    monkeypatch.setattr(gateway_run.threading, "current_thread", lambda: _not_main)
    monkeypatch.setattr("gateway.status.get_running_pid", lambda: None)
    monkeypatch.setattr("gateway.code_skew.record_boot_fingerprint", lambda: None)
    monkeypatch.setattr("gateway.lifecycle_ledger.record_startup", lambda: calls.append("record_startup"))
    monkeypatch.setattr("hermes_cli.nous_auth_keepalive.start_nous_auth_keepalive", lambda: None)
    monkeypatch.setattr("hermes_cli.resource_limits.apply_nofile_soft_limit", lambda: None)

    config = GatewayConfig(zero_cut_handover_enabled=True)
    monkeypatch.setenv("HERMES_GATEWAY_SLOT", "b")
    assert await gateway_run.start_gateway(config=config, replace=False, verbosity=None)
    assert "host_attach" not in calls and "claim" not in calls
    assert "record_startup" not in calls
    assert "compact" not in calls  # only the authoritative gateway rewrites state.db

    calls.clear()
    gateway_slot.reset_gateway_slot_for_tests()
    monkeypatch.delenv("HERMES_GATEWAY_SLOT")
    assert await gateway_run.start_gateway(config=config, replace=False, verbosity=None)
    assert "host_attach" in calls
    assert zero_cut_standby.readiness()["ready"] is False  # a clean early exit never reports ready

    # A serving standby reports ready only after cron and housekeeping have started.
    def start_cron(runner):
        mode["ready_at_cron"] = zero_cut_standby.readiness()["ready"]
        return (None, None, None, None)

    async def shutdown_tail(*args, **kwargs):
        return True

    mode["exit_cleanly"] = False
    gateway_slot.reset_gateway_slot_for_tests()
    monkeypatch.setenv("HERMES_GATEWAY_SLOT", "b")
    monkeypatch.setattr(gateway_run, "_start_gateway_start_cron_and_housekeeping", start_cron)
    monkeypatch.setattr(gateway_run, "_start_gateway_shutdown_tail", shutdown_tail)
    assert await gateway_run.start_gateway(config=config, replace=False, verbosity=None)
    assert mode["ready_at_cron"] is False
    assert zero_cut_standby.readiness()["ready"] is True
    assert "compact" not in calls


@pytest.mark.asyncio
async def test_standby_shutdown_skips_markers(monkeypatch, tmp_path):
    from gateway import run as gateway_run
    from gateway import status
    from gateway.config import GatewayConfig
    from gateway.run_shutdown import GatewayShutdownMixin

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_GATEWAY_SLOT", "b")
    gateway_slot.pin_gateway_slot(GatewayConfig(zero_cut_handover_enabled=True))
    runner = object.__new__(GatewayShutdownMixin)
    runner._restart_requested = True
    runner._restart_command_source = None
    runner._restart_via_service = False
    runner._restart_detached = False
    runner._draining = True
    runner._exit_code = None
    runner._exit_reason = None
    runner._signal_initiated_shutdown = False
    runner._stop_requested_by_signal = False
    runner._update_runtime_status = lambda *args: None
    runner._STUCK_LOOP_FILE = ".restart_failure_counts"
    active_counts = tmp_path / ".restart_failure_counts"
    active_counts.write_text('{"agent:main:telegram:dm:1": 2}', encoding="utf-8")
    ctx = GatewayShutdownMixin._StopContext(
        deferred_count=lambda: 0, active_agents={"agent:main:telegram:dm:1": object()},
        timed_out=False, started_at=time.monotonic())
    monkeypatch.setattr(status, "remove_pid_file", lambda: None)
    monkeypatch.setattr(status, "release_gateway_runtime_lock", lambda: None)
    monkeypatch.setattr(status, "flush_runtime_status_async", lambda **kwargs: asyncio.sleep(0, result=True))
    monkeypatch.setattr(gateway_run, "_shutdown_gateway_health_export", lambda *args: None)
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr("gateway.restart_loop_guard.record_planned_exit", lambda: pytest.fail("standby recorded planned exit"))

    await runner._stop_persist_exit_state(ctx)
    assert not (tmp_path / ".clean_shutdown").exists()
    assert not (tmp_path / ".gateway-planned-restart.json").exists()
    # The stuck-loop counter is per slot: the active slot's counts stay byte-identical.
    assert active_counts.read_text(encoding="utf-8") == '{"agent:main:telegram:dm:1": 2}'
    standby_counts = tmp_path / gateway_slot.slot_filename(".restart_failure_counts", "b")
    assert json.loads(standby_counts.read_text(encoding="utf-8")) == {"agent:main:telegram:dm:1": 1}


def test_backup_and_cleanup_skip_standby_runtime_names(tmp_path):
    from hermes_cli import backup, data_cleanup

    assert "gateway.b.pid" in backup._EXCLUDED_NAMES
    assert {"gateway_state.b.json", "gateway.b.pid", "gateway.b.lock"} <= backup._IMPORT_SKIP_NAMES
    project = tmp_path / "project"
    project.mkdir()
    lock = tmp_path / "gateway.b.lock"
    lock.write_text("runtime", encoding="utf-8")
    plan = data_cleanup.plan_data_removal(tmp_path, project)
    assert lock in plan.keep
