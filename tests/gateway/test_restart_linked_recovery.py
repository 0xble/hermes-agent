"""Real SQLite boundaries for the restart inbox's single recovery owner."""

import sqlite3
import asyncio
import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from agent.turn_context import RequiredInputPersistenceError
from gateway import restart_inbox as inbox
from gateway.config import GatewayConfig, Platform
from gateway.platforms.base import MessageEvent, MessageType
from gateway.run import GatewayRunner
from gateway.session import SessionSource, SessionStore
from hermes_state import SessionDB
from tests.gateway.restart_test_helpers import make_restart_runner


@pytest.fixture
def state(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setattr(inbox, "_db_path", lambda: home / "state.db")
    store = SessionStore(sessions_dir=home / "sessions", config=GatewayConfig())
    store._routing_home = home
    db = SessionDB(db_path=home / "state.db")
    store._db = db
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="chat", user_id="owner", chat_type="dm")
    entry = store.get_or_create_session(source)
    event = MessageEvent(text="inspect this", source=source, message_type=MessageType.TEXT,
                         media_urls=["/tmp/photo.png"], media_types=["image/png"],
                         turn_reasoning_config={"effort": "high"})
    yield store, entry, event, home / "state.db"
    db.close()


def orphan(path, queue_id):
    with sqlite3.connect(path) as conn:
        conn.execute("UPDATE restart_inbox SET owner_pid=999999999, owner_started_at=1 WHERE queue_id=?", (queue_id,))


def claim(state):
    store, entry, event, path = state
    queue_id = inbox.record_event(entry.session_key, event)
    orphan(path, queue_id)
    row = inbox.claim_recoverable(deliverable_targets={("telegram", "default")})[0]
    return row["event"]._restart_inbox_claim, row["event"]


def test_hard_suspension_ordinary_route_replaces_abandoned_link(state):
    store, entry, event, path = state
    link, _ = claim(state)
    store.mark_turn_active(entry.session_key, restart_claim=link)
    assert store.suspend_session(entry.session_key)
    assert inbox.read_rows(path)[0]["state"] == "abandoned"
    replacement = store.get_or_create_session(event.source)
    assert replacement.session_id != entry.session_id
    assert replacement.restart_inbox_link is None
    assert store.mark_turn_active(replacement.session_key)


def test_stop_cancellation_is_exact_and_retryable_after_primary_failure(state, monkeypatch):
    store, entry, _event, path = state
    link, _ = claim(state)
    token = store.mark_turn_active(entry.session_key, restart_claim=link)
    assert store.restart_turn_stop_owner(entry.session_key) == (entry.session_id, token)
    assert not store.cancel_restart_turn(entry.session_key, "another-session", token)
    assert not store.cancel_restart_turn(entry.session_key, entry.session_id, "stale-token")
    assert inbox.read_rows(path)[0]["state"] == "attempting"
    before = entry.to_dict()
    saver = store._db.save_gateway_routing_entry
    monkeypatch.setattr(store._db, "save_gateway_routing_entry", MagicMock(side_effect=OSError("primary failed")))
    mirror = MagicMock()
    monkeypatch.setattr(store, "_persist_routing_data", mirror)
    with pytest.raises(OSError, match="primary failed"):
        store.cancel_restart_turn(entry.session_key, entry.session_id, token)
    assert entry.to_dict() == before
    mirror.assert_not_called()
    assert inbox.read_rows(path)[0]["state"] == "abandoned"
    with pytest.raises(RequiredInputPersistenceError):
        store.mark_turn_active(entry.session_key)
    monkeypatch.setattr(store._db, "save_gateway_routing_entry", saver)
    assert store.cancel_restart_turn(entry.session_key, entry.session_id, token)
    assert entry.active_turn_token is None and entry.restart_inbox_link is None
    assert entry.restart_inbox_settled_at
    persisted = store._db.load_gateway_routing_entries(scope=store._routing_scope())
    assert json.loads(persisted[entry.session_key])["restart_inbox_link"] is None
    next_token = store.mark_turn_active(entry.session_key)
    assert not store.clear_turn_active(entry.session_key, token)
    assert not store.cancel_restart_turn(entry.session_key, entry.session_id, token)
    assert entry.active_turn_token == next_token


