"""Named-node process handles retain their original SSH/run identity across recovery."""
import base64
from dataclasses import FrozenInstanceError
import json
import os
import shlex
import subprocess
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tools import process_registry as pr
from tools.nodes import Node
from tools.process_registry_notifications import format_process_notification
from tools.process_registry_ssh import binding_record
from tests.waiting import wait_until


class RecordedSSH:
    """Deterministic transport boundary, not a substitute process implementation."""
    def __init__(self, node):
        self._hermes_node = node
        self.host = node.ssh
        self.calls = []
        self.state = "running"
        self.output = b""
        self.error = None
        self.wrong_token = False

    def _run_ssh(self, command, timeout):
        request = json.loads(shlex.split(command)[-1])
        self.calls.append(request)
        if self.error is not None:
            raise self.error
        offset = min(request.get("offset", 0), len(self.output))
        result = {"token": "wrong" if self.wrong_token else request["token"],
                  "state": self.state, "pid": 43123, "offset": offset,
                  "size": len(self.output), "output": base64.b64encode(self.output[offset:]).decode()}
        if request["action"] == "stop":
            result.update(state="exited", exit_code=-9, reason="killed", source=request.get("source"))
        elif self.state == "exited":
            result.update(exit_code=0, reason="exited")
        return subprocess.CompletedProcess([], 0, json.dumps(result), "")


@pytest.fixture
def registry(monkeypatch, tmp_path):
    result = pr.ProcessRegistry()
    monkeypatch.setattr(pr, "process_registry", result)
    monkeypatch.setattr(pr, "CHECKPOINT_PATH", tmp_path / "processes.json")
    monkeypatch.setattr(result, "_start_ssh_poller", lambda session: None)
    return result


@pytest.fixture
def node():
    return Node("alpha", "alpha-ssh", "linux")


@pytest.fixture
def spawn(registry, node, monkeypatch, tmp_path):
    from tools import node_execution
    execution = SimpleNamespace(node=node, key="home:chat:alpha-connection", task_id="raw-caller")
    monkeypatch.setattr(node_execution, "current_execution", lambda: execution)

    def launch(peer=None, **kwargs):
        return registry.spawn_via_env(peer or RecordedSSH(node), "printf hello", cwd=str(tmp_path),
                                      task_id="collapsed", owner_task_id="raw-caller", session_key="chat-route",
                                      **kwargs)
    return launch


def test_spawn_binds_immutable_connection_and_raw_notification_owner(spawn, registry, node):
    peer = RecordedSSH(node)
    session = spawn(peer)
    assert session.pid_scope == "ssh"
    assert session.task_id == "raw-caller"
    assert session.ssh_binding.execution_key == "home:chat:alpha-connection"
    assert session.owner_task_id == "raw-caller"
    assert session.session_key == "chat-route"
    with pytest.raises(FrozenInstanceError):
        session.ssh_binding.ssh = "different-host"
    entry, = json.loads(pr.CHECKPOINT_PATH.read_text())
    assert entry["ssh_binding"] == binding_record(session.ssh_binding)
    assert entry["pid_scope"] == "ssh"
    assert [call["action"] for call in peer.calls] == ["launch"]
    assert entry["owner_task_id"] == "raw-caller"


def test_ambiguous_launch_is_persisted_and_never_replayed(spawn, registry, node):
    peer = RecordedSSH(node)
    peer.error = TimeoutError("response dropped")
    with pytest.raises(RuntimeError, match="Do not re-run") as error:
        spawn(peer)
    session, = registry._running.values()
    assert session.id in str(error.value)
    assert session.id in pr.CHECKPOINT_PATH.read_text()
    assert not session.exited
    peer.error = None
    peer.state = "exited"
    peer.output = b"ran exactly once\n"
    assert registry._refresh_ssh(session)
    assert session.exited and session.exit_code == 0
    assert session.output_buffer == "ran exactly once\n"
    assert [call["action"] for call in peer.calls] == ["launch", "inspect"]


