"""A shutdown-spooled event must replay with the same user content and reply context it arrived with."""

import json
from dataclasses import replace
from datetime import datetime
from unittest.mock import MagicMock

from gateway.platforms.event import MessageEvent, MessageType
from gateway.run_pending_recovery import recover_pending_shutdown_flush
from gateway.session import SessionEntry
from gateway.shutdown_flush import flush_overflow_to_file, flush_pending_to_file, recover_pending_to_db
from tests.gateway.restart_test_helpers import make_restart_runner, make_restart_source


def _spooled_runner(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("gateway.run_pending_recovery.get_routing_process_hermes_home", lambda: tmp_path)
    runner, _adapter = make_restart_runner()
    source = replace(make_restart_source(chat_type="group"), message_id="101")
    key = runner._session_key_for_source(source)
    runner.session_store._entries[key] = SessionEntry(
        session_key=key, session_id="sid", created_at=datetime.now(), updated_at=datetime.now(),
        origin=source, resume_pending=True, resume_reason="restart_interrupted",
        last_resume_marked_at=datetime.now(),
    )
    db = MagicMock()
    runner.session_store.resolve_session_id_for_key = MagicMock(return_value=("sid", db))
    runner._startup_restore_queue = []
    runner._startup_restore_tasks = []
    runner._startup_restore_in_progress = True
    return runner, source, key, db


def test_media_only_followup_replays_with_its_attachment(tmp_path, monkeypatch):
    runner, source, key, db = _spooled_runner(tmp_path, monkeypatch)
    voice = MessageEvent(text="", message_type=MessageType.VOICE, source=source, user_id="u1",
                         message_id="102", media_urls=["/media/note.ogg"], media_types=["audio/ogg"])
    assert flush_pending_to_file({key: voice}) == 1
    assert recover_pending_shutdown_flush(runner) == 1
    queued, = runner._startup_restore_queue
    assert (queued.text, queued.message_type) == ("", MessageType.VOICE)
    assert queued.media_urls == ["/media/note.ogg"]
    assert queued.media_types == ["audio/ogg"]
    db.append_message.assert_not_called()


def test_empty_slots_skip_but_media_only_reaches_the_transcript(tmp_path, monkeypatch):
    flush_dir = tmp_path / "pending_messages"
    flush_dir.mkdir()
    monkeypatch.setattr("gateway.shutdown_flush._get_flush_dir", lambda: flush_dir)
    photo = MessageEvent(text="", message_type=MessageType.PHOTO, media_urls=["/media/cat.png"],
                         media_types=["image/png"])
    assert flush_pending_to_file({"a": "", "b": MessageEvent(text="")}) == 0
    assert flush_overflow_to_file({"c": [MessageEvent(text=""), photo]}) == 1
    payload_path, = flush_dir.glob("*.json")
    payload = json.loads(payload_path.read_text())
    payload["data"]["session_id"] = "sid"
    payload_path.write_text(json.dumps(payload))
    db = MagicMock()
    assert recover_pending_to_db(db) == 1
    assert "/media/cat.png" in db.append_message.call_args.kwargs["content"]
    assert list(flush_dir.glob("*.json")) == []


def test_reply_context_and_inline_contract_survive_replay(tmp_path, monkeypatch):
    runner, source, key, db = _spooled_runner(tmp_path, monkeypatch)
    reply = MessageEvent(text="what about this?", message_type=MessageType.DOCUMENT, source=source,
                         user_id="u1", message_id="103", media_urls=["/media/a.txt"],
                         media_types=["text/plain"], media_text_inlined=[False],
                         reply_to_message_id="90", reply_to_text="the earlier answer",
                         reply_to_author_id="bot", reply_to_author_name="Hermes",
                         reply_to_is_own_message=True)
    assert flush_pending_to_file({key: reply}) == 1
    assert recover_pending_shutdown_flush(runner) == 1
    queued, = runner._startup_restore_queue
    assert queued.reply_to_message_id == "90"
    assert queued.reply_to_text == "the earlier answer"
    assert (queued.reply_to_author_id, queued.reply_to_author_name) == ("bot", "Hermes")
    assert queued.reply_to_is_own_message is True
    assert queued.media_text_inlined == [False]
    assert queued.message_type == MessageType.DOCUMENT


def test_legacy_spool_without_new_keys_still_replays(tmp_path, monkeypatch):
    runner, _source, key, db = _spooled_runner(tmp_path, monkeypatch)
    path = tmp_path / "pending_messages" / "pending-legacy.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"session_key": key, "ts": int(datetime.now().timestamp()), "data": {
        "text": "older release", "user_id": "u1", "message_id": "104",
        "media_urls": ["/media/b.png"], "media_types": ["image/png"], "reply_to_message_id": "91",
    }}))
    assert recover_pending_shutdown_flush(runner) == 1
    queued, = runner._startup_restore_queue
    assert (queued.text, queued.message_type) == ("older release", MessageType.TEXT)
    assert queued.media_urls == ["/media/b.png"] and queued.media_text_inlined == []
    assert queued.reply_to_message_id == "91" and queued.reply_to_text is None
    assert queued.reply_to_is_own_message is False