@pytest.mark.asyncio
@pytest.mark.parametrize("primary_fails", [False, True])
async def test_real_stop_settles_link_before_release_and_preserves_conversation(state, monkeypatch, primary_fails):
    store, entry, event, path = state
    link, _ = claim(state)
    old_token = store.mark_turn_active(entry.session_key, restart_claim=link)
    store.mark_resume_pending(entry.session_key)
    store._db.append_message(entry.session_id, "user", "keep this history")
    runner, _ = make_restart_runner()
    runner.session_store = store
    agent = MagicMock(_gateway_turn_process_task_id="", _gateway_turn_process_baseline=None)
    runner._running_agents[entry.session_key] = agent
    runner._evict_cached_agent = MagicMock()
    release = runner._release_running_agent_state
    released = []

    def release_after_persistence(key, **kwargs):
        persisted = json.loads(store._db.load_gateway_routing_entries(scope=store._routing_scope())[key])
        assert persisted["restart_inbox_link"] is None
        assert persisted["active_turn_token"] is None
        assert not persisted["resume_pending"]
        released.append(key)
        return release(key, **kwargs)

    runner._release_running_agent_state = release_after_persistence
    if primary_fails:
        monkeypatch.setattr(store._db, "save_gateway_routing_entry", MagicMock(side_effect=OSError("primary failed")))
        with pytest.raises(OSError, match="primary failed"):
            await runner._handle_stop_command(event)
        assert released == []
        assert runner._running_agents[entry.session_key] is agent
        assert entry.restart_inbox_link and entry.active_turn_token == old_token
        return
    await runner._handle_stop_command(event)
    assert released == [entry.session_key]
    assert inbox.read_rows(path)[0]["state"] == "abandoned"
    resumed = await runner.async_session_store.get_or_create_session(event.source)
    assert resumed.session_id == entry.session_id
    assert not resumed.suspended
    assert store._db.get_messages(entry.session_id)[-1]["content"] == "keep this history"
    assert await runner._mark_durable_active_turn(event, entry.session_key)
    assert not store.clear_turn_active(entry.session_key, old_token)


@pytest.mark.asyncio
async def test_stop_transport_wait_cannot_release_a_replacement_turn(state):
    from tests.gateway.restart_test_helpers import RestartTestAdapter
    store, entry, event, _path = state
    link, _ = claim(state)
    store.mark_turn_active(entry.session_key, restart_claim=link)
    entered, resume = asyncio.Event(), asyncio.Event()

    class HeldAdapter(RestartTestAdapter):
        async def interrupt_session_activity(self, *args, **kwargs):
            entered.set()
            await resume.wait()

    runner, _ = make_restart_runner(HeldAdapter())
    runner.session_store = store
    old_agent = MagicMock(_gateway_turn_process_task_id="", _gateway_turn_process_baseline=None)
    runner._running_agents[entry.session_key] = old_agent
    runner._evict_cached_agent = MagicMock()
    stopping = asyncio.create_task(runner._handle_stop_command(event))
    await asyncio.wait_for(entered.wait(), 2)
    assert entry.restart_inbox_link is None
    # The old finalizer can release its slot while stop awaits transport cleanup.
    runner._release_running_agent_state(entry.session_key)
    replacement = object()
    runner._begin_session_run_generation(entry.session_key)
    runner._running_agents[entry.session_key] = replacement
    new_token = store.mark_turn_active(entry.session_key)
    resume.set()
    await asyncio.wait_for(stopping, 2)
    assert runner._running_agents[entry.session_key] is replacement
    assert entry.active_turn_token == new_token
    runner._evict_cached_agent.assert_not_called()


