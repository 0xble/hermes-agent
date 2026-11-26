"""Soft preemption of machine-origin turns by a typed user message, including the pending-agent window."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger("gateway.run")
_REASON = "gateway user turn preemption"


async def _wait_for_events(events: list[Any], timeout: float) -> bool:
    """Wait for the first real state event; True only when the bound expired with none set."""
    waits = [asyncio.create_task(event.wait()) for event in events if event is not None]
    if not waits:
        return False
    try:
        done, _ = await asyncio.wait(set(waits), timeout=timeout, return_when=asyncio.FIRST_COMPLETED)
        return not done
    finally:
        for waiter in waits:
            waiter.cancel()
        await asyncio.gather(*waits, return_exceptions=True)


def _eligible_machine_waiter(runner: Any, event: Any, holder: Any) -> bool:
    """Keep relay, agent-origin, internal, and non-machine holders on queue semantics."""
    from gateway.response_filters import is_agent_origin_text
    if event is None or is_agent_origin_text(getattr(event, "text", None)):
        return False
    is_user_turn = getattr(runner, "_is_user_turn_event", None)
    if (not is_user_turn(event)) if callable(is_user_turn) else getattr(event, "internal", False):
        return False
    return bool(getattr(getattr(holder, "event", None), "internal", False))


async def _send_queued_ack(runner: Any, event: Any, quick_key: str) -> None:
    from gateway.run_turn_lease_wait import lease_wait_notice
    await lease_wait_notice(runner, quick_key, event)


async def preempt_machine_holder(runner: Any, event: Any, quick_key: str, holder: Any, timeout: float) -> None:
    """Cold path: yield an internal lease holder, waiting (bounded by ``timeout``) for a pending agent."""
    from gateway.meeting_lane import busy_input_mode
    from gateway.run import _AGENT_PENDING_SENTINEL
    if not _eligible_machine_waiter(runner, event, holder) or await busy_input_mode(runner, event, quick_key) == "steer":
        return

    def live_agent() -> Any:
        state = runner._peek_session_state(holder.owner_key)
        turn = getattr(state, "turn", None)
        agent = getattr(turn, "agent", None)
        return state, turn, (None if agent is _AGENT_PENDING_SENTINEL else agent)

    def holds() -> bool:
        current = getattr(runner, "_current_session_run_generation", lambda _key: holder.generation)
        return (holder.lease.holder is holder and not holder.released
                and int(holder.generation) == int(current(holder.owner_key)))

    if not holds():
        return
    state, turn, agent = live_agent()
    if agent is None:
        if await _wait_for_events([getattr(turn, "agent_published_event", None), holder.released_event], timeout):
            await _send_queued_ack(runner, event, quick_key)
        if not holds():
            return
        state, turn, agent = live_agent()
    if agent is None or runner._agent_has_active_subagents(agent):
        return
    compression_check = getattr(runner, "_session_has_compression_in_flight", None)
    if callable(compression_check) and await compression_check(quick_key):
        return
    adapter = runner._delivery_adapter_for(event.source)
    # Final identity check; no await separates it from interrupt().
    _, turn, current = live_agent()
    if (adapter is None or not holds() or current is not agent
            or getattr(turn, "agent_published_generation", holder.generation) != holder.generation):
        return
    try:
        agent.interrupt(None, tool_reason=_REASON)
    except Exception:
        logger.debug("Cold-path machine-turn preemption failed", exc_info=True)
        return
    logger.info("User message preempts autonomous turn on lease wait (session=%s)", quick_key)
    if not getattr(event, "_gateway_busy_ack_sent", False):
        from agent.i18n import t
        from agent.notice_style import ContextNotice
        notice = ContextNotice(t("gateway.busy.ack", head=t("gateway.busy.interrupting_head"), detail="",
                                 tail=t("gateway.busy.interrupting_tail")))
        notice._temporary = True  # type: ignore[attr-defined]
        await runner._send_busy_ack_reply(event, adapter, notice)


async def _authority_preemption(
    runner: Any, event: Any, source: Any, quick_key: str, state: Any, turn: Any, generation: int,
    running_agent: Any,
) -> None:
    """Authority path: the FIFO row is already admitted; cut the machine turn without delaying it."""
    from gateway.run import _AGENT_PENDING_SENTINEL
    from gateway.run_turn_lease_wait import lease_wait_max_seconds
    adapter = runner._delivery_adapter_for(source)
    if adapter is None:
        return
    opener, pending = turn.event, running_agent is _AGENT_PENDING_SENTINEL

    def outcome(mode: str, subagents: bool = False, compression: bool = False) -> Any:
        return runner._BusySteerOutcome(
            effective_mode=mode, demoted_for_subagents=subagents, demoted_for_compression=compression,
            steered=False, redirected=False)

    if pending and await _wait_for_events([turn.agent_published_event, turn.turn_ended_event], lease_wait_max_seconds()):
        # Never published within the bound: behave exactly as plain queue mode (no interrupt, one ack).
        await runner._busy_interrupt_and_ack(event, quick_key, adapter, state, running_agent, outcome("queue"))
        return

    def current() -> Any:
        """The published agent when this exact turn, opener, run generation and agent still hold."""
        agent = turn.agent
        if (turn.event is not opener or agent is None or agent is _AGENT_PENDING_SENTINEL
                or int(runner._current_session_run_generation(quick_key)) != int(generation)):
            return None
        if pending:
            return agent if (turn.agent_published_generation == generation
                             and turn.agent_published_agent is agent) else None
        return agent if agent is running_agent else None

    agent = current()
    if agent is None:
        return
    subagents = bool(runner._agent_has_active_subagents(agent))
    compression = bool(await runner._session_has_compression_in_flight(quick_key))
    if current() is not agent:  # final identity check; no await before interrupt()
        return
    if subagents or compression:
        await runner._busy_interrupt_and_ack(event, quick_key, adapter, state, agent, outcome("queue", subagents, compression))
        return
    try:
        agent.interrupt(None, tool_reason=_REASON)
    except Exception:
        logger.debug("Authority machine-turn preemption failed", exc_info=True)
        return
    preempted = outcome("interrupt")
    preempted.machine_preempted = True  # type: ignore[attr-defined]
    await runner._busy_interrupt_and_ack(event, quick_key, adapter, state, agent, preempted)


def schedule_authority_machine_preemption(
    runner: Any, event: Any, source: Any, quick_key: str, state: Any, turn: Any, generation: int,
    running_agent: Any,
) -> asyncio.Task:
    """Track the preemption as one background task so adapter admission stays non-blocking."""
    task = asyncio.create_task(_authority_preemption(
        runner, event, source, quick_key, state, turn, generation, running_agent))
    retain = getattr(runner, "_retain_background_task", None)
    if callable(retain):
        retain(task)
    else:
        task.add_done_callback(lambda done: done.cancelled() or done.exception())
    return task
