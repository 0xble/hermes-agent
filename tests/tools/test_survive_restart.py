"""Restart-surviving local background process coverage."""

import json
import subprocess
import time

import pytest

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


@pytest.mark.parametrize("command,expected", [
    ("printf 'plain-ok\\n'", "plain-ok"),
    ("printf 'comment-ok\\n' # trailing comment", "comment-ok"),
    ("cat <<EOF\nheredoc-ok\nEOF", "heredoc-ok"),
], ids=["plain", "comment", "heredoc"])
def test_survivor_wrapper_preserves_plain_comments_and_heredocs(tmp_path, monkeypatch, command, expected):
    _home(monkeypatch, tmp_path)
    registry = ProcessRegistry()
    session = registry.spawn_local(command, cwd=str(tmp_path), task_id="task", survive_restart=True)
    session.notify_on_complete = True
    registry._write_checkpoint()
    event = registry.completion_queue.get(timeout=5)
    assert event["completion_reason"] == "exited", event
    assert event["exit_code"] == 0, event
    assert expected in event["output"], event
    assert session.process is not None
    session.process.wait(timeout=3)


def test_survivor_claim_without_delivery_is_replayed_after_restart(tmp_path, monkeypatch):
    home = _home(monkeypatch, tmp_path)
    producer = ProcessRegistry()
    session = _checkpoint_survivor(producer, home, pid=999999, exit_code=0, log_text="replayed\\n")
    assert producer._claim_survivor_delivery(session) is True
    # Also cover a persisted in-flight claim left by an interrupted predecessor.
    from pathlib import Path
    Path(session.delivered_path).write_text('{"state": "claimed"}', encoding="utf-8")
    successor = ProcessRegistry()
    assert successor.recover_from_checkpoint() == 1
    event = successor.completion_queue.get(timeout=5)
    assert event["session_id"] == session.id
    assert event["completion_reason"] == "exited"
    assert "replayed" in event["output"]


def test_survivor_completed_but_undrained_checkpoint_replays_once(tmp_path, monkeypatch):
    home = _home(monkeypatch, tmp_path)
    producer = ProcessRegistry()
    session = _checkpoint_survivor(producer, home, pid=999999, exit_code=0, log_text="durable result\n")
    producer._survivor_poller_loop(session)
    assert producer.completion_queue.qsize() == 1
    assert not (home / "process-runs" / session.id / "delivered").exists()
    assert json.loads((home / "processes.json").read_text(encoding="utf-8-sig"))[0]["session_id"] == session.id
    successor = ProcessRegistry()
    assert successor.recover_from_checkpoint() == 1
    recovered = successor.get(session.id)
    assert recovered is not None and recovered._completion_event.wait(5)
    assert successor.recover_from_checkpoint() == 0
    events = successor.drain_notifications(session_key="session")
    assert len(events) == 1
    assert events[0][0]["session_id"] == session.id
    assert successor.drain_notifications(session_key="session") == []
    assert not successor.is_completion_consumed(session.id)
    successor.acknowledge_survivor_completion(session.id)
    assert not successor.is_completion_consumed(session.id)
    assert json.loads((home / "processes.json").read_text(encoding="utf-8-sig")) == []


def test_survivor_concurrent_successor_drains_deduplicate_the_notice(tmp_path, monkeypatch):
    home = _home(monkeypatch, tmp_path)
    producer = ProcessRegistry()
    session = _checkpoint_survivor(producer, home, pid=999999, exit_code=0, log_text="one result\n")
    first, second = ProcessRegistry(), ProcessRegistry()
    assert first.recover_from_checkpoint() == 1
    assert second.recover_from_checkpoint() == 1
    for successor in (first, second):
        recovered = successor.get(session.id)
        assert recovered is not None and recovered._completion_event.wait(5)
    assert len(first.drain_notifications(session_key="session")) == 1
    first.acknowledge_survivor_completion(session.id)
    assert second.drain_notifications(session_key="session") == []