@pytest.mark.parametrize("terminal", ["delivered", "abandoned"])
def test_stop_finishes_exact_terminal_inbox_row_without_reset(state, terminal):
    store, entry, _event, _path = state
    link, _ = claim(state)
    token = store.mark_turn_active(entry.session_key, restart_claim=link)
    assert inbox.transition_link(entry.restart_inbox_link, terminal)
    assert store.cancel_restart_turn(entry.session_key, entry.session_id, token)
    assert entry.restart_inbox_link is None and entry.restart_inbox_settled_at


def reconcile(state):
    store, _entry, _event, path = state
    return store.reconcile_restart_inbox([path])[str(path)]


def test_crash_before_link_reuses_stable_owner_and_normalized_event(state):
    link, event = claim(state)
    orphan(state[3], link["queue_id"])
    assert reconcile(state) == set()
    again = inbox.claim_recoverable(deliverable_targets={("telegram", "default")})[0]["event"]
    assert again._restart_inbox_claim["input_owner"] == link["input_owner"]
    assert again.media_urls == event.media_urls
    assert again.turn_reasoning_config == event.turn_reasoning_config
    assert again.message_id is None


def test_crash_after_link_before_input_only_replays_original(state):
    store, entry, _event, path = state
    link, _ = claim(state)
    store.mark_turn_active(entry.session_key, restart_claim=link)
    orphan(path, link["queue_id"])
    for _ in range(2):
        assert reconcile(state) == set()
        assert entry.restart_inbox_link["mode"] == "replay"
        assert not entry.resume_pending
        assert store.recover_interrupted_turns() == 0
        assert store.suspend_recently_active() == 0
        assert store.discard_active_turn_markers() == 0
    replay = inbox.claim_recoverable(deliverable_targets={("telegram", "default")})[0]["event"]
    store.mark_turn_active(entry.session_key, restart_claim=replay._restart_inbox_claim)
    assert entry.restart_inbox_link["input_owner"] == link["input_owner"]


@pytest.mark.parametrize("compressed", [False, True])
def test_ingested_input_and_effect_proxy_only_continue(state, compressed):
    store, entry, _event, path = state
    link, _ = claim(state)
    store.mark_turn_active(entry.session_key, restart_claim=link)
    db = store._db
    db.append_message(entry.session_id, "user", "canonical image description", display_metadata={"gateway_input_owner": link["input_owner"]})
    # Represents a tool result after the mandatory input commit.
    db.append_message(entry.session_id, "assistant", "effect completed")
    if compressed:
        db.end_session(entry.session_id, "compression")
        db.create_session("compression-child", source="telegram", parent_session_id=entry.session_id)
    orphan(path, link["queue_id"])
    for _ in range(2):
        assert link["queue_id"] in reconcile(state)
        assert entry.restart_inbox_link["mode"] == "continuation"
        assert entry.resume_pending
        assert inbox.claim_recoverable(deliverable_targets={("telegram", "default")}) == []
    token = store.mark_turn_active(entry.session_key)
    assert store.clear_turn_active(entry.session_key, token)
    assert entry.restart_inbox_link is None
    assert store.suspend_recently_active() == 0


def test_terminal_queue_commit_before_link_clear_and_retention_expiry(state, monkeypatch):
    store, entry, _event, path = state
    link, _ = claim(state)
    token = store.mark_turn_active(entry.session_key, restart_claim=link)
    save = store._save_entry
    monkeypatch.setattr(store, "_save_entry", MagicMock(side_effect=OSError("crash after queue commit")))
    with pytest.raises(OSError):
        store.clear_turn_active(entry.session_key, token)
    assert inbox.linked_row(entry.restart_inbox_link)["state"] == "delivered"
    monkeypatch.setattr(store, "_save_entry", save)
    reconcile(state)
    assert entry.restart_inbox_link is None
    assert not entry.active_turn_token and not entry.resume_pending
    with sqlite3.connect(path) as conn:
        conn.execute("DELETE FROM restart_inbox")
    for _ in range(2):
        reconcile(state)
        assert store.suspend_recently_active() == 0
        assert store.recover_interrupted_turns() == 0
    # Terminal evidence does not block a later user turn.
    assert store.mark_turn_active(entry.session_key)
    assert entry.restart_inbox_settled_at is None


