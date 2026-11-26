"""Pending-agent admission helpers for turn-lease preemption."""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable, Callable, cast

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


async def _authority_machine_preemption_impl(
    runner: Any, event: Any, source: Any, quick_key: str, state: Any, turn: Any,
    generation: int, running_agent: Any,
) -> None:
    """Soft-preempt an authority-owned machine turn without delaying FIFO admission.

    The caller schedules this coroutine and immediately admits ``event`` as a durable FIFO row.
    Every await rechecks the captured turn opener and generation, so a pending publication cannot
    interrupt a successor turn that reused the session slot.
    """
    from gateway.run import _AGENT_PENDING_SENTINEL
    adapter = runner._delivery_adapter_for(source)
    if adapter is None:
        return
    opener = getattr(turn, "event", None)
    published_event = getattr(turn, "agent_published_event", None)
    ended_event = getattr(turn, "turn_ended_event", None)
    if running_agent is _AGENT_PENDING_SENTINEL:
        waits = [ev for ev in (published_event, ended_event) if ev is not None]
        if not waits:
            return
        waiter_tasks = [_tracked_wait_task(runner, ev.wait()) for ev in waits]
        try:
            await asyncio.wait(
                set(waiter_tasks), timeout=5.0, return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            for waiter in waiter_tasks:
                if not waiter.done():
                    waiter.cancel()
            await asyncio.gather(*waiter_tasks, return_exceptions=True)

    current_state = runner._peek_session_state(quick_key)
    if current_state is not state or getattr(current_state, "turn", None) is not turn:
        return
    if getattr(turn, "event", None) is not opener:
        return
    current_generation = runner._current_session_run_generation(quick_key)
    if int(current_generation) != int(generation):
        return
    current_agent = getattr(turn, "agent", None)
    if current_agent is None or current_agent is _AGENT_PENDING_SENTINEL:
        return
    published_generation = getattr(turn, "agent_published_generation", None)
    published_agent = getattr(turn, "agent_published_agent", None)
    if running_agent is _AGENT_PENDING_SENTINEL:
        if published_generation is not None and int(published_generation) != int(generation):
            return
        if published_agent is not None and published_agent is not current_agent:
            return
    elif current_agent is not running_agent:
        return

    demoted_for_subagents = bool(runner._agent_has_active_subagents(current_agent))
    compression_check = cast(
        Callable[[str], Awaitable[bool]] | None,
        getattr(runner, "_session_has_compression_in_flight", None),
    )
    demoted_for_compression = bool(
        callable(compression_check) and await compression_check(quick_key)
    )
    # Compression and subagent checks both await or inspect mutable state; fence the final action.
    current_state = runner._peek_session_state(quick_key)
    if (current_state is not state or getattr(current_state, "turn", None) is not turn
            or getattr(turn, "event", None) is not opener
            or int(runner._current_session_run_generation(quick_key)) != int(generation)
            or getattr(turn, "agent", None) is not current_agent):
        return
    if demoted_for_subagents or demoted_for_compression:
        outcome = runner._BusySteerOutcome(
            effective_mode="queue", demoted_for_subagents=demoted_for_subagents,
            demoted_for_compression=demoted_for_compression, steered=False, redirected=False,
        )
        await runner._busy_interrupt_and_ack(
            event, quick_key, adapter, state, current_agent, outcome,
        )
        return
    try:
        current_agent.interrupt(None, tool_reason="gateway user turn preemption")
    except Exception:
        logger.debug("Authority machine-turn preemption failed", exc_info=True)
        return
    outcome = runner._BusySteerOutcome(
        effective_mode="interrupt", demoted_for_subagents=False,
        demoted_for_compression=False, steered=False, redirected=False,
    )
    setattr(outcome, "machine_preempted", True)
    await runner._busy_interrupt_and_ack(event, quick_key, adapter, state, current_agent, outcome)


def _tracked_wait_task(runner: Any, awaitable: Any) -> asyncio.Task:
    """Create a short-lived waiter under the gateway's normal task accounting."""
    task = asyncio.create_task(awaitable)
    retain = getattr(runner, "_retain_background_task", None)
    if callable(retain):
        return cast(asyncio.Task, retain(task))
    task.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)
    return task


def schedule_authority_machine_preemption(
    runner: Any, event: Any, source: Any, quick_key: str, state: Any, turn: Any,
    generation: int, running_agent: Any,
) -> asyncio.Task:
    """Track the authority preemption so adapter admission remains non-blocking."""
    task = asyncio.create_task(_authority_machine_preemption_impl(
        runner, event, source, quick_key, state, turn, generation, running_agent,
    ))
    retain = getattr(runner, "_retain_background_task", None)
    if callable(retain):
        return cast(asyncio.Task, retain(task))
    task.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)
    return task


def schedule_authority_queued_ack(
    runner: Any, event: Any, source: Any, quick_key: str, state: Any, running_agent: Any,
) -> asyncio.Task:
    """Send queue-mode's existing acknowledgement without delaying FIFO admission."""
    adapter = runner._delivery_adapter_for(source)
    outcome = runner._BusySteerOutcome(
        effective_mode="queue", demoted_for_subagents=False,
        demoted_for_compression=False, steered=False, redirected=False,
    )
    task = asyncio.create_task(runner._busy_interrupt_and_ack(
        event, quick_key, adapter, state, running_agent, outcome,
    ))
    retain = getattr(runner, "_retain_background_task", None)
    if callable(retain):
        return cast(asyncio.Task, retain(task))
    task.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)
    return task


async def preempt_machine_holder(runner: Any, event: Any, quick_key: str, holder: Any, timeout: float) -> None:
    """Validate a user waiter and wait for a pending internal holder to publish."""
    if not _is_eligible_machine_waiter(runner, event, holder):
        return
    from gateway.meeting_lane import busy_input_mode
    if await busy_input_mode(runner, event, quick_key) == "steer":
        return
    if holder.lease.holder is not holder or holder.released:
        return
    current_generation = getattr(runner, "_current_session_run_generation", lambda _key: holder.generation)(holder.owner_key)
    if int(holder.generation) != int(current_generation):
        return
    state = runner._peek_session_state(holder.owner_key)
    running_agent = await _resolve_published_agent(runner, holder, state, timeout)
    if running_agent is None:
        return
    if runner._agent_has_active_subagents(running_agent):
        return
    compression_check = cast(
        Callable[[str], Awaitable[bool]] | None,
        getattr(runner, "_session_has_compression_in_flight", None),
    )
    if callable(compression_check) and await compression_check(quick_key):
        return
    await _interrupt_published_agent(runner, event, quick_key, holder, timeout)

