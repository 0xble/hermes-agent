"""Restart-surviving local background process coverage."""

import json
import subprocess
import time

from tools.process_registry import ProcessRegistry, ProcessSession


def _home(monkeypatch, tmp_path):
    import tools.process_registry as registry_module

    home = tmp_path / "home"
    monkeypatch.setattr(registry_module, "get_hermes_home", lambda: home)
    return home


def _checkpoint_survivor(registry, home, *, pid, session_id="proc_survivor", log_text="", exit_code=None):
    run_dir = home / "process-runs" / session_id
    run_dir.mkdir(parents=True)
    log_path = run_dir / "out.log"
    exit_path = run_dir / "exit"
    delivered_path = run_dir / "delivered"
    log_path.write_text(log_text, encoding="utf-8")
    if exit_code is not None:
        exit_path.write_text(f"{exit_code}\n", encoding="utf-8")
    session = ProcessSession(
        id=session_id,
        command="survivor-test",
        task_id="task",
        session_key="session",
        pid=pid,
        host_start_time=registry._safe_host_start_time(pid),
        survive_restart=True,
        log_path=str(log_path),
        exit_path=str(exit_path),
        delivered_path=str(delivered_path),
        notify_on_complete=True,
    )
    registry._running[session.id] = session
    registry._write_checkpoint()
    return session


def test_gateway_shutdown_sweep_skips_survive_restart_sessions(tmp_path, monkeypatch):
    home = _home(monkeypatch, tmp_path)
    registry = ProcessRegistry()
    session = registry.spawn_local(
        "sleep 5", cwd=str(tmp_path), task_id="task", survive_restart=True,
    )
    assert session.survive_restart is True
    assert registry.kill_all(source="gateway_shutdown") == 0
    assert session.process.poll() is None

    assert registry.kill_all(source="gateway_stop_all") == 1
    session.process.wait(timeout=3)
    assert (home / "process-runs" / session.id).exists()


def test_recovered_survivor_emits_single_completion_with_exit_code(tmp_path, monkeypatch):
    home = _home(monkeypatch, tmp_path)
    producer = ProcessRegistry()
    child = subprocess.Popen(["sh", "-c", "sleep 1"], start_new_session=True)
    session = _checkpoint_survivor(producer, home, pid=child.pid, log_text="SURVIVOR_OK\n")

    recovered = ProcessRegistry()
    assert recovered.recover_from_checkpoint() == 1
    assert recovered.recover_from_checkpoint() == 0
    (home / "process-runs" / session.id / "exit").write_text("0\n", encoding="utf-8")
    deadline = time.monotonic() + 5
    while recovered.completion_queue.empty() and time.monotonic() < deadline:
        time.sleep(0.02)
    event = recovered.completion_queue.get(timeout=1)
    assert event["session_id"] == session.id
    assert event["exit_code"] == 0
    assert "SURVIVOR_OK" in event["output"]
    time.sleep(0.1)
    assert recovered.completion_queue.empty()
    assert json.loads((home / "process-runs" / session.id / "delivered").read_text()) == {"delivered": True}
    child.wait(timeout=3)


def test_recovered_survivor_gone_without_exit_file_reports_lost_once(tmp_path, monkeypatch):
    home = _home(monkeypatch, tmp_path)
    producer = ProcessRegistry()
    child = subprocess.Popen(["sh", "-c", "exit 0"], start_new_session=True)
    child.wait(timeout=3)
    _checkpoint_survivor(producer, home, pid=child.pid, log_text="partial output\n")

    recovered = ProcessRegistry()
    assert recovered.recover_from_checkpoint() == 1
    deadline = time.monotonic() + 5
    while recovered.completion_queue.empty() and time.monotonic() < deadline:
        time.sleep(0.02)
    event = recovered.completion_queue.get(timeout=1)
    assert event["completion_reason"] == "lost"
    assert event["exit_code"] is None
    assert "partial output" in event["output"]
    time.sleep(0.1)
    assert recovered.completion_queue.empty()


def test_stop_all_source_still_kills_survivors(tmp_path, monkeypatch):
    _home(monkeypatch, tmp_path)
    registry = ProcessRegistry()
    session = registry.spawn_local(
        "sleep 5", cwd=str(tmp_path), task_id="task", survive_restart=True,
    )
    assert registry.kill_all(source="gateway_stop_all") == 1
    session.process.wait(timeout=3)
    assert session.process.poll() is not None
