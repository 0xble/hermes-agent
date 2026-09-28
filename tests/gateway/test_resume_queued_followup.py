"""A shutdown-spooled follow-up must not become the interrupted turn's prompt."""

import asyncio
import json
from dataclasses import replace
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.platforms.event import MessageEvent, MessageType
from gateway.run import _prepare_resume_pending_message, _profile_runtime_scope
from gateway.session import SessionEntry
from gateway.shutdown_flush import flush_pending_to_file
from gateway.run_pending_recovery import recover_pending_shutdown_flush
from tests.gateway.restart_test_helpers import make_restart_runner, make_restart_source


@pytest.mark.asyncio
async def test_spooled_followup_waits_for_resumed_answer_with_own_reply_anchor(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    runner, adapter = make_restart_runner()
    runner.config.restart_resume_policy = "continue"
    source = replace(make_restart_source(), message_id="101")
    key = runner._session_key_for_source(source)
    entry = SessionEntry(
        session_key=key, session_id="sid", created_at=datetime.now(), updated_at=datetime.now(),
        origin=source, resume_pending=True, resume_reason="restart_interrupted",
        last_resume_marked_at=datetime.now(),
    )
    runner.session_store._entries[key] = entry
    followup = MessageEvent(
        text="B: answer separately", message_type=MessageType.TEXT,
        source=replace(source, message_id="102"), message_id="102",
    )
    assert flush_pending_to_file({key: followup}) == 1
    rows = [{"role": "user", "content": "A: interrupted"},
            {"role": "assistant", "content": "Operation interrupted."}]
    db = MagicMock()
    db.append_message.side_effect = lambda **kw: rows.append({"role": kw["role"], "content": kw["content"]})
    runner.session_store.resolve_session_id_for_key = MagicMock(return_value=("sid", db))
    runner._startup_restore_queue = []
    runner._startup_restore_tasks = []
    runner._startup_restore_in_progress = True
    monkeypatch.setattr("gateway.run_pending_recovery.get_routing_process_hermes_home", lambda: tmp_path)
    assert recover_pending_shutdown_flush(runner) == 1
    assert [row["content"] for row in rows] == ["A: interrupted", "Operation interrupted."]
    assert not list((tmp_path / "pending_messages").glob("*.json"))

    replies = []
    async def handle(event):
        if event.internal:
            note, _ = _prepare_resume_pending_message(
                "restart_interrupted", event.text, restart_resume_policy="continue")
            assert "CONTINUE the interrupted task" in note
            assert "B: answer separately" not in note
            rows.extend([{"role": "user", "content": note}, {"role": "assistant", "content": "A answer"}])
            replies.append(("A answer", runner._reply_anchor_for_event(event)))
            entry.resume_pending = False
        else:
            rows.extend([{"role": "user", "content": event.text}, {"role": "assistant", "content": "B answer"}])
            replies.append(("B answer", runner._reply_anchor_for_event(event)))
    adapter.handle_message = handle
    assert runner._schedule_resume_pending_sessions() == 1
    await runner._finish_startup_restore()
    assert [row["role"] for row in rows] == ["user", "assistant", "user", "assistant", "user", "assistant"]
    assert rows[-4:][0]["content"].startswith("[System note:")
    assert rows[-2]["content"] == "B: answer separately"
    assert replies == [("A answer", "101"), ("B answer", "102")]
    assert runner._startup_restore_queue == []


def _spooled_runner(tmp_path, monkeypatch, *, pending=True, session_id="sid"):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("gateway.run_pending_recovery.get_routing_process_hermes_home", lambda: tmp_path)
    runner, adapter = make_restart_runner()
    source = replace(make_restart_source(chat_type="group"), message_id="101", user_name="Starter")
    key = runner._session_key_for_source(source)
    runner.session_store._entries[key] = SessionEntry(
        session_key=key, session_id="sid", created_at=datetime.now(), updated_at=datetime.now(),
        origin=source, resume_pending=pending, resume_reason="restart_interrupted",
        last_resume_marked_at=datetime.now(),
    )
    db = MagicMock()
    runner.session_store.resolve_session_id_for_key = MagicMock(return_value=(session_id, db))
    runner._startup_restore_queue = []
    runner._startup_restore_tasks = []
    runner._startup_restore_in_progress = True
    return runner, adapter, source, key, db


def test_shared_followup_replays_under_real_author_and_preserves_context(tmp_path, monkeypatch):
    runner, _, source, key, db = _spooled_runner(tmp_path, monkeypatch)
    actual = replace(source, user_id="u2", user_name="Followup", user_id_alt="alt2",
                     is_bot=True, role_authorized=True, message_id="102")
    followup = MessageEvent(text="/status", source=actual, user_id="u2", user_name="Followup",
                            message_id="102", media_urls=["/media/a"], media_types=["image/png"],
                            reply_to_message_id="100")
    assert flush_pending_to_file({key: followup}) == 1
    assert recover_pending_shutdown_flush(runner) == 1
    queued, = runner._startup_restore_queue
    assert queued.is_command()
    assert queued.user_id == queued.source.user_id == "u2"
    assert queued.user_name == queued.source.user_name == "Followup"
    assert queued.source.user_id_alt == "alt2"
    assert queued.source.is_bot and queued.source.role_authorized
    assert queued.message_id == queued.source.message_id == "102"
    assert queued.media_urls == ["/media/a"]
    assert queued.media_types == ["image/png"]
    assert queued.reply_to_message_id == "100"
    db.append_message.assert_not_called()


@pytest.mark.parametrize("pending,session_id", [(False, "sid"), (True, "different")])
def test_followup_without_matching_pending_resume_appends(tmp_path, monkeypatch, pending, session_id):
    runner, _, source, key, db = _spooled_runner(tmp_path, monkeypatch, pending=pending,
                                                   session_id=session_id)
    assert flush_pending_to_file({key: MessageEvent(text="followup", source=source, user_id="u1")}) == 1
    assert recover_pending_shutdown_flush(runner) == 1
    assert runner._startup_restore_queue == []
    db.append_message.assert_called_once()
    assert db.append_message.call_args.kwargs["content"] == "followup"


def test_legacy_spool_without_author_appends_instead_of_replaying(tmp_path, monkeypatch):
    runner, _, _, key, db = _spooled_runner(tmp_path, monkeypatch)
    path = tmp_path / "pending_messages" / "pending-legacy.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"session_key": key, "ts": int(datetime.now().timestamp()),
                                "data": {"text": "/status", "message_id": "102"}}))
    assert recover_pending_shutdown_flush(runner) == 1
    assert runner._startup_restore_queue == []
    db.append_message.assert_called_once()
    assert not path.exists()


