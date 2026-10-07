"""The launchd reload must not race the old gateway's teardown.

Field timeline (2026-10-06, three updates in a row, all reported as failed):
``bootout`` SIGTERMed the old gateway, whose teardown took ~38s (interrupt agents, kill 17 tool
subprocesses, disconnect adapters). The reload helper waited only the 30s reload budget, bootstrapped
anyway, and the new gateway exited at once with "A gateway already owns this host". launchd then held
the relaunch for ``ThrottleInterval`` (30s), so the replacement started ~30s after the old pid was gone,
past the 20s supervision window. The update reported ``ai.hermes.gateway`` as not restarted although
the new release was serving.
"""

from __future__ import annotations

import plistlib
import re
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway.restart import LAUNCHD_GUI_EXIT_TIMEOUT_CLAMP_S
from hermes_cli import gateway_launchd


def test_reload_waits_for_the_old_gateway_until_launchd_must_have_killed_it(monkeypatch):
    """A zero drain budget leaves a 30s reload budget, shorter than the 38s field teardown."""
    monkeypatch.setattr(gateway_launchd, "_launchd_reload_budget", lambda: 30.0)

    assert gateway_launchd._launchd_old_gateway_exit_budget() > LAUNCHD_GUI_EXIT_TIMEOUT_CLAMP_S


def test_a_long_drain_budget_still_wins(monkeypatch):
    monkeypatch.setattr(gateway_launchd, "_launchd_reload_budget", lambda: 600.0)

    assert gateway_launchd._launchd_old_gateway_exit_budget() == 600.0


def test_deferred_helper_waits_the_exit_budget_not_the_bootstrap_budget(tmp_path, monkeypatch):
    submitted = []
    monkeypatch.setattr(gateway_launchd, "_launchd_reload_budget", lambda: 30.0)
    monkeypatch.setattr(gateway_launchd, "_launchd_reload_log_path", lambda: tmp_path / "reload.log")
    monkeypatch.setattr(gateway_launchd, "_gw", lambda: SimpleNamespace(_append_launchd_reload_log=lambda *_: None))
    monkeypatch.setattr(gateway_launchd.subprocess, "run",
                        lambda args, **_: submitted.append(args) or subprocess.CompletedProcess(args, 0))

    assert gateway_launchd._spawn_deferred_launchd_reload(
        domain="gui/501", label="ai.hermes.gateway", target="gui/501/ai.hermes.gateway",
        plist_path=tmp_path / "ai.hermes.gateway.plist", gateway_pid=4242,
    )
    script = submitted[0][-1]
    exit_budget = int(gateway_launchd._launchd_old_gateway_exit_budget())
    assert f"_wait_deadline=$(($(date +%s) + {exit_budget}))" in script
    assert f"_deadline=$(($(date +%s) + 30))" in script  # bootstrap retries keep their own budget


def test_supervision_window_outlasts_the_generated_throttle_interval(monkeypatch, tmp_path):
    """A replacement that exits early is relaunched one ThrottleInterval later, a healthy outcome."""
    gw = gateway_launchd._gw()
    monkeypatch.setattr(gw, "get_hermes_home", lambda: tmp_path)
    body = plistlib.loads(gateway_launchd.generate_launchd_plist().encode())

    assert body["ThrottleInterval"] == gateway_launchd.LAUNCHD_THROTTLE_INTERVAL_S
    assert gateway_launchd.LAUNCHD_SUPERVISION_VERIFY_TIMEOUT > body["ThrottleInterval"]


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("listing, matches", [
    ('{\n\t"Label" = "ai.hermes.gateway";\n\t"LastExitStatus" = 256;\n\t"PID" = 21305;\n};\n', True),
    ('{\n\t"Label" = "ai.hermes.gateway";\n\t"LastExitStatus" = 256;\n};\n', False),
])
def test_helper_supervision_probe_matches_real_launchctl_output(tmp_path, monkeypatch, listing, matches):
    """The helper only stops retrying bootstrap once this probe sees a positive PID."""
    real_run = subprocess.run
    submitted = []
    monkeypatch.setattr(gateway_launchd, "_launchd_reload_budget", lambda: 30.0)
    monkeypatch.setattr(gateway_launchd, "_launchd_reload_log_path", lambda: tmp_path / "reload.log")
    monkeypatch.setattr(gateway_launchd, "_gw", lambda: SimpleNamespace(_append_launchd_reload_log=lambda *_: None))
    monkeypatch.setattr(gateway_launchd.subprocess, "run",
                        lambda args, **_: submitted.append(args) or subprocess.CompletedProcess(args, 0))
    gateway_launchd._spawn_deferred_launchd_reload(
        domain="gui/501", label="ai.hermes.gateway", target="gui/501/ai.hermes.gateway",
        plist_path=tmp_path / "ai.hermes.gateway.plist", gateway_pid=4242,
    )
    probe = re.search(r"if (launchctl list .*?); then break", submitted[0][-1]).group(1)
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "launchctl").write_text(f"#!/bin/sh\ncat <<'OUT'\n{listing}OUT\n", encoding="utf-8")
    (fake / "launchctl").chmod(0o755)
    result = real_run(["/bin/bash", "-c", f"if {probe}; then echo MATCH; fi"],
                            capture_output=True, text=True, env={"PATH": f"{fake}:/usr/bin:/bin"})
    assert (result.stdout.strip() == "MATCH") is matches