def test_survivor_log_reads_are_bounded_and_unchanged_polls_do_not_read(tmp_path, monkeypatch):
    from pathlib import Path
    home = _home(monkeypatch, tmp_path)
    registry = ProcessRegistry()
    session = _checkpoint_survivor(registry, home, pid=999999, log_text="x" * 100_000)
    session.max_output_chars = 100
    original_open = Path.open
    reads = []

    class BoundedFile:
        def __init__(self, stream):
            self.stream = stream
        def __enter__(self):
            return self
        def __exit__(self, *args):
            self.stream.close()
        def seek(self, offset):
            return self.stream.seek(offset)
        def read(self, size=-1):
            assert 0 <= size <= session.max_output_chars
            reads.append(size)
            return self.stream.read(size)

    def instrumented_open(path, *args, **kwargs):
        stream = original_open(path, *args, **kwargs)
        return BoundedFile(stream) if str(path) == session.log_path and args == ("rb",) else stream

    monkeypatch.setattr(Path, "open", instrumented_open)
    registry._read_survivor_log(session)
    registry._read_survivor_log(session)
    assert reads == [100]
    with original_open(Path(session.log_path), "a") as log:
        log.write("y" * 10_000 + "TAIL")
    registry._read_survivor_log(session)
    assert reads == [100, 100]
    assert len(session.output_buffer) <= 100
    assert session.output_buffer.endswith("TAIL")


def test_survive_restart_rejected_in_delegated_children(tmp_path, monkeypatch):
    from agent.delegation_context import delegated_child_context
    from tools.terminal_tool_background import spawn_background_process
    from unittest.mock import Mock
    import tools.process_registry as module

    registry = Mock()
    monkeypatch.setattr(module, "process_registry", registry)
    with delegated_child_context("child"):
        result = json.loads(spawn_background_process(
            command="echo never", env_type="local", env=None, cwd=str(tmp_path),
            effective_task_id="task", task_id="task", session_key="session",
            workdir=None, effective_pty=False, notify_on_complete=True,
            watch_patterns=None, approval_note=None, pty_disabled_reason=None,
            survive_restart=True,
        ))
    assert result["status"] == "error"
    assert "delegated child" in result["error"]
    registry.spawn_local.assert_not_called()


def test_survivor_delivered_marker_suppresses_restart_replay(tmp_path, monkeypatch):
    home = _home(monkeypatch, tmp_path)
    producer = ProcessRegistry()
    session = _checkpoint_survivor(producer, home, pid=999999, exit_code=0, log_text="delivered\\n")
    assert producer._claim_survivor_delivery(session) is True
    producer._mark_survivor_delivered(session)
    successor = ProcessRegistry()
    assert successor.recover_from_checkpoint() == 1
    time.sleep(0.15)
    assert successor.completion_queue.empty()


def test_survivor_run_dir_pruned_after_delivered_session_ages_out(tmp_path, monkeypatch):
    home = _home(monkeypatch, tmp_path)
    registry = ProcessRegistry()
    session = _checkpoint_survivor(registry, home, pid=999999, exit_code=0)
    from tools.process_registry import FINISHED_TTL_SECONDS
    session.started_at = time.time() - FINISHED_TTL_SECONDS - 1
    assert registry._claim_survivor_delivery(session) is True
    registry._mark_survivor_delivered(session)
    session.exited = True
    registry._running.pop(session.id, None)
    registry._finished[session.id] = session
    registry._prune_if_needed()
    assert not (home / "process-runs" / session.id).exists()


def test_survivor_delivered_artifacts_are_pruned_on_boot_after_ttl(tmp_path, monkeypatch):
    import os
    from pathlib import Path
    from tools.process_registry import FINISHED_TTL_SECONDS

    home = _home(monkeypatch, tmp_path)
    producer = ProcessRegistry()
    session = _checkpoint_survivor(producer, home, pid=999999, exit_code=0)
    assert producer._claim_survivor_delivery(session) is True
    producer._mark_survivor_delivered(session)
    marker = Path(session.delivered_path)
    expired = time.time() - FINISHED_TTL_SECONDS - 1
    os.utime(marker, (expired, expired))
    (home / "processes.json").unlink()
    successor = ProcessRegistry()
    assert successor.recover_from_checkpoint() == 0
    assert not marker.parent.exists()


