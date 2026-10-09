"""A relay names one topic; lobby recovery must never move it to another.

The relay CLI posts ``/queue [relay from=... receipt=...]`` into the target topic. When that topic
no longer exists Telegram files the post at the chat root (no message_thread_id). Topic-mode lobby
recovery used to pin any root message to the user's most recently bound topic, so the relay was
delivered into an unrelated live session (incident 2026-10-08: receipts 63ebcd7d, e9652c3e and
0f9a37c9 for thread 296477 all landed in the newest topic, 298502).
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import Platform
from gateway.platforms.event import MessageEvent
from gateway.response_filters import is_agent_relay_text
from gateway.session import SessionSource
from hermes_state import SessionDB
from tests.gateway.test_telegram_topic_mode import (
    _make_runner,
    _make_source,
    _seed_two_topic_bindings,
)

RELAY = "/queue [relay from=hermes:default/20261008_202958_8d575bdf receipt=0f9a37c9-1095-458d-822e-e0549f6ec4d7]\nResume request"


def _event(text: str, thread_id: str | None) -> MessageEvent:
    return MessageEvent(text=text, source=_make_source(thread_id=thread_id), message_id="298893")


def test_relay_text_is_recognised_with_and_without_command_prefix():
    header = "[relay from=hermes:default/a receipt=r task=t]\nbody"
    assert is_agent_relay_text(header)
    assert is_agent_relay_text("/queue " + header)
    assert is_agent_relay_text("/steer " + header)
    assert not is_agent_relay_text("/queue please do the thing")
    assert not is_agent_relay_text("hello [relay from=x receipt=y]")
    assert not is_agent_relay_text(None)


def _adapter_with_recovery(recover):
    from gateway.platforms.base import BasePlatformAdapter

    adapter = SimpleNamespace(_topic_recovery_fn=recover)
    adapter._apply_topic_recovery = lambda event: BasePlatformAdapter._apply_topic_recovery(adapter, event)
    return adapter


def test_adapter_recovery_never_moves_a_relay_that_lost_its_topic():
    recover = MagicMock(return_value="298502")
    adapter = _adapter_with_recovery(recover)
    event = _event(RELAY, None)
    adapter._apply_topic_recovery(event)
    assert event.source.thread_id is None
    recover.assert_not_called()


def test_adapter_recovery_still_pins_a_plain_lobby_message():
    adapter = _adapter_with_recovery(MagicMock(return_value="298502"))
    event = _event("what's next?", None)
    adapter._apply_topic_recovery(event)
    assert event.source.thread_id == "298502"


@pytest.mark.parametrize("thread_id", [None, "1"])
@pytest.mark.asyncio
async def test_relay_to_deleted_topic_is_refused_not_delivered(tmp_path, thread_id):
    db = SessionDB(db_path=tmp_path / "state.db")
    _seed_two_topic_bindings(db)
    runner = _make_runner(session_db=db)
    runner._handle_message_with_agent = AsyncMock(return_value="agent response")
    runner._should_send_telegram_lobby_reminder = MagicMock(return_value=True)

    result = await runner._handle_message(_event(RELAY, thread_id))

    assert result is None
    runner._handle_message_with_agent.assert_not_awaited()
    runner._should_send_telegram_lobby_reminder.assert_not_called()


@pytest.mark.asyncio
async def test_relay_to_idle_live_topic_lands_in_that_exact_session(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    _seed_two_topic_bindings(db)  # topic 222 is the most recent binding
    runner = _make_runner(session_db=db)

    event = _event(RELAY, "111")  # the older, idle topic
    resolved = await runner._hmwa_resolve_session(event, event.source)

    source, _entry, session_key = resolved
    assert source.thread_id == "111"
    assert session_key.endswith(":111")


@pytest.mark.asyncio
async def test_resolve_session_does_not_recover_a_threadless_relay(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    _seed_two_topic_bindings(db)
    runner = _make_runner(session_db=db)
    runner._recover_telegram_topic_thread_id = MagicMock(return_value="222")

    event = _event(RELAY, None)
    await runner._hmwa_resolve_session(event, event.source)

    runner._recover_telegram_topic_thread_id.assert_not_called()
    assert event.source.thread_id is None
