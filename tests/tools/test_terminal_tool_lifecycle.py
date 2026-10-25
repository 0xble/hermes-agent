"""Regression tests for terminal environment lifecycle cleanup."""

import time
from unittest.mock import Mock

import tools.process_registry as process_registry_module
import tools.terminal_tool as terminal_tool
from tools.process_registry import ProcessRegistry, ProcessSession
from tools.terminal_tool_lifecycle import _cleanup_inactive_envs


def test_cleanup_inactive_env_keeps_container_with_owner_spawned_process(monkeypatch):
    registry = ProcessRegistry()
    registry._running["proc_own1"] = ProcessSession(
        id="proc_own1",
        command="sleep 30",
        task_id="default",
        owner_task_id="sa-1-abc123",
        started_at=time.time(),
    )
    env = Mock()
    old_activity = time.time() - 1000

    monkeypatch.setattr(process_registry_module, "process_registry", registry)
    monkeypatch.setattr(terminal_tool, "_active_environments", {"default": env})
    monkeypatch.setattr(terminal_tool, "_last_activity", {"default": old_activity})
    monkeypatch.setattr(terminal_tool, "_creation_locks", {})

    _cleanup_inactive_envs(lifetime_seconds=300)

    assert terminal_tool._active_environments["default"] is env
    env.cleanup.assert_not_called()
    assert terminal_tool._last_activity["default"] > old_activity
