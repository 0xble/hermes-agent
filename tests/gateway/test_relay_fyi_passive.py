"""FYI relay ingress is transcript-only unless it must wake autonomous work."""

from __future__ import annotations

from datetime import datetime
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform
from gateway.platforms.base import merge_pending_message_event
from gateway.platforms.event import MessageEvent, MessageType
from gateway.response_filters import is_fyi_relay_text
from gateway.run import GatewayRunner
from gateway.run_busy import GatewayBusySessionMixin
from gateway.session import SessionSource


FYI = (
    "📨 Relay title\n\n"
    "A status update.\n"
    "A second detail line.\n"
    "\n"
    "From: Source Agent · FYI, no action requested\n"
    "[relay from=hermes:default/source receipt=receipt-1]"
)
TASK = FYI.replace("receipt=receipt-1", "receipt=receipt-1 task=task-1")


class _Store:
    def __init__(self, entry=None, *, duplicate=False):
        self.entry = entry
        self.duplicate = duplicate
        self.rows = []

    async def lookup_by_session_key(self, _key):
        return self.entry

    async def append_to_transcript(self, session_id, row, **_kwargs):
        self.rows.append((session_id, row))

    async def has_platform_message_id(self, _session_id, _message_id):
        return self.duplicate


@pytest.fixture
def runner(monkeypatch):
    runner = object.__new__(GatewayRunner)
    store = _Store(SimpleNamespace(session_id="target-session"))
    runner.session_store = store
    runner._async_session_store = SimpleNamespace(
        _store=store,
        lookup_by_session_key=store.lookup_by_session_key,
        append_to_transcript=store.append_to_transcript,
        has_platform_message_id=store.has_platform_message_id,
    )
    runner._test_store = store
    runner._sessions = {}
    runner._is_session_running = MagicMock(return_value=False)
    runner._session_state = GatewayRunner._session_state.__get__(runner, GatewayRunner)
    runner._claim_active_session_slot = MagicMock(return_value=(None, None))
    runner._persist_active_agents = MagicMock()
    runner._release_running_agent_state = GatewayRunner._release_running_agent_state.__get__(runner, GatewayRunner)
    runner._refresh_agent_cache_message_count = AsyncMock()
    runner._delivery_adapter_for = MagicMock(return_value=None)
    runner._overflow_queue = MagicMock(return_value=[])

    async def run_in_executor(fn, *args, **kwargs):
        return fn(*args, **kwargs)

    runner._run_in_executor_with_context = run_in_executor
    monkeypatch.setattr("gateway.run._load_gateway_config", lambda: {})
    monkeypatch.setattr("hermes_cli.goals._get_session_db", lambda: object())
    monkeypatch.setattr("hermes_cli.goals.load_goal", lambda _sid: None)
    monkeypatch.setattr("hermes_cli.loops.load_loop", lambda _sid: None)
    return runner


def _event(text, *, reply_expected=None):
    source = SessionSource(
        platform=Platform.TELEGRAM, chat_id="chat", user_id="user", chat_type="dm",
    )
    return MessageEvent(
        text=text, message_type=MessageType.TEXT, source=source, message_id="telegram-1",
        timestamp=datetime(2026, 10, 15, 12, 0, 0), reply_expected=reply_expected,
    )


@pytest.mark.asyncio
async def test_fyi_is_persisted_unchanged_without_a_turn(runner):
    event = _event(FYI)
    assert is_fyi_relay_text(event.text)

    handled = await runner._hm_try_passive_fyi_relay(event, event.source, "session-key")

    assert handled is True
    assert runner._test_store.rows == [
        (
            "target-session",
            {
                "role": "user",
                "content": FYI,
                "timestamp": event.timestamp.timestamp(),
                "message_id": "telegram-1",
            },
        )
    ]
    runner._refresh_agent_cache_message_count.assert_awaited_once_with("session-key", "target-session")


