"""Shared, side-effect-free goal confirmations for slash commands and tools."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from agent.i18n import t

if TYPE_CHECKING:
    from hermes_cli.goals import GoalState


GOAL_CONTROLS = "Controls: /goal status · /goal show · /goal pause · /goal resume · /goal clear"


def format_goal_change(action: str, state: GoalState, change: dict[str, Any] | None = None) -> str:
    """Describe the committed snapshot, never re-read mutable session state."""
    change = change or {}
    if action == "set":
        text = t("gateway.goal.set", budget=state.max_turns, goal=state.goal)
        if change.get("kind") == "goal_replaced":
            text += f"\nReplaced goal: {change['previous_goal']}"
    elif action == "draft":
        text = f"⊙ Goal drafted ({state.status}, {state.max_turns}-turn budget): {state.goal}"
    elif action == "edit":
        text = f"⊙ Goal edited{' and resumed' if change.get('resumed') else ''} ({state.status}): {state.goal}"
    elif action == "pause":
        text = t("gateway.goal.paused", goal=state.goal)
        if state.paused_reason:
            text += f"\nReason: {state.paused_reason}"
    elif action == "resume":
        text = t("gateway.goal.resumed", goal=state.goal)
    elif action == "clear":
        text = f"✓ Goal cleared: {state.goal}"
    elif action == "wait":
        text = f"⏳ Goal parked on {state.waiting_reason or 'background work'}: {state.goal}"
    elif action == "unwait":
        text = f"▶ Goal wait released: {state.goal}"
    elif action in {"subgoal_add", "subgoal_remove"}:
        verb = "added" if action.endswith("add") else "removed"
        text = f"✓ Subgoal {verb} ({change['index']}): {change['text']}\nGoal: {state.goal}"
    elif action == "subgoal_clear":
        text = f"✓ Cleared {change['count']} subgoals.\nGoal: {state.goal}"
    elif action in {"gate_add", "gate_remove"}:
        verb = "added" if action.endswith("add") else "removed"
        text = f"✓ Gate {verb} ({change['index']}): {change['command']}\nGoal: {state.goal}"
        if action == "gate_add":
            text += f"\nTimeout: {change['timeout_seconds']}s · Retries: {change['max_retries']}"
    elif action == "gate_clear":
        text = f"✓ Cleared {change['count']} gates.\nGoal: {state.goal}"
    else:
        return ""
    if action in {"set", "draft", "edit"} and state.has_contract():
        text += "\n\nCompletion contract:\n" + state.contract.render_block()
    if action == "resume" or (action == "edit" and change.get("resumed")):
        text += "\nResume requests reassessment, not permission or confirmation that prerequisites are resolved."
        if state.blocker:
            from hermes_cli.goals_blockers import blocker_summary
            text += "\nPrevious blocker: " + blocker_summary(state.blocker)
    if action != "set":
        text += "\n" + GOAL_CONTROLS
    return text
