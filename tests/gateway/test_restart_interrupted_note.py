"""S2 regressions: one durable visible note per restart-cut human turn."""

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult, _ExtractedResponse
from gateway.platforms.event import MessageEvent, MessageType
from gateway.run import GatewayRunner
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
async def test_cut_human_turn_uses_one_note_when_broadcast_enabled(tmp_path):
    store = _store(tmp_path)
    source = _source("broadcast-dedup")
    entry = store.get_or_create_session(source)
    store.mark_resume_pending(entry.session_key, turn_id="turn-cut", human=True)
    adapter = NoteAdapter()
    runner = object.__new__(GatewayShutdownMixin)
    runner.session_store = store
    runner._async_session_store = AsyncSessionStore(store)
    runner.async_session_store = runner._async_session_store
    runner.config = GatewayConfig(
        restart_resume_policy="continue",
        platforms={Platform.TELEGRAM: PlatformConfig(
            enabled=True, token="***", gateway_restart_notification=True,
        )},
    )
    runner._running_agents = {entry.session_key: object()}
    runner._s2_note_session_keys = {entry.session_key}
    runner._snapshot_running_agents = lambda: [entry.session_key]
    runner._shutdown_notification_target = AsyncMock(
        return_value=(source, "telegram", source.chat_id, source.thread_id, None)
    )
    runner._delivery_adapter_for = lambda _source: adapter
    runner._authorization_adapter = lambda *_args: adapter
    runner._owning_profile = lambda *_args: (True, None)
    runner._thread_metadata_for_target = lambda *args, **kwargs: {"thread_id": source.thread_id}
    runner._served_home_channel_configs = lambda: []
    runner._restart_requested = False
    runner._restart_command_source = None
    runner._restart_reason = None

    await runner._notify_active_sessions_of_shutdown()
    assert adapter.sent == []

    assert await runner._send_interrupted_turn_notes([entry.session_key]) == 1
    assert len(adapter.sent) == 1
    assert "interrupted" in adapter.sent[0][1].lower()


@pytest.mark.asyncio
async def test_user_message_after_note_keeps_new_turn_resumable(tmp_path):
    adapter = NoteAdapter()
    store, entry, _ = _pending_store(tmp_path, adapter)
    store.set_restart_note_message_id(entry.session_key, "note-user")
    event = MessageEvent(text="continue this", message_type=MessageType.TEXT, source=_source(), internal=False)

    await adapter._reconcile_restart_note(event, entry.session_key)
    assert adapter.deleted == [("chat", "note-user")]
    assert store._entries[entry.session_key].resume_turn_id == "turn-2"

    assert store.mark_resume_pending(entry.session_key, turn_id="turn-user", human=True)
    assert store._entries[entry.session_key].resume_pending is True
    assert store._entries[entry.session_key].resume_turn_id == "turn-user"


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


def _configure_real_final_delivery(adapter, store):
    adapter.gateway_runner = SimpleNamespace(
        async_session_store=AsyncSessionStore(store),
        _delivery_adapter_for=lambda _source: adapter,
        config=SimpleNamespace(durable_outbox_enabled=False),
    )
    adapter._record_delivery_obligation = AsyncMock(return_value=None)
    adapter._finalize_delivery_obligation = AsyncMock()


@pytest.mark.asyncio
async def test_real_nonstream_delivery_reconciles_after_runner_clears_resume_pending(tmp_path, monkeypatch):
    """The runner returns text, clears resume_pending, then the adapter performs final delivery."""
    from gateway import run_turn
    from gateway.run_turn import GatewayTurnMixin

    adapter = NoteAdapter()
    store, entry, _ = _pending_store(tmp_path, adapter)
    store.set_restart_note_message_id(entry.session_key, "note-real")
    _configure_real_final_delivery(adapter, store)
    runner = object.__new__(GatewayTurnMixin)
    runner._delivery_adapter_for = lambda _source: adapter
    runner._should_send_voice_reply = lambda *args, **kwargs: False
    monkeypatch.setattr(run_turn, "diagnostic_wake_muted", lambda _event: False)
    event = MessageEvent(text="continue", message_type=MessageType.TEXT, source=_source(), internal=False)
    delivered_response = await runner._hmwa_deliver_turn_response(
        event, event.source, entry, entry.session_key, 1,
        {"already_sent": False}, [], "resumed answer", None, False,
    )
    assert delivered_response == "resumed answer"
    assert store.clear_resume_pending(entry.session_key)
    delivered = []

    await adapter._send_final_text(event, entry.session_key, delivered_response, {}, False, 0, delivered.append)

    assert adapter.deleted == [("chat", "note-real")]
    assert adapter.edited == []
    assert len(adapter.sent) == 1
    assert adapter.sent[0][1] == "resumed answer"
    assert store.get_restart_note(entry.session_key) is None