def test_poll_transport_failure_is_unavailable_not_a_fake_exit(spawn, registry, node):
    peer = RecordedSSH(node)
    session = spawn(peer)
    peer.error = OSError("offline")
    result = json.loads(pr._handle_process({"action": "poll", "session_id": session.id}))
    assert result["node"] == "alpha" and result["ssh"] == "alpha-ssh"
    assert result["status"] == "running" and result["remote_status"] == "unavailable"
    assert not session.exited and session.exit_code is None
    assert registry.completion_queue.empty()
    peer.error = None
    peer.state = "exited"
    assert registry._refresh_ssh(session)
    assert session.exit_code == 0


@pytest.mark.parametrize("action", ["poll", "log", "wait", "kill", "write", "submit", "close", "handoff"])
def test_conflicting_explicit_node_is_rejected_before_action(spawn, node, monkeypatch, action):
    session = spawn()
    from tools import nodes
    monkeypatch.setattr(nodes, "resolve_node", lambda name: Node("alpha", "replacement-ssh", "linux"))
    if action in pr._SESSION_ACTIONS:
        handler = Mock(side_effect=AssertionError("conflicting action ran"))
        monkeypatch.setitem(pr._SESSION_ACTIONS, action, (handler, False))
    else:
        monkeypatch.setattr(pr, "_handoff_process", Mock(side_effect=AssertionError("conflicting handoff ran")))
    result = json.loads(pr._handle_process({"action": action, "session_id": session.id, "node": "alpha"}))
    assert result["status"] == "error"
    assert "conflicts" in result["error"]
    assert result["node"] == "alpha" and result["ssh"] == "alpha-ssh"


def test_omitted_node_uses_handle_and_list_only_filters_when_requested(spawn, registry, monkeypatch):
    session = spawn()
    registry._running["proc_local"] = pr.ProcessSession(id="proc_local", command="local", task_id="raw-caller")
    from tools import nodes
    resolver = Mock(side_effect=AssertionError("configured default must not be resolved"))
    monkeypatch.setattr(nodes, "resolve_node", resolver)
    result = json.loads(pr._handle_process({"action": "poll", "session_id": session.id}))
    assert result["node"] == "alpha"
    assert len(json.loads(pr._handle_process({"action": "list"}))["processes"]) == 2
    resolver.assert_not_called()
    resolver.side_effect = None
    resolver.return_value = Node("alpha", "alpha-ssh", "linux")
    result = json.loads(pr._handle_process({"action": "list", "node": "alpha"}))
    assert [item["session_id"] for item in result["processes"]] == [session.id]
    assert result["node"] == "alpha"


@pytest.mark.parametrize("action", ["write", "submit", "close"])
def test_ssh_stdin_is_explicitly_unsupported_with_provenance(spawn, action):
    session = spawn()
    result = json.loads(pr._handle_process({"action": action, "session_id": session.id, "data": "hi"}))
    assert result["status"] == "error" and "stdin" in result["error"]
    assert result["node"] == "alpha" and result["ssh"] == "alpha-ssh"


def test_receipt_token_mismatch_cannot_complete_or_signal_another_run(spawn, registry, node):
    peer = RecordedSSH(node)
    session = spawn(peer)
    peer.wrong_token = True
    result = registry.kill_process(session.id)
    assert result["status"] == "error" and "token mismatch" in result["error"]
    assert not session.exited and session.id in registry._running
    assert registry.completion_queue.empty()


def test_environment_retargeting_is_refused(spawn, registry, node):
    peer = RecordedSSH(node)
    session = spawn(peer)
    peer.host = "other-host"
    assert not registry._refresh_ssh(session)
    assert "target changed" in session.remote_error
    assert [call["action"] for call in peer.calls] == ["launch"]


