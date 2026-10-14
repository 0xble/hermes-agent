"""A shutdown follow-up is its own future turn, not the predecessor's resume marker."""

import asyncio
import json
from functools import partial
from unittest.mock import AsyncMock, Mock

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.platforms.base import merge_pending_message_event
from gateway.platforms.event import MessageEvent, MessageType
from gateway.run_pending_recovery import _defer_followup
from gateway.session import SessionStore
from gateway.shutdown_flush import flush_pending_to_file, recover_pending_to_db
from tests.gateway.restart_test_helpers import make_restart_runner, make_restart_source


def _setup(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_MANAGED_DIR", str(tmp_path / "managed"))
    runner, adapter = make_restart_runner()
    source = make_restart_source()
    store = SessionStore(tmp_path / "sessions", GatewayConfig())
    entry = store.get_or_create_session(source)
    runner.session_store = store
    runner._startup_restore_queue = []
    runner._is_user_authorized_for_source = lambda source: True
    runner._restored_source = lambda entry: entry.origin
    runner._intake_adapter_for = lambda source: adapter
    runner._delivery_adapter_for = lambda source: adapter
    runner._thread_metadata_for_source = lambda source: {}
    return runner, adapter, entry, source


@pytest.mark.asyncio
@pytest.mark.parametrize("marker_state", ["missing", "cleared", "pending"])
async def test_shutdown_followup_starts_own_turn_without_predecessor_resume_marker(tmp_path, monkeypatch, marker_state, caplog):
    runner, adapter, entry, source = _setup(tmp_path, monkeypatch)
    if marker_state != "missing":
        runner.session_store.mark_resume_pending(entry.session_key, turn_id="answered-predecessor")
    if marker_state == "cleared":
        runner.session_store.clear_resume_pending(entry.session_key)
    event = MessageEvent(text="the queued question", source=source, user_id=source.user_id)
    assert flush_pending_to_file({entry.session_key: event}, reason="shutdown_follow_up") == 1
    db = Mock()
    assert recover_pending_to_db(
        db, session_resolver=lambda *args, **kwargs: (entry.session_id, db),
        deferred_followup=partial(_defer_followup, runner, {}, None),
    ) == 1
    db.append_message.assert_not_called()
    assert [event.text for event in runner._startup_restore_queue] == ["the queued question"]
    called = []

    async def handle(event):
        called.append(event.text)
        event._agent_turn_succeeded = True

    adapter.set_message_handler(handle)
    with caplog.at_level("INFO", logger="gateway.run"):
        assert runner._schedule_resume_pending_sessions(candidates=[entry]) == 0
        assert await runner._drain_startup_restore_queue() == 1
        await asyncio.gather(*list(adapter._background_tasks))
        await asyncio.gather(*list(runner._background_tasks))
    assert called == ["the queued question"]
    assert not list((tmp_path / "pending_messages").glob("*.json"))
    outcomes = [r.getMessage() for r in caplog.records if r.getMessage().startswith("Restart recovery outcome:")]
    assert outcomes == [f"Restart recovery outcome: session={entry.session_key} outcome=resumed"]


@pytest.mark.asyncio
async def test_restored_followup_accepted_without_turn_gets_note_and_skipped_outcome(tmp_path, monkeypatch, caplog):
    runner, adapter, entry, source = _setup(tmp_path, monkeypatch)
    event = MessageEvent(text="saved question", source=source, user_id=source.user_id)
    assert flush_pending_to_file({entry.session_key: event}, reason="shutdown_follow_up") == 1
    db = Mock()
    recover_pending_to_db(db, session_resolver=lambda *args, **kwargs: (entry.session_id, db),
                          deferred_followup=partial(_defer_followup, runner, {}, None))

    async def accept_but_drop(event):
        event._gateway_accepted = True

    adapter.handle_message = accept_but_drop
    with caplog.at_level("INFO", logger="gateway.run"):
        await runner._drain_startup_restore_queue()
        await asyncio.gather(*list(runner._background_tasks))
    assert len(adapter.sent) == 1
    assert "follow-up" in adapter.sent[0]
    outcomes = [r.getMessage() for r in caplog.records if r.getMessage().startswith("Restart recovery outcome:")]
    assert outcomes == [f"Restart recovery outcome: session={entry.session_key} outcome=skipped:accepted-without-turn:noted"]
    assert entry.session_key not in runner._restart_followup_keys
    assert list((tmp_path / "pending_messages").glob("*.json"))


@pytest.mark.asyncio
async def test_restored_followup_failed_real_turn_gets_no_preserved_notice(tmp_path, monkeypatch, caplog):
    runner, adapter, entry, source = _setup(tmp_path, monkeypatch)
    event = MessageEvent(text="saved question", source=source, user_id=source.user_id)
    assert flush_pending_to_file({entry.session_key: event}, reason="shutdown_follow_up") == 1
    db = Mock()
    recover_pending_to_db(db, session_resolver=lambda *args, **kwargs: (entry.session_id, db),
                          deferred_followup=partial(_defer_followup, runner, {}, None))

    async def turn(recovered):
        recovered._agent_turn_succeeded = False

    async def handle(recovered):
        recovered._gateway_accepted = True
        adapter._session_tasks[entry.session_key] = asyncio.create_task(turn(recovered))

    adapter.handle_message = handle
    with caplog.at_level("INFO", logger="gateway.run"):
        await runner._drain_startup_restore_queue()
        await asyncio.gather(*list(runner._background_tasks))
    # The spool is retired, so a "still preserved" notice would be false; the turn reported itself.
    assert adapter.sent == []
    assert not list((tmp_path / "pending_messages").glob("*.json"))
    outcomes = [r.getMessage() for r in caplog.records if r.getMessage().startswith("Restart recovery outcome:")]
    assert outcomes == [f"Restart recovery outcome: session={entry.session_key} outcome=skipped:turn-failed"]
    assert entry.session_key not in runner._restart_followup_keys


@pytest.mark.asyncio
async def test_internal_recovered_followup_consumed_by_successor_reports_result(tmp_path, monkeypatch, caplog):
    """A pending-slot successor owns the turn result, not the parked recovery event."""
    runner, adapter, entry, source = _setup(tmp_path, monkeypatch)
    event = MessageEvent(
        text="internal completion", source=source, user_id=source.user_id,
        internal=True, message_type=MessageType.PHOTO,
    )
    assert flush_pending_to_file({entry.session_key: event}, reason="shutdown_follow_up") == 1
    recover_pending_to_db(Mock(), session_resolver=lambda *args, **kwargs: (entry.session_id, Mock()),
                          deferred_followup=partial(_defer_followup, runner, {}, None))
    recovered = runner._startup_restore_queue.pop()
    recovered._gateway_accepted = True
    consumed = MessageEvent(text="internal completion", source=source, user_id=source.user_id, internal=True,
                             message_type=MessageType.PHOTO)
    adapter._pending_messages[entry.session_key] = consumed
    merge_pending_message_event(adapter._pending_messages, entry.session_key, recovered)

    async def successor():
        runner.session_store.append_to_transcript(entry.session_id, {"role": "user", "content": consumed.text})
        consumed._agent_turn_succeeded = True

    task = asyncio.create_task(successor())
    task._hermes_event = consumed
    adapter._session_tasks[entry.session_key] = task
    with caplog.at_level("INFO", logger="gateway.run"):
        await runner._observe_recovered_followup(adapter, recovered, entry.session_key, task=task)
    assert not list((tmp_path / "pending_messages").glob("*.json"))
    outcomes = [r.getMessage() for r in caplog.records if r.getMessage().startswith("Restart recovery outcome:")]
    assert outcomes == [f"Restart recovery outcome: session={entry.session_key} outcome=resumed"]


@pytest.mark.asyncio
async def test_recovery_ignores_unrelated_inflight_result_before_merged_successor(
    tmp_path, monkeypatch, caplog
):
    """The real busy merge follows the successor, not an unrelated predecessor result."""
    runner, adapter, entry, source = _setup(tmp_path, monkeypatch)
    recovered_source_event = MessageEvent(
        text="internal completion", message_type=MessageType.PHOTO,
        source=source, user_id=source.user_id, internal=True,
    )
    assert flush_pending_to_file(
        {entry.session_key: recovered_source_event}, reason="shutdown_follow_up"
    ) == 1
    recover_pending_to_db(
        Mock(), session_resolver=lambda *args, **kwargs: (entry.session_id, Mock()),
        deferred_followup=partial(_defer_followup, runner, {}, None),
    )
    recovered = runner._startup_restore_queue.pop()
    predecessor = MessageEvent(
        text="predecessor", message_type=MessageType.PHOTO,
        source=source, user_id=source.user_id, internal=True,
    )
    successor_event = MessageEvent(
        text="successor", message_type=MessageType.PHOTO,
        source=source, user_id=source.user_id, internal=True,
    )
    predecessor_done, successor_done = asyncio.Event(), asyncio.Event()

    async def handle(event):
        if event is predecessor:
            await predecessor_done.wait()
            event._agent_turn_succeeded = False
        elif event is successor_event:
            await successor_done.wait()
            event._agent_turn_succeeded = True

    adapter.set_message_handler(handle)
    adapter._start_typing_refresh = lambda *args, **kwargs: None
    adapter._stop_typing_refresh = AsyncMock()
    adapter._run_processing_hook = AsyncMock()
    adapter._release_turn_marker = AsyncMock()
    adapter._fire_post_delivery_callback = AsyncMock()
    adapter._flush_text_debounce_now = AsyncMock()

    adapter._pending_messages[entry.session_key] = successor_event
    merge_pending_message_event(adapter._pending_messages, entry.session_key, recovered)
    assert recovered in getattr(successor_event, "_hermes_absorbed_events", [])
    predecessor_task = asyncio.create_task(
        adapter._process_message_background(predecessor, entry.session_key)
    )
    successor_task = asyncio.create_task(
        adapter._process_message_background(successor_event, entry.session_key)
    )
    adapter._session_tasks[entry.session_key] = predecessor_task
    with caplog.at_level("INFO", logger="gateway.run"):
        observer = asyncio.create_task(
            runner._observe_recovered_followup(
                adapter, recovered, entry.session_key, task=predecessor_task
            )
        )
        predecessor_done.set()
        await asyncio.sleep(0)
        adapter._session_tasks[entry.session_key] = successor_task
        successor_done.set()
        await asyncio.gather(observer, predecessor_task, successor_task)

    assert getattr(recovered, "_agent_turn_succeeded", None) is True
    outcomes = [r.getMessage() for r in caplog.records if r.getMessage().startswith("Restart recovery outcome:")]
    assert outcomes == [f"Restart recovery outcome: session={entry.session_key} outcome=resumed"]


def test_idless_recovered_followup_whose_input_is_already_durable_is_dropped(tmp_path, monkeypatch):
    """An internal follow-up has no platform id, so dedupe must use its durable user row."""
    runner, _adapter, entry, source = _setup(tmp_path, monkeypatch)
    event = MessageEvent(text="internal completion", source=source, user_id=source.user_id, internal=True)
    assert flush_pending_to_file({entry.session_key: event}, reason="shutdown_follow_up") == 1
    spool = next((tmp_path / "pending_messages").glob("*.json"))
    synthetic_id = json.loads(spool.read_text())["data"]["message_id"]
    assert synthetic_id.startswith("hermes-recovery-")
    runner.session_store.append_to_transcript(entry.session_id, {
        "role": "user", "content": "internal completion", "platform_message_id": synthetic_id,
    })
    assert recover_pending_to_db(
        Mock(), session_resolver=lambda *args, **kwargs: (entry.session_id, Mock()),
        deferred_followup=partial(_defer_followup, runner, {}, None)) == 0
    assert runner._startup_restore_queue == []
    assert not list((tmp_path / "pending_messages").glob("*.json"))


@pytest.mark.asyncio
async def test_idless_recovered_followup_spool_retires_when_input_row_lands(tmp_path, monkeypatch):
    """The durability watcher must retire a no-id spool at turn start, before the result."""
    runner, _adapter, entry, source = _setup(tmp_path, monkeypatch)
    event = MessageEvent(text="internal side effect", source=source, user_id=source.user_id, internal=True)
    assert flush_pending_to_file({entry.session_key: event}, reason="shutdown_follow_up") == 1
    recover_pending_to_db(Mock(), session_resolver=lambda *args, **kwargs: (entry.session_id, Mock()),
                          deferred_followup=partial(_defer_followup, runner, {}, None))
    spool = next((tmp_path / "pending_messages").glob("*.json"))
    payload = json.loads(spool.read_text())
    synthetic_id = payload["data"]["message_id"]
    assert synthetic_id.startswith("hermes-recovery-")
    recovered = runner._startup_restore_queue[0]
    assert recovered.message_id == synthetic_id
    runner.session_store.append_to_transcript(entry.session_id, {
        "role": "user", "content": event.text, "platform_message_id": synthetic_id,
    })
    await asyncio.wait_for(runner._retire_spool_once_input_durable(recovered), 2)
    assert not spool.exists()


@pytest.mark.asyncio
async def test_restored_followup_notice_is_sent_inside_its_profile_scope(tmp_path, monkeypatch):
    """A multiplexed secondary profile's follow-up notice resolves that profile's runtime."""
    from gateway import run

    runner, adapter, entry, source = _setup(tmp_path, monkeypatch)
    profile_home = tmp_path / "profiles" / "secondary"
    runner._resolve_profile_home_for_source = lambda _source: profile_home
    active = []
    entered = []

    class _SpyScope:
        def __init__(self, home):
            self.home = home

        async def __aenter__(self):
            entered.append(self.home)
            active.append(self.home)
            return self

        async def __aexit__(self, *exc):
            active.pop()
            return False

    monkeypatch.setattr(run, "_async_profile_runtime_scope", _SpyScope)
    seen = []

    async def deliver(_source, _text):
        seen.append(list(active))
        return True

    runner._deliver_platform_notice = deliver
    event = MessageEvent(text="saved question", source=source, user_id=source.user_id)
    await runner._finish_recovered_followup(event, entry.session_key, "accepted-without-turn")
    assert entered == [profile_home]
    assert seen == [[profile_home]]


@pytest.mark.asyncio
async def test_recovered_followup_spool_retires_at_durable_turn_start_not_admission(tmp_path, monkeypatch):
    """handle_message only schedules the turn; the spool is the only copy until the input row lands,
    and is retired then (mid-turn), so a later restart never replays a prompt with tool effects."""
    runner, adapter, entry, source = _setup(tmp_path, monkeypatch)
    event = MessageEvent(text="do the side effect", source=source, user_id=source.user_id, message_id="m-7")
    assert flush_pending_to_file({entry.session_key: event}, reason="shutdown_follow_up") == 1
    recover_pending_to_db(Mock(), session_resolver=lambda *args, **kwargs: (entry.session_id, Mock()),
                          deferred_followup=partial(_defer_followup, runner, {}, None))
    spool, = (tmp_path / "pending_messages").glob("*.json")
    persist, release = asyncio.Event(), asyncio.Event()

    async def turn(recovered):
        await persist.wait()  # lease, transcript load, hygiene, agent build: no durable input yet
        runner.session_store.append_to_transcript(entry.session_id, {
            "role": "user", "content": recovered.text, "platform_message_id": "m-7"})
        await release.wait()

    async def handle(recovered):
        recovered._gateway_accepted = True
        adapter._session_tasks[entry.session_key] = asyncio.create_task(turn(recovered))

    adapter.handle_message = handle
    assert await runner._drain_startup_restore_queue() == 1
    for _ in range(5):
        await asyncio.sleep(0.05)
    assert spool.exists(), "a crash before the turn persisted its input would lose the follow-up"
    persist.set()
    for _ in range(40):
        if not spool.exists():
            break
        await asyncio.sleep(0.05)
    assert not spool.exists()

    # A second restart cuts the turn: the next boot must not replay the follow-up.
    observers = list(runner._background_tasks)
    for task in observers:
        task.cancel()
    await asyncio.gather(*observers, return_exceptions=True)
    owner = adapter._session_tasks.pop(entry.session_key)
    owner.cancel()
    await asyncio.gather(owner, return_exceptions=True)
    fresh_runner, _adapter, fresh_entry, _source = _setup(tmp_path, monkeypatch)
    assert recover_pending_to_db(
        Mock(), session_resolver=lambda *args, **kwargs: (fresh_entry.session_id, Mock()),
        deferred_followup=partial(_defer_followup, fresh_runner, {}, None)) == 0
    assert fresh_entry.session_key not in runner._restart_followup_keys


def test_followup_spool_whose_input_is_already_durable_is_dropped_not_replayed(tmp_path, monkeypatch):
    """A crash between the durable input row and retirement must fall to resume_pending."""
    runner, _adapter, entry, source = _setup(tmp_path, monkeypatch)
    event = MessageEvent(text="do the side effect", source=source, user_id=source.user_id, message_id="m-8")
    assert flush_pending_to_file({entry.session_key: event}, reason="shutdown_follow_up") == 1
    runner.session_store.append_to_transcript(entry.session_id, {
        "role": "user", "content": "do the side effect", "platform_message_id": "m-8"})
    assert recover_pending_to_db(
        Mock(), session_resolver=lambda *args, **kwargs: (entry.session_id, Mock()),
        deferred_followup=partial(_defer_followup, runner, {}, None)) == 0
    assert runner._startup_restore_queue == []
    assert not list((tmp_path / "pending_messages").glob("*.json"))


def test_reconnect_followup_with_missing_origin_is_retained_without_attribute_error(tmp_path, monkeypatch):
    runner, _adapter, entry, _source = _setup(tmp_path, monkeypatch)
    entry.origin = None
    path = tmp_path / "pending.json"
    data = {"drain_deferred": True, "text": "saved", "source_user_id": "u1"}
    assert _defer_followup(
        runner, {entry.session_key: entry}, Platform.TELEGRAM, entry.session_key,
        entry.session_id, data, path, reconnect_recovery=True,
    ) is None
