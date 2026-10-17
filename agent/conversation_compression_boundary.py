"""Keep read deduplication and loop-guardrail state aligned at context-loss boundaries.

This module is deliberately separate from ``conversation_compression`` so every boundary can use one
small hook without introducing an import cycle. A result that left the model-visible context cannot be
used as evidence that the next identical read made no progress.
"""

from __future__ import annotations

import contextlib
from typing import Any, Optional


def _reset_read_dedup_caches(task_id: str, *, session_id: str = "") -> None:
    """Re-arm repeat-read dedup after a boundary that removed prior tool results."""
    with contextlib.suppress(Exception):
        from tools.file_tools_read_tracking import reset_file_dedup
        reset_file_dedup(task_id)
    if session_id:
        with contextlib.suppress(Exception):
            from tools.computer_use.tool import reset_screenshot_dedup
            reset_screenshot_dedup(session_id)
    with contextlib.suppress(Exception):
        from tools.skills_tool import reset_skill_view_dedup
        reset_skill_view_dedup(task_id)


def mark_context_loss_boundary(
    agent: Any,
    task_id: Optional[str],
    *,
    reset_read_caches: bool = True,
) -> None:
    """Re-baseline read dedup and idempotent success-repeat state together.

    ``reset_read_caches=False`` is for callers without a task id. The guardrail is still reset because
    its state is scoped to the agent, while a cache reset without a task id could affect other tasks.
    """
    if reset_read_caches and task_id:
        _reset_read_dedup_caches(task_id, session_id=getattr(agent, "session_id", None) or "")
    guardrails = getattr(agent, "_tool_guardrails", None)
    reset_progress = getattr(guardrails, "reset_idempotent_read_progress", None)
    if callable(reset_progress):
        reset_progress()