def test_multiplexed_recovery_uses_profile_delivery_adapter(tmp_path, monkeypatch):
    primary = tmp_path / "primary"
    secondary = tmp_path / "secondary"
    primary.mkdir()
    secondary.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(primary))
    monkeypatch.setattr("gateway.run_pending_recovery.get_routing_process_hermes_home", lambda: primary)
    runner, _ = make_restart_runner()
    runner.config = GatewayConfig(multiplex_profiles=True)
    runner._primary_profile_name = "default"
    runner._served_profile_homes = {"default": primary, "other": secondary}
    origin = replace(make_restart_source(chat_type="group"), profile="other", message_id="101")
    key = runner._session_key_for_source(origin)
    runner.session_store._entries[key] = SessionEntry(
        session_key=key, session_id="other-sid", created_at=datetime.now(), updated_at=datetime.now(),
        origin=origin, resume_pending=True,
    )
    other_adapter = object()
    runner._delivery_adapter_for = MagicMock(return_value=other_adapter)
    db = MagicMock()
    runner.session_store.resolve_session_id_for_key = MagicMock(return_value=("other-sid", db))
    runner._startup_restore_queue = []
    with _profile_runtime_scope(secondary, prepared_secret_scope={}):
        assert flush_pending_to_file({key: MessageEvent(text="other", source=origin, user_id="u1")}) == 1
    assert recover_pending_shutdown_flush(runner) == 1
    event, = runner._startup_restore_queue
    assert event.source.profile == "other"
    assert event.source.platform == Platform.TELEGRAM
    resolved_source = runner._delivery_adapter_for.call_args.args[0]
    assert resolved_source.profile == event.source.profile
    assert resolved_source.platform == event.source.platform
    db.append_message.assert_not_called()


@pytest.mark.asyncio
async def test_offline_followup_retried_on_primary_reconnect(tmp_path, monkeypatch):
    runner, adapter, source, key, db = _spooled_runner(tmp_path, monkeypatch)
    assert flush_pending_to_file({key: MessageEvent(text="later", source=source, user_id="u1")}) == 1
    runner.adapters.clear()
    assert recover_pending_shutdown_flush(runner) == 0
    assert list((tmp_path / "pending_messages").glob("*.json"))
    runner._startup_restore_in_progress = False
    runner._failed_platforms = {source.platform: {}}
    runner._publish_primary_adapter = lambda platform, adapter: runner.adapters.__setitem__(platform, adapter)
    runner._update_platform_runtime_status = MagicMock()
    runner._schedule_planned_restart_replay = MagicMock()
    runner._redeliver_failed_obligations_for_platform = AsyncMock()
    runner._schedule_resume_pending_sessions = MagicMock(return_value=0)
    runner._await_startup_warmup = AsyncMock()
    adapter.handle_message = AsyncMock()
    await runner._install_reconnected_adapter(source.platform, adapter)
    assert not list((tmp_path / "pending_messages").glob("*.json"))
    runner._schedule_resume_pending_sessions.assert_called_once()
    adapter.handle_message.assert_awaited_once()
    assert adapter.handle_message.call_args.args[0].text == "later"
    db.append_message.assert_not_called()
