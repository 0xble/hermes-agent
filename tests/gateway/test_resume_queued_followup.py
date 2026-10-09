"""A shutdown-spooled follow-up must not become the interrupted turn's prompt."""

import asyncio
import json
import threading
from dataclasses import replace
from datetime import datetime, timedelta
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
    assert len(list((tmp_path / "pending_messages").glob("*.json"))) == 1

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


@pytest.mark.asyncio
async def test_reconnect_during_drain_retains_arrival_for_boot(tmp_path, monkeypatch):
    runner, adapter, source, key, db = _spooled_runner(tmp_path, monkeypatch, pending=False)
    runner._startup_restore_in_progress = False
    runner._draining = True
    event = MessageEvent(text="after drain", source=source, user_id="u1")
    event._drain_deferred = True
    assert flush_pending_to_file({key: event}, reason="drain_arrival") == 1
    spool, = (tmp_path / "pending_messages").glob("*.json")
    await runner._recover_spool_after_reconnect(source.platform)
    assert spool.exists()
    assert runner._startup_restore_queue == []
    db.append_message.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["offline", "raise", "accepted"])
async def test_claimed_spool_survives_until_replay_acceptance(tmp_path, monkeypatch, outcome):
    runner, adapter, source, key, db = _spooled_runner(tmp_path, monkeypatch)
    assert flush_pending_to_file({key: MessageEvent(text="queued", source=source, user_id="u1")}) == 1
    path, = (tmp_path / "pending_messages").glob("*.json")
    assert recover_pending_shutdown_flush(runner) == 1
    assert path.exists()
    # A second recovery in this process must not claim the same durable event twice.
    assert recover_pending_shutdown_flush(runner) == 0
    assert len(runner._startup_restore_queue) == 1
    runner._startup_restore_in_progress = False
    if outcome == "offline":
        runner.adapters.clear()
    elif outcome == "raise":
        adapter.handle_message = AsyncMock(side_effect=RuntimeError("dispatch failed"))
    else:
        adapter.handle_message = AsyncMock(side_effect=lambda event: setattr(event, "_gateway_accepted", True))
    assert await runner._drain_startup_restore_queue() == (1 if outcome == "accepted" else 0)
    assert path.exists() == (outcome != "accepted")
    if outcome != "accepted":
        # A new process recovers the original spool rather than losing the message.
        fresh, _, _, fresh_key, _ = _spooled_runner(tmp_path, monkeypatch)
        assert recover_pending_shutdown_flush(fresh, candidates=[fresh.session_store._entries[fresh_key]]) == 1
        assert [event.text for event in fresh._startup_restore_queue] == ["queued"]
    db.append_message.assert_not_called()


@pytest.mark.asyncio
async def test_shutdown_preserves_unclaimed_restore_queue_and_reuses_claimed_spool(tmp_path, monkeypatch):
    runner, _, source, key, _ = _spooled_runner(tmp_path, monkeypatch)
    runner._background_tasks = set()
    runner._stop_task = runner._restart_task = None
    runner._pending_messages = {}
    runner._queued_events = {}
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._pending_approvals = {}
    runner._shutdown_event = asyncio.Event()
    runner._active_api_run_count = MagicMock(return_value=0)
    runner._stop_kill_tool_subprocesses_off_loop = AsyncMock()
    claimed = MessageEvent(text="claimed", source=source, user_id="u1")
    fresh = MessageEvent(text="fresh", source=source, user_id="u1")
    assert flush_pending_to_file({key: claimed}) == 1
    path, = (tmp_path / "pending_messages").glob("*.json")
    setattr(claimed, "_hermes_recovery_spool", path)
    runner._startup_restore_queue = [claimed, fresh]
    ctx = MagicMock()
    ctx.elapsed.return_value = 0
    await runner._stop_release_runtime_state(ctx)
    payloads = [json.loads(p.read_text(encoding="utf-8"))["data"]["text"]
                for p in (tmp_path / "pending_messages").glob("*.json")]
    assert sorted(payloads) == ["claimed", "fresh"]


def test_breaker_keeps_pending_followup_spooled(tmp_path, monkeypatch):
    runner, _, source, key, db = _spooled_runner(tmp_path, monkeypatch)
    assert flush_pending_to_file({key: MessageEvent(text="queued", source=source, user_id="u1")}) == 1
    assert recover_pending_shutdown_flush(runner, candidates=None) == 0
    assert list((tmp_path / "pending_messages").glob("*.json"))
    db.append_message.assert_not_called()