@pytest.mark.parametrize("bounded", [False, True])
def test_remote_kill_and_bulk_cleanup_never_touch_host_pids(spawn, registry, node, monkeypatch, bounded):
    session = spawn()
    host_signal = Mock(side_effect=AssertionError("remote PID reached the controller"))
    monkeypatch.setattr(os, "kill", host_signal)
    monkeypatch.setattr(os, "killpg", host_signal, raising=False)
    monkeypatch.setattr(registry, "_terminate_host_pid", host_signal)
    assert registry.kill_all("raw-caller", source="operator", **({"deadline": time.monotonic() + 30} if bounded else {})) == 1
    assert session.exited and session.completion_reason == "killed"
    assert session.termination_source == "operator"
    host_signal.assert_not_called()


def test_failed_remote_kill_stays_tracked_for_retry(spawn, registry, node):
    peer = RecordedSSH(node)
    session = spawn(peer)
    peer.error = OSError("unreachable")
    assert registry.kill_all("raw-caller", source="operator", deadline=time.monotonic() + 30) == 0
    assert session.id in registry._running and not session.exited
    peer.error = None
    assert registry.kill_process(session.id)["status"] == "killed"


def test_recovery_preserves_connection_and_owner_without_current_nodes(spawn, registry, monkeypatch, node):
    session = spawn()
    successor = pr.ProcessRegistry()
    monkeypatch.setattr(successor, "_start_ssh_poller", lambda process: None)
    from tools import nodes
    monkeypatch.setattr(nodes, "resolve_node", Mock(side_effect=AssertionError("recovery consulted current config")))
    host_probe = Mock(side_effect=AssertionError("SSH PID tested on the controller"))
    monkeypatch.setattr(successor, "_detached_host_fate", host_probe)
    assert successor.recover_from_checkpoint() == 1
    adopted = successor._running[session.id]
    assert adopted.ssh_binding == session.ssh_binding
    assert adopted.owner_task_id == "raw-caller" and adopted.session_key == "chat-route"
    from tools.environments import ssh
    peer = RecordedSSH(node)
    constructor = Mock(return_value=peer)
    monkeypatch.setattr(ssh, "SSHEnvironment", constructor)
    assert successor._refresh_ssh(adopted)
    constructor.assert_called_once_with(host="alpha-ssh", user="", sync_files=False)
    host_probe.assert_not_called()
    assert [call["action"] for call in peer.calls] == ["inspect"]


def test_foreign_ssh_handoff_survives_unrelated_checkpoint_writer(spawn):
    session = spawn()
    unrelated = pr.ProcessRegistry()
    unrelated._write_checkpoint()
    entry, = json.loads(pr.CHECKPOINT_PATH.read_text())
    assert entry["session_id"] == session.id
    assert entry["ssh_binding"] == binding_record(session.ssh_binding)


def test_completion_handoff_and_retained_result_preserve_node_and_route(spawn, registry, node, monkeypatch):
    peer = RecordedSSH(node)
    session = spawn(peer)
    session.notify_on_complete = True
    identity = session.ssh_binding
    registry.transfer_ownership(session.id, from_owner="raw-caller", to_owner="parent-owner", to_task_id="parent-task",
                                to_session_key="parent-chat", to_parent_session_id="parent-conversation", notify=True)
    peer.state = "exited"
    peer.output = b"done\n"
    assert registry._refresh_ssh(session)
    event = registry.completion_queue.get_nowait()
    assert event["node"] == "alpha" and event["ssh"] == "alpha-ssh"
    assert event["owner_task_id"] == "parent-owner" and event["session_key"] == "parent-chat"
    assert session.ssh_binding is identity
    assert "Execution node: alpha (SSH: alpha-ssh)" in format_process_notification(event)
    from gateway import session_context
    monkeypatch.setattr(session_context, "get_session_env", lambda key, default="": "parent-conversation"
                        if key == "HERMES_SESSION_ID" else default)
    retained = pr.load_completed_results()[session.id]
    assert retained.ssh_binding == identity and retained.pid_scope == "ssh"
    assert retained.owner_task_id == "parent-owner" and retained.session_key == "parent-chat"
    assert retained.output_buffer == "done\n"
    assert registry.completion_queue.empty()


