"""S2 regressions: one durable visible note per restart-cut human turn."""

import asyncio
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
    def __init__(self, *, edit_result=True, delete_result=True, no_message_id=False, send_delay=0.0):
        super().__init__(PlatformConfig(enabled=True, token="***"), Platform.TELEGRAM)
        self.sent = []
        self.edited = []
        self.deleted = []
        self.edit_result = edit_result
        self.delete_result = delete_result
        self.no_message_id = no_message_id
        self.send_delay = send_delay

    async def connect(self, *, is_reconnect=False):
        return True

    async def disconnect(self):
        return None

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        if self.send_delay:
            await asyncio.sleep(self.send_delay)
        self.sent.append((chat_id, content, metadata))
        return SendResult(success=True, message_id=None if self.no_message_id else f"m{len(self.sent)}")

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


class SwallowCancelNoteAdapter(NoteAdapter):
    """A transport that keeps running after cancellation until the test releases it."""

    def __init__(self):
        super().__init__()
        self.started = asyncio.Event()
        self.release = asyncio.Event()
        self.send_calls = 0

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.send_calls += 1
        self.started.set()
        try:
            await self.release.wait()
        except asyncio.CancelledError:
            await self.release.wait()
            raise RuntimeError("cancel ignored by transport")
        raise RuntimeError("test transport held open")


def _source(thread_id="224426"):
    return SessionSource(
        platform=Platform.TELEGRAM, chat_id="chat", chat_type="group", user_id="u",
        thread_id=thread_id, message_id="inbound",
    )


def _store(tmp_path):
    return SessionStore(tmp_path, GatewayConfig(restart_resume_policy="continue"))


def _note_runner(store, source, adapter):
    runner = object.__new__(GatewayShutdownMixin)
    runner.session_store = store
    runner.async_session_store = AsyncSessionStore(store)
    runner.config = GatewayConfig(restart_resume_policy="continue")
    runner._shutdown_notification_target = AsyncMock(
        return_value=(source, "telegram", source.chat_id, source.thread_id, None),
    )
    runner._delivery_adapter_for = lambda _source: adapter
    runner._authorization_adapter = lambda *_args: adapter
    runner._thread_metadata_for_target = lambda *args, **kwargs: {}
    return runner


def _pending_store(tmp_path, adapter):
    store = _store(tmp_path)
    source = _source()
    entry = store.get_or_create_session(source)
    store.mark_resume_pending(entry.session_key, turn_id="turn-2", human=True)
    adapter.gateway_runner = SimpleNamespace(async_session_store=AsyncSessionStore(store))
    event = MessageEvent(text="", message_type=MessageType.TEXT, source=source, internal=True)
    return store, entry, event


def _configure_real_final_delivery(adapter, store):
    adapter.gateway_runner = SimpleNamespace(
        async_session_store=AsyncSessionStore(store),
        _delivery_adapter_for=lambda _source: adapter,
        config=SimpleNamespace(durable_outbox_enabled=False),
    )
    adapter._record_delivery_obligation = AsyncMock(return_value=None)
    adapter._finalize_delivery_obligation = AsyncMock()
