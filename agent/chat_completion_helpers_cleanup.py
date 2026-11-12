"""Per-turn resource cleanup extracted from chat completion helpers."""

from __future__ import annotations

import logging
import os
from typing import Any


def cleanup_task_resources(
    agent: Any,
    task_id: str,
    *,
    persistent_env_check: Any,
    run_agent_module: Any,
    log: Any,
) -> None:
    """Per-turn VM + browser cleanup for a task.

    Persistent terminal environments are reaped by their idle reaper. Camofox-owned
    browser tabs still use native cleanup; headed visibility alone is not a keep request.
    """
    def _headed() -> bool:
        try:
            from tools import browser_tool
            from tools.browser_tool_cloud import _is_headed_mode
            return _is_headed_mode() and not browser_tool._is_camofox_mode()
        except Exception:
            return bool(os.environ.get("AGENT_BROWSER_HEADED"))

    def _runtime() -> Any:
        return run_agent_module() if callable(run_agent_module) else run_agent_module

    for label, skip, skip_what, cleanup in (
        ("VM", persistent_env_check, "cleanup_vm for persistent env", lambda: _runtime().cleanup_vm(task_id)),
        ("browser", lambda _tid: _headed(), "cleanup_browser for headed session", lambda: _runtime().cleanup_browser(task_id)),
    ):
        try:
            if skip(task_id):
                if agent.verbose_logging:
                    logging.debug(f"Skipping per-turn {skip_what} {task_id}; idle reaper will handle it.")
            else:
                cleanup()
        except Exception as e:  # health: allow BLE001 -- preserve best-effort cleanup boundary
            if agent.verbose_logging:
                log.warning("Failed to cleanup %s for task %s: %s", label, task_id, e)
