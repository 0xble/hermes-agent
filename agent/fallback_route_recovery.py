"""Safe request-boundary recovery for long turns on a fallback route.

Primary restoration and prompt synchronization ported from upstream PR #88628;
optional per-entry stay limits retry earlier backups without clearing the shared
primary cooldown.
"""

import contextlib
import logging
import math
import time

logger = logging.getLogger(__name__)


# Shared-outage state a failed primary restore may legitimately refresh; never rolled back.
_COOLDOWN_BOOKKEEPING = ("_rate_limited_until", "_rate_limit_backoff_count", "_shared_primary_cooldown_record")


def _mid_turn_recovery_allowed(agent):
    """Return whether this route has a real provider-fallback recovery signal."""
    # A held shared record alone is not a signal: an expired record stays cached as a recovery
    # probe after the primary returns, so it can outlive the episode that armed it.
    return bool(
        getattr(agent, "_provider_fallback_active", False)
        and getattr(agent, "_fallback_episode_cooled", False)
    )


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
    # Explicit route bindings (voice turns, /model --once, etc.) also use
    # _fallback_activated, but must remain pinned until their route ends.
    # Uncooled 5xx/transport fallbacks have no mid-turn primary recovery signal.
    if not _mid_turn_recovery_allowed(agent):
        return active_system_prompt
    # restore_primary_runtime applies primary identity before rebuilding the client and reports a
    # rebuild failure as False. Mid-turn, the next request must still use one coherent route, so
    # an unrestored attempt rolls the route back. Cooldown bookkeeping it refreshed is kept.
    snapshot = _capture_route_state(agent)
    for name in _COOLDOWN_BOOKKEEPING:
        snapshot.pop(name, None)
    compressor = getattr(agent, "context_compressor", None)
    compressor_state = dict(vars(compressor)) if compressor is not None else None
    restored = False
    try:
        restored = bool(agent._restore_primary_runtime())
    except Exception:
        logger.warning("Mid-turn primary restore failed; keeping fallback", exc_info=True)
    if not restored:
        _restore_route_state(agent, snapshot)
        if compressor_state is not None:
            vars(compressor).clear()
            vars(compressor).update(compressor_state)
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


def _active_entry_position(agent, cursor):
    """Chain position of the bound fallback entry, the boundary for stay-limit reclaims.

    ``_fallback_index`` is the next-candidate cursor: a failed walk can leave it past the chain
    end while an earlier entry is still the bound route. Using it would treat later entries as
    earlier and demote a working route, so locate the entry that was actually bound.
    """
    chain = getattr(agent, "_fallback_chain", None) or []
    entry = getattr(agent, "_active_fallback_entry", None)
    if isinstance(entry, dict):
        for position, candidate in enumerate(chain):
            if candidate is entry:
                return position
        for position, candidate in enumerate(chain):
            if candidate == entry:
                return position
    return max(0, cursor - 1)


def _close_discarded_route_clients(agent, clients) -> None:
    """Retire clients made unreachable by a failed route re-evaluation."""
    seen = set()
    for client in clients:
        if client is None or id(client) in seen:
            continue
        seen.add(id(client))
        retire = getattr(agent, "_retire_shared_openai_client", None)
        if callable(retire):
            with contextlib.suppress(Exception):
                retire(client, reason="failed_fallback_reevaluation")
            continue
        close = getattr(client, "close", None)
        if callable(close):
            with contextlib.suppress(Exception):
                close()


def _capture_route_state(agent):
    """Snapshot every field a route bind can touch, so a failed bind restores exact objects
    instead of rebuilding them (a rebuild can itself fail and leave a mixed runtime)."""
    from agent.agent_runtime_helpers import _snapshot_switch_state
    from agent.voice_turn_route import _EXTRA_FIELDS, _REASONING_FIELDS
    snapshot = _snapshot_switch_state(agent)
    for name in (*_EXTRA_FIELDS, *_REASONING_FIELDS, "_cached_system_prompt", "_cached_system_prompt_static",
                 "_use_prompt_caching", "_use_native_cache_layout", "request_overrides", "_rate_limited_until",
                 "_shared_primary_cooldown_record", "_fallback_index"):
        if hasattr(agent, name):
            value = getattr(agent, name)
            snapshot[name] = dict(value) if name == "_transport_cache" and isinstance(value, dict) else value
    return snapshot