@pytest.mark.parametrize("failure", ["payload", "missing_row", "read", "missing_session"])
def test_unknown_ingestion_parks_both_consumers(state, monkeypatch, failure):
    store, entry, _event, path = state
    link, _ = claim(state)
    store.mark_turn_active(entry.session_key, restart_claim=link)
    orphan(path, link["queue_id"])
    if failure == "read":
        monkeypatch.setattr(store, "has_input_owner", MagicMock(side_effect=OSError("read failed")))
    elif failure == "missing_session":
        monkeypatch.setattr(store._db, "get_session", lambda _sid: None)
    else:
        with sqlite3.connect(path) as conn:
            conn.execute("DELETE FROM restart_inbox" if failure == "missing_row" else "UPDATE restart_inbox SET event_json='{}'")
    excluded = reconcile(state)
    assert entry.restart_inbox_link["mode"] == "parked"
    assert store.recover_interrupted_turns() == 0
    assert store.discard_active_turn_markers() == 0
    assert inbox.claim_recoverable(deliverable_targets={("telegram", "default")}, excluded_queue_ids=excluded) == []


def test_primary_routing_failure_cannot_be_masked_by_json_mirror(state, monkeypatch):
    store, entry, _event, _path = state
    link, _ = claim(state)
    monkeypatch.setattr(store._db, "save_gateway_routing_entry", MagicMock(side_effect=OSError("primary unavailable")))
    fallback = MagicMock()
    monkeypatch.setattr(store, "_persist_routing_data", fallback)
    with pytest.raises(OSError):
        store.mark_turn_active(entry.session_key, restart_claim=link)
    fallback.assert_not_called()
    assert entry.restart_inbox_link is None and entry.active_turn_token is None


def test_wrong_claim_owner_cannot_admit_and_reset_cancels_before_dropping_link(state):
    store, entry, _event, path = state
    link, _ = claim(state)
    with pytest.raises(RequiredInputPersistenceError):
        store.mark_turn_active(entry.session_key, restart_claim=dict(link, owner_pid=-1))
    store.mark_turn_active(entry.session_key, restart_claim=link)
    store.reset_session(entry.session_key)
    assert inbox.linked_row(link)["state"] == "abandoned"
    orphan(path, link["queue_id"])
    assert inbox.claim_recoverable(deliverable_targets={("telegram", "default")}) == []


@pytest.mark.parametrize("command", [False, True])
def test_legacy_or_control_attempt_without_link_is_parked(state, command):
    store, entry, event, path = state
    if command:
        event.text = "/reset"
    link, _ = claim(state)
    if not command:
        with sqlite3.connect(path) as conn:
            conn.execute("UPDATE restart_inbox SET protocol=0")
    orphan(path, link["queue_id"])
    assert inbox.claim_recoverable(deliverable_targets={("telegram", "default")}) == []
    reconcile(state)
    assert entry.restart_inbox_link["mode"] == "parked"
    assert store.suspend_recently_active() == 0


@pytest.mark.asyncio
async def test_drain_and_error_handler_retain_failed_admission(state):
    store, entry, event, path = state
    queue_id = inbox.record_event(entry.session_key, event)
    orphan(path, queue_id)
    runner, adapter = make_restart_runner()
    runner.session_store = store
    runner._hmwa_stop_typing_for_turn = AsyncMock()

    async def rejected(event):
        assert await runner._mark_durable_active_turn(event, entry.session_key)
        try:
            await runner._hmwa_agent_error_reply(
                RequiredInputPersistenceError("disk failed"), event, event.source, entry, entry.session_key, None,
            )
        finally:
            assert not await runner._clear_durable_active_turn(event)

    adapter.handle_message = rejected
    assert await runner._drain_restart_inbox() == 0
    assert entry.restart_inbox_link is not None and entry.active_turn_token
    assert inbox.linked_row(entry.restart_inbox_link)["state"] == "pending"
    assert not store.has_input_owner(entry.session_id, entry.restart_inbox_link["input_owner"])
    runner._hmwa_stop_typing_for_turn.assert_not_called()


