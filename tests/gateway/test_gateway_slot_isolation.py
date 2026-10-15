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
from pathlib import Path

import pytest

from gateway import gateway_slot
from gateway import zero_cut_standby
from gateway.control_socket import GatewayControlServer, identify_gateway, resolve_server_socket_path

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


def test_slot_pin_is_stable(monkeypatch):
    gateway_slot.reset_gateway_slot_for_tests()
    monkeypatch.setenv("HERMES_GATEWAY_SLOT", "b")
    config = {"gateway": {"zero_cut_handover": {"enabled": True}}}
    assert gateway_slot.pin_gateway_slot(config) == "b"
    monkeypatch.delenv("HERMES_GATEWAY_SLOT")
    assert gateway_slot.effective_gateway_slot() == "b"
    gateway_slot.reset_gateway_slot_for_tests()


def test_standby_readiness_is_local(monkeypatch):
    gateway_slot.reset_gateway_slot_for_tests()
    zero_cut_standby.reset_for_tests()
    monkeypatch.setenv("HERMES_GATEWAY_SLOT", "b")
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