@pytest.mark.asyncio
async def test_real_delivery_keeps_failed_note_and_retries_on_next_final(tmp_path):
    adapter = NoteAdapter(edit_result=False, delete_result=False)
    store, entry, _ = _pending_store(tmp_path, adapter)
    store.set_restart_note_message_id(entry.session_key, "note-retry")
    assert store.clear_resume_pending(entry.session_key)
    _configure_real_final_delivery(adapter, store)
    first_event = MessageEvent(text="continue", message_type=MessageType.TEXT, source=_source(), internal=False)

    await adapter._send_final_text(first_event, entry.session_key, "first answer", {}, False, 0, lambda _r: None)

    assert len(adapter.sent) == 1
    assert store.get_restart_note(entry.session_key)[3] == "note-retry"
    adapter.delete_result = True
    second_event = MessageEvent(text="later", message_type=MessageType.TEXT, source=_source(), internal=False)
    await adapter._send_final_text(second_event, entry.session_key, "later answer", {}, False, 0, lambda _r: None)

    assert adapter.deleted == [("chat", "note-retry"), ("chat", "note-retry")]
    assert store.get_restart_note(entry.session_key) is None


@pytest.mark.asyncio
async def test_new_interruption_replaces_stale_note_before_posting_one(tmp_path):
    adapter = NoteAdapter()
    store = _store(tmp_path)
    source = _source("stale-thread")
    entry = store.get_or_create_session(source)
    store.mark_resume_pending(entry.session_key, turn_id="turn-old", human=True)
    store.set_restart_note_message_id(entry.session_key, "old-note")
    assert store.clear_resume_pending(entry.session_key)
    store.mark_resume_pending(entry.session_key, turn_id="turn-new", human=True)

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

    assert await runner._send_interrupted_turn_notes([entry.session_key]) == 1
    assert adapter.deleted == [("chat", "old-note")]
    assert len(adapter.sent) == 1
    assert store.get_restart_note(entry.session_key)[3] == "m1"


@pytest.mark.asyncio
async def test_non_deleting_adapter_posts_each_consecutive_interruption_note(tmp_path):
    """Adapters without delete support keep old notes visible but never suppress a new turn's note."""
    adapter = NoteAdapter(delete_result=False)
    store = _store(tmp_path)
    source = _source("non-deleting-thread")
    entry = store.get_or_create_session(source)
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

    store.mark_resume_pending(entry.session_key, turn_id="turn-1", human=True)
    assert await runner._send_interrupted_turn_notes([entry.session_key]) == 1
    assert adapter.sent[0][0] == source.chat_id
    assert store.clear_resume_pending(entry.session_key)

    store.mark_resume_pending(entry.session_key, turn_id="turn-2", human=True)
    assert await runner._send_interrupted_turn_notes([entry.session_key]) == 1

    assert len(adapter.sent) == 2
    assert adapter.deleted == [(source.chat_id, "m1")]
    assert store.get_restart_note(entry.session_key)[3] == "m2"


@pytest.mark.asyncio
async def test_sent_no_id_note_is_cleared_at_final_delivery(tmp_path):
    adapter = NoteAdapter(no_message_id=True)
    store, entry, _ = _pending_store(tmp_path, adapter)
    store.set_restart_note_message_id(entry.session_key, "sent:no-id")
    assert store.clear_resume_pending(entry.session_key)
    _configure_real_final_delivery(adapter, store)
    event = MessageEvent(text="continue", message_type=MessageType.TEXT, source=_source(), internal=False)

    await adapter._send_final_text(event, entry.session_key, "normal answer", {}, False, 0, lambda _r: None)

    assert adapter.sent == [("chat", "normal answer", {})]
    assert store.get_restart_note(entry.session_key) is None


