"""Real launchd proof for an active and standby generation under disposable labels only."""
from __future__ import annotations

import json
import os
import plistlib
import subprocess
import sys
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


@pytest.mark.macos_only
def test_active_and_passive_generations_use_distinct_records(request):
    """Boot a real active gateway beside a strictly passive standby."""
    notes = Path.home() / ".hermes/cache/seamless-p3"
    tmp_path = Path(tempfile.mkdtemp(prefix="slice1-launchd-", dir=notes))
    request.addfinalizer(lambda: shutil.rmtree(tmp_path))
    home = tmp_path / "profile"
    home.mkdir()
    (home / "config.yaml").write_text(
        "gateway:\n  overlap_handover:\n    enabled: true\n"
        "platforms:\n  telegram:\n    enabled: false\n")
    (home / ".env").write_text("TELEGRAM_BOT_TOKEN=123456:stub-no-network\n")
    # The token is deliberately inert: the adapter is explicitly disabled, so
    # the active dispatcher and cron loop can run without network access.
    repository = Path(__file__).resolve().parents[2]
    python = Path(sys.executable)
    domain = f"gui/{os.getuid()}"
    labels = [f"ai.hermes.p3test-{uuid.uuid4().hex}" for _ in range(2)]
    plists = []
    for label in labels:
        plist = tmp_path / f"{label}.plist"
        plist.write_bytes(plistlib.dumps({
            "Label": label, "RunAtLoad": True, "KeepAlive": {"SuccessfulExit": False},
            "ExitTimeOut": 60,
            "ProgramArguments": [str(python), "-m", "hermes_cli.main", "gateway", "run"] +
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
        return len(rows) == 2 and all(row["state"] == "ready" for row in rows)

    try:
        for plist in plists:
            subprocess.run(["launchctl", "bootstrap", domain, str(plist)], check=True, timeout=15)
        deadline = time.monotonic() + 18
        while time.monotonic() < deadline and not ready():
            time.sleep(.2)
        if not ready():
            diagnostic = {
                "rows": read_generation_status(home),
                "files": [p.name for p in home.iterdir()],
                "logs": [(p.name, p.read_text(errors="replace")[-3000:]) for p in tmp_path.glob("*.out")],
                "errors": [(p.name, p.read_text(errors="replace")[-3000:]) for p in tmp_path.glob("*.err")],
                "jobs": [subprocess.run(["launchctl", "print", f"{domain}/{label}"],
                                        capture_output=True, text=True, timeout=5).stdout[-1000:] for label in labels],
            }
            Path.home().joinpath(".hermes/cache/seamless-p3/slice1-active-probe.json").write_text(
                json.dumps(diagnostic, indent=2))
        assert ready(), "active probe diagnostics saved to notes dir"
        rows = read_generation_status(home)
        assert {row["label"] for row in rows} == set(labels)
        assert len({row["pid"] for row in rows}) == 2
        assert (home / "gateway.pid").exists()
        from gateway.control_socket import resolve_client_socket_path
        control_path = resolve_client_socket_path(home)
        assert control_path is not None and control_path.exists()
        assert (home / "gateway_state.json").exists()
        assert {row["state"] for row in rows} == {"ready"}
        assert sum(bool(row["leases"]) for row in rows) == 1
        active = next(row for row in rows if row["leases"])
        assert active["label"] == labels[0]
        standby = next(row for row in rows if not row["leases"])
        assert standby["label"] == labels[1]
        for row in rows:
            suffix = row["id"]
            assert (home / f"gateway.{suffix}.pid").is_file()
            assert (home / f"gateway_state.{suffix}.json").is_file()
            state = json.loads((home / f"gateway_state.{suffix}.json").read_text())
            assert state["state"] == "ready"
            assert Path(state["socket_path"]).exists()
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
        while time.monotonic() < deadline and list(home.glob("gateway.*.pid")):
            time.sleep(.2)
        assert not list(home.glob("gateway.*.pid"))
        assert not list(home.glob("gateway_state.*.json"))
        assert not read_generation_status(home)[0]["leases"]
