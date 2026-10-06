"""S2 regressions: one durable visible note per restart-cut human turn."""

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.event import MessageEvent, MessageType
from gateway.run_shutdown import GatewayShutdownMixin
from gateway.session import AsyncSessionStore, SessionSource, SessionStore


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(tmp_path / "managed"))


class NoteAdapter(BasePlatformAdapter):
    def __init__(self, *, edit_result=True, delete_result=True):
        super().__init__(PlatformConfig(enabled=True, token="***"), Platform.TELEGRAM)
        self.sent = []
        self.edited = []
        self.deleted = []
        self.edit_result = edit_result
        self.delete_result = delete_result

    async def connect(self, *, is_reconnect=False):
        return True

    async def disconnect(self):
        return None

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sent.append((chat_id, content, metadata))
        return SendResult(success=True, message_id=f"m{len(self.sent)}")

    async def send_typing(self, chat_id, metadata=None):
        return None

    async def get_chat_info(self, chat_id):
        return {"id": chat_id}

    async def edit_message(self, chat_id, message_id, content, *, finalize=False):
        self.edited.append((chat_id, message_id, content, finalize))
        return SendResult(success=self.edit_result, message_id=message_id)

    async def delete_message(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))
        return self.delete_result


def _source(thread_id="224426"):
    return SessionSource(
        platform=Platform.TELEGRAM, chat_id="chat", chat_type="group", user_id="u",
        thread_id=thread_id, message_id="inbound",
    )


def _store(tmp_path):
    return SessionStore(tmp_path, GatewayConfig(restart_resume_policy="continue"))


@pytest.mark.asyncio
async def test_human_restart_note_is_visible_once_even_when_broadcast_is_disabled(tmp_path):
    store = _store(tmp_path)
    source = _source()
    entry = store.get_or_create_session(source)
    assert store.mark_resume_pending(entry.session_key, turn_id="turn-1", human=True)

    adapter = NoteAdapter()
    runner = object.__new__(GatewayShutdownMixin)
    runner.session_store = store
    runner._async_session_store = AsyncSessionStore(store)
    runner.async_session_store = runner._async_session_store
    runner.config = GatewayConfig(
        restart_resume_policy="continue",
        platforms={Platform.TELEGRAM: PlatformConfig(
            enabled=True, token="***", gateway_restart_notification=False,
        )},
    )
    runner._shutdown_notification_target = AsyncMock(
        return_value=(source, "telegram", source.chat_id, source.thread_id, None)
    )
    runner._delivery_adapter_for = lambda _source: adapter
    runner._authorization_adapter = lambda *_args: adapter
    runner._thread_metadata_for_target = lambda *args, **kwargs: {"thread_id": source.thread_id}

    first = await runner._send_interrupted_turn_notes([entry.session_key])
    second = await runner._send_interrupted_turn_notes([entry.session_key])

    assert first == 1
    assert second == 0
    assert len(adapter.sent) == 1
    assert adapter.sent[0][2]["thread_id"] == source.thread_id
    assert store.get_restart_note(entry.session_key)[3] == "m1"


@pytest.mark.asyncio
async def test_internal_restart_turn_has_no_note(tmp_path):
    store = _store(tmp_path)
    source = _source("272142")
    entry = store.get_or_create_session(source)
    store.mark_resume_pending(entry.session_key, turn_id="internal-1", human=False)
    adapter = NoteAdapter()
    runner = object.__new__(GatewayShutdownMixin)
    runner.session_store = store
    runner._async_session_store = AsyncSessionStore(store)
    runner.async_session_store = runner._async_session_store
    runner.config = GatewayConfig(restart_resume_policy="continue")
    runner._shutdown_notification_target = AsyncMock(
        return_value=(source, "telegram", source.chat_id, source.thread_id, None)
    )
    runner._delivery_adapter_for = lambda _source: adapter
    runner._authorization_adapter = lambda *_args: adapter
    runner._thread_metadata_for_target = lambda *args, **kwargs: {"thread_id": source.thread_id}

    assert await runner._send_interrupted_turn_notes([entry.session_key]) == 0
    assert adapter.sent == []


def _pending_store(tmp_path, adapter):
    store = _store(tmp_path)
    source = _source()
    entry = store.get_or_create_session(source)
    store.mark_resume_pending(entry.session_key, turn_id="turn-2", human=True)
    adapter.gateway_runner = SimpleNamespace(async_session_store=AsyncSessionStore(store))
    event = MessageEvent(text="", message_type=MessageType.TEXT, source=source, internal=True)
    return store, entry, event