@pytest.mark.parametrize("reason", ["stale", "unauthorized", "running"])
def test_ineligible_resume_appends_followup(tmp_path, monkeypatch, reason):
    runner, _, source, key, db = _spooled_runner(tmp_path, monkeypatch)
    entry = runner.session_store._entries[key]
    if reason == "stale":
        entry.last_resume_marked_at = datetime.now() - timedelta(hours=3)
    elif reason == "unauthorized":
        runner._is_user_authorized_for_source = MagicMock(return_value=False)
    else:
        runner._is_session_running = MagicMock(return_value=True)
    assert flush_pending_to_file({key: MessageEvent(text="queued", source=source, user_id="u1")}) == 1
    path, = (tmp_path / "pending_messages").glob("*.json")
    assert recover_pending_shutdown_flush(runner) == 1
    assert not path.exists()
    assert runner._startup_restore_queue == []
    db.append_message.assert_called_once()
    assert db.append_message.call_args.kwargs["content"] == "queued"


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
        origin=origin, resume_pending=True, resume_reason="restart_interrupted",
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
async def test_offline_followup_retried_on_primary_reconnect(tmp_path, monkeypatch, caplog):
    runner, adapter, source, key, db = _spooled_runner(tmp_path, monkeypatch)
    assert flush_pending_to_file({key: MessageEvent(text="later", source=source, user_id="u1")}) == 1
    runner.adapters.clear()
    assert recover_pending_shutdown_flush(runner) == 0
    assert "delivery adapter offline" in caplog.text
    assert list((tmp_path / "pending_messages").glob("*.json"))
    runner._startup_restore_in_progress = False
    runner._failed_platforms = {source.platform: {}}
    runner._publish_primary_adapter = lambda platform, adapter: runner.adapters.__setitem__(platform, adapter)
    runner._update_platform_runtime_status = MagicMock()
    runner._schedule_planned_restart_replay = MagicMock()
    runner._redeliver_failed_obligations_for_platform = AsyncMock()
    runner._schedule_resume_pending_sessions = MagicMock(return_value=0)
    runner._await_startup_warmup = AsyncMock()
    adapter.handle_message = AsyncMock(side_effect=lambda event: setattr(event, "_gateway_accepted", True))
    await runner._install_reconnected_adapter(source.platform, adapter)
    await runner._reconnect_spool_tasks[source.platform]
    assert not list((tmp_path / "pending_messages").glob("*.json"))
    runner._schedule_resume_pending_sessions.assert_called_once()
    adapter.handle_message.assert_awaited_once()
    assert adapter.handle_message.call_args.args[0].text == "later"
    db.append_message.assert_not_called()


def test_direct_session_id_spool_skips_resolver_and_queues(tmp_path, monkeypatch):
    runner, _, source, key, _ = _spooled_runner(tmp_path, monkeypatch)
    path = tmp_path / "pending_messages" / "direct.json"
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps({"session_key": key, "ts": int(datetime.now().timestamp()),
                                "data": {"text": "separate", "session_id": "sid",
                                         "source_user_id": "u1"}}))
    runner.session_store.resolve_session_id_for_key.side_effect = AssertionError("resolver must be skipped")
    assert recover_pending_shutdown_flush(runner) == 1
    assert [event.text for event in runner._startup_restore_queue] == ["separate"]
    assert path.exists()


def test_non_auto_resume_reason_appends_to_transcript(tmp_path, monkeypatch):
    runner, _, source, key, db = _spooled_runner(tmp_path, monkeypatch)
    runner.session_store._entries[key].resume_reason = "manual_pause"
    assert flush_pending_to_file({key: MessageEvent(text="ordinary", source=source, user_id="u1")}) == 1
    assert recover_pending_shutdown_flush(runner) == 1
    assert not runner._startup_restore_queue
    db.append_message.assert_called_once()
    assert db.append_message.call_args.kwargs["content"] == "ordinary"


