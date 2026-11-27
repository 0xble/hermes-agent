from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from gateway.config import Platform
from gateway.platforms.event import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.run_turn import _apply_failed_turn_response, _failed_turn_copy_is_user_facing


@pytest.mark.asyncio
async def test_failed_internal_turn_is_requeued_once_instead_of_becoming_a_refusal():
    runner = object.__new__(GatewayRunner)
    runner._is_goal_continuation_event = lambda event: False
    runner._delivery_adapter_for = lambda source: object()
    runner._session_key_for_source = lambda source: "session-key"
    runner._enqueue_fifo = MagicMock()
    source = SimpleNamespace(platform=Platform.SLACK)
    event = MessageEvent(
        text="background result",
        message_type=MessageType.TEXT,
        source=source,
        internal=True,
        reply_expected=False,
        metadata={},
    )

    result = await GatewayRunner._retry_failed_synthetic_event(
        runner, event, source, {"failed": True, "completed": False, "failure_retryable": True}
    )

    assert result is True
    runner._enqueue_fifo.assert_called_once()
    retry = runner._enqueue_fifo.call_args.args[1]
    assert retry.text == event.text
    assert retry.metadata["gateway_internal_retry_count"] == 1
    assert _failed_turn_copy_is_user_facing(event, "internal_notification") is False

    # A second admission cannot create an unbounded provider retry loop.
    assert await GatewayRunner._retry_failed_synthetic_event(
        runner, retry, source, {"failed": True, "completed": False, "failure_retryable": True}
    ) is False
    assert runner._enqueue_fifo.call_count == 1


@pytest.mark.asyncio
async def test_local_failed_internal_retry_keeps_retry_stamp_through_admission(monkeypatch):
    """A LOCAL admission must retain the one-retry stamp on an unmarked internal wake."""
    from gateway import session_automation, session_route_wake

    runner = object.__new__(GatewayRunner)
    runner._is_goal_continuation_event = lambda event: False
    runner._delivery_adapter_for = lambda source: object()
    runner._session_key_for_source = lambda source: "session-key"
    source = SimpleNamespace(platform=Platform.LOCAL, chat_id="chat")
    authority = SimpleNamespace(runner=runner)
    admitted = []

    monkeypatch.setattr(
        "gateway.session_detached.route_entry",
        lambda _authority, _key: SimpleNamespace(session_id="session-id"),
    )

    async def local_admit(_runner, adapter, source, text, producer, metadata=None, *, event=None):
        wake = session_route_wake._capture(
            authority, adapter, source, text, "session-key", "retry-id", metadata, event,
        )
        admitted.append(wake)
        return wake is not None

    monkeypatch.setattr(session_automation, "admit_route_wake", local_admit)
    event = MessageEvent(
        text="background result", message_type=MessageType.TEXT, source=source,
        internal=True, reply_expected=False, metadata={},
    )
    failure = {"failed": True, "completed": False, "failure_retryable": True}

    assert await GatewayRunner._retry_failed_synthetic_event(runner, event, source, failure) is True
    retry = admitted[-1]
    assert retry.metadata["gateway_internal_retry_count"] == 1
    assert await GatewayRunner._retry_failed_synthetic_event(runner, retry, source, failure) is False
    assert len(admitted) == 1


def test_unknown_provenance_internal_failed_turn_has_empty_durable_boundary(tmp_path, monkeypatch):
    """Notification-category wakes are internal even when provenance classification is unknown."""
    from agent.conversation_loop import _close_durable_failed_turn
    from gateway.platforms.event import MessageEvent
    from gateway.response_filters import display_kind_for_event
    from gateway.session import SessionSource
    from hermes_state import SessionDB

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    db = SessionDB(tmp_path / "state.db")
    sid = "unknown-internal"
    db.create_session(session_id=sid, source="telegram", model="m")
    db.append_message(sid, "user", "background wake")
    messages = [{"role": "user", "content": "background wake"}]
    wake = MessageEvent(
        text="background wake", source=SessionSource(platform=Platform.TELEGRAM, chat_id="chat"),
        internal=True, metadata={"notification_category": "diagnostic"},
    )
    assert display_kind_for_event(wake) == "internal_notification"
    agent = SimpleNamespace(
        _session_db=db, session_id=sid, _turn_provenance="unknown", _turn_display_kind="internal_notification",
        _flush_messages_to_session_db=lambda rows: db.append_message(
            sid, rows[-1]["role"], rows[-1]["content"]),
    )

    _close_durable_failed_turn(agent, {
        "completed": False, "failed": True, "failure_reason": "server_error",
        "messages": messages, "current_turn_user_idx": 0,
    })

    assert messages[-1]["content"] == ""
    assert db.get_messages(sid)[-1]["content"] == ""
    db.close()


@pytest.mark.asyncio
async def test_interrupted_synthetic_turn_is_not_requeued_even_with_retryable_result():
    runner = object.__new__(GatewayRunner)
    runner._is_goal_continuation_event = lambda event: False
    runner._enqueue_fifo = MagicMock()
    source = SimpleNamespace(platform=Platform.SLACK)
    event = MessageEvent(text="background result", source=source, internal=True, metadata={})

    assert await GatewayRunner._retry_failed_synthetic_event(
        runner, event, source, {"failed": True, "interrupted": True, "failure_retryable": True}
    ) is False
    runner._enqueue_fifo.assert_not_called()


