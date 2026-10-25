"""A classified billing 400 on a long session is durably persisted, not reset/dropped."""
from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.run_turn import GatewayTurnMixin, is_context_overflow_failure_result
from gateway.session import AsyncSessionStore, SessionSource, SessionStore
from hermes_state import SessionDB


@pytest.fixture
def long_session(tmp_path):
    store = SessionStore(sessions_dir=tmp_path, config=GatewayConfig())
    store._db = SessionDB(db_path=tmp_path / "state.db")
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="123", user_id="u1")
    entry = store.get_or_create_session(source)
    store._db.create_session(session_id=entry.session_id, source="telegram", user_id="u1")
    history = [{"role": "user" if i % 2 == 0 else "assistant", "content": str(i)} for i in range(60)]
    store._db.replace_messages(entry.session_id, history)
    yield store, source, entry, history
    store._db.close()


@pytest.mark.asyncio
async def test_long_session_billing_400_persists_and_does_not_auto_reset(long_session):
    store, source, session_entry, history = long_session
    runner = object.__new__(GatewayTurnMixin)
    runner.async_session_store = AsyncSessionStore(store)
    runner.async_session_store.reset_session = AsyncMock(side_effect=AssertionError("unexpected auto-reset"))
    runner._session_db = None
    runner._refresh_agent_cache_message_count = AsyncMock()
    original_session_id = session_entry.session_id
    agent_result = {
        "failed": True,
        "failure_reason": "billing",
        "error": "HTTP 400: Your credit balance is too low to access the Anthropic API",
        "messages": history,
        "history_offset": len(history),
    }
    failed, hidden, overflow = runner._hmwa_classify_turn_failure(agent_result, history, session_entry)
    assert failed is True
    assert hidden is False
    assert overflow is False

    prepared = SimpleNamespace(
        history=history, persist_user_message=None, message_text="please continue",
        persist_user_timestamp=None, persist_user_display_kind=None, persistence_owner=None,
    )
    event = SimpleNamespace(message_id="msg-1", internal=False)
    response, returned_entry = await runner._hmwa_compression_exhaustion_reset(
        agent_result, "billing failure", session_entry, session_entry.session_key, source,
    )
    await runner._hmwa_persist_turn_transcript(
        event=event, source=source, session_entry=returned_entry,
        session_key=session_entry.session_key, agent_result=agent_result, agent_messages=history,
        prepared=prepared, response=response, agent_failed_early=failed,
        hidden_reasoning_incomplete=hidden, is_context_overflow_failure=overflow,
    )
    # Read the exact durable target, not a persistence mock or an agent-returned message list.
    transcript = await runner.async_session_store.load_transcript(original_session_id)
    assert transcript[-2]["role"] == "user"
    assert transcript[-2]["content"] == "please continue"
    assert transcript[-1]["role"] == "assistant"
    assert len(transcript) == len(history) + 2
    assert returned_entry.session_id == original_session_id
    assert response == "billing failure"
    runner.async_session_store.reset_session.assert_not_awaited()


@pytest.mark.parametrize("reason", ["billing", "auth", "rate_limit", "content_policy_blocked"])
def test_classified_nonoverflow_400_overrides_long_history_heuristic(reason):
    assert not is_context_overflow_failure_result(
        {"failed": True, "failure_reason": reason, "error": "HTTP 400 invalid request"}, 100,
    )




def test_format_error_keeps_large_400_growth_guard():
    assert is_context_overflow_failure_result(
        {"failed": True, "failure_reason": "format_error", "error": "HTTP 400 invalid request"}, 100,
    )


@pytest.mark.parametrize("reason", [None, "unknown", "context_overflow", "payload_too_large", "long_context_tier"])
def test_unclassified_and_genuine_overflow_keep_the_growth_loop_guard(reason):
    assert is_context_overflow_failure_result(
        {"failed": True, "failure_reason": reason, "error": "HTTP 400 request too large"}, 100,
    )
