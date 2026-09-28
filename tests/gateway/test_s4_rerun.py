"""Disposable S4.2 feasibility probes, not a replacement for router/executor E2E."""
from pathlib import Path

import pytest

from spikes.s4_rerun.probe import run_crash_windows, run_scoped_lock_conflict


def test_twenty_randomized_accepted_send_crashes_are_held(tmp_path):
    evidence = tmp_path / "evidence"
    runs = run_crash_windows(tmp_path, evidence, count=20)
    assert len(runs) == 20
    assert all(run["accepted_sends"] == 1 and run["recovery_sends"] == 0 and
               run["ambiguous_rows"] == 1 and run["duplicate_sends"] == 0
               for run in runs), runs
    assert len(list(evidence.glob("crash-*.json"))) == 20


@pytest.mark.spawns_gateway_lookalike
def test_native_scoped_lock_rejects_second_process(tmp_path):
    receipt = run_scoped_lock_conflict(tmp_path)
    assert receipt["holder_acquired"] and not receipt["contender_acquired"]
    assert receipt["after_holder_exit_acquired"]