@pytest.mark.parametrize("notify", [True, False], ids=["agent", "text-watcher"])
def test_survivor_pending_artifacts_and_results_are_not_pruned(tmp_path, monkeypatch, notify):
    from pathlib import Path
    from tools.process_registry import FINISHED_TTL_SECONDS

    home = _home(monkeypatch, tmp_path)
    registry = ProcessRegistry()
    session = _checkpoint_survivor(registry, home, pid=999999, exit_code=0)
    session.notify_on_complete = notify
    session.watcher_interval = 1
    session.watcher_platform = "telegram"
    session.watcher_chat_id = "123"
    session.started_at = time.time() - FINISHED_TTL_SECONDS - 1
    registry._survivor_poller_loop(session)
    registry._prune_if_needed()
    assert Path(session.log_path).parent.exists()
    assert session.id in registry._finished
    assert json.loads((home / "processes.json").read_text(encoding="utf-8-sig"))[0]["session_id"] == session.id


def test_survivor_artifact_sweeps_are_throttled_per_home(tmp_path, monkeypatch):
    import os
    from pathlib import Path
    from tools.process_registry import FINISHED_TTL_SECONDS

    home = _home(monkeypatch, tmp_path)
    run_dir = home / "process-runs" / "proc_aged"
    run_dir.mkdir(parents=True)
    marker = run_dir / "delivered"
    marker.write_text('{"state": "delivered", "delivered": true}', encoding="utf-8")
    registry = ProcessRegistry()
    clock = [100.0]
    monkeypatch.setattr("tools.process_registry_survival.time.monotonic", lambda: clock[0])
    registry._prune_if_needed()
    assert run_dir.exists()  # Not old enough on the first sweep.
    expired = time.time() - FINISHED_TTL_SECONDS - 1
    os.utime(marker, (expired, expired))

    scans = []
    original_iterdir = Path.iterdir

    def iterdir(path):
        if path == run_dir.parent:
            scans.append(path)
        return original_iterdir(path)

    monkeypatch.setattr(Path, "iterdir", iterdir)
    clock[0] += 59
    registry._prune_if_needed()
    assert run_dir.exists()
    assert not scans
    # A different profile still gets its first sweep immediately.
    second_home = tmp_path / "second-home"
    second_run = second_home / "process-runs" / "proc_second"
    second_run.mkdir(parents=True)
    second_marker = second_run / "delivered"
    delivered_text = marker.read_text(encoding="utf-8-sig")
    second_marker.write_text(delivered_text, encoding="utf-8")
    os.utime(second_marker, (expired, expired))
    monkeypatch.setattr("tools.process_registry.get_hermes_home", lambda: second_home)
    registry._prune_if_needed()
    assert not second_run.exists()
    monkeypatch.setattr("tools.process_registry.get_hermes_home", lambda: home)
    clock[0] += 1
    registry._prune_if_needed()
    assert scans == [run_dir.parent]
    assert not run_dir.exists()


def test_survivor_spawn_failure_prunes_run_dir(tmp_path, monkeypatch):
    import tools.process_registry as registry_module

    home = _home(monkeypatch, tmp_path)
    monkeypatch.setattr(registry_module.subprocess, "Popen", lambda *args, **kwargs: (_ for _ in ()).throw(OSError("boom")))
    registry = ProcessRegistry()
    try:
        registry.spawn_local("echo never", cwd=str(tmp_path), task_id="task", survive_restart=True)
    except OSError:
        pass
    else:
        raise AssertionError("spawn_local should propagate the spawn failure")
    assert not list((home / "process-runs").glob("*/"))