@pytest.mark.asyncio
async def test_reconnect_gate_uses_normalized_session_identity(tmp_path, monkeypatch):
    runner, _, source, _, _ = _spooled_runner(tmp_path, monkeypatch)
    runner._startup_restore_in_progress = False
    normalized = replace(source, thread_id="topic-recovered")
    key = runner._session_key_for_source(normalized)
    assert key != runner._session_key_for_source(source)
    runner._normalize_source_for_session_key = MagicMock(return_value=normalized)
    runner._reconnect_restore_keys = {key: 1}
    event = MessageEvent(text="held", source=source, user_id="u1")
    assert await runner._hm_admit_event(event) is None
    assert runner._startup_restore_queue == [event]
    runner._normalize_source_for_session_key.assert_called_once_with(source)


@pytest.mark.asyncio
async def test_reconnect_without_work_does_not_gate_other_chat(tmp_path, monkeypatch):
    runner, _, source, _, _ = _spooled_runner(tmp_path, monkeypatch, pending=False)
    runner._startup_restore_in_progress = False
    await runner._recover_spool_after_reconnect(source.platform)
    assert not runner._startup_restore_in_progress
    assert not getattr(runner, "_reconnect_restore_keys", {})
    assert not runner._startup_restore_queue


@pytest.mark.asyncio
async def test_overlapping_reconnects_hold_only_owned_sessions(tmp_path, monkeypatch):
    runner, adapter, source, key, _ = _spooled_runner(tmp_path, monkeypatch)
    runner._startup_restore_in_progress = False
    runner._await_startup_warmup = AsyncMock()
    first_started, second_started = asyncio.Event(), asyncio.Event()
    first_release, second_release = asyncio.Event(), asyncio.Event()
    seen = []
    async def handle(event):
        if event.internal:
            if event.source.chat_id == source.chat_id:
                first_started.set()
                await first_release.wait()
                seen.append("first")
            else:
                second_started.set()
                await second_release.wait()
                seen.append("second")
        else:
            seen.append(event.text)
    adapter.handle_message = handle
    first = asyncio.create_task(runner._recover_spool_after_reconnect(source.platform))
    await asyncio.wait_for(first_started.wait(), 5)
    other = replace(source, chat_id="other")
    other_key = runner._session_key_for_source(other)
    runner.session_store._entries[other_key] = SessionEntry(
        session_key=other_key, session_id="other-sid", created_at=datetime.now(), updated_at=datetime.now(),
        origin=other, resume_pending=True, resume_reason="restart_interrupted",
        last_resume_marked_at=datetime.now(),
    )
    second = asyncio.create_task(runner._recover_spool_after_reconnect(source.platform))
    await asyncio.wait_for(second_started.wait(), 5)
    assert runner._reconnect_restore_keys.get(key)
    assert runner._reconnect_restore_keys.get(other_key)
    runner._scale_to_zero_note_real_inbound = MagicMock()
    runner._hm_pre_gateway_dispatch_hook = AsyncMock(side_effect=lambda event, source: event)
    runner._is_user_authorized_for_source = MagicMock(return_value=True)
    runner._admit_bot_message_for_source = MagicMock(return_value=True)
    unrelated = replace(source, chat_id="unrelated")
    free_event = MessageEvent(text="free", source=unrelated)
    free_admission = await runner._hm_admit_event(free_event)
    assert free_admission is not None and free_admission[0] is free_event
    held_event = MessageEvent(text="held", source=other)
    assert await runner._hm_admit_event(held_event) is None
    assert runner._startup_restore_queue[-1] is held_event
    first_release.set()
    await first
    assert runner._reconnect_restore_keys.get(other_key)
    assert not runner._startup_restore_in_progress
    second_release.set()
    await second
    assert not runner._reconnect_restore_keys
    assert seen == ["first", "second", "held"]


@pytest.mark.asyncio
async def test_boot_drain_logs_undrained_reconnect_owned_count(tmp_path, monkeypatch, caplog):
    runner, _, source, key, _ = _spooled_runner(tmp_path, monkeypatch)
    runner._startup_restore_queue = [MessageEvent(text="held", source=source)]
    runner._reconnect_restore_keys = {key: 1}
    with caplog.at_level("WARNING", logger="gateway.run"):
        assert await runner._drain_startup_restore_queue() == 0
    assert "left 1 queued message(s)" in caplog.text
    assert len(runner._startup_restore_queue) == 1


