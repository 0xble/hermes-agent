"""A real /stop must retain internal FIFO work for its explicit command tail.

Drive adapter ingress and the real runner command/busy callbacks. Transport, the agent,
and the compression-lock read are controlled boundaries; no lifecycle helper is invoked.
"""
import asyncio
import time
from unittest.mock import AsyncMock, MagicMock, patch

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.event import MessageEvent
from gateway.run import GatewayRunner
from gateway.session import SessionSource, build_session_key
from tests.gateway.restart_test_helpers import RestartTestAdapter


def test_stop_owns_retained_wake_until_command_tail():
    asyncio.run(_stop_retains_wake_for_command_tail())


async def _stop_retains_wake_for_command_tail():
    adapter = RestartTestAdapter()
    runner = GatewayRunner(GatewayConfig(
        platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="fixture")}))
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._is_user_authorized = lambda source, *, allow_adapter_delegation=True: True
    runner._agent_has_active_subagents = lambda running_agent: False
    runner._busy_input_mode = "interrupt"
    source = SessionSource(platform=Platform.TELEGRAM, user_id="u1", chat_id="c1",
                           user_name="tester", chat_type="dm")
    key = build_session_key(source)
    agent = MagicMock()
    agent.get_activity_summary.return_value = {
        "seconds_since_activity": 0.0, "last_activity_desc": "api_call",
        "api_call_count": 1, "max_iterations": 60,
    }
    runner._running_agents[key] = agent
    runner._running_agents_ts[key] = time.time() - 120
    runner._pause_goal_for_stop = AsyncMock()
    runner._async_session_store = MagicMock()
    runner._async_session_store.get_resume_pending_marker = AsyncMock(return_value=None)
    runner.pairing_store = MagicMock()
    before_handoff, never = asyncio.Event(), asyncio.Event()
    wake_waiting, wake_release, all_processed = asyncio.Event(), asyncio.Event(), asyncio.Event()
    processed, requeue_counts, tail_snapshot = [], [], []
    old_owner = None
    real_stop_typing = adapter._stop_typing_refresh
    real_tail = adapter._drain_pending_after_session_command

    async def compression_locked(session_key):
        event = getattr(asyncio.current_task(), "_hermes_event", None)
        if getattr(event, "message_id", None) == "wake":
            wake_waiting.set()
            await wake_release.wait()
            return False
        return getattr(event, "message_id", None) == "queued"

    async def run_agent(event, source, _quick_key: str, run_generation: int):
        processed.append(event.text)
        requeue_counts.append(adapter._requeue_counts.get(key, 0))
        if len(processed) == 2:
            all_processed.set()
        return "processed: " + event.text

    async def stop_typing(*args, **kwargs):
        nonlocal old_owner
        if old_owner is None and key in adapter._pending_messages:
            old_owner = asyncio.current_task()
            before_handoff.set()
            await never.wait()  # Cancel the actual pre-handoff owner, not a retry-counter proxy.
        return await real_stop_typing(*args, **kwargs)

    async def command_tail(session_key, command_guard):
        tail_snapshot.append((adapter._pending_messages.get(key), adapter._session_tasks.get(key)))
        return await real_tail(session_key, command_guard)

    def event(text, message_id, *, internal=False):
        return MessageEvent(text=text, source=source, message_id=message_id, internal=internal)

    runner._session_has_compression_in_flight = compression_locked
    runner._handle_message_with_agent = run_agent
    runner._run_post_turn_hooks = AsyncMock()
    # The same standalone callbacks selected by the production adapter wiring.
    adapter.set_message_handler(runner._primary_message_handler())
    adapter.set_busy_session_handler(runner._primary_busy_session_handler())
    wake = event("retained internal obligation", "wake", internal=True)
    with (patch.object(adapter, "_stop_typing_refresh", stop_typing),
          patch.object(adapter, "_drain_pending_after_session_command", command_tail)):
        try:
            await adapter.handle_message(event("stale human follow-up", "queued"))
            await asyncio.wait_for(before_handoff.wait(), 5)
            await adapter.handle_message(wake)
            await adapter.handle_message(event("/stop", "stop"))
            assert old_owner is not None
            assert old_owner.cancelled()
            agent.interrupt.assert_called_with("Stop requested")
            assert any("Stopped" in message for message in adapter.sent)
            assert tail_snapshot == [(wake, None)], (
                "cancelled owner consumed the retained wake and spawned an uncancelled successor "
                "before the real /stop command tail", tail_snapshot)
            await asyncio.wait_for(wake_waiting.wait(), 5)
            await adapter.handle_message(event("fresh followup", "followup"))
            wake_release.set()
            await asyncio.wait_for(all_processed.wait(), 5)
            assert processed == ["retained internal obligation", "fresh followup"], processed
            assert requeue_counts == [0, 0], "genuine FIFO follow-ups must not enter requeue backoff"
        finally:
            wake_release.set()
            await adapter.cancel_background_tasks()
