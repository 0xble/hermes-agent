"""Idle-cache bounds and exact process lifetime, with isolated deterministic process stubs."""
import io
import json
import logging
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tools import code_kernel as local
from tools import code_kernel_remote as remote


class Process:
    def __init__(self, pid=424242):
        self.pid, self.returncode = pid, None
        self.stdin, self.stdout, self.stderr = io.BytesIO(), io.BytesIO(), io.BytesIO()

    def poll(self):
        return self.returncode


class Environment:
    def __init__(self):
        self.commands = []

    def execute(self, command, **_):
        self.commands.append(command)
        return {"output": "ALIVE\n" if "kill -0" in command else "", "returncode": 0}


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    local.shutdown_all_kernels()
    remote.shutdown_all_remote_kernels()
    monkeypatch.setattr(local, "_lifecycle_limits", lambda: (2, 10))
    monkeypatch.setattr(local, "_sweep_stale_staging_dirs", lambda: None)
    monkeypatch.setattr(local, "_ensure_background_reaper", lambda: None)
    monkeypatch.setattr(local, "_resolve_owner", lambda task: task)
    from tools.code_execution_tool import _kill_process_group
    monkeypatch.setattr("tools.code_execution_tool._kill_process_group",
                        lambda proc, **_: setattr(proc, "returncode", 0))
    yield _kill_process_group
    local.shutdown_all_kernels()
    remote.shutdown_all_remote_kernels()


def call_local(owner, code="cell"):
    return local.execute_in_session_kernel(
        code, task_id=owner, mode="strict", child_python=sys.executable, child_cwd="",
        sandbox_tools=frozenset(), timeout=2, max_tool_calls=1, reset=False, is_interrupted=None,
    )


def call_remote(owner, env):
    return remote.execute_in_remote_kernel(
        "cell", env=env, env_type="ssh", task_env_id=owner,
        sandbox_tools=frozenset(), timeout=2, max_tool_calls=1, reset=False,
    )


@pytest.mark.parametrize("backend", ["local", "remote"])
def test_last_detach_reconciles_idle_overshoot_without_rejecting_attached_work(monkeypatch, backend):
    entered, resume = threading.Barrier(5), threading.Event()

    def cell(*_, **__):
        entered.wait(5)
        assert resume.wait(5)
        return "{}" if backend == "local" else {}

    env = Environment()
    if backend == "local":
        monkeypatch.setattr(local, "_run_cell", cell)
        registry, invoke = local._REGISTRY, call_local
    else:
        monkeypatch.setattr(remote, "_run_attached_cell", cell)
        monkeypatch.setattr(remote, "_spawn_remote_kernel", lambda env, env_type, owner, *args, **_: remote.RemoteKernel(
            env, env_type, "/isolated/kernel/" + owner, "123", "not-logged", owner,
        ))
        registry, invoke = remote._REGISTRY, lambda owner: call_remote(owner, env)
    with ThreadPoolExecutor(max_workers=4) as pool:
        pending = [pool.submit(invoke, "owner-" + str(i)) for i in range(4)]
        try:
            entered.wait(5)
            with registry.lock:
                assert len(registry.kernels) == 4
                assert all(kernel.attached == 1 for kernel in registry.kernels.values())
        finally:
            resume.set()
        for future in pending:
            future.result(5)
    assert len(registry.kernels) == 2
    assert all(kernel.attached == 0 for kernel in registry.kernels.values())


def test_existing_reaper_reconciles_nonexpired_unpinned_cache(monkeypatch):
    for i in range(4):
        kernel = local.SessionKernel(("owner-" + str(i),))
        local._KERNELS[kernel.key] = kernel
    local._reap_once()
    assert len(local._KERNELS) == 2


