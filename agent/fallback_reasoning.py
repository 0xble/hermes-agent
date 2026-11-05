"""Reasoning precedence when activating an agent fallback route."""
import logging

logger = logging.getLogger("agent.chat_completion_helpers")

def _reresolve_fallback_reasoning_config(agent, fallback: dict | None = None) -> None:
    """Apply a fallback route's reasoning setting, else per-model/global config.

    Preset fallbacks carry ``reasoning_effort`` on the concrete chain entry; resolving only
    from the global config silently discarded that route-level contract.
    """
    try:
        from hermes_constants import parse_reasoning_effort, resolve_reasoning_config
        if getattr(agent, "_reasoning_effort_pinned", False):
            logger.info("Fallback %s: retaining explicitly pinned reasoning_config", agent.model)
            return
        if isinstance(fallback, dict) and "reasoning_effort" in fallback:
            parsed = parse_reasoning_effort(fallback["reasoning_effort"])
            if parsed is None:
                logger.warning("Fallback %s: invalid reasoning_effort %r; using config resolution", agent.model, fallback["reasoning_effort"])
            else:
                agent.reasoning_config = parsed
                logger.info("Fallback %s: using route reasoning_config: %s", agent.model, parsed)
                return
        # Re-resolve for non-preset/legacy fallbacks. A config failure must not kill the swap.
        from hermes_cli.config import load_config
        agent.reasoning_config = resolve_reasoning_config(load_config() or {}, agent.model)
        logger.info("Fallback %s: reasoning_config resolved: %s", agent.model, agent.reasoning_config)
    except Exception as _reasoning_err:
        logger.debug("Failed to resolve reasoning_config for fallback %s; keeping current: %s", agent.model, _reasoning_err, exc_info=True)