@pytest.mark.live_system_guard_bypass  # the wrapper's snapshotted child is SIGKILLed after reparenting to init
def test_gateway_shutdown_sweep_skips_survive_restart_sessions(tmp_path, monkeypatch):
    home = _home(monkeypatch, tmp_path)
    registry = ProcessRegistry()
    session = registry.spawn_local(
        "exec sleep 5", cwd=str(tmp_path), task_id="task", survive_restart=True,
    )
    assert session.survive_restart is True
    assert registry.kill_all(source="gateway_shutdown") == 0
    assert session.process.poll() is None

    assert registry.kill_all(source="cli.stop") == 1
    session.process.wait(timeout=3)
    assert (home / "process-runs" / session.id).exists()


@pytest.mark.live_system_guard_bypass  # the wrapper's snapshotted child is SIGKILLed after reparenting to init
def test_killed_survivor_replays_as_killed_after_restart(tmp_path, monkeypatch):
    home = _home(monkeypatch, tmp_path)
    producer = ProcessRegistry()
    session = producer.spawn_local(
        "exec sleep 30", cwd=str(tmp_path), task_id="task", survive_restart=True,
    )
    session.notify_on_complete = True
    producer._write_checkpoint()

    assert producer.kill_all(source="cli.stop") == 1
    session.process.wait(timeout=3)

    successor = ProcessRegistry()
    assert successor.recover_from_checkpoint() == 1
    event = successor.completion_queue.get(timeout=5)
    assert event["session_id"] == session.id
    assert event["completion_reason"] == "killed"
    assert event["termination_source"] == "cli.stop"
    assert event["exit_code"] == -15
    assert (home / "process-runs" / session.id / "exit").exists()


def test_scoped_survivor_spawn_gets_user_bus_env(monkeypatch, tmp_path):
    from types import SimpleNamespace
    from unittest.mock import MagicMock
    import tools.process_registry as registry_module

    registry = ProcessRegistry()
    seen = {}
    monkeypatch.setattr(
        registry_module,
        "restart_safe_gateway_child_argv",
        lambda *args, **kwargs: SimpleNamespace(mode="scoped", argv=["systemd-run", "--user"]),
    )

    def add_bus_env(env):
        seen["base"] = dict(env)
        result = dict(env)
        result["DBUS_SESSION_BUS_ADDRESS"] = "unix:path=/run/user/123/bus"
        return result

    monkeypatch.setattr(registry_module, "systemd_user_bus_env", add_bus_env)
    fake_proc = MagicMock(pid=4242)
    monkeypatch.setattr(registry_module.subprocess, "Popen", MagicMock(return_value=fake_proc))
    monkeypatch.setattr(registry, "_safe_host_start_time", lambda _pid: 1)
    monkeypatch.setattr(registry, "_track_started", lambda *args, **kwargs: None)

    session = registry.spawn_local("echo scoped", cwd=str(tmp_path), task_id="task", survive_restart=True)

    assert session.systemd_unit == f"hermes-worker-{session.id}.scope"
    assert seen["base"]
    assert registry_module.subprocess.Popen.call_args.kwargs["env"]["DBUS_SESSION_BUS_ADDRESS"] == \
        "unix:path=/run/user/123/bus"


def test_shutdown_stops_survivor_poller_before_it_claims_delivery(tmp_path, monkeypatch):
    """A shutdown fence must beat the old gateway's poller/marker race."""
    home = _home(monkeypatch, tmp_path)
    registry = ProcessRegistry()
    session = _checkpoint_survivor(registry, home, pid=999999, exit_code=0)
    session.process = None
    monkeypatch.setattr(registry, "_detached_host_fate", lambda *_args: "running")
    claimed = []
    monkeypatch.setattr(registry, "_survivor_delivery_pending", lambda _session: claimed.append(True) or True)

    registry.stop_survivor_pollers()
    registry._survivor_poller_loop(session)

    assert claimed == []
    assert session.exited is False


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
    assert not (home / "process-runs" / session.id / "delivered").exists()
    assert recovered.claim_survivor_completion(session.id) is True
    recovered.acknowledge_survivor_completion(session.id)
    assert json.loads((home / "process-runs" / session.id / "delivered").read_text(encoding="utf-8-sig"))["delivered"] is True
    snapshot = recovered.poll(session.id)
    assert "SURVIVOR_OK" in snapshot["output_preview"]
    assert "note" not in snapshot
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
    assert event["termination_source"] == "survivor_lost"
    assert event["exit_code"] is None
    assert "partial output" in event["output"]
    time.sleep(0.1)
    assert recovered.completion_queue.empty()