def test_standalone_does_not_enumerate_profile_homes(state, monkeypatch):
    runner, _adapter = make_restart_runner()
    runner.session_store = state[0]
    profiles = MagicMock(side_effect=AssertionError("standalone enumerated profiles"))
    monkeypatch.setattr("gateway.run._multiplex_profile_homes", profiles)
    assert runner._reconcile_restart_recovery()
    profiles.assert_not_called()


@pytest.mark.asyncio
async def test_named_profile_offline_clean_start_then_reconnect_uses_its_inbox(tmp_path, monkeypatch):
    import hermes_state
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    root = tmp_path / "root"
    named = root / "profiles" / "named"
    named.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setattr(hermes_state, "DEFAULT_DB_PATH", hermes_state._IMPORT_DEFAULT_DB_PATH)
    monkeypatch.setattr("gateway.run._multiplex_profile_homes", lambda _config: [("named", named)])
    config = GatewayConfig(multiplex_profiles=True)
    store = SessionStore(sessions_dir=root / "sessions", config=config)
    store._profile_home_cache["named"] = named
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="named-chat", user_id="user", chat_type="dm", profile="named")
    event = MessageEvent(text="named original", source=source, media_urls=["/tmp/input.png"], media_types=["image/png"])
    scope = set_hermes_home_override(named)
    try:
        entry = store.get_or_create_session(source)
        queue_id = inbox.record_event(entry.session_key, event, "named")
        orphan(named / "state.db", queue_id)
        queued = inbox.claim_recoverable(deliverable_targets={("telegram", "named")})[0]["event"]
        store.mark_turn_active(entry.session_key, restart_claim=queued._restart_inbox_claim)
    finally:
        reset_hermes_home_override(scope)
    orphan(named / "state.db", queue_id)
    runner, adapter = make_restart_runner()
    runner.config = config
    runner.session_store = store
    runner.adapters = {}
    runner._profile_adapters = {}
    runner._profile_failed_platforms = {}
    assert runner._reconcile_restart_recovery()
    marker = root / ".clean_shutdown"
    marker.write_text("clean")
    await runner._consume_clean_shutdown_marker(marker)
    assert entry.restart_inbox_link["mode"] == "replay"
    assert not entry.resume_pending
    assert await runner._drain_restart_inbox() == 0
    delivered = []
    async def handle(replay):
        delivered.append(replay)
        assert await runner._mark_durable_active_turn(replay, entry.session_key)
        assert await runner._clear_durable_active_turn(replay)
    adapter.handle_message = handle
    runner._secondary_reconnect_attempt = AsyncMock(return_value=(adapter, True))
    runner._sync_voice_mode_state_to_adapter = MagicMock()
    runner._redeliver_failed_obligations_for_platform = AsyncMock()
    await asyncio.wait_for(runner._run_secondary_profile_reconnect("named", Platform.TELEGRAM), timeout=3)
    assert len(delivered) == 1 and delivered[0].media_urls == event.media_urls
    assert store._routing_db.get_session(entry.session_id) is None
    assert store._db_for_key(entry.session_key).get_session(entry.session_id) is not None
    assert inbox.read_rows(named / "state.db")[0]["state"] == "delivered"
    assert entry.restart_inbox_link is None
    store.close_all_db_handles()