@pytest.mark.parametrize("backend", ["local", "remote"])
def test_idle_reaping_preserves_live_pinned_namespace(backend):
    owner = "owner::child::live-child"
    key = (owner,)
    if backend == "local":
        kernel = local.SessionKernel(key)
        kernel.proc = Process()
        local._KERNELS[key] = kernel
        registry, reap = local._REGISTRY, local._reap_once
    else:
        kernel = remote.RemoteKernel(Environment(), "ssh", "/isolated/pinned", "123", "token", owner)
        remote._REMOTE_KERNELS[key] = kernel
        registry = remote._REGISTRY
        def reap():
            with registry.lock:
                expired = remote._reap_unlocked(10)
            for item in expired:
                item.kill()
    kernel.pinned = True
    kernel.last_used = time.monotonic() - 100
    reap()
    assert registry.kernels[key] is kernel
    registry.shutdown(owner)
    assert key not in registry.kernels


def test_dead_process_is_reaped_before_idle_timeout():
    kernel = local.SessionKernel(("dead",))
    kernel.proc = Process()
    kernel.proc.returncode = 0
    kernel.pinned = True
    local._KERNELS[kernel.key] = kernel
    local._reap_once()
    assert kernel.key not in local._KERNELS


@pytest.fixture
def spawn_stub(monkeypatch):
    proc = Process()
    killed = Mock(side_effect=lambda *_, **__: setattr(proc, "returncode", 0))
    monkeypatch.setattr("tools.code_execution_tool._kill_process_group", killed)
    monkeypatch.setattr(local, "_rpc_forever", lambda *_, **__: None)
    monkeypatch.setattr(local, "_stdout_reader", lambda *_, **__: None)
    monkeypatch.setattr(local, "_stderr_reader", lambda *_, **__: None)
    monkeypatch.setattr(local.subprocess, "Popen", lambda *_, **__: proc)
    return proc, killed


def spawn(kernel):
    local._spawn(kernel, task_id="task", child_python=sys.executable, child_cwd="",
                 sandbox_tools=frozenset(), max_tool_calls=1)


def test_closed_before_first_spawn_cannot_create_a_process(monkeypatch, spawn_stub):
    proc, killed = spawn_stub
    kernel = local.SessionKernel(("closed",))
    kernel.teardown()
    popen = Mock(return_value=proc)
    monkeypatch.setattr(local.subprocess, "Popen", popen)
    try:
        with pytest.raises(RuntimeError, match="closed|retired"):
            spawn(kernel)
        popen.assert_not_called()
        killed.assert_not_called()
    finally:
        kernel.teardown()


def test_owner_boundary_during_popen_retires_late_process_once(monkeypatch, spawn_stub):
    proc, killed = spawn_stub
    entered, resume = threading.Event(), threading.Event()
    kernel = local.SessionKernel(("closing",))
    kernel.attached = 1
    local._KERNELS[kernel.key] = kernel

    def popen(*_, **__):
        entered.set()
        assert resume.wait(5)
        return proc

    monkeypatch.setattr(local.subprocess, "Popen", popen)
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = pool.submit(spawn, kernel)
        assert entered.wait(5)
        closing = pool.submit(local.shutdown_kernels_for_owner, "closing")
        try:
            assert kernel.stop_event.wait(5)
        finally:
            resume.set()
        closing.result(5)
        with pytest.raises(RuntimeError, match="closed|retired"):
            pending.result(5)
    assert proc.poll() is not None
    killed.assert_called_once_with(proc, escalate=True)
    kernel.teardown()
    killed.assert_called_once_with(proc, escalate=True)