@pytest.mark.live_system_guard_bypass  # the wrapper's snapshotted child is SIGKILLed after reparenting to init
def test_stop_all_source_still_kills_survivors(tmp_path, monkeypatch):
    _home(monkeypatch, tmp_path)
    registry = ProcessRegistry()
    session = registry.spawn_local(
        "sleep 5", cwd=str(tmp_path), task_id="task", survive_restart=True,
    )
    assert registry.kill_all(source="cli.stop") == 1
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
    checkpoint = json.loads((home / "processes.json").read_text(encoding="utf-8-sig"))
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
    checkpoint = (home / "processes.json").read_text(encoding="utf-8-sig")
    recovered = ProcessRegistry()
    assert recovered.recover_from_checkpoint() == 1
    event = recovered.completion_queue.get(timeout=5)
    assert event["session_id"] == session_id
    assert event["exit_code"] == 9
    assert "SURVIVOR_BOOT_OK" in event["output"]
    assert recovered.completion_queue.empty()
    assert recovered.claim_survivor_completion(session_id) is True
    recovered.acknowledge_survivor_completion(session_id)
    # Even replaying the old checkpoint cannot duplicate an acknowledged completion.
    (home / "processes.json").write_text(checkpoint, encoding="utf-8")
    again = ProcessRegistry()
    assert again.recover_from_checkpoint() == 1
    time.sleep(0.2)
    assert again.completion_queue.empty()


def _ready_successors(home, monkeypatch, count=2, **survivor):
    producer = ProcessRegistry()
    session = _checkpoint_survivor(producer, home, pid=999999, exit_code=0, log_text="one result\n", **survivor)
    successors = [ProcessRegistry() for _ in range(count)]
    for successor in successors:
        assert successor.recover_from_checkpoint() == 1
        recovered = successor.get(session.id)
        assert recovered is not None and recovered._completion_event.wait(5)
    return session, successors


def test_survivor_claim_is_exclusive_before_any_consumer_acknowledges(tmp_path, monkeypatch):
    """Two live successors must not both hand the same completion to a renderer."""
    home = _home(monkeypatch, tmp_path)
    session, (first, second) = _ready_successors(home, monkeypatch)
    assert len(first.drain_notifications(session_key="session")) == 1
    # first has not rendered/acknowledged yet: its claim alone must fence second.
    assert second.drain_notifications(session_key="session") == []
    claim = json.loads((home / "process-runs" / session.id / "delivered").read_text(encoding="utf-8-sig"))
    assert claim["state"] == "claimed" and claim["owner"]["pid"]
    first.acknowledge_survivor_completion(session.id)
    assert second.drain_notifications(session_key="session") == []
    assert second.completion_queue.empty()