def test_primary_failure_reopen_never_inherits_json_only_admission(state, monkeypatch):
    store, entry, _event, path = state
    link, _ = claim(state)
    original = store._db.save_gateway_routing_entry
    monkeypatch.setattr(store._db, "save_gateway_routing_entry", MagicMock(side_effect=OSError("upsert failed")))
    mirror = MagicMock()
    monkeypatch.setattr(store, "_persist_routing_data", mirror)
    with pytest.raises(OSError):
        store.mark_turn_active(entry.session_key, restart_claim=link)
    mirror.assert_not_called()
    monkeypatch.setattr(store._db, "save_gateway_routing_entry", original)
    fresh = SessionStore(sessions_dir=path.parent / "sessions", config=store.config)
    fresh._routing_home = path.parent
    fresh._db = store._db
    fresh._loaded = False
    fresh._entries = {}
    fresh._ensure_loaded()
    restored = fresh._entries[entry.session_key]
    assert restored.restart_inbox_link is None and restored.active_turn_token is None
    orphan(path, link["queue_id"])
    assert inbox.claim_recoverable(deliverable_targets={("telegram", "default")})


@pytest.mark.asyncio
async def test_multiple_rows_never_enter_adapter_busy_coalescing(state):
    store, entry, event, path = state
    first = inbox.record_event(entry.session_key, event)
    event.message_id = "second"
    second = inbox.record_event(entry.session_key, event)
    orphan(path, first)
    orphan(path, second)
    runner, adapter = make_restart_runner()
    runner.session_store = store
    adapter._session_tasks = {}
    release = asyncio.Event()
    delivered = []
    async def execute(replay):
        assert await runner._mark_durable_active_turn(replay, entry.session_key)
        await release.wait()
        assert await runner._clear_durable_active_turn(replay)
        adapter._session_tasks.pop(entry.session_key)
    async def handle(replay):
        assert not adapter._session_tasks, "inbox replay entered busy adapter coalescing"
        delivered.append(replay._restart_inbox_queue_id)
        adapter._session_tasks[entry.session_key] = asyncio.create_task(execute(replay))
    adapter.handle_message = handle
    assert await runner._drain_restart_inbox() == 1
    assert await runner._drain_restart_inbox() == 0
    assert len(delivered) == 1
    untouched = next(row for row in inbox.read_rows(path) if row["queue_id"] not in delivered)
    assert untouched["state"] == "pending" and untouched["attempts"] == 0
    release.set()
    async def wait_for_both():
        while len(delivered) < 2 or adapter._session_tasks:
            await asyncio.sleep(0.01)
    await asyncio.wait_for(wait_for_both(), 3)
    assert set(delivered) == {first, second}
    assert all(row["state"] == "delivered" for row in inbox.read_rows(path))
    runner._running = False
    if runner._background_tasks:
        await asyncio.gather(*list(runner._background_tasks))


@pytest.mark.asyncio
async def test_adapter_busy_race_refuses_before_coalescing(state):
    _store, entry, _event, _path = state
    _link, replay = claim(state)
    runner, adapter = make_restart_runner()
    runner.session_store = state[0]
    adapter._event_session_key = lambda _event: entry.session_key
    adapter._handle_message_while_active = AsyncMock(side_effect=AssertionError("coalesced replay"))
    release = asyncio.Event()
    task = asyncio.create_task(release.wait())
    adapter._active_sessions[entry.session_key] = asyncio.Event()
    adapter._session_tasks[entry.session_key] = task
    try:
        with pytest.raises(inbox.RestartInboxBusy) as error:
            await adapter.handle_message(replay)
        assert error.value.task is task
        assert not adapter._pending_messages
        adapter._handle_message_while_active.assert_not_called()
    finally:
        release.set()
        await task