def test_cell_failure_retires_once_and_does_not_log_exception_secrets(monkeypatch, caplog, spawn_stub):
    proc, killed = spawn_stub
    kernel = local.SessionKernel(("failure",))
    kernel.proc = proc
    local._KERNELS[kernel.key] = kernel
    secret = "sk-secret-cell-and-exception-never-log"
    monkeypatch.setattr(local, "_await_cell", Mock(side_effect=RuntimeError(secret)))
    caplog.set_level(logging.INFO)
    result = local._run_cell(
        kernel, kernel.key, secret, task_id="failed-task", child_python=sys.executable, child_cwd="",
        sandbox_tools=frozenset(), timeout=2, max_tool_calls=1, is_interrupted=None,
        exec_start=time.monotonic(), state_reset=False, state_reset_note=None,
    )
    assert json.loads(result)["status"] == "error"
    assert kernel.key not in local._KERNELS
    kernel.teardown()
    killed.assert_called_once_with(proc, escalate=True)
    assert secret not in caplog.text


def test_lifecycle_logs_use_current_identity_and_exclude_cell_content(monkeypatch, caplog, spawn_stub):
    proc, killed = spawn_stub
    monkeypatch.setattr(local, "_resolve_owner", lambda _: "shared-owner")
    monkeypatch.setattr("gateway.status.get_process_start_time", lambda pid: 778899)
    monkeypatch.setattr("psutil.Process", lambda pid: SimpleNamespace(memory_info=lambda: SimpleNamespace(rss=123456)))
    monkeypatch.setattr(local, "_await_cell", lambda *_, **__: ("ok", {"status": "ok", "execution_count": 1}))
    monkeypatch.setattr(local, "_cell_result", lambda *_, **__: {})
    caplog.set_level(logging.INFO)
    call_local("task-a", "SECRET-CODE-NOT-A-LOG")
    call_local("task-b", "OTHER-SECRET-NOT-A-LOG")
    local.shutdown_kernels_for_owner("shared-owner")
    records = [json.loads(record.message.split("kernel_lifecycle ", 1)[1])
               for record in caplog.records if "kernel_lifecycle " in record.message]
    assert {record["event"] for record in records} >= {"start", "cell_start", "cell_end", "retire"}
    assert {record["task"] for record in records if record["event"] == "cell_start"} == {"task-a", "task-b"}
    assert all(record["pid"] == proc.pid and record["process_start"] == 778899 for record in records)
    assert all(record["rss_bytes"] == 123456 for record in records)
    assert all(record["owner"] == "shared-owner" and "caller" in record for record in records)
    assert all("attached" in record and "pinned" in record and "reason" in record for record in records)
    assert "SECRET" not in caplog.text
    killed.assert_called_once_with(proc, escalate=True)


def test_parallel_remote_acquire_uses_one_pending_record(monkeypatch):
    env = Environment()
    entered, resume = threading.Event(), threading.Event()
    spawned = []
    selected_twice = threading.Event()

    class ObservedMap(dict):
        lookups = 0

        def get(self, key, default=None):
            value = super().get(key, default)
            self.lookups += 1
            if self.lookups >= 2:
                selected_twice.set()
            return value

    observed = ObservedMap()
    monkeypatch.setattr(remote, "_REMOTE_KERNELS", observed)
    monkeypatch.setattr(remote._REGISTRY, "kernels", observed)

    def create(env, env_type, owner, *args, **_):
        spawned.append(owner)
        entered.set()
        assert resume.wait(5)
        return remote.RemoteKernel(env, env_type, "/isolated/parallel", "123", "token", owner)

    monkeypatch.setattr(remote, "_spawn_remote_kernel", create)
    monkeypatch.setattr(remote, "_run_attached_cell", lambda *_, **__: {})
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(call_remote, "same", env)
        assert entered.wait(5)
        second = pool.submit(call_remote, "same", env)
        try:
            assert selected_twice.wait(5)
        finally:
            resume.set()
        first.result(5), second.result(5)
    assert spawned == ["same"]
    assert len(remote._REMOTE_KERNELS) == 1


