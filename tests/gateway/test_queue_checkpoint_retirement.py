"""Accepted-queue checkpoints retire when their input is consumed or discarded.

Regressions for the replay leak in the same-session recovery PR: (a) a gateway-authored wake
with no platform id was re-claimed on every boot; (b) input consumed before a /new, reset or
compression rotation replayed into the successor session, because retirement looked the id up
in the CURRENT session; (c) /stop, /new and stale-lock heal dropped queued work in memory while
its checkpoint stayed and replayed after restart. Also the busy photo-merge deepcopy crash and
the unauthorized-owner spool that was retained forever while logging 'Dropping'.
"""
import json
import os
import threading
import time
from types import SimpleNamespace

import pytest

from gateway import shutdown_flush
from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter
from gateway.platforms.event import GOAL_CONTINUATION_METADATA_KEY, MessageEvent, MessageType
from gateway.session import build_session_key
from hermes_state import SessionDB
from tests.gateway.test_steer_command import _make_runner, _make_source, _session_entry


def _event(text, mid, **kw):
    return MessageEvent(text=text, source=_make_source(), message_id=mid, message_type=MessageType.TEXT, **kw)


def _spools(home):
    return sorted((home / "pending_messages").glob("pending-*.json"))


def _boot(db, home, sid="successor"):
    """One startup recovery pass that claims every recoverable spool as a follow-up."""
    claimed = []

    def claim(session_key, session_id, data, path):
        claimed.append(data.get("text"))
        return True

    shutdown_flush.recover_pending_spool(
        session_db=db, session_resolver=lambda *a, **k: (sid, db), deferred_followup=claim)
    return claimed


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    return tmp_path


# ── P1 (a): gateway-authored wakes are not durably checkpointed ──

def test_goal_and_heartbeat_wakes_are_accepted_without_a_replayable_checkpoint(home):
    runner, adapter = _make_runner(_session_entry())
    key = build_session_key(_make_source())
    goal = MessageEvent(text="[Continuing toward your standing goal]", source=_make_source(),
                        metadata={GOAL_CONTINUATION_METADATA_KEY: True}, reply_expected=False)
    tick = MessageEvent(text="heartbeat tick", source=_make_source())
    tick._heartbeat_session_id = "sid"
    assert runner._enqueue_fifo(key, goal, adapter)
    assert runner._enqueue_fifo(key, tick, adapter)
    assert _spools(home) == []
    db = SessionDB(db_path=home / "state.db")
    db.create_session("successor", "telegram")
    assert _boot(db, home) == [] and _boot(db, home) == []
    db.close()


def test_id_less_internal_completion_is_retired_on_consumption_not_replayed(home):
    """The checkpoint is the completion's only copy, so it stays until consumed; then it is
    retired by path (it has no platform id the old session lookup could ever match)."""
    from agent.compression_recovery import notify_input_admitted
    from gateway.shutdown_flush import retire_queue_checkpoint

    wake = MessageEvent(text="background job finished", source=_make_source(), internal=True)
    assert shutdown_flush.checkpoint_queued_event("k", wake)
    assert len(_spools(home)) == 1
    agent = SimpleNamespace(_input_admitted_callback=lambda: retire_queue_checkpoint(wake, reason="turn_admitted"))
    notify_input_admitted(agent, {"role": "user", "content": wake.text, "_db_persisted": True})
    db = SessionDB(db_path=home / "state.db")
    db.create_session("successor", "telegram")
    assert _boot(db, home) == [] and _boot(db, home) == []
    db.close()


def test_legacy_goal_continuation_checkpoint_is_dropped_not_replayed_every_boot(home):
    # A checkpoint written by the previous build for an id-less goal continuation.
    wake = MessageEvent(text="[Continuing toward your standing goal]", source=_make_source(),
                        metadata={GOAL_CONTINUATION_METADATA_KEY: True})
    data = shutdown_flush._serialise_value(wake)
    data.update(drain_deferred=True, queue_accepted=True)
    shutdown_flush._write_payload(shutdown_flush._get_flush_dir(), {
        "session_key": "k", "reason": "queue_accepted", "ts": int(time.time()), "seq": 0, "data": data})
    db = SessionDB(db_path=home / "state.db")
    db.create_session("successor", "telegram")
    assert _boot(db, home) == []
    assert _spools(home) == []
    db.close()


# ── P1 (b): consumption retires by the checkpoint's own identity ──

def test_consumed_input_does_not_replay_into_a_rotated_successor_session(home):
    from agent.compression_recovery import notify_input_admitted
    from gateway.shutdown_flush import retire_queue_checkpoint

    event = _event("inspect only", "tg:41")
    assert shutdown_flush.checkpoint_queued_event("k", event)
    # The turn admits it: its row lives in the ORIGINAL session ...
    agent = SimpleNamespace(_input_admitted_callback=lambda: retire_queue_checkpoint(event, reason="turn_admitted"))
    notify_input_admitted(agent, {"role": "user", "content": "inspect only", "_db_persisted": True})
    assert agent._input_admitted_callback is None
    # ... then /new rotated the key to a successor. Nothing may replay there.
    db = SessionDB(db_path=home / "state.db")
    db.create_session("successor", "telegram")
    assert _boot(db, home) == [] and _boot(db, home) == []
    assert _spools(home) == []
    db.close()


