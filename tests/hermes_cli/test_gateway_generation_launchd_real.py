"""Real launchd proof for an active and standby generation under disposable labels only."""
from __future__ import annotations

import json
import os
import plistlib
import subprocess
import sys
import signal
import tempfile
import shutil
import time
import uuid
from pathlib import Path

import pytest

from hermes_cli.gateway_generation_status import read_generation_status
from tests.hermes_cli.immutable_launchd_cleanup import register_disposable_label, sweep_prior_sessions


@pytest.fixture(scope="module", autouse=True)
def _sweep(request):
    sweep_prior_sessions(request)


@pytest.mark.platforms("macos")
@pytest.mark.live_system_guard_bypass
def test_active_and_passive_generations_use_distinct_records(request):
    """Boot a real active gateway beside a strictly passive standby."""
    tmp_path = Path(tempfile.mkdtemp(prefix="p3g-", dir="/tmp"))
    # Retain failed diagnostics. Only remove the fixture after all assertions pass.
    home = tmp_path / "profile"
    home.mkdir()
    (home / "config.yaml").write_text(
        "gateway:\n  overlap_handover:\n    enabled: true\n"
        "platforms:\n  telegram:\n    enabled: false\n")
    (home / ".env").write_text("TELEGRAM_BOT_TOKEN=123456:stub-no-network\n", encoding="utf-8")
    # The token is deliberately inert: the adapter is explicitly disabled, so
    # the active dispatcher and cron loop can run without network access.
    repository = Path(__file__).resolve().parents[2]
    python = Path(sys.executable)
    domain = f"gui/{os.getuid()}"  # windows-footgun: ok -- macos_only launchd fixture
    labels = [f"ai.hermes.p3test-{uuid.uuid4().hex}" for _ in range(2)]
    plists = []
    bound_sockets: list[Path] = []
    active = None
    active_state = None
    from gateway.generation import GenerationCoordinator
    coordinator = GenerationCoordinator(home)
    for label in labels:
        plist = tmp_path / f"{label}.plist"
        plist.write_bytes(plistlib.dumps({
            "Label": label, "RunAtLoad": True, "KeepAlive": {"SuccessfulExit": False},
            "ExitTimeOut": 60,
            "ProgramArguments": [str(python), "-u", "-m", "hermes_cli.main", "gateway", "run"] +
                                (["--standby"] if label != labels[0] else []),
            "WorkingDirectory": str(repository),
            "EnvironmentVariables": {"HERMES_HOME": str(home), "PYTHONPATH": str(repository),
                                     "HERMES_RELEASE_SHA": label[-8:], "HERMES_LAUNCHD_LABEL": label,
                                     "HERMES_GATEWAY_LOCK_DIR": str(tmp_path / "gateway-locks")},
            "StandardOutPath": str(tmp_path / f"{label}.out"),
            "StandardErrorPath": str(tmp_path / f"{label}.err"),
        }))
        register_disposable_label(request, label, plist)
        plists.append(plist)

    def ready():
        rows = read_generation_status(home)
        if not (len(rows) == 2 and {row["state"] for row in rows} == {"serving", "standby"}
                and all(row["pid"] > 0 for row in rows)):
            return False
        # Serving is authority, not readiness. Wait for the existing runtime
        # projection and both sockets before inspecting the native fixture.
        try:
            for row in rows:
                record = json.loads((home / f"gateway_state.{row['id']}.json").read_text(encoding="utf-8"))
                if not Path(record["socket_path"]).exists():
                    return False
            runtime = json.loads((home / "gateway_state.json").read_text(encoding="utf-8"))
            return runtime.get("gateway_state") == "running"
        except (OSError, ValueError, KeyError):
            return False

    try:
        for plist in plists:
            subprocess.run(["launchctl", "bootstrap", domain, str(plist)], check=True, timeout=15)
        # Both fresh interpreters import the real CLI and gateway. Allow cold
        # bytecode repair and shared-host load before requiring runtime readiness.
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline and not ready():
            time.sleep(.2)
        if not ready():
            diagnostic = {
                "rows": read_generation_status(home),
                "files": [p.name for p in home.iterdir()],
                "logs": [(p.name, p.read_text(encoding="utf-8", errors="replace")[-3000:]) for p in tmp_path.glob("*.out")],
                "errors": [(p.name, p.read_text(encoding="utf-8", errors="replace")[-3000:]) for p in tmp_path.glob("*.err")],
                "jobs": [subprocess.run(["launchctl", "print", f"{domain}/{label}"],
                                        capture_output=True, text=True, timeout=5).stdout[-1000:] for label in labels],
            }
            (tmp_path / "diagnostics.json").write_text(json.dumps(diagnostic, indent=2), encoding="utf-8")
        assert ready(), f"active probe diagnostics: {tmp_path / 'diagnostics.json'}"
        rows = read_generation_status(home)
        assert {row["label"] for row in rows} == set(labels)
        assert len({row["pid"] for row in rows}) == 2
        assert (home / "gateway.pid").exists()
        from gateway.control_socket import resolve_client_socket_path
        control_path = resolve_client_socket_path(home)
        assert control_path is not None and control_path.exists()
        assert (home / "gateway_state.json").exists()
        assert {row["state"] for row in rows} == {"serving", "standby"}
        assert sum(bool(row["leases"]) for row in rows) == 1
        active = next(row for row in rows if row["leases"])
        assert active["label"] == labels[0]
        standby = next(row for row in rows if not row["leases"])
        assert standby["label"] == labels[1]
        assert active["leases"][0].startswith("active_generation@")
        from gateway.control_socket import CONTROL_PROTOCOL_VERSION
        import socket
        active_state = json.loads((home / f"gateway_state.{active['id']}.json").read_text(encoding="utf-8"))
        with socket.socket(socket.AF_UNIX) as sock:
            sock.settimeout(3)
            sock.connect(active_state["socket_path"])
            sock.sendall(b'{"id":1,"verb":"identify","protocol":1}\n')
            answer = json.loads(sock.makefile("rb").readline())
        assert answer["ok"] is True
        assert answer["protocol"] == CONTROL_PROTOCOL_VERSION
        assert answer["result"]["pid"] == active["pid"]
        for row in rows:
            suffix = row["id"]
            assert (home / f"gateway.{suffix}.pid").is_file()
            assert (home / f"gateway_state.{suffix}.json").is_file()
            state = json.loads((home / f"gateway_state.{suffix}.json").read_text(encoding="utf-8"))
            assert state["state"] == row["state"]
            assert Path(state["socket_path"]).exists()
            bound_sockets.append(Path(state["socket_path"]))
        status = subprocess.run(
            [str(python), "-m", "hermes_cli.main", "gateway", "status"],
            cwd=repository, env={**os.environ, "HERMES_HOME": str(home), "PYTHONPATH": str(repository),
                                 "HERMES_GATEWAY_LOCK_DIR": str(tmp_path / "gateway-locks")},
            capture_output=True, text=True, timeout=30,
        )
        assert status.returncode == 0, status.stderr
        assert all(label in status.stdout for label in labels)
        assert "Overlap generations:" in status.stdout
        assert "Gateway is running (PID:" in status.stdout

        # Legacy KeepAlive must recover without stealing a live lease.
        from gateway.status import _get_process_start_time
        assert active["start_fingerprint"] == f"{active['pid']}:{_get_process_start_time(active['pid'])}"
        os.kill(active["pid"], signal.SIGKILL)  # windows-footgun: ok -- macos_only, real SIGKILL is the contract
        deadline = time.monotonic() + 60
        successor = None
        while time.monotonic() < deadline:
            candidates = [row for row in read_generation_status(home)
                          if row["label"] == labels[0] and row["id"] != active["id"]
                          and row["state"] == "serving" and row["leases"]]
            if candidates:
                successor = candidates[0]
                break
            time.sleep(.25)
        assert successor is not None, (tmp_path / f"{labels[0]}.err").read_text(encoding="utf-8", errors="replace")[-3000:]
        assert successor["pid"] != active["pid"]
        retired = next(row for row in coordinator.generations() if row["id"] == active["id"])
        assert (retired["state"], retired["verdict"]) == ("exited", "failed")
        assert coordinator.leases()[0]["generation_id"] == successor["id"]
        assert successor["leases"][0] != active["leases"][0]
        bound_sockets.append(Path(json.loads((home / f"gateway_state.{successor['id']}.json").read_text(encoding="utf-8"))["socket_path"]))
    finally:
        for label in labels:
            subprocess.run(["launchctl", "bootout", f"{domain}/{label}"], capture_output=True, timeout=15)
            deadline = time.monotonic() + 10
            while time.monotonic() < deadline:
                if subprocess.run(["launchctl", "print", f"{domain}/{label}"], capture_output=True,
                                  timeout=5).returncode != 0:
                    break
                time.sleep(.2)
            assert subprocess.run(["launchctl", "print", f"{domain}/{label}"], capture_output=True,
                                  timeout=5).returncode != 0
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline and [p for p in home.glob("gateway.*.pid")
                                                 if active is None or p.name != f"gateway.{active['id']}.pid"]:
            time.sleep(.2)
        if active is not None:
            assert not [p for p in home.glob("gateway.*.pid") if p.name != f"gateway.{active['id']}.pid"]
            assert not [p for p in home.glob("gateway_state.*.json") if p.name != f"gateway_state.{active['id']}.json"]
            assert not any(row["leases"] for row in read_generation_status(home) if row["id"] != active["id"])
        if active_state is not None:
            assert not any(path.exists() for path in bound_sockets if path != Path(active_state["socket_path"]))
        standby_out = tmp_path / f"{labels[1]}.out"
        if standby_out.exists():
            assert "Messaging platforms + cron scheduler" not in standby_out.read_text(encoding="utf-8")

    shutil.rmtree(tmp_path)  # Reached only after successful assertions and cleanup.
