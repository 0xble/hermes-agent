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


def test_terminal_opt_in_reaches_wrapper_and_checkpoints_notification_route(tmp_path, monkeypatch):
    from types import SimpleNamespace
    import tools.process_registry as registry_module
    import tools.terminal_tool as terminal
    from tools.terminal_tool_background import _spawn
    from unittest.mock import Mock

    home = _home(monkeypatch, tmp_path)
    registry = ProcessRegistry()
    monkeypatch.setattr(registry_module, "process_registry", registry)
    plan = SimpleNamespace(
        env_type="local", cwd=str(tmp_path), effective_task_id="task", config={},
        effective_timeout=5, promoted_from_foreground_timeout=None, host_cwd=str(tmp_path),
    )
    monkeypatch.setattr(terminal, "_plan_execution", lambda *_a, **_k: plan)
    monkeypatch.setattr(terminal, "_acquire_env", lambda *_a: SimpleNamespace(env={}))
    monkeypatch.setattr(terminal, "_pre_exec_block", lambda *_a, **_k: None)
    monkeypatch.setattr(terminal, "_run_approval_guards", lambda *_a, **_k: SimpleNamespace(note=None))
    result = json.loads(terminal.terminal_tool(
        "sleep 0.3; printf 'SURVIVOR_TOOL_OK\\n'; exit 7", task_id="task", background=True,
        notify_on_complete=True, survive_restart=True, _host_local=True,
    ))
    assert result.get("survive_restart") is True, result
    session = registry.get(result["session_id"])
    assert session is not None and session.process is not None
    checkpoint = json.loads((home / "processes.json").read_text())
    assert checkpoint[0]["notify_on_complete"] is True
    assert checkpoint[0]["session_key"] == "task"
    event = registry.completion_queue.get(timeout=5)
    assert event["exit_code"] == 7
    assert "SURVIVOR_TOOL_OK" in event["output"]
    assert registry.poll(session.id)["exit_code"] == 7
    session.process.wait(timeout=3)

    # Existing sandbox backgrounds must not receive the local-only keyword.
    sandbox_registry = Mock()
    _spawn(sandbox_registry, command="echo ok", env=SimpleNamespace(), env_type="docker",
           effective_task_id="task", task_id="task", session_key="task",
           cwd=str(tmp_path), effective_pty=False)
    assert "survive_restart" not in sandbox_registry.spawn_via_env.call_args.kwargs
    for options in ({"background": False}, {"background": True, "pty": True}):
        rejected = json.loads(terminal.terminal_tool("echo no", survive_restart=True, _host_local=True, **options))
        assert rejected["status"] == "error"
        assert "non-PTY" in rejected["error"]


def test_real_wrapper_survives_producer_exit_and_reports_one_recovered_result(tmp_path, monkeypatch):
    import sys
    import textwrap

    home = _home(monkeypatch, tmp_path)
    # A separate producer exits without cleanup, as the old gateway does after
    # its survivor-excluding shutdown sweep. Only the real wrapper owns the job.
    producer = subprocess.run([sys.executable, "-c", textwrap.dedent(f"""
        import os
        from pathlib import Path
        import tools.process_registry as module
        module.get_hermes_home = lambda: Path({str(home)!r})
        registry = module.ProcessRegistry()
        session = registry.spawn_local(
            "sleep 0.4; printf 'SURVIVOR_BOOT_OK\\\\n'; exit 9",
            cwd={str(tmp_path)!r}, task_id="task", session_key="session", survive_restart=True,
        )
        session.notify_on_complete = True
        registry._write_checkpoint()
        assert registry.kill_all(source="gateway_shutdown") == 0
        print(session.id, flush=True)
        os._exit(0)
    """)], capture_output=True, text=True, timeout=10)
    assert producer.returncode == 0, producer.stderr
    session_id = producer.stdout.strip()
    checkpoint = (home / "processes.json").read_text()
    recovered = ProcessRegistry()
    assert recovered.recover_from_checkpoint() == 1
    event = recovered.completion_queue.get(timeout=5)
    assert event["session_id"] == session_id
    assert event["exit_code"] == 9
    assert "SURVIVOR_BOOT_OK" in event["output"]
    assert recovered.completion_queue.empty()
    # Even replaying the old checkpoint cannot duplicate the completion claim.
    (home / "processes.json").write_text(checkpoint)
    again = ProcessRegistry()
    assert again.recover_from_checkpoint() == 1
    time.sleep(0.2)
    assert again.completion_queue.empty()
