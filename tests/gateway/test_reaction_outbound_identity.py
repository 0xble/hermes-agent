"""Reply platform ids land on the exact assistant row, not a content match or a copy block."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import quote

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


def _real_store(tmp_path, monkeypatch):
    import hermes_state
    from gateway.config import GatewayConfig
    from gateway.session import AsyncSessionStore, SessionStore

    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", tmp_path / "state.db")
    return AsyncSessionStore(SessionStore(sessions_dir=tmp_path, config=GatewayConfig()))


@pytest.mark.asyncio
async def test_gateway_written_assistant_row_keeps_the_uid_delivery_stamps(tmp_path, monkeypatch):
    store = _real_store(tmp_path, monkeypatch)
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="chat", chat_type="dm")
    entry = await store.get_or_create_session(source)
    runner = GatewayTurnMixin.__new__(GatewayTurnMixin)
    runner._session_db = None
    runner.async_session_store = store
    runner._hmwa_store_turn_prompt_tokens = AsyncMock()
    runner._refresh_agent_cache_message_count = AsyncMock()
    event = MessageEvent(text="hello", message_type=MessageType.TEXT, message_id="in-db", source=source)
    prepared = SimpleNamespace(
        history=[], persist_user_message="hello", persist_user_timestamp=None,
        persist_user_display_kind=None, persistence_owner=None, hygiene_adoption_epoch=0,
        message_text="hello",
    )
    agent_messages = [
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "stored answer"},
    ]
    agent_result = {"agent_persisted": False, "history_offset": 0, "last_prompt_tokens": 0}
    await runner._hmwa_persist_turn_transcript(
        event=event, source=source, session_entry=entry, session_key=entry.session_key,
        agent_result=agent_result, agent_messages=agent_messages, prepared=prepared,
        response="stored answer", agent_failed_early=False, hidden_reasoning_incomplete=False,
        is_context_overflow_failure=False,
    )
    stamped = await store.record_outbound_platform_message(entry.session_key, "reply-db", "", event._transcript_message_uid)
    rows = store._store._db.get_messages(entry.session_id)
    assistant = [row for row in rows if row["role"] == "assistant"]
    assert stamped is True
    assert len(assistant) == 1
    assert assistant[0]["message_uid"] == event._transcript_message_uid
    assert assistant[0]["platform_message_id"] == "reply-db"


@pytest.mark.asyncio
async def test_proxy_turn_writes_the_uid_delivery_already_named(tmp_path, monkeypatch):
    store = _real_store(tmp_path, monkeypatch)
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="chat", chat_type="dm")
    entry = await store.get_or_create_session(source)
    runner = GatewayTurnMixin.__new__(GatewayTurnMixin)
    runner._session_db = object()
    runner.async_session_store = store
    runner._hmwa_store_turn_prompt_tokens = AsyncMock()
    runner._refresh_agent_cache_message_count = AsyncMock()
    event = MessageEvent(text="hello", message_type=MessageType.TEXT, message_id="in-proxy", source=source)
    prepared = SimpleNamespace(
        history=[{"role": "user", "content": "prior"}], persist_user_message="hello",
        persist_user_timestamp=None, persist_user_display_kind=None, persistence_owner=None,
        hygiene_adoption_epoch=0, message_text="hello",
    )
    proxy_assistant = {"role": "assistant", "content": "proxy answer"}
    agent_result = {
        "agent_persisted": True, "_gateway_user_turn_persisted": True, "history_offset": 0,
        "last_prompt_tokens": 0, "messages": [{"role": "user", "content": "hello"}, proxy_assistant],
    }
    from gateway.outbound_reply import queued_reply_metadata
    queued_reply_metadata({"thread_id": "7"}, agent_result)
    await runner._hmwa_persist_turn_transcript(
        event=event, source=source, session_entry=entry, session_key=entry.session_key,
        agent_result=agent_result, agent_messages=agent_result["messages"], prepared=prepared,
        response="proxy answer", agent_failed_early=False, hidden_reasoning_incomplete=False,
        is_context_overflow_failure=False,
    )
    named = event._transcript_message_uid
    stamped = await store.record_outbound_platform_message(entry.session_key, "reply-proxy", "", named)
    rows = [row for row in store._store._db.get_messages(entry.session_id) if row["role"] == "assistant"]
    assert stamped is True
    assert rows[0]["message_uid"] == named == event._transcript_message_uid
    assert rows[0]["platform_message_id"] == "reply-proxy"


@pytest.mark.asyncio
async def test_multi_assistant_gateway_turn_stamps_the_visible_final_row(tmp_path, monkeypatch):
    """A tool-call row written before the final answer must not keep the platform id."""
    from gateway.outbound_reply import reply_uid_for_final

    store = _real_store(tmp_path, monkeypatch)
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="chat", chat_type="dm")
    entry = await store.get_or_create_session(source)
    runner = GatewayTurnMixin.__new__(GatewayTurnMixin)
    runner._session_db = object()
    runner.async_session_store = store
    runner._hmwa_store_turn_prompt_tokens = AsyncMock()
    runner._refresh_agent_cache_message_count = AsyncMock()
    event = MessageEvent(text="hello", message_type=MessageType.TEXT, message_id="in-tools", source=source)
    prepared = SimpleNamespace(
        history=[{"role": "user", "content": "prior"}], persist_user_message="hello",
        persist_user_timestamp=None, persist_user_display_kind=None, persistence_owner=None,
        hygiene_adoption_epoch=0, message_text="hello",
    )
    tool_call = {"id": "call-1", "type": "function", "function": {"name": "lookup", "arguments": "{}"}}
    tool_assistant = {
        "role": "assistant", "content": "", "tool_calls": [tool_call], "message_uid": "uid-tool-call",
    }
    tool_result = {
        "role": "tool", "content": "found", "tool_call_id": "call-1", "message_uid": "uid-tool-result",
    }
    final_assistant = {"role": "assistant", "content": "visible answer"}
    messages = [
        {"role": "user", "content": "hello"},
        tool_assistant,
        tool_result,
        final_assistant,
    ]
    agent_result = {
        "agent_persisted": False, "_gateway_user_turn_persisted": True, "history_offset": 0,
        "last_prompt_tokens": 0, "messages": messages, "final_response": "visible answer",
    }
    await runner._hmwa_persist_turn_transcript(
        event=event, source=source, session_entry=entry, session_key=entry.session_key,
        agent_result=agent_result, agent_messages=messages, prepared=prepared,
        response="visible answer", agent_failed_early=False, hidden_reasoning_incomplete=False,
        is_context_overflow_failure=False,
    )
    named = reply_uid_for_final(event, {})
    stamped = await store.record_outbound_platform_message(entry.session_key, "reply-final", "", named)
    rows = [row for row in store._store._db.get_messages(entry.session_id) if row["role"] == "assistant"]
    assert stamped is True
    assert named == getattr(event, "_transcript_message_uid", None) == rows[1]["message_uid"]
    assert named != "uid-tool-call"
    assert [(row["message_uid"], row.get("platform_message_id")) for row in rows] == [
        ("uid-tool-call", None),
        (named, "reply-final"),
    ]


@pytest.mark.asyncio
async def test_queued_media_keeps_uid_out_of_adapter_metadata(tmp_path, monkeypatch):
    """Queued media sees public metadata; the stamp still names the row."""
    from gateway.outbound_reply import queued_reply_metadata
    from gateway.run import GatewayRunner

    store = _real_store(tmp_path, monkeypatch)
    source = SessionSource(
        platform=Platform.DISCORD, chat_id="discord-thread", chat_type="group", thread_id="discord-thread")
    entry = await store.get_or_create_session(source)
    assistant = {"role": "assistant", "content": "first response", "message_uid": "uid-media"}
    await store.append_to_transcript(entry.session_id, assistant)
    media_path = tmp_path / "queued-first-response.png"
    media_path.write_bytes(b"not-a-real-png-but-a-real-file")

    class _MediaAdapter(_Adapter):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.sent: list = []
            self.image_batches: list = []

        async def send_multiple_images(self, chat_id, images, metadata=None, human_delay=0.0):
            self.image_batches.append({"chat_id": chat_id, "images": images, "metadata": metadata})
            return SendResult(success=True, message_id="img-1")

    adapter = _MediaAdapter(PlatformConfig(enabled=True, token="***"), Platform.DISCORD)
    adapter._session_store = store._store
    adapter._owner_profile = None
    adapter.gateway_runner = None
    runner = object.__new__(GatewayRunner)
    runner.adapters = {Platform.DISCORD: adapter}
    runner._delivery_adapter_for = lambda source: adapter  # type: ignore[method-assign]
    runner.session_store = store._store  # type: ignore[assignment]
    event = MessageEvent(
        text="hello", message_type=MessageType.TEXT, message_id="in-media", source=source)
    event._transcript_message_uid = "uid-media"  # type: ignore[attr-defined]
    metadata = queued_reply_metadata({"thread_id": "discord-thread"}, {"messages": [assistant]})
    assert metadata == {"thread_id": "discord-thread"}

    delivered = await runner._deliver_queued_first_response(
        f"first response\nMEDIA:{media_path}", source=source, adapter=adapter, metadata=metadata,
        event_message_id="in-media", transcript_message_uid=event._transcript_message_uid,
        session_key=entry.session_key, inbound_message_id="in-media")
    tasks = list(getattr(store, "_outbound_stamp_tasks", ()))
    if tasks:
        await asyncio.wait(tasks, timeout=2)

    rows = [row for row in store._store._db.get_messages(entry.session_id) if row["role"] == "assistant"]
    assert delivered is True
    assert adapter.sent[0][1] == {"thread_id": "discord-thread", "notify": True}
    assert adapter.image_batches == [{
        "chat_id": "discord-thread",
        "images": [(f"file://{quote(str(media_path))}", "")],
        "metadata": {"thread_id": "discord-thread"},
    }]
    assert all("_transcript_message_uid" not in (item[1] or {}) for item in adapter.sent)
    assert rows[0]["message_uid"] == "uid-media"
    assert rows[0]["platform_message_id"] == "reply-1"


@pytest.mark.asyncio
async def test_attachment_image_batch_keeps_uid_out_of_adapter_metadata():
    """Normal attachment delivery keeps routing keys and never carries row identity."""
    from gateway.platforms.base import _ExtractedResponse

    class _BatchAdapter(_Adapter):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.sent = []
            self.image_batches = []

        async def send_multiple_images(self, chat_id, images, metadata=None, human_delay=0.0):
            self.image_batches.append({"chat_id": chat_id, "images": images, "metadata": metadata})
            return SendResult(success=True, message_id="img-attach")

    adapter = _BatchAdapter(PlatformConfig(enabled=True, token="***"), Platform.TELEGRAM)
    adapter.gateway_runner = None
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="chat", chat_type="dm", thread_id="topic-9")
    event = MessageEvent(text="photo", message_type=MessageType.TEXT, message_id="in-attach", source=source)
    event._transcript_message_uid = "uid-final"  # type: ignore[attr-defined]
    metadata = {"thread_id": "topic-9", "notify": True}
    recorded = []
    extracted = _ExtractedResponse(
        text_content="", images=[("https://cdn.example/photo.png", "photo")],
        media_files=[], local_files=[], force_document_attachments=False, pre_extract="photo",
    )

    await adapter._deliver_attachments(
        event, extracted, metadata, anything_sent=True, record_delivery=recorded.append)

    assert adapter.image_batches == [{
        "chat_id": "chat",
        "images": [("https://cdn.example/photo.png", "photo")],
        "metadata": {"thread_id": "topic-9", "notify": True},
    }]
    assert recorded[0].success is True
    assert recorded[0].message_id == "img-attach"


@pytest.mark.asyncio
async def test_tts_caption_only_reply_stamps_caption_message_id(tmp_path):
    class _StampStore:
        def __init__(self):
            self.calls = []
            self._outbound_stamp_tasks = set()

        def record_outbound_platform_message(self, *args):
            self.calls.append(args)
            return True

    adapter = _Adapter(PlatformConfig(enabled=True, token="***"), Platform.TELEGRAM)
    adapter._session_store = _StampStore()
    adapter._record_delivery_obligation = AsyncMock(return_value=None)

    async def play_tts(**kwargs):
        assert kwargs["metadata"] == {"thread_id": "topic"}
        assert kwargs["caption"] == "visible reply"
        return SendResult(success=True, message_id="caption-1")

    adapter.play_tts = play_tts  # type: ignore[method-assign]
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="chat", chat_type="dm", thread_id="topic")
    event = MessageEvent(text="voice", message_type=MessageType.VOICE, message_id="in", source=source)
    event._transcript_message_uid = "uid-caption"  # type: ignore[attr-defined]
    delivered = await adapter._play_tts_file(
        event, "visible reply", str(tmp_path / "reply.ogg"), True, {"thread_id": "topic"},
        lambda _result: None, "session-1")
    tasks = list(adapter._session_store._outbound_stamp_tasks)
    if tasks:
        await asyncio.gather(*tasks)
    assert delivered is True
    assert adapter._session_store.calls == [("session-1", "caption-1", "", "uid-caption")]