@pytest.mark.parametrize("owner_alive", [True, False])
def test_survivor_only_claim_owner_can_settle_or_release(tmp_path, monkeypatch, owner_alive):
    home = _home(monkeypatch, tmp_path)
    session, (first, second) = _ready_successors(home, monkeypatch)
    assert len(first.drain_notifications(session_key="session")) == 1
    marker = home / "process-runs" / session.id / "delivered"
    claim = json.loads(marker.read_text(encoding="utf-8-sig"))
    monkeypatch.setattr(second, "_survivor_claim_is_live", lambda _owner: owner_alive)
    # An unrelated acknowledgement cannot settle even an abandoned claim: it must
    # first win a new claim, not pretend to acknowledge the old owner's delivery.
    assert second._mark_survivor_delivered(second.get(session.id)) is False
    assert second.release_survivor_completion(session.id) is False
    assert json.loads(marker.read_text(encoding="utf-8-sig")) == claim
    assert first.release_survivor_completion(session.id) is True
    assert not marker.exists()
    assert len(second.drain_notifications(session_key="session")) == 1
    second.acknowledge_survivor_completion(session.id)
    assert json.loads(marker.read_text(encoding="utf-8-sig"))["delivered"] is True


def test_survivor_concurrent_drainers_deliver_exactly_once(tmp_path, monkeypatch):
    import threading

    home = _home(monkeypatch, tmp_path)
    session, successors = _ready_successors(home, monkeypatch, count=4)
    barrier = threading.Barrier(len(successors))
    delivered = []

    def drain(registry):
        barrier.wait()
        delivered.extend(registry.drain_notifications(session_key="session"))

    threads = [threading.Thread(target=drain, args=(registry,)) for registry in successors]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(5)
    assert len(delivered) == 1


def test_survivor_claim_by_crashed_owner_is_reclaimed_once(tmp_path, monkeypatch):
    """A crash between claim and delivered must still allow exactly one later delivery."""
    import sys
    import textwrap

    home = _home(monkeypatch, tmp_path)
    producer = ProcessRegistry()
    session = _checkpoint_survivor(producer, home, pid=999999, exit_code=0, log_text="after crash\n")
    crashed = subprocess.run([sys.executable, "-c", textwrap.dedent(f"""
        import os
        from pathlib import Path
        import tools.process_registry as module
        module.get_hermes_home = lambda: Path({str(home)!r})
        registry = module.ProcessRegistry()
        assert registry.recover_from_checkpoint() == 1
        assert registry.get({session.id!r})._completion_event.wait(5)
        assert len(registry.drain_notifications(session_key="session")) == 1
        os._exit(0)  # crash after claim, before render/acknowledgement
    """)], capture_output=True, text=True, timeout=20)
    assert crashed.returncode == 0, crashed.stderr
    claim = json.loads((home / "process-runs" / session.id / "delivered").read_text(encoding="utf-8-sig"))
    assert claim["state"] == "claimed"
    successor = ProcessRegistry()
    assert successor.recover_from_checkpoint() == 1
    assert successor.get(session.id)._completion_event.wait(5)
    events = successor.drain_notifications(session_key="session")
    assert len(events) == 1 and "after crash" in events[0][0]["output"]
    successor.acknowledge_survivor_completion(session.id)
    assert json.loads((home / "processes.json").read_text(encoding="utf-8-sig")) == []
    again = ProcessRegistry()
    again.recover_from_checkpoint()
    time.sleep(0.2)
    assert again.drain_notifications(session_key="session") == []


def test_survivor_claim_by_execd_predecessor_in_same_pid_is_reclaimed(tmp_path, monkeypatch):
    """An in-place exec keeps the PID and start time; an unknown registry nonce is not live."""
    import os

    home = _home(monkeypatch, tmp_path)
    session, (successor,) = _ready_successors(home, monkeypatch, count=1)
    stale = {"state": "claimed", "owner": {"pid": os.getpid(), "start": ProcessRegistry._safe_host_start_time(os.getpid()),
                                            "nonce": "predecessor-image"}}
    (home / "process-runs" / session.id / "delivered").write_text(json.dumps(stale), encoding="utf-8")
    assert len(successor.drain_notifications(session_key="session")) == 1


