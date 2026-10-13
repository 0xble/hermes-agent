"""Safe request-boundary recovery for long turns on a fallback route.

Primary restoration and prompt synchronization ported from upstream PR #88628;
optional per-entry stay limits retry earlier backups without clearing the shared
primary cooldown.
"""

import logging
import math
import time

logger = logging.getLogger(__name__)


def _restore_primary_runtime_for_iteration(agent, active_system_prompt):
    """Restore an expired primary fallback during a long-running turn.

    ``build_turn_context`` handles the normal between-turn restoration. A
    turn with many tool calls can remain inside ``run_conversation`` after the
    primary cooldown expires, so the outer iteration loop needs the same
    opportunity to recover the primary runtime. Keep the request-local system
    prompt in sync with the restored runtime as well; otherwise the next
    request would call the primary model with the fallback identity.
    """
    if not getattr(agent, "_fallback_activated", False):
        return active_system_prompt
    try:
        if not agent._restore_primary_runtime():
            return active_system_prompt
    except Exception:
        logger.warning("Mid-turn primary restore failed; keeping fallback", exc_info=True)
        return active_system_prompt

    restored_system_prompt = getattr(agent, "_cached_system_prompt", None)
    return (
        restored_system_prompt
        if isinstance(restored_system_prompt, str)
        else active_system_prompt
    )


def _fallback_max_stay_seconds(agent):
    """Return the active fallback entry's optional mid-turn stay limit."""
    chain = getattr(agent, "_fallback_chain", None) or []
    entry = getattr(agent, "_active_fallback_entry", None)
    if not isinstance(entry, dict):
        index = int(getattr(agent, "_fallback_index", 0) or 0) - 1
        if index < 0 or index >= len(chain) or not isinstance(chain[index], dict):
            return None
        entry = chain[index]
    value = entry.get("max_stay_seconds")
    if isinstance(value, bool):
        return None
    try:
        seconds = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return seconds if math.isfinite(seconds) and seconds > 0 else None


def _maybe_recheck_fallback_route_for_iteration(agent, active_system_prompt):
    """Re-evaluate an aged fallback only at the next model-request boundary."""
    if not getattr(agent, "_fallback_activated", False):
        return active_system_prompt
    max_stay = _fallback_max_stay_seconds(agent)
    if max_stay is None:
        return _restore_primary_runtime_for_iteration(agent, active_system_prompt)
    started = getattr(agent, "_fallback_stay_started_at", None)
    if not isinstance(started, (int, float)):
        agent._fallback_stay_started_at = time.monotonic()
        return _restore_primary_runtime_for_iteration(agent, active_system_prompt)
    if time.monotonic() - started <= max_stay:
        return _restore_primary_runtime_for_iteration(agent, active_system_prompt)

    previous_index = getattr(agent, "_fallback_index", 0)
    from agent.agent_runtime_helpers import _snapshot_switch_state, _restore_switch_snapshot
    snapshot = _snapshot_switch_state(agent)
    for name in (
        "_fallback_index", "_fallback_activated", "_provider_fallback_active", "_provider_fallback_route",
        "_active_fallback_entry",
        "_cached_system_prompt", "_cached_system_prompt_static", "_use_prompt_caching", "_use_native_cache_layout",
        "reasoning_config", "reasoning_override", "_pre_fallback_reasoning_override", "request_overrides",
        "_rate_limited_until", "_rate_limit_backoff_count", "_shared_primary_cooldown_record",
    ):
        if hasattr(agent, name):
            snapshot[name] = getattr(agent, name)
    compressor = getattr(agent, "context_compressor", None)
    compressor_state = dict(vars(compressor)) if compressor is not None else None
    committed = False
    try:
        if agent._restore_primary_runtime():
            committed = True
            restored = getattr(agent, "_cached_system_prompt", None)
            return restored if isinstance(restored, str) else active_system_prompt

        # The shared primary cooldown is still active. Re-walk from the first
        # chain entry using the normal candidate-skip rules; this may land on
        # the same entry when earlier candidates remain unavailable.
        agent._fallback_index = 0
        if agent._try_activate_fallback(reason=None):
            from agent.conversation_loop import _sync_failover_system_message
            prompt = _sync_failover_system_message(agent, None, active_system_prompt)
            committed = True
            return prompt
        return active_system_prompt
    except Exception:
        logger.warning("Mid-turn fallback re-evaluation failed; keeping current route", exc_info=True)
        return active_system_prompt
    finally:
        if not committed:
            _restore_switch_snapshot(agent, snapshot)
            agent._fallback_index = previous_index
            if compressor_state is not None:
                vars(compressor).update(compressor_state)
        agent._fallback_stay_started_at = time.monotonic()


