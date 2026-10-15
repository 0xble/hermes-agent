"""FYI relay ingress is transcript-only unless it must wake autonomous work."""

from __future__ import annotations

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform
from gateway.platforms.base import merge_pending_message_event
from gateway.platforms.event import MessageEvent, MessageType
from gateway.response_filters import is_fyi_relay_text
from gateway.run import GatewayRunner
from gateway.session import SessionSource


FYI = (
    "📨 Relay title\n\n"
    "A status update.\n"
    "From: Source Agent · FYI, no action requested\n"
    "[relay from=hermes:default/source receipt=receipt-1]"
)
TASK = FYI.replace("receipt=receipt-1", "receipt=receipt-1 task=task-1")


class _Store:
    def __init__(self, entry=None):
        self.entry = entry
        self.rows = []

    async def lookup_by_session_key(self, _key):
        return self.entry

    async def append_to_transcript(self, session_id, row, **_kwargs):
        self.rows.append((session_id, row))

    async def has_platform_message_id(self, _session_id, _message_id):
        return False


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
    runner._refresh_agent_cache_message_count = AsyncMock()

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
async def test_idle_fyi_rechecks_session_before_persisting(runner):
    runner._is_session_running = MagicMock(side_effect=[True])
    event = _event(FYI)

    assert await runner._hm_try_passive_fyi_relay(event, event.source, "session-key") is False
    assert runner._test_store.rows == []


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
    runner._claim_active_session_slot = MagicMock(side_effect=AssertionError("FYI started a turn"))
    runner._handle_message_with_agent = AsyncMock(side_effect=AssertionError("FYI started a model call"))

    assert await runner._handle_admitted_message(event) is None
    assert runner._claim_active_session_slot.call_count == 0
    assert runner._handle_message_with_agent.call_count == 0
    assert runner._test_store.rows[0][1]["content"] == FYI




@pytest.mark.asyncio
async def test_task_relay_still_runs(runner):
    event = _event(TASK)
    assert not is_fyi_relay_text(event.text)

    assert await runner._hm_try_passive_fyi_relay(event, event.source, "session-key") is False
    assert runner._test_store.rows == []


@pytest.mark.asyncio
async def test_idle_recheck_queues_passive_fyi_when_turn_wins_race(runner):
    event = _event(FYI)
    runner._hm_admit_event = AsyncMock(return_value=(event, event.source, False))
    runner._draining = False
    runner._hm_is_registered_command = MagicMock(return_value=False)
    runner._quick_command_alias_text = MagicMock(return_value=None)
    runner._hm_estop_gate = MagicMock(return_value=None)
    runner._session_key_for_source = MagicMock(return_value="session-key")
    runner._hm_pending_reply_intercepts = AsyncMock(return_value=None)
    runner._hm_evict_idle_stale_agent = MagicMock()
    runner._hm_dispatch_idle_commands = AsyncMock(return_value=(False, None))
    runner._is_session_running = MagicMock(side_effect=[False, False, True])
    runner._hm_try_passive_fyi_relay = AsyncMock(return_value=True)
    runner._hm_handle_running_session_message = AsyncMock(
        side_effect=AssertionError("passive FYI was not admitted through the busy path")
    )

    assert await runner._handle_admitted_message(event) is None
    runner._hm_try_passive_fyi_relay.assert_awaited_once_with(
        event, event.source, "session-key", busy=True,
    )


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
async def test_deferred_busy_fyi_is_not_written_until_after_outer_turn_rows(runner):
    event = _event(FYI)
    adapter = _DrainAdapter(event)
    result = {"final_response": "completed"}

    pending_event, pending = await runner._run_agent_drain_pending(result, adapter, event.source, "session-key")

    assert pending_event is None
    assert pending is None
    assert result["_deferred_passive_fyi_events"] == [event]
    assert runner._test_store.rows == []

    runner._test_store.rows.extend([
        ("target-session", {"role": "user", "content": "failed turn"}),
        ("target-session", {"role": "assistant", "content": "failed notice"}),
    ])
    deferred = result.pop("_deferred_passive_fyi_events")
    assert await runner._hm_try_passive_fyi_relay(
        deferred[0], deferred[0].source, "session-key", after_turn=True,
    ) is True
    assert [row[1]["content"] for row in runner._test_store.rows] == [
        "failed turn", "failed notice", FYI,
    ]




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