@pytest.mark.platforms("posix")
def test_remote_protocol_executes_and_stops_real_child_on_mock_transport(registry, tmp_path, monkeypatch):
    """Exercise the exact remote program locally, without SSH or a live node."""
    from tools.process_registry_ssh import _REMOTE_REQUEST, _REMOTE_WORKER
    import sys
    root = tmp_path / "peer-home"
    root.mkdir()
    token = "a" * 32

    def request(action, **extra):
        result = subprocess.run([sys.executable, "-c", _REMOTE_REQUEST,
                                 json.dumps({"action": action, "token": token, **extra})],
                                env={"HOME": str(root), "PATH": os.defpath},
                                text=True, capture_output=True, timeout=10, check=True)
        return json.loads(result.stdout)

    record = request("launch", command="printf 'peer-ready\\n'; exec sleep 60", cwd=str(root), worker=_REMOTE_WORKER)
    try:
        assert record["state"] == "running"
        assert record["token"] == token
        def ready():
            return b"peer-ready" in base64.b64decode(request("inspect").get("output", ""))
        assert wait_until(ready, timeout=10)
        result = request("stop", wait=3, source="operator")
        assert result["state"] == "exited" and result["reason"] == "killed"
        assert result["source"] == "operator"
        assert base64.b64decode(result["output"]) == b"peer-ready\n"
        # A completed receipt remains readable, but cannot relaunch the command.
        assert request("inspect")["exit_code"] == -9
    finally:
        request("stop", wait=3)


@pytest.mark.platforms("posix")
def test_stale_remote_pid_receipt_cannot_kill_a_reused_pid(tmp_path):
    from tools.process_registry_ssh import _REMOTE_REQUEST
    import sys
    token = "c" * 32
    run = tmp_path / ".cache" / "hermes" / "processes" / token
    run.mkdir(parents=True)
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], stdin=subprocess.DEVNULL)
    try:
        (run / "receipt.json").write_text(json.dumps({"token": token, "state": "running", "pid": child.pid}))
        result = subprocess.run([sys.executable, "-c", _REMOTE_REQUEST,
                                 json.dumps({"action": "stop", "token": token, "wait": 0})],
                                env={"HOME": str(tmp_path), "PATH": os.defpath},
                                text=True, capture_output=True, timeout=10, check=True)
        assert json.loads(result.stdout)["state"] == "unavailable"
        assert child.poll() is None
        assert not (run / "stop.json").exists()
    finally:
        child.kill()
        child.wait(timeout=10)


def test_utf8_output_split_between_ssh_polls_is_not_corrupted(spawn, registry, node):
    peer = RecordedSSH(node)
    session = spawn(peer)
    encoded = "left→right\n".encode()
    peer.output = encoded[:5]
    assert registry._refresh_ssh(session)
    peer.output = encoded
    peer.state = "exited"
    assert registry._refresh_ssh(session)
    assert session.output_buffer == "left→right\n"


@pytest.mark.parametrize("value", [None, "", False, 1])
def test_invalid_explicit_node_never_resolves_default(registry, monkeypatch, value):
    from tools import nodes
    resolver = Mock(side_effect=AssertionError("invalid node resolved a default"))
    monkeypatch.setattr(nodes, "resolve_node", resolver)
    result = json.loads(pr._handle_process({"action": "list", "node": value}))
    assert result["status"] == "error"
    resolver.assert_not_called()


def test_checkpoint_failure_prevents_remote_launch(spawn, registry, node, monkeypatch):
    import utils
    peer = RecordedSSH(node)
    monkeypatch.setattr(utils, "atomic_json_write", Mock(side_effect=OSError("fixture disk unavailable")))
    with pytest.raises(RuntimeError, match="no remote command was launched"):
        spawn(peer)
    assert peer.calls == []
    assert registry._running == {}