@pytest.mark.asyncio
async def test_replayed_fyi_message_id_is_not_appended_again(runner):
    runner._test_store.duplicate = True
    event = _event(FYI)

    assert await runner._hm_try_passive_fyi_relay(event, event.source, "session-key") is True

    assert runner._test_store.rows == []
    runner._refresh_agent_cache_message_count.assert_not_awaited()
    assert event._agent_turn_succeeded is True


@pytest.mark.asyncio
async def test_idle_fyi_rechecks_session_before_persisting(runner):
    runner._is_session_running = MagicMock(return_value=True)
    event = _event(FYI)

    assert await runner._hm_try_passive_fyi_relay(event, event.source, "session-key") is False
    assert runner._test_store.rows == []


@pytest.mark.asyncio
async def test_topic_root_lobby_guard_precedes_passive_admission(runner):
    event = _event(FYI)
    runner._hm_admit_event = AsyncMock(return_value=(event, event.source, False))
    runner._draining = False
    runner._hm_is_registered_command = MagicMock(return_value=False)
    runner._quick_command_alias_text = MagicMock(return_value=None)
    runner._hm_estop_gate = MagicMock(return_value=None)
    runner._session_key_for_source = MagicMock(return_value="session-key")
    runner._hm_pending_reply_intercepts = AsyncMock(return_value=None)
    runner._hm_evict_idle_stale_agent = MagicMock()
    runner._is_session_running = MagicMock(return_value=False)
    runner._hm_dispatch_idle_commands = AsyncMock(return_value=(False, None))
    runner._is_telegram_topic_root_lobby = MagicMock(return_value=True)
    runner._should_send_telegram_lobby_reminder = MagicMock(return_value=False)
    runner._hm_try_passive_fyi_relay = AsyncMock(side_effect=AssertionError("lobby must win"))

    assert await runner._handle_admitted_message(event) is None
    runner._hm_try_passive_fyi_relay.assert_not_awaited()


@pytest.mark.asyncio
async def test_passive_admission_yields_to_orphaned_fifo(runner):
    adapter = SimpleNamespace(_pending_messages={})
    runner._delivery_adapter_for.return_value = adapter
    runner._overflow_queue.return_value = [_event("old queued event")]

    assert runner._hm_has_orphaned_fifo("session-key", _event(FYI).source) is True

    runner._overflow_queue.return_value = []
    assert runner._hm_has_orphaned_fifo("session-key", _event(FYI).source) is False


@pytest.mark.asyncio
async def test_two_queued_fyis_use_real_fifo_and_persist_in_order(runner):
    first, second = _event(FYI), _event(FYI.replace("receipt-1", "receipt-2"))
    adapter = _DrainAdapter(first)
    runner._overflow_queue = MagicMock(return_value=[second])

    pending_event, pending = await runner._run_agent_drain_pending(
        {"final_response": "completed"}, adapter, first.source, "session-key",
    )

    assert pending_event is None and pending is None
    first = adapter.get_pending_message("session-key")
    assert first is not None
    assert runner._hm_has_orphaned_fifo("session-key", first.source) is False
    assert await runner._hm_try_passive_fyi_relay(first, first.source, "session-key") is True

    second = runner._promote_queued_event("session-key", adapter, None)
    assert second is not None
    assert await runner._hm_try_passive_fyi_relay(second, second.source, "session-key") is True
    assert [row[1]["content"] for row in runner._test_store.rows] == [FYI, second.text]
    assert not hasattr(getattr(runner, "_handle_message_with_agent", None), "call_count") or runner._handle_message_with_agent.call_count == 0