@pytest.mark.asyncio
async def test_overlapping_off_loop_recovery_claims_spool_once(tmp_path, monkeypatch):
    runner, _adapter, source, key, _db = _spooled_runner(tmp_path, monkeypatch)
    assert flush_pending_to_file({key: MessageEvent(text="queued", source=source, user_id="u1")}) == 1
    runner._schedule_resume_pending_sessions = MagicMock(return_value=0)
    candidates = runner._resume_pending_candidates()
    claimed = threading.Event()
    release = threading.Event()
    claim_count = 0
    original_defer = __import__("gateway.run_pending_recovery", fromlist=["_defer_followup"])._defer_followup

    def counted_defer(*args, **kwargs):
        nonlocal claim_count
        result = original_defer(*args, **kwargs)
        if result is True:
            claim_count += 1
            claimed.set()
            release.wait(timeout=2)
        return result

    monkeypatch.setattr("gateway.run_pending_recovery._defer_followup", counted_defer)
    boot = asyncio.create_task(runner._recover_pending_shutdown_flush_off_loop(
        candidates=candidates, failure_message="boot recovery failed"))
    await asyncio.to_thread(claimed.wait)
    reconnect = asyncio.create_task(runner._recover_spool_after_reconnect(source.platform))
    await asyncio.sleep(0)
    release.set()
    await asyncio.gather(boot, reconnect)

    assert claim_count == 1
    assert len(runner._startup_restore_queue) == 1


@pytest.mark.asyncio
async def test_resolver_only_recovery_does_not_open_default_state_db(tmp_path, monkeypatch):
    runner, _adapter, source, key, db = _spooled_runner(tmp_path, monkeypatch)
    assert flush_pending_to_file({key: MessageEvent(text="queued", source=source, user_id="u1")}) == 1
    monkeypatch.setattr(
        "hermes_state_registry.acquire",
        MagicMock(side_effect=AssertionError("resolver-only recovery must not open state.db")),
    )

    assert recover_pending_shutdown_flush(runner) == 1
    assert runner._startup_restore_queue
    db.append_message.assert_not_called()


@pytest.mark.asyncio
async def test_malformed_data_does_not_abort_profile_recovery(tmp_path, monkeypatch):
    runner, _adapter, source, key, _db = _spooled_runner(tmp_path, monkeypatch)
    bad = tmp_path / "pending_messages" / "bad.json"
    bad.parent.mkdir(exist_ok=True)
    bad.write_text(json.dumps({"session_key": key, "ts": 1, "data": ["not-a-mapping"]}))
    assert flush_pending_to_file({key: MessageEvent(text="healthy", source=source, user_id="u1")}) == 1

    assert recover_pending_shutdown_flush(runner) == 1
    assert bad.exists()
    assert [event.text for event in runner._startup_restore_queue] == ["healthy"]


@pytest.mark.asyncio
async def test_reconnect_during_boot_drain_replays_owned_followup_first(tmp_path, monkeypatch, caplog):
    runner, adapter, source, key, db = _spooled_runner(tmp_path, monkeypatch)
    runner._schedule_resume_pending_sessions = MagicMock(return_value=0)
    runner._await_startup_warmup = AsyncMock()
    assert flush_pending_to_file({key: MessageEvent(text="older", source=source, user_id="u1")}) == 1
    seen = []
    started, release = asyncio.Event(), asyncio.Event()

    async def handle(event):
        if event.text == "older":
            started.set()
            await release.wait()
        seen.append(event.text)
        event._gateway_accepted = True

    adapter.handle_message = handle
    reconnect = asyncio.create_task(runner._recover_spool_after_reconnect(source.platform))
    await asyncio.wait_for(started.wait(), 5)
    assert await runner._drain_startup_restore_queue() == 0
    assert "left 1 queued message(s)" not in caplog.text  # already claimed and dispatching
    release.set()
    await reconnect
    # Boot's drain cannot take a key held by the reconnect. The reconnect must
    # have dispatched it before releasing its ownership, even while boot is gated.
    assert seen == ["older"]
    assert not list((tmp_path / "pending_messages").glob("*.json"))
    await runner._finish_startup_restore()
    assert seen == ["older"]
    assert not runner._startup_restore_queue
    db.append_message.assert_not_called()