def test_admission_callback_waits_for_durable_ack_and_fires_once(home):
    from agent.compression_recovery import notify_input_admitted

    fired = []
    agent = SimpleNamespace(_input_admitted_callback=lambda: fired.append(1))
    notify_input_admitted(agent, {"role": "user", "content": "x"})  # flush did not land
    assert fired == [] and agent._input_admitted_callback is not None
    notify_input_admitted(agent, {"role": "user", "content": "x", "_db_persisted": True})
    notify_input_admitted(agent, {"role": "user", "content": "x", "_db_persisted": True})
    assert fired == [1]


def test_turn_runner_retires_consumed_checkpoint_at_turn_admission(home, monkeypatch):
    """End-to-end through the gateway's turn worker: run_conversation's turn-start flush
    fires the callback the runner installed for the consumed queued event."""
    from gateway.run_turn_runner import TurnRunner
    from gateway.turn_context import TurnContext
    from agent.compression_recovery import notify_input_admitted

    event = _event("queued follow-up", "tg:77")
    assert shutdown_flush.checkpoint_queued_event("k", event)
    spool = event._hermes_recovery_spool

    class _Agent:
        _input_admitted_callback = None

        def run_conversation(self, message, **kwargs):
            assert spool.exists()  # not retired before admission
            notify_input_admitted(self, {"role": "user", "content": message, "_db_persisted": True})
            assert not spool.exists()
            return {"final_response": "ok", "messages": []}

    ctx = TurnContext(source=_make_source(), session_key="k", session_id="sid", message="queued follow-up",
                      queued_event=event)
    runner = SimpleNamespace(_consume_pending_native_image_paths=lambda key: [],
                             _delivery_adapter_for=lambda source: None)
    turn = TurnRunner(runner, ctx)
    agent = _Agent()
    turn._run_conversation_with_approval(agent, [], None, None, None)
    assert not spool.exists()
    assert agent._input_admitted_callback is None


def test_failed_admission_keeps_the_checkpoint_and_drops_the_callback(home):
    from gateway.run_turn_runner import TurnRunner
    from gateway.turn_context import TurnContext

    event = _event("queued follow-up", "tg:78")
    assert shutdown_flush.checkpoint_queued_event("k", event)

    class _Agent:
        _input_admitted_callback = None

        def run_conversation(self, message, **kwargs):
            raise RuntimeError("storage down before admission")

    ctx = TurnContext(source=_make_source(), session_key="k", session_id="sid", message="m", queued_event=event)
    turn = TurnRunner(SimpleNamespace(_consume_pending_native_image_paths=lambda key: [],
                                      _delivery_adapter_for=lambda source: None), ctx)
    agent = _Agent()
    with pytest.raises(RuntimeError):
        turn._run_conversation_with_approval(agent, [], None, None, None)
    assert event._hermes_recovery_spool.exists()
    assert agent._input_admitted_callback is None  # never fires for a later turn


# ── P1 (c): discarded queued work retires its checkpoint ──

class _Adapter(BasePlatformAdapter):
    def __init__(self):
        super().__init__(PlatformConfig(enabled=True, token="t"), Platform.TELEGRAM)

    async def connect(self, *, is_reconnect=False):
        return True

    async def disconnect(self):
        pass

    async def get_chat_info(self, chat_id):
        return {}

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        return None


@pytest.mark.asyncio
async def test_cancel_and_stale_lock_heal_discard_retires_checkpoint(home):
    adapter = _Adapter()
    for key, discard in (("k1", "cancel"), ("k2", "heal")):
        event = _event(f"{discard} me", f"id-{key}")
        assert shutdown_flush.checkpoint_queued_event(key, event)
        adapter._pending_messages[key] = event
        if discard == "cancel":
            await adapter.cancel_session_processing(key)
        else:
            done = SimpleNamespace(done=lambda: True)
            adapter._active_sessions[key] = object()
            adapter._session_tasks[key] = done
            assert adapter._heal_stale_session_lock(key)
        assert key not in adapter._pending_messages
    assert _spools(home) == []


def test_conversation_boundary_retires_overflow_checkpoints(home):
    runner, adapter = _make_runner(_session_entry())
    runner._session_model_override_epochs = {}
    key = build_session_key(_make_source())
    head, tail = _event("head", "q1"), _event("tail", "q2")
    assert runner._enqueue_fifo(key, head, adapter)
    assert runner._enqueue_fifo(key, tail, adapter)
    assert len(_spools(home)) == 2
    runner._clear_conversation_scope(key, reason="session_reset")
    # The overflow tail died with the conversation; the head is the adapter's (cancel path).
    remaining = [json.loads(p.read_text())["data"]["text"] for p in _spools(home)]
    assert remaining == ["head"]


def test_discarded_pending_slash_command_retires_checkpoint(home):
    from gateway.shutdown_flush import retire_queue_checkpoint
    event = _event("/new", "q9")
    assert shutdown_flush.checkpoint_queued_event("k", event)
    assert retire_queue_checkpoint(event, reason="discarded_pending_command")
    assert _spools(home) == [] and event._hermes_recovery_spool is None