@pytest.mark.asyncio
async def test_busy_fyi_is_queued_for_ordered_idle_persistence(runner):
    runner._queue_or_replace_pending_event = MagicMock(return_value=True)
    event = _event(FYI)

    assert await runner._hm_try_passive_fyi_relay(event, event.source, "session-key", busy=True) is True
    runner._queue_or_replace_pending_event.assert_called_once_with("session-key", event)
    assert event._gateway_accepted is True
    assert runner._test_store.rows == []


@pytest.mark.asyncio
async def test_busy_fyi_queue_failure_is_not_marked_accepted(runner):
    runner._queue_or_replace_pending_event = MagicMock(return_value=False)
    event = _event(FYI)

    assert await runner._hm_try_passive_fyi_relay(event, event.source, "session-key", busy=True) is False
    assert event._gateway_accepted is False
    assert runner._test_store.rows == []


class _DrainAdapter:
    def __init__(self, event):
        self._pending_messages = {"session-key": event}

    def get_pending_message(self, session_key):
        return self._pending_messages.pop(session_key, None)


@pytest.mark.asyncio
async def test_busy_fyi_is_persisted_by_post_turn_drain_without_followup(runner):
    event = _event(FYI)
    adapter = _DrainAdapter(event)
    runner._test_store.rows.extend([
        ("target-session", {"role": "user", "content": "current turn"}),
        ("target-session", {"role": "assistant", "content": "completed"}),
    ])
    runner._run_agent = AsyncMock(side_effect=AssertionError("passive FYI started a follow-up turn"))

    result = {"final_response": "completed"}
    pending_event, pending = await runner._run_agent_drain_pending(
        result, adapter, event.source, "session-key",
    )

    assert pending_event is None
    assert pending is None
    assert result["_deferred_passive_fyi_events"] == [event]
    assert [row[1]["content"] for row in runner._test_store.rows] == ["current turn", "completed"]
    runner._run_agent.assert_not_awaited()




@pytest.mark.asyncio
async def test_failed_turn_persists_deferred_fyi_after_closing_notice(runner, monkeypatch):
    event = _event(FYI)
    session_entry = SimpleNamespace(session_id="target-session", session_key="session-key")
    prepared = SimpleNamespace(
        history=[], hygiene_adoption_epoch=0, persist_user_message=FYI,
        persist_user_timestamp=None, message_text=FYI, persist_user_display_kind=None,
        persistence_owner=None,
    )
    result = {
        "agent_persisted": False,
        "tools": [],
        "_deferred_passive_fyi_events": [event],
    }
    runner._session_db = None
    runner._hmwa_store_turn_prompt_tokens = AsyncMock()
    runner._hmwa_failed_turn_notice = MagicMock(return_value="failed notice")
    runner._hmwa_close_failed_turn = AsyncMock(
        side_effect=lambda sid, notice: runner._test_store.rows.append(
            (sid, {"role": "assistant", "content": notice}),
        )
    )
    monkeypatch.setattr("gateway.run._resolve_gateway_model", lambda: "test-model")

    await runner._hmwa_persist_turn_transcript(
        event=event, source=event.source, session_entry=session_entry, session_key="session-key",
        agent_result=result, agent_messages=[], prepared=prepared, response="",
        agent_failed_early=True, hidden_reasoning_incomplete=False, is_context_overflow_failure=False,
    )

    assert [row[1]["role"] for row in runner._test_store.rows] == [
        "session_meta", "user", "assistant", "user",
    ]
    assert runner._test_store.rows[-1][1]["content"] == FYI


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


def test_receipt_header_is_part_of_the_persisted_user_row():
    assert "[relay from=hermes:default/source receipt=receipt-1]" in FYI