def _restore_route_state(agent, snapshot):
    """Restore a captured route and retire only clients created after the capture."""
    from agent.agent_runtime_helpers import _restore_switch_snapshot
    original = {id(snapshot.get(n)) for n in ("client", "_anthropic_client") if snapshot.get(n) is not None}
    created = [c for c in (getattr(agent, "client", None), getattr(agent, "_anthropic_client", None))
               if c is not None and id(c) not in original]
    _restore_switch_snapshot(agent, snapshot)
    _close_discarded_route_clients(agent, created)


def _try_reclaim_earlier_fallback(agent, before_index):
    """A stay expiry is recovery, not a failure: try only better-ranked entries."""
    from agent.chat_completion_helpers import (
        _fallback_entry_key, _finalize_fallback_route, _should_skip_fallback_candidate,
    )
    from agent.route_binding import bind_route_entry

    unavailable = getattr(agent, "_unavailable_fallback_keys", None)
    if unavailable is None:
        unavailable = agent._unavailable_fallback_keys = set()
    for index, entry in enumerate(agent._fallback_chain[:before_index]):
        provider = (entry.get("provider") or "").strip().lower()
        model = (entry.get("model") or "").strip()
        if _should_skip_fallback_candidate(agent, entry, _fallback_entry_key(entry), provider, model, unavailable):
            continue
        attempt = _capture_route_state(agent)
        try:
            bound = bind_route_entry(agent, entry, provider, model)
        except Exception:
            logger.debug("Earlier fallback %s could not be rebound", model, exc_info=True)
            bound = None
        if bound is None:
            # Each candidate starts from the pre-attempt route, never a partial failed bind.
            _restore_route_state(agent, attempt)
            continue
        _finalize_fallback_route(agent, provider)
        agent._fallback_index = index + 1
        logger.debug("Returned to earlier fallback %s via %s", agent.model, provider)
        return True
    return False


def _maybe_recheck_fallback_route_for_iteration(agent, active_system_prompt):
    """Re-evaluate an aged fallback only at the next model-request boundary."""
    if not getattr(agent, "_fallback_activated", False):
        return active_system_prompt
    if not getattr(agent, "_provider_fallback_active", False):
        return active_system_prompt
    # Voice bindings preserve the session's fallback provenance while running
    # on their own route; recovery must wait until that explicit binding ends.
    if getattr(agent, "_voice_route_state", None) or getattr(agent, "_turn_route_task", None):
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
    snapshot = _capture_route_state(agent)
    compressor = getattr(agent, "context_compressor", None)
    compressor_state = dict(vars(compressor)) if compressor is not None else None
    committed = False
    try:
        if _mid_turn_recovery_allowed(agent) and agent._restore_primary_runtime():
            committed = True
            restored = getattr(agent, "_cached_system_prompt", None)
            return restored if isinstance(restored, str) else active_system_prompt

        # An active primary cooldown still blocks restoration; an uncooled
        # failure has no mid-turn recovery signal. Retry only entries strictly
        # earlier than the current one: stay expiry must never demote a route.
        agent._fallback_index = 0
        if _try_reclaim_earlier_fallback(agent, _active_entry_position(agent, previous_index)):
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
            _restore_route_state(agent, snapshot)
            if compressor_state is not None:
                vars(compressor).clear()
                vars(compressor).update(compressor_state)
        if getattr(agent, "_provider_fallback_active", False):
            agent._fallback_stay_started_at = time.monotonic()
