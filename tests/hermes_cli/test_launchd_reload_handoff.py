"""Run the deferred helper against fake launchctl; never touch a host service."""

import json
import subprocess
import sys
import textwrap
from types import SimpleNamespace

import pytest

from hermes_cli import gateway_launchd


@pytest.fixture
def run_helper(tmp_path, monkeypatch):
    real_run = subprocess.run
    submitted = []
    log = tmp_path / "reload.log"
    monkeypatch.setattr(gateway_launchd, "_launchd_reload_log_path", lambda: log)
    monkeypatch.setattr(gateway_launchd, "_mark_planned_gateway_restart", lambda _: None)
    monkeypatch.setattr(gateway_launchd, "_gw", lambda: SimpleNamespace(_append_launchd_reload_log=lambda *_: None))
    # An exited throwaway child gives the helper a real, already-dead old PID.
    with subprocess.Popen([sys.executable, "-c", "pass"]) as child:
        child.wait(timeout=10)
        old_pid = child.pid
    monkeypatch.setattr(gateway_launchd.subprocess, "run",
                        lambda args, **_: submitted.append(args) or subprocess.CompletedProcess(args, 0))
    fake = tmp_path / "bin"
    fake.mkdir()
    trace = tmp_path / "trace"
    state = tmp_path / "state.json"
    # Skip only the initial handoff delay. Timing regressions use real sleeps thereafter.
    (fake / "sleep").write_text(
        '#!/bin/sh\nprintf "sleep %s\\n" "$1" >> "$TRACE"\n'
        'if [ ! -e "$SLEEP_STATE" ]; then touch "$SLEEP_STATE"; '
        'elif [ "$REAL_WAIT" = 1 ]; then /bin/sleep "$1"; fi\n'
    )
    (fake / "launchctl").write_text(f"#!{sys.executable}\n" + textwrap.dedent(r"""
        import json
        import os
        import sys
        import time
        from pathlib import Path

        verb = sys.argv[1]
        with open(os.environ["TRACE"], "a") as trace:
            trace.write(verb + "\n")
        path = Path(os.environ["STATE"])
        state = json.loads(path.read_text()) if path.exists() else {
            "prints": 0, "lists": 0, "bootstrap_times": [], "registered": False,
        }
        rc = 0
        if verb == "print":
            state["prints"] += 1
            rc = 0 if (os.environ["NEVER_UNLOADS"] == "1" or
                       state["prints"] <= int(os.environ["LOADED_PROBES"])) else 1
        elif verb == "bootstrap":
            state["bootstrap_times"].append(time.monotonic())
            failure = int(os.environ["FAILURE"])
            elapsed = state["bootstrap_times"][-1] - state["bootstrap_times"][0]
            if failure == 64 or (failure and (len(state["bootstrap_times"]) == 1 or
                                             elapsed < float(os.environ["TRANSIENT_FOR"]))):
                rc = failure
            else:
                state["registered"] = True
        elif verb == "list":
            state["lists"] += 1
            if os.environ["NEVER_UNLOADS"] == "1":
                # The old label can still have a PID; that must not count as a successful reload.
                print('"PID" = 999;')
            elif state["registered"]:
                pid = os.environ["MISSING_PID"] if state["lists"] <= int(os.environ["PID_PROBES"]) else "123"
                if pid:
                    print('"PID" = ' + pid + ';')
        path.write_text(json.dumps(state))
        sys.exit(rc)
        """))
    for executable in fake.iterdir():
        executable.chmod(0o755)

    def execute(*, budget=30, failure=0, transient_for=0, loaded_probes=0, never_unloads=False,
                pid_probes=0, missing_pid="", real_wait=False):
        monkeypatch.setattr(gateway_launchd, "_launchd_reload_budget", lambda: float(budget))
        assert gateway_launchd._spawn_deferred_launchd_reload(
            domain="gui/501", label="test.hermes.gateway", target="gui/501/test.hermes.gateway",
            plist_path=tmp_path / "gateway.plist", gateway_pid=old_pid,
        )
        result = real_run(["/bin/bash", "-c", submitted[0][-1]], timeout=20, capture_output=True, text=True,
                          env={"PATH": f"{fake}:/usr/bin:/bin", "TRACE": str(trace), "STATE": str(state),
                               "SLEEP_STATE": str(tmp_path / "slept"), "REAL_WAIT": str(int(real_wait)),
                               "FAILURE": str(failure), "TRANSIENT_FOR": str(transient_for),
                               "LOADED_PROBES": str(loaded_probes), "NEVER_UNLOADS": str(int(never_unloads)),
                               "PID_PROBES": str(pid_probes), "MISSING_PID": missing_pid})
        assert result.returncode == 0, result.stderr
        return SimpleNamespace(events=trace.read_text().splitlines(), state=json.loads(state.read_text()),
                               log=log.read_text() if log.exists() else "")

    return execute


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("failure", [0, 5, 37, 64])
def test_helper_bootstraps_without_blind_delay_and_retries_only_transients(run_helper, failure):
    outcome = run_helper(failure=failure)
    events = outcome.events
    first_bootstrap = events.index("bootstrap")
    assert events[:first_bootstrap] == ["sleep 2", "bootout", "print"]
    if failure in (5, 37):
        assert events.count("bootstrap") == 2
        between = events[first_bootstrap + 1:events.index("bootstrap", first_bootstrap + 1)]
        assert "sleep 0.2" in between, "transient bootstrap failure must back off"
    else:
        assert events.count("bootstrap") == 1
        assert not any(event.startswith("sleep") for event in events[first_bootstrap + 1:])
    assert outcome.state["registered"] is (failure != 64)
    if failure == 64:
        assert "permanent bootstrap error" in outcome.log
        assert "bootstrap rc=64" in outcome.log
    else:
        assert "FAILED" not in outcome.log


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("failure, recovers", [(5, True), (37, True), (5, False)])
def test_helper_retries_transients_beyond_old_attempt_cap(run_helper, failure, recovers):
    # At this scaled budget the old cap gives up after ~0.8s; the failure lasts >=2s.
    outcome = run_helper(budget=8 if recovers else 4, failure=failure,
                         transient_for=4.0 if recovers else 60.0, real_wait=True)
    assert outcome.state["registered"] is recovers, outcome.log
    times = outcome.state["bootstrap_times"]
    assert times[-1] - times[0] >= 2.0
    backoffs = [float(event.split()[1]) for event in outcome.events[1:] if event.startswith("sleep")]
    assert backoffs[0] == 0.2
    assert max(backoffs) > backoffs[0]
    assert max(backoffs) <= 2.0
    if recovers:
        assert 2.0 in backoffs
        assert "FAILED" not in outcome.log
    else:
        assert "transient bootstrap errors exhausted reload budget" in outcome.log
        assert f"bootstrap rc={failure}" in outcome.log


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("never_unloads", [False, True])
def test_helper_waits_only_while_label_is_still_loaded(run_helper, never_unloads):
    outcome = run_helper(budget=4, loaded_probes=3, never_unloads=never_unloads, real_wait=never_unloads)
    if never_unloads:
        assert outcome.events.count("print") > 3
        assert "bootstrap" not in outcome.events
        assert "label never unloaded" in outcome.log
        assert "bootstrap rc=not-attempted" in outcome.log
        assert "permanent bootstrap error" not in outcome.log
        assert "bootstrapped with no positive PID" not in outcome.log
    else:
        first_bootstrap = outcome.events.index("bootstrap")
        assert outcome.events[:first_bootstrap] == ["sleep 2", "bootout"] + ["print", "sleep 0.2"] * 3 + ["print"]
        assert outcome.state["registered"]
        assert "FAILED" not in outcome.log


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("pid_probes, missing_pid", [(3, "-1"), (1000, ""), (1000, "0")])
def test_helper_waits_for_a_positive_pid_without_bootstrapping_again(run_helper, pid_probes, missing_pid):
    outcome = run_helper(budget=4, pid_probes=pid_probes, missing_pid=missing_pid, real_wait=pid_probes == 1000)
    assert outcome.events.count("bootstrap") == 1
    assert outcome.state["registered"]
    assert outcome.state["lists"] > 1
    if pid_probes == 1000:
        assert "bootstrapped with no positive PID" in outcome.log
        assert "bootstrap rc=0" in outcome.log
        assert "label never unloaded" not in outcome.log
        assert "permanent bootstrap error" not in outcome.log
    else:
        assert "FAILED" not in outcome.log
