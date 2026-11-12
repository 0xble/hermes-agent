"""Reply platform ids land on the exact assistant row, not a content match or a copy block."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.event import MessageEvent, MessageType
from gateway.run_turn import GatewayTurnMixin
from gateway.session import SessionSource
from hermes_state import SessionDB


class _Adapter(BasePlatformAdapter):
    async def connect(self, **kwargs):
        return True

    async def disconnect(self):
        return None

    async def get_chat_info(self, chat_id):
        return None

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sent.append((content, metadata))
        message_id = "copy-1" if (metadata or {}).get("copy_block") else "reply-1"
        return SendResult(success=True, message_id=message_id)


class _Store:
    def __init__(self, db, session_id):
        self.db = db
        self.session_id = session_id
        self._outbound_stamp_tasks = set()

    def record_outbound_platform_message(self, session_key, platform_message_id, content, message_uid=None):
        return self.db.record_outbound_platform_message(
            self.session_id, platform_message_id, content, message_uid=message_uid)


def _ids(db, session_id):
    rows = db._conn.execute(
        "SELECT message_uid, platform_message_id, active FROM messages "
        "WHERE session_id = ? AND role = 'assistant' ORDER BY id",
        (session_id,),
    ).fetchall()
    return [tuple(row) for row in rows]


async def _drain_stamps(store):
    tasks = list(getattr(store, "_outbound_stamp_tasks", ()))
    if tasks:
        await asyncio.wait(tasks, timeout=2)


@pytest.mark.asyncio
async def test_nonstreamed_turn_stamps_the_reply_not_a_copy_block(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session("sid", "telegram")
        older = {"role": "assistant", "content": "stored answer"}
        current = {"role": "assistant", "content": "stored answer"}
        db.append_messages_batch("sid", [older, current])
        store = _Store(db, "sid")
        adapter = _Adapter(PlatformConfig(enabled=True), Platform.TELEGRAM)
        adapter.sent = []
        adapter._session_store = store
        adapter._start_typing_refresh = lambda *a, **k: None
        adapter._stop_typing_refresh = AsyncMock()
        adapter._run_processing_hook = AsyncMock()
        adapter._fire_post_delivery_callback = AsyncMock()
        adapter._flush_text_debounce_now = AsyncMock()
        adapter._finish_session_task = lambda *a, **k: None
        adapter.gateway_runner = None

        async def handler(event):
            event._transcript_message_uid = current["message_uid"]
            return "visible reply\n\n[[copy]]paste me[[/copy]]"

        adapter._message_handler = handler
        source = SessionSource(platform=Platform.TELEGRAM, chat_id="chat", chat_type="dm")
        event = MessageEvent(text="hello", message_type=MessageType.TEXT, message_id="in-1", source=source)
        await adapter._process_message_background(event, "agent:main:telegram:dm:chat")
        await _drain_stamps(store)
        rows = _ids(db, "sid")
        sent = [(str(content), dict(metadata or {})) for content, metadata in adapter.sent]
    finally:
        db.close()

    assert rows == [
        (older["message_uid"], None, 1),
        (current["message_uid"], "reply-1", 1),
    ]
    assert any("paste me" in content for content, _metadata in sent)
    assert all(item[1].get("_transcript_message_uid") != current["message_uid"] or not item[1].get("copy_block")
               for item in sent)


@pytest.mark.asyncio
async def test_streamed_turn_stamps_the_surviving_reply_id(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session("sid", "slack")
        older = {"role": "assistant", "content": "stored answer"}
        current = {"role": "assistant", "content": "stored answer"}
        db.append_messages_batch("sid", [older, current])
        store = _Store(db, "sid")
        adapter = _Adapter(PlatformConfig(enabled=True), Platform.SLACK)
        adapter.sent = []
        adapter._session_store = store
        adapter.gateway_runner = None
        runner = GatewayTurnMixin.__new__(GatewayTurnMixin)
        runner._session_db = object()
        runner.session_store = store
        runner.async_session_store = SimpleNamespace(
            append_to_transcript=AsyncMock(),
            has_platform_message_id=AsyncMock(return_value=False),
        )
        runner._hmwa_store_turn_prompt_tokens = AsyncMock()
        runner._refresh_agent_cache_message_count = AsyncMock()
        runner._delivery_adapter_for = lambda source: adapter
        runner._event_thread_metadata = lambda event, source: {}
        runner._should_send_voice_reply = lambda *args, **kwargs: False
        runner._deliver_media_from_response = AsyncMock(return_value=False)
        source = SessionSource(platform=Platform.SLACK, chat_id="C1", chat_type="group")
        event = MessageEvent(text="hello", message_type=MessageType.TEXT, message_id="in-2", source=source)
        agent_messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "stored answer", "message_uid": current["message_uid"]},
        ]
        agent_result = {
            "agent_persisted": True,
            "history_offset": 0,
            "already_sent": True,
            "_delivered_platform_message_id": "stream-last",
            "last_prompt_tokens": 0,
        }
        prepared = SimpleNamespace(
            history=[{"role": "user", "content": "prior"}],
            persist_user_message="hello", persist_user_timestamp=None,
            persist_user_display_kind=None, persistence_owner=None, hygiene_adoption_epoch=0,
        )
        session_entry = SimpleNamespace(session_id="sid", session_key="agent:main:slack:group:C1")
        await runner._hmwa_persist_turn_transcript(
            event=event, source=source, session_entry=session_entry, session_key=session_entry.session_key,
            agent_result=agent_result, agent_messages=agent_messages, prepared=prepared,
            response="stored answer plus a footer the user did not see as the body",
            agent_failed_early=False, hidden_reasoning_incomplete=False, is_context_overflow_failure=False,
        )
        await runner._hmwa_deliver_turn_response(
            event, source, session_entry, session_entry.session_key, 1,
            agent_result, agent_messages, "stored answer", "", False,
        )
        await _drain_stamps(store)
        rows = _ids(db, "sid")
    finally:
        db.close()

    assert rows == [
        (older["message_uid"], None, 1),
        (current["message_uid"], "stream-last", 1),
    ]