@pytest.mark.asyncio
async def test_reconnect_live_inbound_waits_until_older_followup_finishes(tmp_path, monkeypatch):
    runner, adapter, source, key, _ = _spooled_runner(tmp_path, monkeypatch)
    runner._startup_restore_in_progress = False
    runner._schedule_resume_pending_sessions = MagicMock(return_value=0)
    runner._await_startup_warmup = AsyncMock()
    assert flush_pending_to_file({key: MessageEvent(text="older", source=source, user_id="u1")}) == 1
    older_started = asyncio.Event()
    release_older = asyncio.Event()
    seen = []

    async def handle(event):
        if event.text == "older":
            older_started.set()
            await release_older.wait()
        seen.append(event.text)

    adapter.handle_message = handle
    runner._scale_to_zero_note_real_inbound = MagicMock()
    runner._hm_pre_gateway_dispatch_hook = AsyncMock(side_effect=lambda event, source: event)
    runner._is_user_authorized_for_source = MagicMock(return_value=True)
    runner._admit_bot_message_for_source = MagicMock(return_value=True)
    recovery = asyncio.create_task(runner._recover_spool_after_reconnect(source.platform))
    await asyncio.wait_for(older_started.wait(), 5)
    live = MessageEvent(text="live", source=source, user_id="u1")
    assert await runner._hm_admit_event(live) is None
    assert seen == []
    release_older.set()
    await asyncio.wait_for(recovery, 5)
    assert seen == ["older", "live"]
    assert not runner._reconnect_restore_keys


def test_boot_snapshot_records_once_across_recovery_and_schedule(tmp_path, monkeypatch):
    runner, _, _, _, _ = _spooled_runner(tmp_path, monkeypatch)
    recorded = MagicMock(return_value=False)
    monkeypatch.setattr("gateway.restart_loop_guard.check_and_record", recorded)
    candidates = runner._resume_pending_candidates()
    assert candidates is not None and len(candidates) == 1
    assert recover_pending_shutdown_flush(runner, candidates=candidates) == 0
    runner._auto_resume_ready = MagicMock(return_value=None)
    assert runner._schedule_resume_pending_sessions(candidates=candidates) == 0
    recorded.assert_called_once()


def test_failed_boot_snapshot_is_not_reenumerated_by_recovery_or_scheduler(tmp_path, monkeypatch):
    runner, _, _, _, _ = _spooled_runner(tmp_path, monkeypatch)
    runner._resume_pending_candidates = MagicMock(return_value=None)
    assert recover_pending_shutdown_flush(runner, candidates=None) == 0
    assert runner._schedule_resume_pending_sessions(candidates=None) == 0
    runner._resume_pending_candidates.assert_not_called()


@pytest.mark.asyncio
async def test_reconnects_do_not_spend_boot_breaker_budget(tmp_path, monkeypatch):
    runner, _, source, key, _ = _spooled_runner(tmp_path, monkeypatch)
    runner._startup_restore_in_progress = False
    runner.adapters.clear()  # an offline stale session persists across multiple other reconnects
    runner.session_store._entries[key].origin = replace(source, platform=Platform.DISCORD)
    recorded = MagicMock(return_value=False)
    monkeypatch.setattr("gateway.restart_loop_guard.check_and_record", recorded)
    for _ in range(3):
        await runner._recover_spool_after_reconnect(Platform.TELEGRAM)
    recorded.assert_not_called()
    assert runner.session_store._entries[key].resume_pending


@pytest.mark.asyncio
async def test_recovery_failure_still_schedules_reconnect_resume(tmp_path, monkeypatch):
    runner, _, source, _, _ = _spooled_runner(tmp_path, monkeypatch)
    runner._startup_restore_in_progress = False
    runner._schedule_resume_pending_sessions = MagicMock(return_value=0)
    monkeypatch.setattr("gateway.run_pending_recovery.recover_pending_shutdown_flush",
                        MagicMock(side_effect=OSError("spool unavailable")))
    await runner._recover_spool_after_reconnect(source.platform)
    runner._schedule_resume_pending_sessions.assert_called_once()


def test_unrelated_reconnect_retention_does_not_warn(tmp_path, monkeypatch, caplog):
    runner, _, source, key, _ = _spooled_runner(tmp_path, monkeypatch)
    other = replace(source, platform=Platform.DISCORD)
    runner.session_store._entries[key].origin = other
    assert flush_pending_to_file({key: MessageEvent(text="later", source=other, user_id="u1")}) == 1
    with caplog.at_level("WARNING", logger="gateway.run"):
        assert recover_pending_shutdown_flush(runner, platform=Platform.TELEGRAM) == 0
    assert list((tmp_path / "pending_messages").glob("*.json"))
    assert "delivery adapter offline or" not in caplog.text