def test_reused_pid_survivor_without_exit_file_is_not_adopted(tmp_path, monkeypatch):
    home = _home(monkeypatch, tmp_path)
    producer = ProcessRegistry()
    session = _checkpoint_survivor(producer, home, pid=999999, log_text="stale\n")
    successor = ProcessRegistry()
    monkeypatch.setattr(successor, "_detached_host_fate", lambda *_args: "reused")
    signalled = []
    monkeypatch.setattr("os.kill", lambda *args: signalled.append(args))
    assert successor.recover_from_checkpoint() == 0
    assert successor.get(session.id) is None
    time.sleep(0.2)
    assert successor.completion_queue.empty()
    assert signalled == []
    assert json.loads((home / "processes.json").read_text(encoding="utf-8-sig")) == []


def test_reused_pid_survivor_with_exit_file_reports_real_result_without_supervision(tmp_path, monkeypatch):
    home = _home(monkeypatch, tmp_path)
    producer = ProcessRegistry()
    session = _checkpoint_survivor(producer, home, pid=999999, exit_code=4, log_text="finished\n")
    successor = ProcessRegistry()
    monkeypatch.setattr(successor, "_detached_host_fate", lambda *_args: "reused")
    successor.recover_from_checkpoint()
    recovered = successor.get(session.id)
    # Finished synchronously: no poller ever supervises the recycled PID.
    assert recovered is not None and recovered.exited and recovered._reader_thread is None
    event = successor.completion_queue.get(timeout=1)
    assert event["exit_code"] == 4 and event["completion_reason"] == "exited"
    assert successor.completion_queue.empty()


def test_reused_pid_pending_result_survives_a_second_restart(tmp_path, monkeypatch):
    home = _home(monkeypatch, tmp_path)
    producer = ProcessRegistry()
    session = _checkpoint_survivor(producer, home, pid=999999, exit_code=4, log_text="finished\n")
    first = ProcessRegistry()
    monkeypatch.setattr(first, "_detached_host_fate", lambda *_args: "reused")
    assert first.recover_from_checkpoint() == 1
    assert json.loads((home / "processes.json").read_text(encoding="utf-8-sig"))[0]["pid"] is None
    # Crash before any consumer claimed the result. The detached PID is not its identity.
    second = ProcessRegistry()
    assert second.recover_from_checkpoint() == 1
    recovered = second.get(session.id)
    assert recovered.exited and recovered.pid is None and recovered._reader_thread is None
    events = second.drain_notifications(session_key="session")
    assert len(events) == 1 and events[0][0]["exit_code"] == 4
    assert events[0][0]["output"] == "finished\n"
    second.acknowledge_survivor_completion(session.id)
    assert json.loads((home / "processes.json").read_text(encoding="utf-8-sig")) == []
    third = ProcessRegistry()
    assert third.recover_from_checkpoint() == 0
    assert third.drain_notifications(session_key="session") == []


@pytest.mark.parametrize("survive_restart", [False, True])
def test_file_backed_survivor_ready_uses_normal_output_hooks(tmp_path, monkeypatch, survive_restart):
    import queue
    import shlex

    _home(monkeypatch, tmp_path)
    registry = ProcessRegistry()
    gate = tmp_path / "release-ready"
    session = registry.spawn_local(
        command=f"while [ ! -e {shlex.quote(str(gate))} ]; do sleep 0.02; done; printf 'READY\\n'",
        cwd=str(tmp_path), task_id="task", session_key="session", survive_restart=survive_restart,
    )
    session.watch_patterns = ["READY"]
    streamed = []
    monkeypatch.setattr(registry, "_emit_output", lambda _session, text: streamed.append(text))
    gate.touch()
    try:
        assert session._completion_event.wait(5)
        events = []
        while True:
            try:
                events.append(registry.completion_queue.get_nowait())
            except queue.Empty:
                break
        assert [event["output"] for event in events if event.get("type") == "watch_match"] == ["READY"]
        assert "READY\n" in "".join(streamed)
        # Polling the same durable log must not emit the chunk twice.
        if survive_restart:
            registry._read_survivor_log(session)
        assert "".join(streamed) == "READY\n"
    finally:
        registry.kill_all(source="test_cleanup")
