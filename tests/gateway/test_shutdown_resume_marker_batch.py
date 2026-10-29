"""Batch durability and idempotence for shutdown resume markers."""

import asyncio
import json
import threading
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.run_shutdown import GatewayShutdownMixin
from gateway.session import AsyncSessionStore, SessionSource, SessionStore, build_session_key


@pytest.fixture(autouse=True)
def _isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))


def _source(index: int) -> SessionSource:
    return SessionSource(
        platform=Platform.TELEGRAM,
        chat_id=f"chat-{index}",
        chat_type="dm",
        user_id=f"user-{index}",
    )


def _runner(store, keys):
    runner = object.__new__(GatewayShutdownMixin)
    runner.session_store = store
    runner.async_session_store = AsyncSessionStore(store)
    runner._running_agents = {key: object() for key in keys}
    runner._restart_requested = True
    runner._peek_session_state = lambda _key: SimpleNamespace(
        turn=SimpleNamespace(event=SimpleNamespace(internal=False))
    )
    return runner


@pytest.mark.asyncio
async def test_pre_drain_marks_six_sessions_with_one_save_and_is_durable(tmp_path):
    store = SessionStore(tmp_path / "sessions", GatewayConfig())
    entries = [store.get_or_create_session(_source(index)) for index in range(6)]
    for index, entry in enumerate(entries):
        assert store.mark_turn_active(entry.session_key, human=index % 2 == 0)
    keys = [build_session_key(_source(index)) for index in range(6)]
    runner = _runner(store, keys)
    runner._peek_session_state = lambda key: SimpleNamespace(
        turn=SimpleNamespace(event=SimpleNamespace(internal=keys.index(key) % 2 != 0))
    )
    runner._active_cron_job_count = lambda: 0
    runner._active_api_run_count = lambda: 0
    runner._running_agent_count = lambda: len(keys)
    runner._cron_drain_timeout = 0
    ctx = SimpleNamespace(elapsed=lambda: 0, deferred_count=lambda: 0)
    save_started, release_save = threading.Event(), threading.Event()
    drain_started = asyncio.Event()
    generation = store._routing_generation
    original_save = store._save

    def gated_save():
        save_started.set()
        assert release_save.wait(10), "test did not release the durable save"
        original_save()

    async def drain(*_args):
        drain_started.set()
        assert save.call_count == 1
        assert db_save.call_count == 1
        assert mirror_save.call_count == 1
        assert store._routing_generation == generation + 1
        # The drain boundary sees both real durable backends, not just live entries.
        fresh = SessionStore(store.sessions_dir, store.config)
        fresh._ensure_loaded()
        mirror = json.loads((store.sessions_dir / "sessions.json").read_text())
        for index, key in enumerate(keys):
            entry = fresh._entries[key]
            assert entry.resume_pending is True
            assert entry.resume_reason == "restart_timeout"
            assert entry.resume_turn_id == entries[index].active_turn_token
            assert entry.resume_human is (index % 2 == 0)
            assert mirror[key]["resume_marker_token"] == entry.resume_marker_token
            assert mirror[key]["resume_turn_id"] == entry.resume_turn_id
            assert mirror[key]["resume_human"] == entry.resume_human
        fresh.close_all_db_handles()
        return dict(runner._running_agents), True

    runner._drain_active_agents = drain
    with patch.object(store, "_save", side_effect=gated_save) as save, patch.object(
        store._routing_db, "replace_gateway_routing_entries",
        wraps=store._routing_db.replace_gateway_routing_entries,
    ) as db_save, patch.object(store, "_save_sessions_json", wraps=store._save_sessions_json) as mirror_save:
        task = asyncio.create_task(runner._stop_drain_active_work(2.0, ctx))
        try:
            assert await asyncio.wait_for(asyncio.to_thread(save_started.wait, 10), 15)
            assert not drain_started.is_set()
        finally:
            release_save.set()
            await asyncio.wait_for(task, 15)
    assert drain_started.is_set()
    store.close_all_db_handles()


@pytest.mark.asyncio
async def test_timeout_remark_same_turn_is_noop_but_still_returns_owed_key(tmp_path):
    from tests.gateway.test_restart_interrupted_note import NoteAdapter, _note_runner

    store = SessionStore(tmp_path / "sessions", GatewayConfig())
    source = _source(1)
    entry = store.get_or_create_session(source)
    assert store.mark_turn_active(entry.session_key, human=True)
    runner = _runner(store, [entry.session_key])
    first = await runner._mark_running_sessions_resume_pending("pre-drain")
    marker = store.get_resume_pending_marker(entry.session_key)
    generation = store._routing_generation
    with patch.object(store, "_save", wraps=store._save) as save:
        second = await runner._mark_running_sessions_resume_pending("timeout")

    assert first == [entry.session_key]
    assert second == [entry.session_key]
    assert save.call_count == 0
    assert store._routing_generation == generation
    assert store.get_resume_pending_marker(entry.session_key) == marker
    adapter = NoteAdapter()
    note_runner = _note_runner(store, source, adapter)
    note_runner._restart_requested = True
    assert await note_runner._send_interrupted_turn_notes(second) == 1
    assert await note_runner._send_interrupted_turn_notes(second) == 0
    assert len(adapter.sent) == 1
    store.close_all_db_handles()


