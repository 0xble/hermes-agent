"""A classified billing 400 on a long session is persisted, not reset/dropped."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from gateway.run_turn import GatewayTurnMixin


class _Store:
    def __init__(self) -> None:
        self.appended = []
        self.updated = []
        self.reset_called = False

    async def append_to_transcript(self, session_id, row, **kwargs):
        self.appended.append((session_id, row, kwargs))

    async def update_session(self, session_key, **kwargs):
        self.updated.append((session_key, kwargs))

    async def reset_session(self, *_args, **_kwargs):
        self.reset_called = True
        raise AssertionError("billing failures must not auto-reset the session")

    async def transcript_tail_role(self, _session_id):
        return "user"

    async def has_platform_message_id(self, _session_id, _message_id):
        return False


@pytest.mark.asyncio
async def test_long_session_billing_400_persists_and_does_not_auto_reset():
    runner = object.__new__(GatewayTurnMixin)
    store = _Store()
    runner.async_session_store = store
    runner._session_db = None
    runner._refresh_agent_cache_message_count = AsyncMock()
    runner._hmwa_close_failed_turn = AsyncMock()
    runner._hmwa_failed_turn_notice = Mock(return_value="failed turn")
    runner._evict_cached_agent = Mock()
    runner._clear_conversation_scope = Mock()
    runner._session_model_override_lock = Mock()
    runner._sync_telegram_topic_binding = Mock()

    session_entry = SimpleNamespace(session_id="session-1", session_key="key-1")
    history = [{"role": "user" if i % 2 == 0 else "assistant", "content": str(i)} for i in range(60)]
    agent_result = {
        "failed": True,
        "failure_reason": "billing",
        "error": "HTTP 400: Your credit balance is too low to access the Anthropic API",
        "messages": history,
        "history_offset": 0,
    }

    failed, hidden, overflow = runner._hmwa_classify_turn_failure(agent_result, history, session_entry)

    assert failed is True
    assert hidden is False
    assert overflow is False

    prepared = SimpleNamespace(
        history=history,
        persist_user_message=None,
        message_text="please continue",
        persist_user_timestamp=None,
        persist_user_display_kind=None,
        persistence_owner=None,
    )
    event = SimpleNamespace(message_id="msg-1", internal=False)
    source = SimpleNamespace(platform=SimpleNamespace(value="cli"))
    await runner._hmwa_persist_turn_transcript(
        event=event,
        source=source,
        session_entry=session_entry,
        session_key="key-1",
        agent_result=agent_result,
        agent_messages=history,
        prepared=prepared,
        response="billing failure",
        agent_failed_early=failed,
        hidden_reasoning_incomplete=hidden,
        is_context_overflow_failure=overflow,
    )

    assert any(row[1]["role"] == "user" for row in store.appended)
    runner._hmwa_close_failed_turn.assert_awaited_once()

    response, returned_entry = await runner._hmwa_compression_exhaustion_reset(
        agent_result, "billing failure", session_entry, "key-1", source,
    )

    assert returned_entry is session_entry
    assert response == "billing failure"
    assert store.reset_called is False