@pytest.mark.asyncio
async def test_attachment_only_final_reconciles_restart_note(tmp_path):
    adapter = NoteAdapter()
    store, entry, event = _pending_store(tmp_path, adapter)
    store.set_restart_note_message_id(entry.session_key, "note-attachment")
    adapter._deliver_media_attachments = AsyncMock()
    extracted = _ExtractedResponse(
        text_content="", images=[], media_files=[("answer.txt", False)], local_files=[],
        force_document_attachments=False, pre_extract="MEDIA: answer.txt",
    )

    await adapter._deliver_attachments(
        event, extracted, {}, anything_sent=False, record_delivery=lambda _result: None,
        session_key=entry.session_key,
    )

    assert adapter.deleted == [("chat", "note-attachment")]
    assert store.get_restart_note(entry.session_key)[3] is None


@pytest.mark.asyncio
async def test_resumed_answer_deletes_note_and_sends_fresh_message(tmp_path):
    adapter = NoteAdapter()
    store, entry, event = _pending_store(tmp_path, adapter)
    store.set_restart_note_message_id(entry.session_key, "note-7")

    await adapter._reconcile_restart_note(event, entry.session_key)

    assert adapter.edited == []
    assert adapter.deleted == [("chat", "note-7")]
    assert store.get_restart_note(entry.session_key)[3] is None


@pytest.mark.asyncio
async def test_resumed_answer_delete_send_fallback_has_no_orphan(tmp_path):
    adapter = NoteAdapter(edit_result=False, delete_result=True)
    store, entry, event = _pending_store(tmp_path, adapter)
    store.set_restart_note_message_id(entry.session_key, "note-8")
    _configure_real_final_delivery(adapter, store)

    await adapter._send_final_text(event, entry.session_key, "replacement", {}, False, 0, lambda _r: None)

    assert adapter.sent[-1][1] == "replacement"
    assert adapter.deleted == [("chat", "note-8")]
    assert store.get_restart_note(entry.session_key)[3] is None


@pytest.mark.asyncio
async def test_user_message_recovery_turn_reconciles_note(tmp_path):
    adapter = NoteAdapter()
    store, entry, _ = _pending_store(tmp_path, adapter)
    store.set_restart_note_message_id(entry.session_key, "note-user")
    event = MessageEvent(text="continue this", message_type=MessageType.TEXT, source=_source(), internal=False)

    await adapter._reconcile_restart_note(event, entry.session_key)

    assert adapter.edited == []
    assert adapter.deleted == [("chat", "note-user")]
    assert store.get_restart_note(entry.session_key)[3] is None


