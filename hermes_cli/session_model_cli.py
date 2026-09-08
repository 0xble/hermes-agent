"""Interactive CLI adapter for turn-bound model controls."""
from __future__ import annotations


def apply_session_model(cli, agent, selection):
    from agent.agent_runtime_helpers import _build_primary_runtime_snapshot
    from hermes_cli.session_model import _identity
    if cli.agent is not agent or _identity(agent) != selection.baseline:
        raise ValueError("The owning session changed before the queued switch could apply.")
    route = selection.route
    if route is not None:
        if not cli._stage_and_swap_model(route, cli.model):
            raise ValueError("Native model switch failed; prior model retained.")
        cli._persist_model_switch_to_session(route)
        cli._pending_model_switch_note = (
            f"[Model switched to {route.new_model} via {route.target_provider}.]"
        )
    cli.reasoning_config = selection.reasoning
    agent.reasoning_config = selection.reasoning
    agent._primary_runtime = _build_primary_runtime_snapshot(agent, agent.api_mode)