def test_remote_owner_boundary_fences_pending_spawn(monkeypatch):
    env = Environment()
    entered, resume = threading.Event(), threading.Event()
    created = []

    def create(env, env_type, owner, *args, **_):
        kernel = remote.RemoteKernel(env, env_type, "/isolated/late", "123", "token", owner)
        created.append(kernel)
        entered.set()
        assert resume.wait(5)
        return kernel

    monkeypatch.setattr(remote, "_spawn_remote_kernel", create)
    monkeypatch.setattr(remote, "_run_attached_cell", lambda *_, **__: {})
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = pool.submit(call_remote, "closing", env)
        assert entered.wait(5)
        closing = pool.submit(remote.shutdown_remote_kernels_for_owner, "closing")
        # The boundary must make its decision while spawn is still pending.
        deadline = time.monotonic() + 5
        try:
            while time.monotonic() < deadline:
                with remote._REGISTRY.lock:
                    if not remote._REMOTE_KERNELS:
                        break
                threading.Event().wait(0.005)
        finally:
            resume.set()
        closing.result(5)
        assert pending.result(5) is None
    assert not remote._REMOTE_KERNELS
    assert sum("pkill -TERM" in command for command in env.commands) == 1


def test_shutdown_during_socket_setup_cleans_resources_without_spawning(monkeypatch, spawn_stub):
    from pathlib import Path

    proc, killed = spawn_stub
    kernel = local.SessionKernel(("setup-closing",))
    entered, resume = threading.Event(), threading.Event()
    resources = {}
    bind = local._bind_rpc_socket

    def blocked_bind(item):
        endpoint = bind(item)
        resources.update(tmpdir=item.tmpdir, sock=item.server_sock, path=item.sock_path)
        entered.set()
        assert resume.wait(5)
        return endpoint

    monkeypatch.setattr(local, "_bind_rpc_socket", blocked_bind)
    popen = Mock(return_value=proc)
    monkeypatch.setattr(local.subprocess, "Popen", popen)
    with ThreadPoolExecutor(max_workers=2) as pool:
        pending = pool.submit(spawn, kernel)
        assert entered.wait(5)
        closing = pool.submit(kernel.teardown)
        try:
            assert kernel.stop_event.wait(5)
        finally:
            resume.set()
        closing.result(5)
        with pytest.raises(RuntimeError, match="closed|retired"):
            pending.result(5)
    popen.assert_not_called()
    killed.assert_not_called()
    assert not Path(resources["tmpdir"]).exists()
    assert resources["sock"].fileno() == -1
    if resources["path"]:
        assert not Path(resources["path"]).exists()
    assert kernel.death_pipe_w is None


def test_remote_events_redact_identity_and_never_probe_host_pid(monkeypatch, caplog):
    from agent.delegation_context import delegated_child_context

    secret = "sk-" + "q7Z9" * 12
    owner = "owner password=" + secret
    monkeypatch.setattr(local, "_resolve_owner", lambda _: owner)
    monkeypatch.setattr(remote, "_current_caller", lambda _: "caller password=" + secret)
    host_probe = Mock(side_effect=AssertionError("remote PID is not a host PID"))
    monkeypatch.setattr("gateway.status.get_process_start_time", host_probe)
    idle_exits = []
    env = Environment()

    def create(env, env_type, owner, *_, idle_exit):
        idle_exits.append(idle_exit)
        return remote.RemoteKernel(env, env_type, "/isolated/pinned", "123", secret, owner)

    monkeypatch.setattr(remote, "_spawn_remote_kernel", create)
    monkeypatch.setattr(remote, "_run_attached_cell", lambda *_, **__: {})
    caplog.set_level(logging.INFO)
    with delegated_child_context("logging-child"):
        call_remote("task password=" + secret, env)
    remote.shutdown_remote_kernels_for_owner(owner)
    records = [json.loads(record.message.split("kernel_lifecycle ", 1)[1])
               for record in caplog.records if "kernel_lifecycle " in record.message]
    assert {record["event"] for record in records} == {"start", "cell_start", "cell_end", "retire"}
    assert len({record["kernel_id"] for record in records}) == 1
    assert all(record["pid"] == "123" for record in records)
    assert all(record["process_start"] == record["rss_bytes"] == "unknown" for record in records)
    assert all(record["pinned"] for record in records)
    assert secret not in caplog.text
    assert len(idle_exits) == 1 and idle_exits[0] > 1800  # pinned: longer, still finite
    host_probe.assert_not_called()


