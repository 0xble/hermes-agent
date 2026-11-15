from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from gateway.config import Platform
from gateway.platforms.event import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.run_turn import _failed_turn_copy_is_user_facing


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