@pytest.mark.asyncio
async def test_mixed_fifo_keeps_ordinary_work_ahead_of_passive_fyis(runner):
    first = _event(FYI)
    ordinary = _event("Please continue.")
    second = _event(FYI.replace("receipt-1", "receipt-2"))
    adapter = _DrainAdapter(first)
    runner._overflow_queue = MagicMock(return_value=[ordinary, second])

    pending_event, pending = await runner._run_agent_drain_pending(
        {"final_response": "completed"}, adapter, first.source, "session-key",
    )

    assert pending_event is ordinary
    assert pending == ordinary.text
    assert adapter._pending_messages["session-key"] is second
    assert runner._overflow_queue.return_value == [first]
    assert runner._hm_has_orphaned_fifo("session-key", first.source) is False


@pytest.mark.asyncio
async def test_passive_write_claim_blocks_interleaving_turn(runner):
    runner._is_session_running = GatewayRunner._is_session_running.__get__(runner, GatewayRunner)
    async def no_active_wakes(_fn, *_args, **_kwargs):
        return None, None
    runner._run_in_executor_with_context = no_active_wakes
    entered_duplicate = asyncio.Event()
    release_duplicate = asyncio.Event()

    async def has_message(*_args):
        entered_duplicate.set()
        await release_duplicate.wait()
        return False

    runner._test_store.has_platform_message_id = has_message
    runner.async_session_store.has_platform_message_id = has_message
    first, second = _event(FYI), _event(FYI.replace("receipt-1", "receipt-2"))
    first_task = asyncio.create_task(runner._hm_try_passive_fyi_relay(first, first.source, "session-key"))
    await entered_duplicate.wait()
    assert await runner._hm_try_passive_fyi_relay(second, second.source, "session-key") is False
    release_duplicate.set()
    assert await first_task is True
    assert len(runner._test_store.rows) == 1


def test_secondary_profile_reads_its_own_passive_flag(runner, monkeypatch):
    runner.config = SimpleNamespace(multiplex_profiles=True)
    runner._primary_profile_name = "default"
    runner._relay_fyi_passive_by_profile = {"secondary": {"relay": {"fyi_passive": False}}}
    source = _event(FYI).source
    source.profile = "secondary"
    monkeypatch.setattr("gateway.run._load_gateway_config", lambda: {"relay": {"fyi_passive": True}})
    assert runner._relay_fyi_passive_enabled(source) is False


@pytest.mark.asyncio
async def test_busy_passive_fyi_sends_no_queued_ack(runner):
    event = _event(FYI)
    adapter = SimpleNamespace(_pending_messages={})
    runner._delivery_adapter_for = MagicMock(return_value=adapter)
    runner._is_user_authorized_for_source = MagicMock(return_value=True)
    runner._admit_bot_message_for_source = MagicMock(return_value=True)
    runner._strict_session_pin_holds = AsyncMock(return_value=True)
    runner._route_plaintext_approval_while_busy = AsyncMock(return_value=False)
    runner._hm_is_registered_command = MagicMock(return_value=False)
    runner._queue_or_replace_pending_event = MagicMock()
    runner._resolve_busy_steer_or_redirect = AsyncMock(
        return_value=SimpleNamespace(steered=False, redirected=False, effective_mode="queue",
                                      demoted_for_subagents=False, demoted_for_compression=False)
    )
    runner._send_busy_ack_reply = AsyncMock()
    runner._peek_session_state = MagicMock(return_value=SimpleNamespace(turn=SimpleNamespace(agent=object())))
    runner._draining = False

    assert await GatewayBusySessionMixin._handle_active_session_busy_message(
        runner, event, "session-key",
    ) is True
    runner._queue_or_replace_pending_event.assert_called_once_with("session-key", event)
    runner._send_busy_ack_reply.assert_not_awaited()