@pytest.mark.parametrize("failure", ["no-pid", "transport", "dead"])
def test_remote_spawn_failure_logs_exclude_transport_output(monkeypatch, caplog, failure):
    secret = "TRANSPORT-CODE-OUTPUT-TOKEN-NOT-A-LOG"

    class FailedEnvironment(Environment):
        def execute(self, command, **kwargs):
            if "launch" in command:
                if failure == "transport":
                    raise RuntimeError(secret)
                return {"output": secret if failure == "no-pid" else "PID:123", "returncode": 0}
            return {"output": secret, "returncode": 0}

    monkeypatch.setattr("tools.code_execution_tool._ship_file_to_remote", lambda *_, **__: None)
    monkeypatch.setattr("tools.code_execution_tool._ship_env_file_and_launch", lambda *_, **__: "launch")
    caplog.set_level(logging.DEBUG)
    assert call_remote("failed", FailedEnvironment()) is None
    assert secret not in caplog.text
    assert not remote._REMOTE_KERNELS


def test_remote_same_key_cells_are_serial_and_other_owners_are_not_blocked(monkeypatch):
    env = Environment()
    entered, resume, selected_twice = threading.Event(), threading.Event(), threading.Event()
    calls = []

    class ObservedMap(dict):
        def get(self, key, default=None):
            result = super().get(key, default)
            if result is not None and result.attached:
                selected_twice.set()
            return result

    observed = ObservedMap()
    monkeypatch.setattr(remote, "_REMOTE_KERNELS", observed)
    monkeypatch.setattr(remote._REGISTRY, "kernels", observed)
    monkeypatch.setattr(remote, "_spawn_remote_kernel", lambda env, env_type, owner, *_, **__: remote.RemoteKernel(
        env, env_type, "/isolated/" + owner, "123", "token", owner))

    def cell(kernel, *_, **__):
        calls.append(kernel.owner)
        if len(calls) == 1:
            entered.set()
            assert resume.wait(5)
        return {}

    monkeypatch.setattr(remote, "_run_attached_cell", cell)
    with ThreadPoolExecutor(max_workers=3) as pool:
        first = pool.submit(call_remote, "same", env)
        assert entered.wait(5)
        second = pool.submit(call_remote, "same", env)
        try:
            assert selected_twice.wait(5)
            other = pool.submit(call_remote, "other", env)
            assert other.result(5) == {}
            assert calls == ["same", "other"]
        finally:
            resume.set()
        first.result(5), second.result(5)
    assert calls == ["same", "other", "same"]


def test_ambiguous_remote_cell_failure_never_replays_via_fail_open(monkeypatch, caplog):
    from tools import code_execution_tool as facade

    class CheckedEnvironment(Environment):
        def execute(self, command, **kwargs):
            if command.startswith("command -v"):
                return {"output": "OK", "returncode": 0}
            return super().execute(command, **kwargs)

    env = CheckedEnvironment()
    monkeypatch.setattr(facade, "_get_or_create_env", lambda _: (env, "ssh"))
    monkeypatch.setattr(facade, "_load_config", lambda: {"timeout": 2, "max_tool_calls": 3})
    monkeypatch.setattr(remote, "_spawn_remote_kernel", lambda env, env_type, owner, *_, **__: remote.RemoteKernel(
        env, env_type, "/isolated/ambiguous", "123", "token", owner))
    monkeypatch.setattr(facade, "_rpc_poll_loop", lambda *_, **__: None)
    cell = Mock(side_effect=RuntimeError("AMBIGUOUS-CELL-CODE-TOKEN-OUTPUT"))
    fallback = Mock()
    monkeypatch.setattr(remote, "_run_remote_cell", cell)
    monkeypatch.setattr(facade, "_run_remote_per_call", fallback)
    caplog.set_level(logging.DEBUG)
    result = json.loads(facade._execute_remote("side_effect()", "ambiguous", None))
    assert result["status"] == "error"
    assert result["kernel"]["ended"] and result["kernel"]["state_lost"]
    assert "may have run" in result["error"]
    assert cell.call_count == 1
    fallback.assert_not_called()
    assert "AMBIGUOUS-CELL-CODE-TOKEN-OUTPUT" not in caplog.text
    assert not remote._REMOTE_KERNELS