@pytest.mark.asyncio
async def test_streamed_resumed_answer_deletes_note_before_marker_clear(tmp_path, monkeypatch):
    from gateway import run_turn
    from gateway.run_turn import GatewayTurnMixin

    adapter = NoteAdapter()
    store, entry, _ = _pending_store(tmp_path, adapter)
    store.set_restart_note_message_id(entry.session_key, "note-stream")
    runner = object.__new__(GatewayTurnMixin)
    runner._delivery_adapter_for = lambda _source: adapter
    runner._should_send_voice_reply = lambda *args, **kwargs: False
    runner._deliver_media_from_response = AsyncMock()
    adapter.gateway_runner = SimpleNamespace(async_session_store=AsyncSessionStore(store))
    event = MessageEvent(text="", message_type=MessageType.TEXT, source=_source(), internal=True)
    monkeypatch.setattr(run_turn, "diagnostic_wake_muted", lambda _event: False)

    delivered = await runner._hmwa_deliver_turn_response(
        event, event.source, entry, entry.session_key, 1,
        {"already_sent": True}, [], "streamed answer", None, False,
    )

    assert delivered is None
    assert adapter.deleted == [("chat", "note-stream")]
    assert store._entries[entry.session_key].resume_pending is True
    assert store.get_restart_note(entry.session_key)[3] is None
    assert store.clear_resume_pending(entry.session_key)


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
async def test_success_without_message_id_uses_sentinel_and_deduplicates(tmp_path):
    store = _store(tmp_path)
    source = _source("no-id-thread")
    entry = store.get_or_create_session(source)
    store.mark_resume_pending(entry.session_key, turn_id="turn-no-id", human=True)
    adapter = NoteAdapter(no_message_id=True)
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

    assert await runner._send_interrupted_turn_notes([entry.session_key]) == 1
    assert await runner._send_interrupted_turn_notes([entry.session_key], reclaim_pending=True) == 0
    assert len(adapter.sent) == 1
    assert store.get_restart_note(entry.session_key)[3] == "sent:no-id"


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
    runner._restart_requested = False
    runner._shutdown_notification_target = AsyncMock(
        return_value=(source, "telegram", source.chat_id, source.thread_id, None)
    )
    runner._delivery_adapter_for = lambda _source: adapter
    runner._authorization_adapter = lambda *_args: adapter
    runner._thread_metadata_for_target = lambda *args, **kwargs: {"thread_id": source.thread_id}

    assert await runner._send_interrupted_turn_notes([entry.session_key], reclaim_pending=True) == 1
    assert "Hermes is restarting" in adapter.sent[0][1]
    assert store.get_restart_note(entry.session_key)[3] == "m1"
    assert store._entries[entry.session_key].resume_pending is True




@pytest.mark.asyncio
async def test_shutdown_note_batch_is_bounded_before_interrupting_agents(tmp_path, monkeypatch):
    """A wedged transport cannot hold the real interrupt phase beyond the note deadline."""
    from gateway.run import GatewayRunner
    from tests.gateway.restart_test_helpers import make_restart_runner

    runner, _ = make_restart_runner()
    store = _store(tmp_path)
    source = _source("shutdown-bound")
    entry = store.get_or_create_session(source)
    entry.active_turn_token = "turn-live"
    runner.session_store = store
    runner._async_session_store = AsyncSessionStore(store)
    runner._running_agents = {entry.session_key: object()}
    runner._peek_session_state = lambda _key: SimpleNamespace(
        turn=SimpleNamespace(event=MessageEvent(text="work", message_type=MessageType.TEXT, source=source))
    )
    runner._is_user_turn_event = lambda event: not event.internal
    adapter = NoteAdapter(send_delay=10.0)
    runner._shutdown_notification_target = AsyncMock(
        return_value=(source, "telegram", source.chat_id, source.thread_id, None)
    )
    runner._delivery_adapter_for = lambda _source: adapter
    runner._authorization_adapter = lambda *_args: adapter
    runner._thread_metadata_for_target = lambda *args, **kwargs: {"thread_id": source.thread_id}
    runner._post_interrupt_grace_timeout = lambda: 0.01
    runner._interrupt_running_agents = Mock()
    runner._active_api_run_count = lambda: 0
    runner._notify_interrupted_cron_jobs = AsyncMock(return_value=0)
    monkeypatch.setattr(GatewayRunner, "_post_interrupt_grace_timeout", lambda self: 0.01)
    monkeypatch.setattr(GatewayRunner, "_stop_kill_tool_subprocesses_off_loop", AsyncMock(return_value=[]))
    ctx = GatewayShutdownMixin._StopContext(deferred_count=lambda: 0)
    ctx.started_at = asyncio.get_running_loop().time()
    started = asyncio.get_running_loop().time()
    await runner._stop_interrupt_remaining_work(ctx)
    elapsed = asyncio.get_running_loop().time() - started

    assert elapsed < 2.7
    runner._interrupt_running_agents.assert_called()
    assert store.get_restart_note(entry.session_key)[3].startswith("pending:")


