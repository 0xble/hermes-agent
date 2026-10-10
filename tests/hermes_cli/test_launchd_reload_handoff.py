"""Run the deferred helper against fake launchctl; never touch a host service."""

import subprocess
import sys
from types import SimpleNamespace

import pytest

from hermes_cli import gateway_launchd


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("failure", [0, 5, 37, 64])
def test_helper_bootstraps_without_blind_delay_and_retries_only_transients(tmp_path, monkeypatch, failure):
    real_run = subprocess.run
    submitted = []
    monkeypatch.setattr(gateway_launchd, "_launchd_reload_log_path", lambda: tmp_path / "reload.log")
    monkeypatch.setattr(gateway_launchd, "_launchd_reload_budget", lambda: 30.0)
    monkeypatch.setattr(gateway_launchd, "_mark_planned_gateway_restart", lambda _: None)
    monkeypatch.setattr(gateway_launchd, "_gw", lambda: SimpleNamespace(_append_launchd_reload_log=lambda *_: None))
    # An exited throwaway child gives the helper a real, already-dead old PID.
    with subprocess.Popen([sys.executable, "-c", "pass"]) as child:
        child.wait(timeout=10)
        old_pid = child.pid
    monkeypatch.setattr(gateway_launchd.subprocess, "run",
                        lambda args, **_: submitted.append(args) or subprocess.CompletedProcess(args, 0))
    assert gateway_launchd._spawn_deferred_launchd_reload(
        domain="gui/501", label="test.hermes.gateway", target="gui/501/test.hermes.gateway",
        plist_path=tmp_path / "gateway.plist", gateway_pid=old_pid,
    )
    script = submitted[0][-1]
    handoff = script.split("sleep 1; done;", 1)[-1].split("launchctl bootstrap", 1)[0]
    assert "sleep 1;" not in handoff, "unconditional post-exit delay remains"
    assert "$_attempts -ge 16" in script  # unchanged 30s/2s maximum retry count
    fake = tmp_path / "bin"
    fake.mkdir()
    trace = tmp_path / "trace"
    (fake / "sleep").write_text('#!/bin/sh\nprintf "sleep %s\\n" "$1" >> "$TRACE"\n')
    (fake / "launchctl").write_text(
        '#!/bin/sh\nprintf "%s\\n" "$1" >> "$TRACE"\n'
        'case "$1" in\n'
        'print) exit 1;;\n'
        'bootstrap) if [ ! -e "$STATE" ]; then touch "$STATE"; exit "$FAILURE"; fi;;\n'
        'list) if [ -e "$STATE" ] && [ "$FAILURE" != 64 ]; then printf \'"PID" = 123;\\n\'; fi;;\n'
        'esac\n'
    )
    for executable in fake.iterdir():
        executable.chmod(0o755)
    result = real_run(["/bin/bash", "-c", script], timeout=10, capture_output=True, text=True,
                      env={"PATH": f"{fake}:/usr/bin:/bin", "TRACE": str(trace),
                           "STATE": str(tmp_path / "state"), "FAILURE": str(failure)})
    assert result.returncode == 0, result.stderr
    events = trace.read_text().splitlines()
    first_bootstrap = events.index("bootstrap")
    assert events[:first_bootstrap] == ["sleep 2", "bootout", "print"]
    if failure in (5, 37):
        assert events.count("bootstrap") == 2
        between = events[first_bootstrap + 1:events.index("bootstrap", first_bootstrap + 1)]
        assert "sleep 0.2" in between, "transient bootstrap failure must back off"
    else:
        assert events.count("bootstrap") == 1
        assert not any(event.startswith("sleep") for event in events[first_bootstrap + 1:])


@pytest.mark.platforms("posix")
def test_helper_waits_only_while_label_is_still_loaded(tmp_path, monkeypatch):
    submitted = []
    monkeypatch.setattr(gateway_launchd, "_launchd_reload_budget", lambda: 30.0)
    monkeypatch.setattr(gateway_launchd, "_launchd_reload_log_path", lambda: tmp_path / "reload.log")
    monkeypatch.setattr(gateway_launchd, "_mark_planned_gateway_restart", lambda _: None)
    monkeypatch.setattr(gateway_launchd, "_gw", lambda: SimpleNamespace(_append_launchd_reload_log=lambda *_: None))
    monkeypatch.setattr(gateway_launchd.subprocess, "run",
                        lambda args, **_: submitted.append(args) or subprocess.CompletedProcess(args, 0))
    gateway_launchd._spawn_deferred_launchd_reload(
        domain="gui/501", label="test.hermes.gateway", target="gui/501/test.hermes.gateway",
        plist_path=tmp_path / "gateway.plist", gateway_pid=999999999,
    )
    # The label probe must precede bootstrap, with a bounded, conditional wait.
    script = submitted[0][-1]
    assert "while launchctl print gui/501/test.hermes.gateway" in script
    before_bootstrap = script.split("launchctl bootstrap", 1)[0]
    assert "sleep 0.2; done" in before_bootstrap
    assert "$_deadline" in before_bootstrap