def test_remote_retired_poller_cannot_dispatch_a_late_transport_read(monkeypatch):
    from tools import code_execution_rpc as rpc

    stop = threading.Event()
    dispatch = Mock()
    monkeypatch.setattr(rpc, "_handle_rpc_request", dispatch)

    class LateRead:
        def execute(self, command, **_):
            if command.startswith("ls "):
                return {"output": "/isolated/rpc/req_000001"}
            assert command.startswith("cat ")
            stop.set()
            return {"output": json.dumps({"token": "token", "seq": 1,
                                          "tool": "read_file", "args": {"path": "secret"}})}

    rpc._rpc_poll_loop(LateRead(), "/isolated/rpc", "cell", [], [0], 3,
                       frozenset({"read_file"}), stop, "token")
    dispatch.assert_not_called()


@pytest.mark.parametrize("pinned", [False, True])
def test_remote_runner_always_self_exits_and_pinned_outlives_idle_reaper(monkeypatch, tmp_path, pinned):
    spawned = []

    def spawn_remote(env, env_type, owner, *_, idle_exit, **__):
        spawned.append(idle_exit)
        return remote.RemoteKernel(env, env_type, "/isolated/kernel/" + owner, "123", "token", owner)

    monkeypatch.setattr("agent.delegation_context.is_delegated_child_context", lambda: pinned)
    monkeypatch.setattr(remote, "_spawn_remote_kernel", spawn_remote)
    monkeypatch.setattr(remote, "_run_attached_cell", lambda *_, **__: {})
    remote.execute_in_remote_kernel(
        "cell", env=Environment(), env_type="ssh", task_env_id="owner", sandbox_tools=frozenset(),
        timeout=2, max_tool_calls=1, reset=False, idle_exit=10,
    )
    idle_exit = spawned[0]
    # A pinned runner is skipped by the host reaper, so it needs a longer window, never none.
    assert idle_exit > 10 if pinned else idle_exit == 10

    monkeypatch.setenv("HERMES_KERNEL_DIR", str(tmp_path))
    namespace: dict = {"__name__": "isolated_runner"}
    source = remote.REMOTE_KERNEL_RUNNER_SOURCE.format(
        cell_source=local.RUNNER_CELL_SOURCE, capture_limit=1000, idle_exit=idle_exit)
    exec(compile(source, "isolated-remote-runner", "exec"), namespace)
    times = iter([0, idle_exit + 1])

    class StillRunning(Exception):
        pass

    def idle_wait(_):
        raise StillRunning

    namespace["os"] = SimpleNamespace(listdir=lambda _: [])
    namespace["time"] = SimpleNamespace(time=lambda: next(times), sleep=idle_wait)
    namespace["main"]()


@pytest.mark.parametrize("stage", ["teardown", "spawn"])
def test_attribution_failure_never_aborts_claimed_spawn_or_teardown(monkeypatch, spawn_stub, stage):
    proc, killed = spawn_stub
    failing = Mock(side_effect=RuntimeError("attribution"))
    kernel = local.SessionKernel(("attribution",))
    if stage == "spawn":
        monkeypatch.setattr("agent.redact.redact_sensitive_text", failing)
    spawn(kernel)
    assert kernel.proc is proc
    tmpdir = kernel.tmpdir
    assert tmpdir and os.path.isdir(tmpdir)
    monkeypatch.setattr("agent.redact.redact_sensitive_text", failing)
    kernel.teardown()
    killed.assert_called_once_with(proc, escalate=True)
    assert not os.path.exists(tmpdir)
    env = Environment()
    remote.RemoteKernel(env, "ssh", "/isolated/attribution", "123", "token", "owner").kill()
    assert any("kill" in command for command in env.commands)
    assert any("rm -rf" in command for command in env.commands)
    failing.assert_called()