@pytest.mark.asyncio
async def test_settlement_read_failure_preserves_executed_claim_and_dispatches_remaining(state, monkeypatch):
    store, first_entry, first_event, path = state
    second_source = SessionSource(
        platform=Platform.TELEGRAM, chat_id="second-chat", user_id="owner", chat_type="dm"
    )
    second_entry = store.get_or_create_session(second_source)
    second_event = MessageEvent(
        text="second input", source=second_source, message_type=MessageType.TEXT,
        message_id="second-message",
    )
    first_id = inbox.record_event(first_entry.session_key, first_event)
    second_id = inbox.record_event(second_entry.session_key, second_event)
    orphan(path, first_id)
    orphan(path, second_id)

    runner, adapter = make_restart_runner()
    runner.session_store = store
    # The real lifecycle schedules another drain after settlement. Keep this test focused on the
    # current preclaimed batch while still using the real SQLite claim/dispatch/finalization path.
    runner._schedule_restart_inbox_drain = MagicMock()
    original_linked_row = inbox.linked_row

    async def handle(replay):
        session_key = {
            first_event.source.chat_id: first_entry.session_key,
            second_source.chat_id: second_entry.session_key,
        }[replay.source.chat_id]
        assert await runner._mark_durable_active_turn(replay, session_key)
        if replay._restart_inbox_queue_id == first_id:
            def flaky_linked_row(link):
                if link["queue_id"] == first_id:
                    raise OSError("inbox settlement read failed")
                return original_linked_row(link)
            monkeypatch.setattr(inbox, "linked_row", flaky_linked_row)
            return
        assert await runner._clear_durable_active_turn(replay)

    adapter.handle_message = AsyncMock(side_effect=handle)

    assert await runner._drain_restart_inbox() == 2
    assert {call.args[0]._restart_inbox_queue_id for call in adapter.handle_message.await_args_list} == {
        first_id, second_id
    }
    rows = {row["queue_id"]: row for row in inbox.read_rows(path)}
    # The first turn executed, so its claim remains attempting/owned for reconciliation; it must
    # not be released and replayed merely because the settlement read was unavailable.
    assert rows[first_id]["state"] == "attempting"
    assert rows[first_id]["attempts"] == 1
    assert first_entry.restart_inbox_link and first_entry.restart_inbox_link["mode"] == "active"
    # A later preclaimed row still settles in the same batch.
    assert rows[second_id]["state"] == "delivered"
    assert not second_entry.restart_inbox_link


@pytest.mark.asyncio
async def test_fast_completed_model_schedules_remaining_inbox(state):
    store, entry, event, path = state
    first = inbox.record_event(entry.session_key, event)
    event.message_id = "next"
    second = inbox.record_event(entry.session_key, event)
    orphan(path, first)
    orphan(path, second)
    runner, adapter = make_restart_runner()
    runner.session_store = store
    delivered = []
    async def handle(replay):
        delivered.append(replay._restart_inbox_queue_id)
        assert await runner._mark_durable_active_turn(replay, entry.session_key)
        assert await runner._clear_durable_active_turn(replay)
    adapter.handle_message = handle
    assert await runner._drain_restart_inbox() == 1
    async def wait_finished():
        while len(delivered) < 2:
            await asyncio.sleep(0.01)
    await asyncio.wait_for(wait_finished(), 3)
    runner._running = False
    if runner._background_tasks:
        await asyncio.gather(*list(runner._background_tasks))
    assert set(delivered) == {first, second}


@pytest.mark.asyncio
async def test_control_background_handler_writes_terminal_receipt(state):
    store, entry, event, path = state
    event.text = "/reset"
    queue_id = inbox.record_event(entry.session_key, event)
    orphan(path, queue_id)
    runner, adapter = make_restart_runner()
    runner.session_store = store
    adapter._event_session_key = lambda _event: entry.session_key
    assert await runner._drain_restart_inbox() == 1
    async def finished():
        while inbox.read_rows(path)[0]["state"] != "delivered":
            await asyncio.sleep(0.01)
    await asyncio.wait_for(finished(), 3)
    runner._running = False
    await asyncio.gather(*list(adapter._session_tasks.values()), *list(runner._background_tasks))
    assert not entry.restart_inbox_link