def test_startup_note_candidates_reject_legacy_and_stale_rows():
    from gateway.run_startup import GatewayStartupMixin

    runner = object.__new__(GatewayStartupMixin)
    runner._auto_resume_ready = lambda entry: object() if entry.session_key == "fresh" else None
    candidates = [
        SimpleNamespace(session_key="legacy", resume_turn_id=None),
        SimpleNamespace(session_key="stale", resume_turn_id="turn-stale"),
        SimpleNamespace(session_key="fresh", resume_turn_id="turn-fresh"),
    ]

    assert runner._startup_interrupted_note_candidates(candidates) == ["fresh"]


@pytest.mark.asyncio
async def test_startup_boot_send_path_posts_interrupted_notes_first(monkeypatch):
    from gateway.run_startup import GatewayStartupMixin
    import gateway.run as run_module

    runner = object.__new__(GatewayStartupMixin)
    calls = []
    runner._claim_pending_obligations = AsyncMock(return_value=[])
    runner._send_restart_notification = AsyncMock(side_effect=lambda: calls.append("restart"))
    runner._schedule_update_notification_watch = lambda: calls.append("update-watch")
    runner._redeliver_claimed_obligations = AsyncMock(side_effect=lambda _rows: calls.append("redeliver"))
    runner._send_interrupted_turn_notes = AsyncMock(
        side_effect=lambda keys, **kwargs: calls.append((keys, kwargs)),
    )
    runner._retain_background_task = lambda task: None
    runner._late_failure_callback = lambda *args, **kwargs: (lambda task: None)
    monkeypatch.setattr(run_module, "_startup_restore_drain_timeout_secs", lambda: 0)

    await runner._await_startup_boot_sends(
        planned_restart_notification_pending=False,
        interrupted_note_keys=["fresh"],
    )

    assert calls == [
        (["fresh"], {"reclaim_pending": True}),
        "restart",
        "update-watch",
        "redeliver",
    ]


@pytest.mark.asyncio
async def test_startup_claims_ledger_answer_before_resume_snapshot(tmp_path, monkeypatch):
    """An answered ledger row must not be resumed or get a startup interruption note (#91969)."""
    from gateway import delivery_ledger as ledger
    from gateway.run_startup import GatewayStartupMixin
    import gateway.run_pending_recovery as pending_recovery
    import gateway.run_startup as startup_module
    import gateway.run as run_module

    monkeypatch.setattr(ledger, "_db_path", lambda: tmp_path / "state.db")
    obligation_id = ledger.compute_obligation_id("answered-session", "inbound", "answer")
    ledger.record_obligation(
        obligation_id=obligation_id, session_key="answered-session", platform="telegram",
        chat_id="chat", thread_id="thread", content="answer",
    )
    ledger.mark_attempting(obligation_id)
    with ledger._connect() as conn:
        conn.execute(
            "UPDATE delivery_obligations SET owner_pid=?, owner_started_at=? WHERE obligation_id=?",
            (999999999, 1, obligation_id),
        )

    store = _store(tmp_path / "sessions")
    source = _source("startup-order")
    entry = store.get_or_create_session(source)
    entry.session_key = "answered-session"
    store._entries["answered-session"] = entry
    store._entries.pop(entry.session_key, None) if entry.session_key != "answered-session" else None
    store.mark_resume_pending("answered-session", turn_id="turn-answered", human=True)

    runner = object.__new__(GatewayStartupMixin)
    runner.session_store = store
    runner.async_session_store = AsyncSessionStore(store)
    runner.adapters = {Platform.TELEGRAM: object()}
    runner._profile_adapters = {}
    runner._restart_loop_guard_config = lambda: (100, 3600, 0)
    runner._auto_resume_ready = lambda _entry: (object(), source)
    runner._start_post_connect_services = AsyncMock()
    runner._send_restart_notification = AsyncMock()
    runner._schedule_update_notification_watch = Mock()
    runner._redeliver_claimed_obligations = AsyncMock()
    note_calls = []
    runner._send_interrupted_turn_notes = AsyncMock(
        side_effect=lambda keys, **kwargs: note_calls.append((keys, kwargs)),
    )
    scheduled = []
    runner._schedule_resume_pending_sessions = Mock(
        side_effect=lambda **kwargs: scheduled.append(kwargs["candidates"]),
    )
    runner._finish_startup_restore = AsyncMock()
    runner._schedule_auto_resume_delegations = Mock()
    runner._send_session_db_warning_notifications = AsyncMock()
    monkeypatch.setattr(run_module, "_startup_restore_drain_timeout_secs", lambda: 0)
    monkeypatch.setattr(startup_module, "recover_pending_shutdown_flush", lambda *args, **kwargs: None,
                        raising=False)
    monkeypatch.setattr(pending_recovery, "recover_pending_shutdown_flush", lambda *args, **kwargs: None)
    monkeypatch.setattr("gateway.restart_loop_guard.check_and_record", lambda *args, **kwargs: False)

    await GatewayStartupMixin._start_finish_wiring(runner, 0)

    assert entry.resume_pending is False
    assert note_calls == []
    assert scheduled == [[]]