@pytest.mark.asyncio
async def test_idle_dispatch_persists_fyi_without_claiming_a_turn(runner):
    event = _event(FYI)
    runner._hm_admit_event = AsyncMock(return_value=(event, event.source, False))
    runner._draining = False
    runner._hm_is_registered_command = MagicMock(return_value=False)
    runner._quick_command_alias_text = MagicMock(return_value=None)
    runner._hm_estop_gate = MagicMock(return_value=None)
    runner._session_key_for_source = MagicMock(return_value="session-key")
    runner._hm_pending_reply_intercepts = AsyncMock(return_value=None)
    runner._hm_evict_idle_stale_agent = MagicMock()
    runner._is_session_running = MagicMock(return_value=False)
    runner._hm_dispatch_idle_commands = AsyncMock(return_value=(False, None))
    runner._handle_message_with_agent = AsyncMock(side_effect=AssertionError("FYI started a model call"))

    assert await runner._handle_admitted_message(event) is None
    assert runner._claim_active_session_slot.call_count == 1
    assert runner._handle_message_with_agent.call_count == 0
    assert runner._test_store.rows[0][1]["content"] == FYI


@pytest.mark.asyncio
async def test_task_relay_still_runs(runner):
    event = _event(TASK)
    assert not is_fyi_relay_text(event.text)

    assert await runner._hm_try_passive_fyi_relay(event, event.source, "session-key") is False
    assert runner._test_store.rows == []


@pytest.mark.asyncio
async def test_idle_race_uses_busy_path_without_passive_write(runner):
    event = _event(FYI)
    runner._hm_admit_event = AsyncMock(return_value=(event, event.source, False))
    runner._draining = False
    runner._hm_is_registered_command = MagicMock(return_value=False)
    runner._quick_command_alias_text = MagicMock(return_value=None)
    runner._hm_estop_gate = MagicMock(return_value=None)
    runner._session_key_for_source = MagicMock(return_value="session-key")
    runner._hm_pending_reply_intercepts = AsyncMock(return_value=None)
    runner._hm_evict_idle_stale_agent = MagicMock()
    runner._is_session_running = MagicMock(side_effect=[False, False, True])
    runner._hm_handle_running_session_message = AsyncMock(return_value=None)

    assert await runner._handle_admitted_message(event) is None
    runner._hm_handle_running_session_message.assert_awaited_once_with(event, event.source, "session-key")
    assert runner._test_store.rows == []


@pytest.mark.asyncio
async def test_passive_admission_marks_restart_recovery_as_succeeded(runner):
    event = _event(FYI)

    assert await runner._hm_try_passive_fyi_relay(event, event.source, "session-key") is True
    assert event._agent_turn_succeeded is True


@pytest.mark.asyncio
async def test_task_and_two_fyi_merges_fail_closed(runner):
    for first, second in ((TASK, FYI), (FYI, FYI)):
        pending = {}
        merge_pending_message_event(pending, "session-key", _event(first), merge_text=True)
        merge_pending_message_event(pending, "session-key", _event(second), merge_text=True)
        merged = pending["session-key"]

        assert merged._gateway_human_text_merged is True
        assert await runner._hm_try_passive_fyi_relay(merged, merged.source, "session-key") is False
        assert runner._test_store.rows == []


@pytest.mark.asyncio
async def test_fyi_with_typed_tail_runs(runner):
    event = _event(FYI + "\n\nPlease summarize this.")

    assert await runner._hm_try_passive_fyi_relay(event, event.source, "session-key") is False
    assert runner._test_store.rows == []


@pytest.mark.asyncio
async def test_explicit_reply_expected_keeps_turn_path(runner):
    event = _event(FYI, reply_expected=True)

    assert await runner._hm_try_passive_fyi_relay(event, event.source, "session-key") is False
    assert runner._test_store.rows == []


@pytest.mark.asyncio
async def test_goal_active_keeps_turn_path(runner, monkeypatch):
    monkeypatch.setattr("hermes_cli.goals.load_goal", lambda _sid: SimpleNamespace(status="active"))
    event = _event(FYI)

    assert await runner._hm_try_passive_fyi_relay(event, event.source, "session-key") is False
    assert runner._test_store.rows == []