@pytest.mark.asyncio
async def test_inbound_without_restore_gate_does_not_derive_restore_key(tmp_path, monkeypatch):
    runner, _, source, _, _ = _spooled_runner(tmp_path, monkeypatch)
    runner._startup_restore_in_progress = False
    runner._reconnect_restore_keys = {}
    runner._session_key_for_source = MagicMock(side_effect=AssertionError("no restore gate"))
    runner._scale_to_zero_note_real_inbound = MagicMock()
    runner._hm_pre_gateway_dispatch_hook = AsyncMock(side_effect=lambda event, source: event)
    runner._is_user_authorized_for_source = MagicMock(return_value=True)
    runner._admit_bot_message_for_source = MagicMock(return_value=True)
    event = MessageEvent(text="live", source=source, user_id="u1")
    admitted = await runner._hm_admit_event(event)
    assert admitted is not None and admitted[0] is event
    runner._session_key_for_source.assert_not_called()


@pytest.mark.asyncio
async def test_reconnect_keeps_other_platforms_followup_until_its_resume(tmp_path, monkeypatch):
    runner, adapter, source, key, _ = _spooled_runner(tmp_path, monkeypatch, pending=False)
    runner._startup_restore_in_progress = False
    other = replace(source, platform=Platform.DISCORD, chat_id="discord-room")
    other_key = runner._session_key_for_source(other)
    runner.session_store._entries[other_key] = SessionEntry(
        session_key=other_key, session_id="other-sid", created_at=datetime.now(), updated_at=datetime.now(),
        origin=other, resume_pending=True, resume_reason="restart_interrupted",
        last_resume_marked_at=datetime.now(),
    )
    runner.session_store.resolve_session_id_for_key = MagicMock(return_value=("other-sid", MagicMock()))
    runner.adapters[Platform.DISCORD] = adapter
    assert flush_pending_to_file({other_key: MessageEvent(text="discord followup", source=other, user_id="u1")}) == 1
    await runner._recover_spool_after_reconnect(Platform.TELEGRAM)
    assert list((tmp_path / "pending_messages").glob("*.json"))
    assert not runner._startup_restore_queue


@pytest.mark.parametrize("route", ["replay", "append"])
@pytest.mark.parametrize("kind", ["internal", "reply_not_expected", "human"])
def test_shutdown_spool_keeps_machinery_silence_contract(tmp_path, monkeypatch, route, kind):
    """A process-completion notice spooled at shutdown must come back as machinery.

    Losing ``internal``/``reply_expected`` made the recovered notice a human turn, so the
    agent's correct NO_REPLY drew the "No reply was written" fallback after every restart.
    """
    from gateway.response_filters import (
        INTERNAL_NOTIFICATION_DISPLAY_KIND, display_kind_for_event, silence_allowed,
    )
    runner, _, source, key, db = _spooled_runner(tmp_path, monkeypatch, pending=route == "replay")
    event = MessageEvent(
        text="[INTERNAL NOTIFICATION] proc exited" if kind == "internal" else "typed by a person",
        source=source, user_id="u1", internal=kind == "internal",
        reply_expected=False if kind == "reply_not_expected" else None,
        metadata={"notification_origin": "process_registry_synthetic"} if kind == "internal" else {},
    )
    assert flush_pending_to_file({key: event}, reason="shutdown") == 1
    assert recover_pending_shutdown_flush(runner) == 1
    expect_silent = kind != "human"
    if route == "replay":
        recovered, = runner._startup_restore_queue
        assert recovered.internal is (kind == "internal")
        assert recovered.reply_expected is (False if kind == "reply_not_expected" else None)
        if kind == "internal":
            assert recovered.metadata["notification_origin"] == "process_registry_synthetic"
        assert silence_allowed(display_kind_for_event(recovered), recovered.reply_expected) is expect_silent
        db.append_message.assert_not_called()
    else:
        assert runner._startup_restore_queue == []
        row = db.append_message.call_args.kwargs
        assert row["content"] == event.text
        display_kind = row.get("display_kind")
        assert display_kind == (INTERNAL_NOTIFICATION_DISPLAY_KIND if kind == "internal" else None)
        reply_expected = (row.get("display_metadata") or {}).get("reply_expected")
        assert silence_allowed(display_kind, reply_expected) is expect_silent
