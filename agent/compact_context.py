"""Agent-initiated context compaction request gates and lifecycle."""

from __future__ import annotations

import json
import threading
from typing import Any, Optional

from agent.model_metadata import estimate_messages_tokens_rough


MAX_COMPACTION_FOCUS_CHARS = 1500
_DEFAULT_FLOOR_RATIO = 0.30
_DEFAULT_COOLDOWN_API_CALLS = 10


def _compressor(agent: Any) -> Any:
    return getattr(agent, "context_compressor", None)


def context_usage(agent: Any, messages: Optional[list] = None) -> tuple[int, int]:
    """Return ``(current context tokens, automatic threshold tokens)`` for model feedback."""
    compressor = _compressor(agent)
    threshold = int(getattr(compressor, "threshold_tokens", 0) or 0)
    current = int(getattr(compressor, "last_prompt_tokens", 0) or 0)
    if current <= 0 or current == -1:
        current = int(getattr(agent, "_last_prompt_tokens", 0) or 0)
    if current <= 0:
        current = estimate_messages_tokens_rough(messages or getattr(agent, "_session_messages", []) or [])
    return max(0, current), max(0, threshold)


def _set_state(agent: Any, state: str) -> None:
    with _request_lock(agent):
        agent._agent_compaction_request_state = state


def _result(*, accepted: bool, state: str = "refused", reason: str, current: int, threshold: int) -> str:
    return json.dumps({
        "accepted": accepted,
        "state": state,
        "reason": reason,
        "current_context_tokens": current,
        "auto_compress_threshold_tokens": threshold,
    }, ensure_ascii=False)


_LOCK_INIT = threading.Lock()


def _request_lock(agent: Any) -> threading.Lock:
    # agent_init creates the lock up front; the guarded fallback covers bare test agents.
    lock = getattr(agent, "_agent_compaction_request_lock", None)
    if lock is None:
        with _LOCK_INIT:
            lock = getattr(agent, "_agent_compaction_request_lock", None)
            if lock is None:
                lock = threading.Lock()
                agent._agent_compaction_request_lock = lock
    return lock


def request_compaction(agent: Any, args: dict, *, messages: Optional[list] = None) -> str:
    """Record one request for the next safe boundary without compacting in-flight tools."""
    current, threshold = context_usage(agent, messages)
    reason = str(args.get("reason") or "").strip()
    focus = str(args.get("focus") or "").strip()
    if len(focus) > MAX_COMPACTION_FOCUS_CHARS:
        return _result(
            accepted=False,
            reason=f"focus exceeds {MAX_COMPACTION_FOCUS_CHARS} characters; shorten narration, preserving exact identifiers, paths, decisions and open items, then retry",
            current=current, threshold=threshold,
        )
    if not reason:
        return _result(accepted=False, reason="reason is required", current=current, threshold=threshold)

    with _request_lock(agent):
        compressor = _compressor(agent)
        if str(getattr(agent, "api_mode", "") or "") == "codex_app_server":
            return _result(
                accepted=False,
                reason="agent-requested compaction is unsupported for codex_app_server; native thread compaction cannot preserve focus",
                current=current,
                threshold=threshold,
            )
        if not bool(getattr(agent, "compression_enabled", True)):
            return _result(accepted=False, reason="compression is disabled", current=current, threshold=threshold)
        if bool(getattr(compressor, "awaiting_real_usage_after_compression", False)):
            return _result(
                accepted=False,
                reason="a compaction already ran; wait for fresh provider usage before requesting another",
                current=current,
                threshold=threshold,
            )
        if bool(getattr(agent, "_agent_requested_compaction_this_turn", False)):
            return _result(accepted=False, reason="already requested or compacted by the agent this turn", current=current, threshold=threshold)

        _ratio = getattr(agent, "compression_agent_request_floor_ratio", None)
        floor_ratio = _DEFAULT_FLOOR_RATIO if _ratio is None else max(0.0, float(_ratio))
        floor = int(threshold * floor_ratio) if threshold > 0 else 0
        if floor > 0 and current < floor:
            return _result(
                accepted=False,
                reason=f"context is below the agent-request floor ({current:,} < {floor:,} tokens)",
                current=current,
                threshold=threshold,
            )

        cooldown = int(getattr(agent, "compression_agent_request_cooldown_api_calls", _DEFAULT_COOLDOWN_API_CALLS) or 0)
        # Session-wide count: the per-turn _api_call_count restarts every turn.
        api_calls = int(getattr(agent, "session_api_calls", 0) or 0)
        last_call = getattr(agent, "_last_agent_compaction_request_api_call", None)
        if last_call is not None and api_calls - int(last_call) < cooldown:
            return _result(
                accepted=False,
                reason=f"agent-requested compaction cooldown active; wait {cooldown - (api_calls - int(last_call))} API calls",
                current=current,
                threshold=threshold,
            )

        attempts = int(getattr(agent, "_compression_attempts", 0) or 0)
        max_attempts = int(getattr(agent, "max_compression_attempts", 0) or 0)
        if max_attempts > 0 and attempts >= max_attempts:
            return _result(
                accepted=False,
                reason=f"compression attempt budget exhausted ({attempts}/{max_attempts})",
                current=current,
                threshold=threshold,
            )

        agent._pending_agent_compaction = {"focus": focus, "reason": reason}
        agent._agent_requested_compaction_this_turn = True
        agent._last_agent_compaction_request_api_call = api_calls
        agent._agent_compaction_request_state = "accepted"
        return _result(
            accepted=True,
            state="accepted",
            reason="accepted; compaction will run at the next safe post-tool boundary",
            current=current,
            threshold=threshold,
        )