def test_process_cleanup_failures_never_log_subprocess_secrets(monkeypatch, caplog, isolated):
    secret = "SUBPROCESS-ARGV-ENV-EXCEPTION-NOT-A-LOG"
    tree_kill = Mock(side_effect=RuntimeError(secret))
    monkeypatch.setattr("agent.deadline.kill_process_tree", tree_kill)
    proc = SimpleNamespace(pid=424242, kill=Mock(side_effect=RuntimeError(secret)), wait=Mock())
    caplog.set_level(logging.DEBUG)
    isolated(proc, escalate=True)
    tree_kill.assert_called_once()
    proc.kill.assert_called_once()
    proc.wait.assert_called_once_with(timeout=5)
    assert secret not in caplog.text


def test_remote_runner_idle_window_starts_after_a_long_cell_answers(monkeypatch, tmp_path):
    """A cell longer than the idle window must not make the runner exit right after answering."""
    cells = tmp_path / "cells"
    cells.mkdir()
    (cells / "cell_req_000001.json").write_text(json.dumps({"id": "000001", "code": "pass"}))
    monkeypatch.setenv("HERMES_KERNEL_DIR", str(tmp_path))
    namespace: dict = {"__name__": "isolated_runner"}
    exec(compile(remote.REMOTE_KERNEL_RUNNER_SOURCE.format(
        cell_source=local.RUNNER_CELL_SOURCE, capture_limit=1000, idle_exit=10),
        "isolated-remote-runner", "exec"), namespace)
    clock = [0.0]

    def long_cell(request, count):
        clock[0] += 60  # six idle windows of work
        return {"id": request["id"], "status": "ok", "execution_count": count}, None

    class StillRunning(Exception):
        pass

    def idle_wait(_):
        raise StillRunning

    namespace["run_cell"] = long_cell
    namespace["time"] = SimpleNamespace(time=lambda: clock[0], sleep=idle_wait)
    with pytest.raises(StillRunning):
        namespace["main"]()
    assert (cells / "cell_res_000001.json").exists()


def test_remote_runner_never_runs_a_request_reclaimed_after_reading(monkeypatch, tmp_path):
    """The host may rename a request between the runner's read and its removal."""
    cells = tmp_path / "cells"
    cells.mkdir()
    req = cells / "cell_req_000001.json"
    req.write_text(json.dumps({"id": "000001", "code": "pass"}))
    monkeypatch.setenv("HERMES_KERNEL_DIR", str(tmp_path))
    namespace: dict = {"__name__": "isolated_runner"}
    exec(compile(remote.REMOTE_KERNEL_RUNNER_SOURCE.format(
        cell_source=local.RUNNER_CELL_SOURCE, capture_limit=1000, idle_exit=10),
        "isolated-remote-runner", "exec"), namespace)
    real_load = json.load

    def load_then_reclaim(f):
        request = real_load(f)
        req.rename(str(req) + ".cancelled")  # the host's reclaim lands mid-pickup
        return request

    class StillRunning(Exception):
        pass

    def idle_wait(_):
        raise StillRunning

    ran = Mock()
    namespace["json"] = SimpleNamespace(load=load_then_reclaim, dump=json.dump)
    namespace["run_cell"] = ran
    namespace["time"] = SimpleNamespace(time=lambda: 0.0, sleep=idle_wait)
    with pytest.raises(StillRunning):
        namespace["main"]()
    ran.assert_not_called()
