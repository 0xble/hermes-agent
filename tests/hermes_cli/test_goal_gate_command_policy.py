"""Gates use the configured terminal, never a parallel host shell."""
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from hermes_cli.goals import GoalGate, run_gate


@pytest.mark.parametrize("result", [
    {"exit_code": -1, "error": "Denied by command policy"},
    {"exit_code": None, "status": "pending", "output": "Approval pending"},
    {"exit_code": None, "status": "yielded_to_background"},
    {"exit_code": False},
])
def test_gate_requires_verified_terminal_success(monkeypatch, result):
    execute = Mock(return_value=json.dumps(result))
    monkeypatch.setattr("tools.terminal_tool.terminal_tool", execute)
    passed, code, _ = run_gate(GoalGate(command="python -m pytest"), task_id="goal-session")
    assert not passed and code == -1
    assert execute.call_args.kwargs["task_id"] == "goal-session"
    assert execute.call_args.kwargs["_allow_yield"] is False
    assert "_host_local" not in execute.call_args.kwargs
    assert "force" not in execute.call_args.kwargs


@pytest.mark.parametrize("backend", ["local", "docker"])
def test_gate_cache_fingerprints_only_the_actual_local_workspace(monkeypatch, tmp_path, backend):
    from hermes_cli.goals import GoalManager, GoalState
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("tools.terminal_tool._get_env_config", lambda: {"env_type": backend, "cwd": "/default"})
    monkeypatch.setattr("tools.terminal_tool.get_session_cwd", lambda _: "/session-workspace")
    fingerprint = Mock(return_value="unchanged-host")
    monkeypatch.setattr("hermes_cli.goals.workspace_fingerprint", fingerprint)
    execute = Mock(return_value=(True, 0, "fixed"))
    monkeypatch.setattr("hermes_cli.goals.run_gate", execute)
    manager = GoalManager("cache-test")
    manager._state = GoalState(goal="verify", gates=[GoalGate(command="tests", last_exit_code=1, last_failed_fingerprint="unchanged-host")])
    manager._check_gates()
    if backend == "local":
        fingerprint.assert_called_once_with(cwd="/session-workspace")
        execute.assert_not_called()
    else:
        fingerprint.assert_not_called()
        assert manager.state is not None
        execute.assert_called_once_with(manager.state.gates[0], task_id="cache-test")


def test_gate_routes_to_configured_backend_without_yield(monkeypatch):
    import tools.terminal_tool as terminal
    env = Mock()
    env.execute.return_value = {"output": "verified in sandbox", "exit_code": 0}
    plan = SimpleNamespace(env_type="docker", cwd="/workspace", effective_task_id="goal-session", effective_timeout=42, config={})
    monkeypatch.setattr(terminal, "_plan_execution", Mock(return_value=plan))
    monkeypatch.setattr(terminal, "_acquire_env", Mock(return_value=env))
    monkeypatch.setattr(terminal, "_pre_exec_block", Mock())
    monkeypatch.setattr(terminal, "_run_approval_guards", Mock(return_value=SimpleNamespace(note=None, approved_run=False)))
    monkeypatch.setattr(terminal, "finalize_foreground_result", lambda **kwargs: json.dumps(kwargs["result"]))
    yield_handler = Mock(side_effect=AssertionError("verifiers must not yield"))
    monkeypatch.setattr(terminal, "_yield_kwargs", yield_handler)
    assert run_gate(GoalGate(command="test-command", timeout_seconds=42), task_id="goal-session") == (True, 0, "verified in sandbox")
    env.execute.assert_called_once_with("test-command", timeout=42, cwd="/workspace", bounded_capture=True)
    yield_handler.assert_not_called()