@pytest.mark.asyncio
async def test_timeout_bulk_marks_late_and_successor_turns_without_replacing_existing_marker(tmp_path):
    from gateway.run import _AGENT_PENDING_SENTINEL

    store = SessionStore(tmp_path / "sessions", GatewayConfig())
    entries = [store.get_or_create_session(_source(index)) for index in range(3)]
    for entry in entries:
        store.mark_turn_active(entry.session_key)
    keys = [entry.session_key for entry in entries]
    runner = _runner(store, keys[:2])
    assert await runner._mark_running_sessions_resume_pending("pre-drain") == keys[:2]
    original_marker = store.get_resume_pending_marker(keys[0])
    predecessor_marker = store.get_resume_pending_marker(keys[1])
    successor_token = store.mark_turn_active(keys[1])
    runner._running_agents[keys[2]] = object()
    runner._running_agents["pending-only"] = _AGENT_PENDING_SENTINEL
    with patch.object(store, "_save", wraps=store._save) as save:
        assert await runner._mark_running_sessions_resume_pending("timeout") == keys
    assert save.call_count == 1
    assert store.get_resume_pending_marker(keys[0]) == original_marker
    assert store.get_resume_pending_marker(keys[1]) != predecessor_marker
    assert entries[1].resume_turn_id == successor_token
    assert entries[2].resume_turn_id == entries[2].active_turn_token
    store.close_all_db_handles()


@pytest.mark.parametrize("change", ["reason", "human"])
def test_same_turn_changed_metadata_is_persisted_without_rotating_marker(tmp_path, change):
    store = SessionStore(tmp_path / "sessions", GatewayConfig())
    entry = store.get_or_create_session(_source(1))
    turn_id = store.mark_turn_active(entry.session_key)
    store.mark_resume_pending(entry.session_key, turn_id=turn_id)
    marker = store.get_resume_pending_marker(entry.session_key)
    reason = "shutdown_timeout" if change == "reason" else "restart_timeout"
    human = change != "human"
    with patch.object(store, "_save", wraps=store._save) as save:
        assert store.mark_resume_pending_many([(entry.session_key, turn_id, human)], reason) == [entry.session_key]
    assert save.call_count == 1
    assert store.get_resume_pending_marker(entry.session_key) == marker
    durable = json.loads(store._routing_db.load_gateway_routing_entries(scope=store._routing_scope())[entry.session_key])
    assert durable["resume_reason"] == reason
    assert durable["resume_human"] is human
    store.close_all_db_handles()


@pytest.mark.asyncio
async def test_cut_turn_note_candidates_survive_removed_or_suspended_routes(tmp_path):
    store = SessionStore(tmp_path / "sessions", GatewayConfig())
    entry = store.get_or_create_session(_source(1))
    store.suspend_session(entry.session_key)
    keys = [entry.session_key, "removed-route"]
    runner = _runner(store, keys)
    with patch.object(store, "_save", wraps=store._save) as save:
        assert await runner._mark_running_sessions_resume_pending("timeout") == keys
    assert save.call_count == 0
    assert not entry.resume_pending
    store.close_all_db_handles()


def test_bulk_skips_missing_suspended_and_empty_batches(tmp_path):
    store = SessionStore(tmp_path / "sessions", GatewayConfig())
    entry = store.get_or_create_session(_source(1))
    store.suspend_session(entry.session_key)
    with patch.object(store, "_save", wraps=store._save) as save:
        assert store.mark_resume_pending_many([]) == []
        assert store.mark_resume_pending_many([(entry.session_key, "turn", True), ("missing", "turn", True)]) == []
    assert save.call_count == 0
    assert entry.resume_pending is False
    store.close_all_db_handles()


def test_bulk_failed_save_does_not_make_retry_a_false_noop(tmp_path):
    store = SessionStore(tmp_path / "sessions", GatewayConfig())
    entry = store.get_or_create_session(_source(1))
    token = store.mark_turn_active(entry.session_key)
    markers = [(entry.session_key, token, True)]
    with patch.object(store, "_save", side_effect=OSError("unavailable")):
        with pytest.raises(OSError, match="unavailable"):
            store.mark_resume_pending_many(markers)
    assert entry.resume_pending is False
    with patch.object(store, "_save", wraps=store._save) as save:
        assert store.mark_resume_pending_many(markers) == [entry.session_key]
    assert save.call_count == 1
    fresh = SessionStore(store.sessions_dir, store.config)
    fresh._ensure_loaded()
    assert fresh._entries[entry.session_key].resume_turn_id == token
    fresh.close_all_db_handles()
    store.close_all_db_handles()


def test_bulk_save_keeps_routing_home_and_rejects_stale_full_snapshot(tmp_path):
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    store = SessionStore(tmp_path / "sessions", GatewayConfig(multiplex_profiles=True))
    sources = [_source(1), replace(_source(2), profile="secondary")]
    entries = [store.get_or_create_session(source) for source in sources]
    markers = [(entry.session_key, store.mark_turn_active(entry.session_key), True) for entry in entries]
    with store._lock:
        stale_data, stale_generation = store._snapshot_routing_locked()
    secondary_home = tmp_path / "home" / "profiles" / "secondary"
    secondary_home.mkdir(parents=True)
    override = set_hermes_home_override(str(secondary_home))
    try:
        assert store.mark_resume_pending_many(markers) == [entry.session_key for entry in entries]
    finally:
        reset_hermes_home_override(override)
    store._persist_routing_data(stale_data, stale_generation)
    fresh = SessionStore(store.sessions_dir, store.config)
    fresh._ensure_loaded()
    for entry in entries:
        assert fresh._entries[entry.session_key].resume_pending is True
        assert fresh._entries[entry.session_key].resume_turn_id == entry.active_turn_token
    assert not (secondary_home / "state.db").exists()
    fresh.close_all_db_handles()
    store.close_all_db_handles()
