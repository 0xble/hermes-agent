"""A detached gateway must not outlive its routing truth-table case."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import psutil
import pytest

from tests.waiting import wait_until

from . import test_routing_truth_table as truth
from ._routing_helpers import RunResult, hermetic_env
from ._routing_processes import case_processes, stop_case_processes


@pytest.mark.platforms("posix")
@pytest.mark.live_system_guard_bypass  # daemon is reparented; cleanup proves its case-home ownership
@pytest.mark.parametrize("case_raises", [False, True], ids=["success", "failed-leg"])
def test_case_reaps_detached_gateway(tmp_path: Path, monkeypatch, case_raises: bool) -> None:
    """Exercise the real daemon spawner, not a child sharing run_hermes's process group."""
    case = next(c for c in truth.CASES if c.id == "profile_override_routes_per_profile")
    root = tmp_path / case.id
    child = None

    def launch(argv, home, **_kwargs):
        nonlocal child
        if argv[:2] == ["-p", "work"]:
            # The finite CLI now uses --client-owned. Exercise the real legacy idle-exit
            # spawner explicitly so this test retains the historical detached-process shape.
            # Force the plain detached spawn fallback: on macOS the spawner otherwise prefers
            # a one-off launchd job, and this regression's subject is the session escape, not
            # launchd. Cleanup keys on HERMES_HOME, not on how the owner was detached.
            code = (
                "import sys,time; from pathlib import Path; "
                "import hermes_cli.gateway_runtime_start as start; "
                "start.start_gateway_job = lambda *a, **k: None; "
                "p=start.spawn_unmanaged_gateway(Path(sys.argv[1]),deadline=time.monotonic()+30,idle_exit=True); "
                "print(p.pid)"
            )
            result = subprocess.run([sys.executable, "-c", code, str(home / ".hermes")],
                                    env=hermetic_env(home, proxy=_kwargs.get("proxy")), cwd=home,
                                    capture_output=True, text=True, check=True, timeout=45)
            child = psutil.Process(int(result.stdout.strip().splitlines()[-1]))
            wait_until(lambda: child in case_processes(root), timeout=15,
                       what="detached gateway's explicit synthetic HERMES_HOME")
            assert os.getpgid(child.pid) == child.pid
            assert child.ppid() != os.getpid()

            def ready():
                status = home / ".hermes" / "gateway_state.json"
                try:
                    return json.loads(status.read_text()).get("gateway_state") == "running"
                except (FileNotFoundError, json.JSONDecodeError):
                    return False

            wait_until(ready, timeout=120, what="detached gateway runtime to publish running")
            if case_raises:
                raise RuntimeError("scripted failed leg")
        return RunResult(argv, 0, "", "", 0)

    monkeypatch.setattr(truth, "run_hermes", launch)
    try:
        if case_raises:
            with pytest.raises(RuntimeError, match="scripted failed leg"):
                truth._run_case(case, tmp_path)
        else:
            truth._run_case(case, tmp_path)
        assert child is not None, "profile-override leg did not start its detached gateway"
        survivors = case_processes(root)
        details = [{"pid": p.pid, "ppid": p.ppid(), "pgid": os.getpgid(p.pid),
                    "HERMES_HOME": p.environ()["HERMES_HOME"], "argv": p.cmdline()} for p in survivors]
        assert not survivors, f"processes survived routing case teardown: {json.dumps(details)}"
    finally:
        # The survivor assertion runs first; even a red regression must not itself leak.
        stop_case_processes(root)


@pytest.mark.platforms("posix")
def test_cleanup_without_pid_record_keeps_adjacent_case(tmp_path: Path) -> None:
    """An importing owner has no PID file; an adjacent case is never cleanup authority."""
    root, sibling_root = tmp_path / "case", tmp_path / "case-other"
    processes = []
    try:
        for location in (root, sibling_root):
            home = location / "home"
            (home / ".hermes").mkdir(parents=True)
            proc = subprocess.Popen([sys.executable, "-c", "import threading; threading.Event().wait(120)"],
                                    env=hermetic_env(home), cwd=home, start_new_session=True)
            processes.append(proc)
            wait_until(lambda: any(p.pid == proc.pid for p in case_processes(location)), timeout=15,
                       what="synthetic importing owner's environment")
        stop_case_processes(root)
        assert processes[0].wait(timeout=15) is not None
        assert processes[1].poll() is None
        assert not case_processes(root)
    finally:
        for proc in processes:
            if proc.poll() is None:
                owner = psutil.Process(proc.pid)
                home = owner.environ().get("HERMES_HOME")
                assert home and Path(home).resolve().is_relative_to(tmp_path.resolve())
                owner.kill()
            proc.wait(timeout=15)