# ── bounded expiry ──

def test_queue_checkpoint_has_a_bounded_expiry(home):
    event = _event("ancient", "old1")
    assert shutdown_flush.checkpoint_queued_event("k", event)
    old = time.time() - shutdown_flush.QUEUE_CHECKPOINT_MAX_AGE_SECONDS - 60
    os.utime(event._hermes_recovery_spool, (old, old))
    db = SessionDB(db_path=home / "state.db")
    db.create_session("successor", "telegram")
    assert _boot(db, home) == []
    assert _spools(home) == []
    db.close()


# ── P2: busy photo merge with a live raw_message ──

def test_busy_photo_merge_survives_uncopyable_raw_message(home):
    runner, adapter = _make_runner(_session_entry())
    key = build_session_key(_make_source())
    head = MessageEvent(text="first photo", source=_make_source(), message_id="p1",
                        message_type=MessageType.PHOTO, media_urls=["/tmp/a.jpg"], media_types=["image/jpeg"],
                        raw_message=SimpleNamespace(lock=threading.Lock()))
    follow = MessageEvent(text="second photo", source=_make_source(), message_id="p2",
                          message_type=MessageType.PHOTO, media_urls=["/tmp/b.jpg"], media_types=["image/jpeg"],
                          raw_message=SimpleNamespace(lock=threading.Lock()))
    assert runner._enqueue_fifo(key, head, adapter)
    assert runner._queue_or_replace_pending_event(key, follow) is True  # busy photo follow-up
    merged = adapter._pending_messages[key]
    assert merged.media_urls == ["/tmp/a.jpg", "/tmp/b.jpg"]
    assert head.media_urls == ["/tmp/a.jpg"]  # a failed checkpoint must leave the live head intact
    assert merged.raw_message is head.raw_message
    # One checkpoint: the merged head carries both photos; the follow-up's own copy retired.
    files = _spools(home)
    assert len(files) == 1
    assert "second photo" in json.loads(files[0].read_text())["data"]["text"]


# ── P2: unauthorized accepted spool is quarantined, not retained forever ──

def test_unauthorized_accepted_spool_is_quarantined(home):
    from gateway import run_pending_recovery
    from gateway.session import SessionSource

    source = SessionSource(platform=Platform.TELEGRAM, chat_id="chat", chat_type="dm", user_id="u")
    entry = SimpleNamespace(origin=SimpleNamespace(platform=Platform.TELEGRAM), resume_pending=False,
                            session_id="sid", suspended=False)

    class Store:
        _lock = threading.Lock()
        _entries = {"k": entry}

        def _ensure_loaded_locked(self):
            return None

    class Runner:
        session_store = Store()
        _startup_restore_queue = []
        _draining = False

        def _is_session_running(self, _key): return False
        def _restored_source(self, _entry): return source
        def _resume_owner_authorized(self, _key, _source): return False

    event = _event("not yours", "u1")
    assert shutdown_flush.checkpoint_queued_event("k", event)
    path = event._hermes_recovery_spool
    data = json.loads(path.read_text())["data"]
    result = run_pending_recovery._defer_followup(Runner(), {}, None, "k", "sid", data, path, recovered_events=[])
    assert result is False
    assert not path.exists()
    assert path.with_name(path.name + shutdown_flush.QUARANTINE_SUFFIX).exists()
    assert _spools(home) == []


def test_declined_accepted_checkpoint_is_quarantined_without_transcript_row(home):
    """Recovery must stop at a declined claim: no naked user row for rejected input."""
    from functools import partial

    from gateway import run_pending_recovery
    from gateway.session import SessionSource

    source = SessionSource(platform=Platform.TELEGRAM, chat_id="chat", chat_type="dm", user_id="u")
    entry = SimpleNamespace(origin=SimpleNamespace(platform=Platform.TELEGRAM), resume_pending=False,
                            session_id="sid", suspended=False)

    class Store:
        _lock = threading.Lock()
        _entries = {"k": entry}

        def _ensure_loaded_locked(self):
            return None

    class Runner:
        session_store = Store()
        _startup_restore_queue = []
        _draining = False

        def _is_session_running(self, _key): return False
        def _restored_source(self, _entry): return source
        def _resume_owner_authorized(self, _key, _source): return False

    db = SessionDB(db_path=home / "state.db")
    db.create_session("sid", "telegram")
    event = _event("accepted queued input", "u1")
    assert shutdown_flush.checkpoint_queued_event("k", event)
    path = event._hermes_recovery_spool
    recovered, _held = shutdown_flush.recover_pending_spool(
        session_db=db, session_resolver=lambda *a, **k: ("sid", db),
        deferred_followup=partial(run_pending_recovery._defer_followup, Runner(), {}, None,
                                  recovered_events=[]))
    assert recovered == 0
    assert not path.exists()
    assert path.with_name(path.name + shutdown_flush.QUARANTINE_SUFFIX).exists()
    assert [m for m in db.get_messages("sid") if m.get("role") == "user"] == []
    db.close()