@pytest.mark.asyncio
async def test_loop_active_keeps_turn_path(runner, monkeypatch):
    monkeypatch.setattr("hermes_cli.loops.load_loop", lambda _sid: SimpleNamespace(status="active"))
    event = _event(FYI)

    assert await runner._hm_try_passive_fyi_relay(event, event.source, "session-key") is False
    assert runner._test_store.rows == []


@pytest.mark.asyncio
async def test_flag_off_keeps_turn_path(runner, monkeypatch):
    monkeypatch.setattr("gateway.run._load_gateway_config", lambda: {"relay": {"fyi_passive": False}})
    event = _event(FYI)

    assert await runner._hm_try_passive_fyi_relay(event, event.source, "session-key") is False
    assert runner._test_store.rows == []


@pytest.mark.asyncio
async def test_unbound_session_keeps_turn_path(runner):
    runner._test_store.entry = None
    event = _event(FYI)

    assert await runner._hm_try_passive_fyi_relay(event, event.source, "session-key") is False
    assert runner._test_store.rows == []


class _DrainAdapter:
    def __init__(self, *events):
        self._pending_messages = {"session-key": events[0]} if events else {}
        self._overflow = list(events[1:])

    def get_pending_message(self, session_key):
        return self._pending_messages.pop(session_key, None)


@pytest.mark.asyncio
async def test_post_turn_drain_requeues_fyi_for_idle_admission(runner):
    event = _event(FYI)
    adapter = _DrainAdapter(event)
    runner._draining = False
    runner._overflow_queue = MagicMock(return_value=[])
    runner._promote_queued_event = MagicMock(side_effect=lambda _key, _adapter, pending: pending)

    pending_event, pending = await runner._run_agent_drain_pending(
        {"final_response": "completed"}, adapter, event.source, "session-key",
    )

    assert pending_event is None
    assert pending is None
    assert adapter._pending_messages["session-key"] is event
    assert runner._test_store.rows == []


@pytest.mark.asyncio
async def test_shutdown_drain_preserves_busy_fyi(runner):
    event = _event(FYI)
    adapter = _DrainAdapter(event)
    runner._draining = True
    runner._overflow_queue = MagicMock(return_value=[])
    runner._promote_queued_event = MagicMock(side_effect=lambda _key, _adapter, pending: pending)
    runner._preserve_drain_event = MagicMock(return_value=True)

    pending_event, pending = await runner._run_agent_drain_pending(
        {"final_response": "completed"}, adapter, event.source, "session-key",
    )

    assert pending_event is None
    assert pending is None
    runner._preserve_drain_event.assert_called_once_with("session-key", event)
    assert runner._test_store.rows == []


@pytest.mark.asyncio
async def test_human_text_merged_before_fyi_keeps_turn_path(runner):
    pending = {}
    human = _event("Please review this.")
    relay = _event(FYI)
    merge_pending_message_event(pending, "session-key", human, merge_text=True)
    merge_pending_message_event(pending, "session-key", relay, merge_text=True)

    merged = pending["session-key"]
    assert merged._gateway_human_text_merged is True
    assert await runner._hm_try_passive_fyi_relay(merged, merged.source, "session-key") is False
    assert runner._test_store.rows == []




@pytest.mark.asyncio
async def test_drain_skips_fyi_and_runs_next_ordinary_event(runner):
    fyi = _event(FYI)
    ordinary = _event("Please continue.")
    adapter = _DrainAdapter(fyi)
    runner._draining = False
    runner._overflow_queue = MagicMock(return_value=[])
    runner._promote_queued_event = MagicMock(side_effect=[fyi, ordinary])

    pending_event, pending = await runner._run_agent_drain_pending(
        {"final_response": "completed"}, adapter, ordinary.source, "session-key",
    )

    assert pending_event is ordinary
    assert pending == "Please continue."
    assert adapter._pending_messages["session-key"] is fyi