@pytest.mark.asyncio
async def test_retry_has_fresh_outbox_transport_identity_and_epoch_stop_fence():
    runner = object.__new__(GatewayRunner)
    runner._is_goal_continuation_event = lambda event: False
    runner._delivery_adapter_for = lambda source: object()
    runner._session_key_for_source = lambda source: "session-key"
    runner._session_invalidation_epoch = lambda key: 2
    runner._user_stop_latched = lambda *keys: False
    runner._enqueue_fifo = MagicMock()
    source = SimpleNamespace(platform=Platform.SLACK)
    event = MessageEvent(text="background result", source=source, internal=True, metadata={})
    event._outbox_admission_id = "old-admission"
    event._outbox_transport_id = "old-transport"
    event._outbox_home = "old-home"
    event._outbox_turn_id = "old-turn"

    assert await GatewayRunner._retry_failed_synthetic_event(
        runner, event, source, {"failed": True, "completed": False, "failure_retryable": True},
        invalidation_epoch=2,
    ) is True
    retry = runner._enqueue_fifo.call_args.args[1]
    assert retry._outbox_transport_id != "old-transport"
    assert not hasattr(retry, "_outbox_admission_id")
    assert not hasattr(retry, "_outbox_home")
    assert not hasattr(retry, "_outbox_turn_id")

    runner._enqueue_fifo.reset_mock()
    runner._session_invalidation_epoch = lambda key: 3
    assert await GatewayRunner._retry_failed_synthetic_event(
        runner, event, source, {"failed": True, "completed": False, "failure_retryable": True},
        invalidation_epoch=2,
    ) is False
    runner._enqueue_fifo.assert_not_called()


@pytest.mark.asyncio
async def test_stop_latch_blocks_failed_synthetic_retry():
    runner = object.__new__(GatewayRunner)
    runner._is_goal_continuation_event = lambda event: False
    runner._session_key_for_source = lambda source: "session-key"
    runner._session_invalidation_epoch = lambda key: 1
    runner._user_stop_latched = lambda *keys: True
    runner._enqueue_fifo = MagicMock()
    source = SimpleNamespace(platform=Platform.SLACK)
    event = MessageEvent(text="background result", source=source, internal=True, metadata={})

    assert await GatewayRunner._retry_failed_synthetic_event(
        runner, event, source, {"failed": True, "completed": False, "failure_retryable": True},
        invalidation_epoch=1,
    ) is False
    runner._enqueue_fifo.assert_not_called()


@pytest.mark.asyncio
async def test_failed_human_turn_is_not_requeued():
    runner = object.__new__(GatewayRunner)
    runner._is_goal_continuation_event = lambda event: False
    runner._enqueue_fifo = MagicMock()
    source = SimpleNamespace(platform=Platform.SLACK)
    event = MessageEvent(text="do it", message_type=MessageType.TEXT, source=source)

    assert await GatewayRunner._retry_failed_synthetic_event(
        runner, event, source, {"failed": True, "completed": False, "failure_retryable": True}
    ) is False
    runner._enqueue_fifo.assert_not_called()
    assert _failed_turn_copy_is_user_facing(event, None) is True


@pytest.mark.asyncio
async def test_post_turn_hooks_retries_unsuccessful_synthetic_turn_and_suppresses_copy():
    runner = object.__new__(GatewayRunner)
    source = SimpleNamespace(platform=Platform.SLACK, chat_id="chat")
    event = MessageEvent(
        text="[Continuing toward your standing goal]\\nGoal: test",
        source=source, internal=True, reply_expected=False, metadata={},
    )
    retry = AsyncMock(return_value=True)
    runner._retry_failed_synthetic_event = retry
    runner._final_text_for_post_turn_hooks = lambda result, event: result.get("final_response", "")
    runner._turn_failed = lambda: True
    runner._session_key_for_source = lambda source: "session-key"
    object.__setattr__(runner, "session_store", object())
    object.__setattr__(runner, "_async_session_store", SimpleNamespace(
        _store=runner.session_store,
        lookup_by_session_key=AsyncMock(return_value=None),
        get_or_create_session=AsyncMock(return_value=SimpleNamespace(session_id="session-id")),
    ))

    with patch("gateway.session_authorities.active_authority", return_value=None), \
         patch("gateway.session_viewer_automation.viewer_driven", return_value=False):
        await GatewayRunner._run_post_turn_hooks(
            runner, agent_result={"failed": True, "completed": False, "final_response": "provider detail"},
            source=source, is_internal=True, event=event, existing_session_only=True,
        )

    retry.assert_awaited_once()
    assert _apply_failed_turn_response(
        "provider detail", event, None, agent_failed_early=True,
        is_context_overflow_failure=False, automatic_notice_allowed=True,
        notice="Your request was not processed. Send it again if you still want me to carry it out.",
    ) == ""


@pytest.mark.parametrize("text", ["[Continuing toward your standing goal]\nGoal: x",
                                  "[/loop wakeup #1]", "[Heartbeat — recurring instruction, fires every minute]"])
def test_cli_only_scheduler_provenance_gets_goal_loop_origin(text):
    from hermes_cli.cli_loops_mixin import ScheduledWakeup, _turn_origin_for_cli_message

    assert _turn_origin_for_cli_message(ScheduledWakeup(text)) == "goal_loop"
    assert _turn_origin_for_cli_message(text) == "user"