@pytest.mark.asyncio
async def test_post_delivery_resume_clear_uses_turn_start_marker(tmp_path, monkeypatch):
    from gateway import run_heartbeat_acceptance
    from gateway.run_turn import GatewayTurnMixin

    store = _store(tmp_path)
    source = _source("marker-turn")
    entry = store.get_or_create_session(source)
    store.mark_resume_pending(entry.session_key, turn_id="turn-marker", human=True)
    marker = store.get_resume_pending_marker(entry.session_key)
    runner = object.__new__(GatewayTurnMixin)
    runner.async_session_store = AsyncSessionStore(store)
    runner._hmwa_resolve_session = AsyncMock(return_value=(source, entry, entry.session_key))
    runner._hmwa_prepare_turn = AsyncMock(
        return_value=(runner._PreparedTurn([], "", "message", True, None, None), []),
    )
    runner.hooks = SimpleNamespace(emit=AsyncMock())
    runner._revive_blocked_goal_for_user_turn = AsyncMock()
    runner._persist_prompt_pins = AsyncMock()
    runner._pinned_channel_inputs = lambda *_args, **_kwargs: (None, source)
    runner._reply_anchor_for_event = lambda _event: None
    runner._run_agent = AsyncMock(return_value={"final_response": "done"})
    runner._hmwa_stop_typing_for_turn = AsyncMock()
    runner._is_user_turn_event = lambda _event: False
    runner._is_session_run_current = lambda *_args: True
    runner._hmwa_shape_agent_response = AsyncMock(return_value=("done", False, []))
    runner._hmwa_prepend_reasoning = lambda _result, response, *_args: response
    runner._hmwa_runtime_footer_line = lambda *_args: None
    runner._hmwa_post_turn_hooks = AsyncMock()
    runner._hmwa_classify_turn_failure = lambda *_args: (False, False, False)
    runner._hmwa_compression_exhaustion_reset = AsyncMock(return_value=("done", entry))
    runner._hmwa_persist_turn_transcript = AsyncMock()
    runner._hmwa_deliver_turn_response = AsyncMock(return_value="done")
    runner._clear_session_env = lambda _tokens: None
    clear_resume_pending = AsyncMock(side_effect=lambda key, **kwargs: store.clear_resume_pending(key, **kwargs))
    runner.async_session_store.clear_resume_pending = clear_resume_pending
    monkeypatch.setattr(run_heartbeat_acceptance, "heartbeat_owner_is_current", lambda *_args: True)

    event = MessageEvent(text="", message_type=MessageType.TEXT, source=source, internal=True)
    assert await runner._handle_message_with_agent(event, source, entry.session_key, 1) == "done"
    clear_resume_pending.assert_awaited_once_with(entry.session_key, expected_marker=marker)



def test_pending_note_claim_is_atomic_and_recoverable(tmp_path):
    store = _store(tmp_path)
    entry = store.get_or_create_session(_source())
    store.mark_resume_pending(entry.session_key, turn_id="turn-3", human=True)
    marker = store.get_resume_pending_marker(entry.session_key)

    assert store.claim_restart_note(entry.session_key, expected_marker=marker)
    assert not store.claim_restart_note(entry.session_key, expected_marker=marker)
    assert store.get_restart_note(entry.session_key)[3].startswith("pending:")
    assert store.claim_restart_note(entry.session_key, expected_marker=marker, reclaim_pending=True)