def take_pending_compaction(agent: Any) -> Optional[dict]:
    """Atomically claim a pending request for one post-tool boundary."""
    with _request_lock(agent):
        if getattr(agent, "_active_agent_compaction", None) is not None:
            return None
        pending = getattr(agent, "_pending_agent_compaction", None)
        if not isinstance(pending, dict):
            return None
        claimed = dict(pending)
        agent._active_agent_compaction = claimed
        return claimed


def release_pending_compaction(agent: Any) -> None:
    """Return a claimed request after a temporary lock/transient defer."""
    with _request_lock(agent):
        active = getattr(agent, "_active_agent_compaction", None)
        if isinstance(active, dict):
            agent._pending_agent_compaction = dict(active)
        agent._active_agent_compaction = None
        agent._agent_compaction_request_state = "deferred"


def clear_pending_compaction(agent: Any) -> None:
    with _request_lock(agent):
        agent._pending_agent_compaction = None
        agent._active_agent_compaction = None


def mark_compaction_refused(agent: Any) -> None:
    with _request_lock(agent):
        agent._pending_agent_compaction = None
        agent._active_agent_compaction = None
        agent._agent_compaction_request_state = "refused"


def mark_compaction_failure(agent: Any) -> None:
    """A hard failure consumes the request; temporary lock/transient blocks do not."""
    with _request_lock(agent):
        agent._pending_agent_compaction = None
        agent._active_agent_compaction = None
        agent._agent_compaction_request_state = "failed"


def reset_compaction_request_turn(agent: Any) -> None:
    with _request_lock(agent):
        agent._pending_agent_compaction = None
        agent._active_agent_compaction = None
        agent._agent_requested_compaction_this_turn = False
        agent._agent_compaction_request_state = None


def commit_compaction(agent: Any) -> None:
    with _request_lock(agent):
        agent._pending_agent_compaction = None
        agent._active_agent_compaction = None
        agent._agent_compaction_request_state = "committed"
        # A committed pass must wait for the next provider usage sample before another request.
        compressor = _compressor(agent)
        if compressor is not None:
            compressor.awaiting_real_usage_after_compression = True
        # Keep the per-turn flag armed so duplicate calls are refused until turn end.
        agent._agent_requested_compaction_this_turn = True
