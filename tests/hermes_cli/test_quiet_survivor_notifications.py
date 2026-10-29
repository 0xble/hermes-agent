"""Quiet one-shot follow-ups settle the same durable lease as interactive consumers."""

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from hermes_cli import quiet_single_query as qsq
import tools.process_registry as registry_module
from tools.process_registry import ProcessRegistry, ProcessSession


def _pending_result(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setattr(registry_module, "get_hermes_home", lambda: home)
    run_dir = home / "process-runs" / "proc_quiet"
    run_dir.mkdir(parents=True)
    (run_dir / "out.log").write_text("quiet result\n", encoding="utf-8")
    (run_dir / "exit").write_text("0\n", encoding="utf-8")
    producer = ProcessRegistry()
    producer._running["proc_quiet"] = ProcessSession(
        id="proc_quiet", command="echo quiet", pid=999999, task_id="task", session_key="session",
        survive_restart=True, notify_on_complete=True, log_path=str(run_dir / "out.log"),
        exit_path=str(run_dir / "exit"), delivered_path=str(run_dir / "delivered"),
    )
    producer._write_checkpoint()
    return home, run_dir


def test_quiet_success_acknowledges_before_fresh_successor(tmp_path, monkeypatch):
    home, run_dir = _pending_result(tmp_path, monkeypatch)
    # The one-shot exits: an in-process nonce would otherwise mask its unacknowledged lease.
    child = subprocess.run([sys.executable, "-c", textwrap.dedent(f"""
        import json
        from pathlib import Path
        import tools.process_registry as module
        from hermes_cli.quiet_single_query import continue_quiet_notify_completions
        module.get_hermes_home = lambda: Path({str(home)!r})
        registry = module.ProcessRegistry()
        module.process_registry = registry
        assert registry.recover_from_checkpoint() == 1
        assert registry.get('proc_quiet')._completion_event.wait(5)
        turns = []
        def run_turn(text):
            assert json.loads(Path({str(run_dir / 'delivered')!r}).read_text())['state'] == 'claimed'
            turns.append(text)
            return {{'final_response': 'received'}}
        result = continue_quiet_notify_completions('session', run_turn, linger_budget=0)
        assert result == {{'final_response': 'received'}} and len(turns) == 1
    """)], capture_output=True, text=True, timeout=20)
    assert child.returncode == 0, child.stderr
    successor = ProcessRegistry()
    successor.recover_from_checkpoint()
    session = successor.get("proc_quiet")
    if session is not None:
        assert session._completion_event.wait(5)
    assert successor.drain_notifications(session_key="session") == []
    assert json.loads((run_dir / "delivered").read_text(encoding="utf-8-sig"))["delivered"] is True
    assert json.loads((home / "processes.json").read_text(encoding="utf-8-sig")) == []


@pytest.mark.parametrize("failure", ["raise", "failed", "partial", "interrupted", "incomplete", "none"])
def test_quiet_failed_admission_releases_and_retries_same_result(tmp_path, monkeypatch, failure):
    home, run_dir = _pending_result(tmp_path, monkeypatch)
    registry = ProcessRegistry()
    monkeypatch.setattr(registry_module, "process_registry", registry)
    assert registry.recover_from_checkpoint() == 1
    assert registry.get("proc_quiet")._completion_event.wait(5)
    calls = []

    def fail(text):
        calls.append(text)
        assert json.loads((run_dir / "delivered").read_text(encoding="utf-8-sig"))["state"] == "claimed"
        if failure == "raise":
            raise RuntimeError("turn not admitted")
        return {
            "failed": {"failed": True}, "partial": {"partial": True},
            "interrupted": {"interrupted": True}, "incomplete": {"completed": False}, "none": None,
        }[failure]

    if failure == "raise":
        with pytest.raises(RuntimeError, match="not admitted"):
            qsq.continue_quiet_notify_completions("session", fail, linger_budget=0)
    else:
        qsq.continue_quiet_notify_completions("session", fail, linger_budget=0, max_rounds=1)
    assert not (run_dir / "delivered").exists()
    assert json.loads((home / "processes.json").read_text(encoding="utf-8-sig"))
    result = qsq.continue_quiet_notify_completions(
        "session", lambda text: calls.append(text) or {"final_response": "retried"}, linger_budget=0,
    )
    assert result == {"final_response": "retried"}
    assert len(calls) == 2 and calls[0] == calls[1]
    assert json.loads((run_dir / "delivered").read_text(encoding="utf-8-sig"))["delivered"] is True
    assert json.loads((home / "processes.json").read_text(encoding="utf-8-sig")) == []
