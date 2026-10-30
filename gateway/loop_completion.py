"""Bind final-response loop controls to the definition the answering turn observed."""

from __future__ import annotations

from typing import Optional

LoopIdentity = tuple[str, float, int, int]


def capture_loop_identity(session_id: str) -> Optional[LoopIdentity]:
    from hermes_cli.loops import load_loop

    state = load_loop(session_id)
    if state is None or state.status != "active":
        return None
    return session_id, state.created_at, state.version, state.control_generation


def complete_loop_response(session_id: str, identity: LoopIdentity, response: str) -> str:
    """Stop only the observed active definition, atomically with claim/control fencing.

    This is an explicit completion, not settlement of an unrelated tick. Tick claims and
    recovery may advance while the turn runs. A revision, replacement, pause or resume wins.
    """
    from hermes_cli.loops import LoopManager, response_signals_complete

    if not response_signals_complete(response) or len(identity) != 4 or identity[0] != session_id:
        return ""
    manager = LoopManager(session_id)

    def apply(state):
        if state is None or state.status != "active" or (
            session_id, state.created_at, state.version, state.control_generation
        ) != tuple(identity):
            return False
        manager._apply_stop(state, "done", "agent signaled the task is complete")
        return True

    if not manager._mutate_current(apply, failure=False):
        return ""
    return "✓ Loop finished — task complete."


def carry_response(result, event) -> None:
    """The terminal queued turn owns completion, including an absent loop capture."""
    if "queued_terminal_loop_completion_identity" in result:
        event._loop_completion_identity = result["queued_terminal_loop_completion_identity"]
    if result.get("queued_chain_loop_tick"):
        event._post_turn_loop_tick = result["queued_chain_loop_tick"]


def carry_followup(result, identity, loop_tick):
    """Preserve the deepest terminal capture and latest tick in an in-band chain."""
    if isinstance(result, dict):
        if "queued_terminal_loop_completion_identity" not in result:
            result = {**result, "queued_terminal_loop_completion_identity": identity}
        if loop_tick and not result.get("queued_chain_loop_tick"):
            result = {**result, "queued_chain_loop_tick": loop_tick}
    return result