@pytest.mark.asyncio
async def test_resumed_answer_edits_note_and_clears_marker(tmp_path):
    adapter = NoteAdapter()
    store, entry, event = _pending_store(tmp_path, adapter)
    store.set_restart_note_message_id(entry.session_key, "note-7")

    result = await adapter._reconcile_restart_note(event, entry.session_key, "resumed answer", {})

    assert result.success is True
    assert adapter.edited == [("chat", "note-7", "resumed answer", True)]
    assert adapter.deleted == []
    assert store.get_restart_note(entry.session_key)[3] is None


@pytest.mark.asyncio
async def test_resumed_answer_delete_send_fallback_has_no_orphan(tmp_path):
    adapter = NoteAdapter(edit_result=False, delete_result=True)
    store, entry, event = _pending_store(tmp_path, adapter)
    store.set_restart_note_message_id(entry.session_key, "note-8")

    result = await adapter._reconcile_restart_note(event, entry.session_key, "replacement", {})
    sent = await adapter.send("chat", "replacement")

    assert result is None
    assert adapter.deleted == [("chat", "note-8")]
    assert sent.success is True
    assert store.get_restart_note(entry.session_key)[3] is None




@pytest.mark.asyncio
async def test_crash_recovery_reclaims_unposted_note_once(tmp_path):
    store = _store(tmp_path)
    source = _source("crash-thread")
    entry = store.get_or_create_session(source)
    store.mark_resume_pending(entry.session_key, turn_id="turn-crash", human=True)
    marker = store.get_resume_pending_marker(entry.session_key)
    assert store.claim_restart_note(entry.session_key, expected_marker=marker)
    adapter = NoteAdapter()
    runner = object.__new__(GatewayShutdownMixin)
    runner.session_store = store
    runner._async_session_store = AsyncSessionStore(store)
    runner.async_session_store = runner._async_session_store
    runner.config = GatewayConfig(restart_resume_policy="continue")
    runner._shutdown_notification_target = AsyncMock(
        return_value=(source, "telegram", source.chat_id, source.thread_id, None)
    )
    runner._delivery_adapter_for = lambda _source: adapter
    runner._authorization_adapter = lambda *_args: adapter
    runner._thread_metadata_for_target = lambda *args, **kwargs: {"thread_id": source.thread_id}

    assert await runner._send_interrupted_turn_notes(
        [entry.session_key], reclaim_pending=True,
    ) == 1
    assert len(adapter.sent) == 1
    assert await runner._send_interrupted_turn_notes(
        [entry.session_key], reclaim_pending=True,
    ) == 0


@pytest.mark.asyncio
async def test_non_continue_policy_uses_restart_notice_and_keeps_marker(tmp_path):
    store = _store(tmp_path)
    source = _source("ask-thread")
    entry = store.get_or_create_session(source)
    store.mark_resume_pending(entry.session_key, turn_id="turn-ask", human=True)
    adapter = NoteAdapter()
    runner = object.__new__(GatewayShutdownMixin)
    runner.session_store = store
    runner._async_session_store = AsyncSessionStore(store)
    runner.async_session_store = runner._async_session_store
    runner.config = GatewayConfig(restart_resume_policy="ask")
    runner._shutdown_notification_target = AsyncMock(
        return_value=(source, "telegram", source.chat_id, source.thread_id, None)
    )
    runner._delivery_adapter_for = lambda _source: adapter
    runner._authorization_adapter = lambda *_args: adapter
    runner._thread_metadata_for_target = lambda *args, **kwargs: {"thread_id": source.thread_id}

    assert await runner._send_interrupted_turn_notes([entry.session_key]) == 1
    assert "Hermes is restarting" in adapter.sent[0][1]
    assert store.get_restart_note(entry.session_key)[3] == "m1"
    assert store._entries[entry.session_key].resume_pending is True


def test_pending_note_claim_is_atomic_and_recoverable(tmp_path):
    store = _store(tmp_path)
    entry = store.get_or_create_session(_source())
    store.mark_resume_pending(entry.session_key, turn_id="turn-3", human=True)
    marker = store.get_resume_pending_marker(entry.session_key)

    assert store.claim_restart_note(entry.session_key, expected_marker=marker)
    assert not store.claim_restart_note(entry.session_key, expected_marker=marker)
    assert store.get_restart_note(entry.session_key)[3].startswith("pending:")
    assert store.claim_restart_note(entry.session_key, expected_marker=marker, reclaim_pending=True)
