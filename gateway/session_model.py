"""Gateway adapter for the native turn-bound session model API."""
from __future__ import annotations


def apply_session_model(runner, agent, source, session_key, selection):
    """Coroutine factory kept separate from the turn runner's hot path."""
    return _apply(runner, agent, source, session_key, selection)


async def _apply(runner, agent, source, session_key, selection):
    from gateway.slash_commands_model import _ModelSwitchContext, _model_switch_skew_guard
    from hermes_constants import get_hermes_home
    from agent.agent_runtime_helpers import _build_primary_runtime_snapshot

    from hermes_cli.session_model import _identity
    if runner._live_agent_for_session_control(session_key) is not agent or _identity(agent) != selection.baseline:
        raise ValueError("The owning session changed before the queued switch could apply.")
    error = _model_switch_skew_guard()
    if error:
        raise ValueError(error)
    # These assignments cannot fail after a native switch succeeds. The native
    # switch owns client/compressor rollback. No global reasoning/config writes.
    route = selection.route
    if route is not None:
        ctx = _ModelSwitchContext(session_key=session_key, source=source,
            config_path=get_hermes_home() / "config.yaml", persist_global=False,
            current_model=agent.model, current_provider=agent.provider,
            current_base_url=agent.base_url)
        agent.switch_model(new_model=route.new_model, new_provider=route.target_provider,
            api_key=route.api_key, base_url=route.base_url, api_mode=route.api_mode,
            capabilities=route.runtime_capabilities)
    agent.reasoning_config = selection.reasoning
    agent._primary_runtime = _build_primary_runtime_snapshot(agent, agent.api_mode)
    runner._set_session_reasoning_override(session_key, selection.reasoning)
    if route is not None:
        await runner._record_model_switch(route, ctx, source=source, one_turn=False, picker=False)
    else:
        runner._evict_idle_agent_after_session_control(session_key)
