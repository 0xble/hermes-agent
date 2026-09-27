"""A shutdown-spooled follow-up must not become the interrupted turn's prompt."""

import asyncio
from dataclasses import replace
from datetime import datetime
from unittest.mock import MagicMock

import pytest

from gateway.platforms.event import MessageEvent, MessageType
from gateway.run import _prepare_resume_pending_message
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
