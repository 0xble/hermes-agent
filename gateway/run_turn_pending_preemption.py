"""Pending-agent admission helpers for turn-lease preemption."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger("gateway.run")


async def await_pending_agent_or_release(holder: Any, state: Any, timeout: float, runner: Any) -> None:
    """Wait only for this holder's publication or release, never cancel its task."""
    turn = getattr(state, "turn", None) if state is not None else None
    published = getattr(turn, "agent_published_event", None)
    released = getattr(holder, "released_event", None)
    if published is not None and released is not None:
        publication_wait = asyncio.create_task(published.wait())
        release_wait = asyncio.create_task(released.wait())
        try:
            await asyncio.wait(
                {publication_wait, release_wait}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            for task in (publication_wait, release_wait):
                if not task.done():
                    task.cancel()
            await asyncio.gather(publication_wait, release_wait, return_exceptions=True)
        return
    # Compatibility for partially constructed test runners and old state objects. The production
    # path always has both events; this fallback remains bounded by the lease wait.
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while holder.lease.holder is holder and not holder.released:
        current = runner._peek_session_state(holder.owner_key)  # type: ignore[reportAttributeAccessIssue]
        current_agent = getattr(getattr(current, "turn", None), "agent", None)
        from gateway.run import _AGENT_PENDING_SENTINEL
        if current_agent is not None and current_agent is not _AGENT_PENDING_SENTINEL:
            return
        remaining = deadline - loop.time()
        if remaining <= 0:
            return
        await asyncio.sleep(min(0.05, remaining))


async def _send_preemption_ack(runner: Any, event: Any, adapter: Any) -> None:
    """Send the existing interrupting ack once for a cold-path user waiter."""
    if getattr(event, "_gateway_busy_ack_sent", False):
        return
    from agent.i18n import t
    from agent.notice_style import ContextNotice
    notice = ContextNotice(t(
        "gateway.busy.ack", head=t("gateway.busy.interrupting_head"), detail="",
        tail=t("gateway.busy.interrupting_tail")))
    notice._temporary = True  # type: ignore[attr-defined]
    send_ack = getattr(runner, "_send_busy_ack_reply", None)
    if callable(send_ack):
        await send_ack(event, adapter, notice)  # type: ignore[reportGeneralTypeIssues]


async def _interrupt_published_agent(runner: Any, event: Any, quick_key: str, holder: Any, timeout: float) -> None:
    """Recheck the holder after the final await, then interrupt and acknowledge preemption."""
    from gateway.run import _AGENT_PENDING_SENTINEL
    if holder.lease.holder is not holder or holder.released:
        return
    state = runner._peek_session_state(holder.owner_key)
    running_agent = getattr(getattr(state, "turn", None), "agent", None) if state is not None else None
    if running_agent is None or running_agent is _AGENT_PENDING_SENTINEL:
        return
    if runner._agent_has_active_subagents(running_agent):
        return
    adapter = runner._delivery_adapter_for(event.source)
    if adapter is None:
        return
    try:
        running_agent.interrupt(None, tool_reason="gateway user turn preemption")
    except Exception:
        logger.debug("Cold-path machine-turn preemption failed", exc_info=True)
        return
    logger.info("User message preempts autonomous turn on lease wait (session=%s)", quick_key)
    await _send_preemption_ack(runner, event, adapter)



def _is_eligible_machine_waiter(runner: Any, event: Any, holder: Any) -> bool:
    """Keep relay, agent-origin, internal, and non-machine holders on queue semantics."""
    if event is None:
        return False
    is_user_turn = getattr(runner, "_is_user_turn_event", None)
    if callable(is_user_turn) and not is_user_turn(event):
        return False
    if not callable(is_user_turn) and getattr(event, "internal", False):
        return False
    from gateway.response_filters import is_agent_origin_text
    if is_agent_origin_text(getattr(event, "text", None)):
        return False
    return bool(getattr(getattr(holder, "event", None), "internal", False))


async def _resolve_published_agent(runner: Any, holder: Any, state: Any, timeout: float) -> Any:
    """Return this holder's published agent, or None after release/timeout/staleness."""
    from gateway.run import _AGENT_PENDING_SENTINEL
    agent = getattr(getattr(state, "turn", None), "agent", None) if state is not None else None
    if agent is not None and agent is not _AGENT_PENDING_SENTINEL:
        return agent
    await await_pending_agent_or_release(holder, state, timeout, runner)
    if holder.lease.holder is not holder or holder.released:
        return None
    state = runner._peek_session_state(holder.owner_key)
    agent = getattr(getattr(state, "turn", None), "agent", None) if state is not None else None
    return None if agent is None or agent is _AGENT_PENDING_SENTINEL else agent


async def preempt_machine_holder(runner: Any, event: Any, quick_key: str, holder: Any, timeout: float) -> None:
    """Validate a user waiter and wait for a pending internal holder to publish."""
    if not _is_eligible_machine_waiter(runner, event, holder):
        return
    from gateway.meeting_lane import busy_input_mode
    if await busy_input_mode(runner, event, quick_key) == "steer":
        return
    if holder.lease.holder is not holder or holder.released:
        return
    state = runner._peek_session_state(holder.owner_key)
    running_agent = await _resolve_published_agent(runner, holder, state, timeout)
    if running_agent is None:
        return
    if runner._agent_has_active_subagents(running_agent):
        return
    compression_check = getattr(runner, "_session_has_compression_in_flight", None)
    if callable(compression_check) and await compression_check(quick_key):  # type: ignore[reportGeneralTypeIssues]
        return
    await _interrupt_published_agent(runner, event, quick_key, holder, timeout)

