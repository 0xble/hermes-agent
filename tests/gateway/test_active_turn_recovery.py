"""Regression tests for exact durable active-turn restart recovery.

A long-running gateway turn can outlive the legacy 120-second
``updated_at`` crash heuristic.  These tests require an exact persisted
marker, compare-and-swap cleanup, and promotion into the existing
``resume_pending`` recovery path after an unclean exit.
"""

import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest

from gateway.config import GatewayConfig, Platform
from gateway.run import GatewayRunner
from gateway.session import SessionEntry, SessionSource, SessionStore


ACTIVE_TURN_MAX_AGE_SECONDS = 60 * 60


def _make_source(chat_id: str = "active-turn-chat") -> SessionSource:
    return SessionSource(
        platform=Platform.DISCORD,
        chat_id=chat_id,
        user_id="user-1",
        chat_type="channel",
        thread_id="thread-1",
    )


def _make_store(tmp_path) -> SessionStore:
    store = SessionStore(sessions_dir=tmp_path, config=GatewayConfig())
    # Exercise the legacy JSON fallback deterministically.  ``_save_entry``
    # must still persist correctly when state.db is unavailable.
    store._db = None
    return store


def _make_db_store(tmp_path) -> SessionStore:
    from hermes_state import SessionDB

    sessions_dir = tmp_path / "sessions"
    store = SessionStore(sessions_dir=sessions_dir, config=GatewayConfig())
    if store._db is not None:
        store._db.close()
    store._db = SessionDB(db_path=tmp_path / "state.db")
    return store


def _close_store_db(store: SessionStore) -> None:
    db = store._db
    assert db is not None
    db.close()


def _entry_for(store: SessionStore, source: SessionSource) -> SessionEntry:
    key = store._generate_session_key(source)
    with store._lock:
        store._ensure_loaded_locked()
        return store._entries[key]


def test_active_turn_fields_round_trip_and_legacy_payload_defaults(tmp_path):
    store = _make_store(tmp_path)
    source = _make_source()
    entry = store.get_or_create_session(source)

    token = store.mark_turn_active(entry.session_key)
    assert token

    payload = _entry_for(store, source).to_dict()
    assert payload["active_turn_token"] == token
    assert payload["active_turn_started_at"] is not None

    restored = SessionEntry.from_dict(payload)
    assert restored.active_turn_token == token
    assert restored.active_turn_started_at is not None

    payload.pop("active_turn_token")
    payload.pop("active_turn_started_at")
    legacy = SessionEntry.from_dict(payload)
    assert legacy.active_turn_token is None
    assert legacy.active_turn_started_at is None

    payload["active_turn_token"] = {"invalid": "not-a-token"}
    payload["active_turn_started_at"] = datetime.now().isoformat()
    corrupt = SessionEntry.from_dict(payload)
    assert corrupt.active_turn_token is None
    assert corrupt.active_turn_started_at is None


def test_mark_refreshes_updated_at_for_legacy_upgrade_fallback(tmp_path):
    store = _make_store(tmp_path)
    source = _make_source()
    entry = store.get_or_create_session(source)
    old_updated_at = datetime.now() - timedelta(hours=2)
    with store._lock:
        store._entries[entry.session_key].updated_at = old_updated_at

    token = store.mark_turn_active(entry.session_key)

    assert token is not None
    assert _entry_for(store, source).updated_at > old_updated_at


def test_active_turn_clear_is_compare_and_swap(tmp_path):
    store = _make_store(tmp_path)
    source = _make_source()
    entry = store.get_or_create_session(source)

    first = store.mark_turn_active(entry.session_key)
    second = store.mark_turn_active(entry.session_key)
    assert first is not None
    assert second is not None
    assert first != second

    assert store.clear_turn_active(entry.session_key, first) is False
    assert _entry_for(store, source).active_turn_token == second

    assert store.clear_turn_active(entry.session_key, second) is True
    current = _entry_for(store, source)
    assert current.active_turn_token is None
    assert current.active_turn_started_at is None


def test_failed_mark_persistence_does_not_leak_marker_into_later_save(tmp_path):
    store = _make_store(tmp_path)
    source = _make_source()
    entry = store.get_or_create_session(source)
    real_save_entry = store._save_entry
    store._save_entry = MagicMock(side_effect=OSError("disk unavailable"))

    with pytest.raises(OSError, match="disk unavailable"):
        store.mark_turn_active(entry.session_key)

    current = _entry_for(store, source)
    assert current.active_turn_token is None
    assert current.active_turn_started_at is None

    # A later unrelated save must not make the failed marker durable.
    store._save_entry = real_save_entry
    with store._lock:
        store._entries[entry.session_key].updated_at = datetime.now()
        store._save()

    reloaded = _make_store(tmp_path)
    assert reloaded.recover_interrupted_turns() == 0
    assert _entry_for(reloaded, source).active_turn_token is None


def test_failed_clear_persistence_keeps_token_retryable_and_durable_clear_wins(tmp_path):
    store = _make_store(tmp_path)
    source = _make_source()
    entry = store.get_or_create_session(source)
    token = store.mark_turn_active(entry.session_key)
    assert token is not None

    real_save_entry = store._save_entry
    store._save_entry = MagicMock(side_effect=OSError("disk unavailable"))

    with pytest.raises(OSError, match="disk unavailable"):
        store.clear_turn_active(entry.session_key, token)

    current = _entry_for(store, source)
    assert current.active_turn_token == token
    assert current.active_turn_started_at is not None

    store._save_entry = real_save_entry
    assert store.clear_turn_active(entry.session_key, token) is True

    reloaded = _make_store(tmp_path)
    persisted = _entry_for(reloaded, source)
    assert persisted.active_turn_token is None
    assert persisted.active_turn_started_at is None


def test_state_db_failure_atomic_marker_round_trip(tmp_path):
    store = _make_db_store(tmp_path)
    source = _make_source("state-db-active-turn")
    entry = store.get_or_create_session(source)
    real_save_entry = store._save_entry

    store._save_entry = MagicMock(side_effect=OSError("state.db unavailable"))
    with pytest.raises(OSError, match="state.db unavailable"):
        store.mark_turn_active(entry.session_key)
    assert _entry_for(store, source).active_turn_token is None

    store._save_entry = real_save_entry
    token = store.mark_turn_active(entry.session_key)
    assert token is not None

    store._save_entry = MagicMock(side_effect=OSError("state.db unavailable"))
    with pytest.raises(OSError, match="state.db unavailable"):
        store.clear_turn_active(entry.session_key, token)
    assert _entry_for(store, source).active_turn_token == token

    store._save_entry = real_save_entry
    assert store.clear_turn_active(entry.session_key, token) is True
    _close_store_db(store)

    reloaded = _make_db_store(tmp_path)
    assert reloaded.recover_interrupted_turns() == 0
    persisted = _entry_for(reloaded, source)
    assert persisted.active_turn_token is None
    assert persisted.active_turn_started_at is None
    _close_store_db(reloaded)


def test_state_db_commit_survives_legacy_mirror_failure(tmp_path):
    store = _make_db_store(tmp_path)
    source = _make_source("state-db-mirror-failure")
    entry = store.get_or_create_session(source)
    db = store._db
    assert db is not None
    db.save_gateway_routing_entry = MagicMock(
        side_effect=OSError("fast upsert unavailable")
    )
    store._save_sessions_json = MagicMock(
        side_effect=OSError("legacy mirror unavailable")
    )

    token = store.mark_turn_active(entry.session_key)
    assert token is not None
    _close_store_db(store)

    reloaded = _make_db_store(tmp_path)
    recovered = _entry_for(reloaded, source)
    assert recovered.active_turn_token == token
    _close_store_db(reloaded)


def test_exact_old_active_turn_recovers_even_when_updated_at_is_stale(tmp_path):
    store = _make_store(tmp_path)
    source = _make_source()
    entry = store.get_or_create_session(source)
    token = store.mark_turn_active(entry.session_key)

    with store._lock:
        current = store._entries[entry.session_key]
        current.updated_at = datetime.now() - timedelta(hours=2)
        current.active_turn_started_at = datetime.now() - timedelta(minutes=10)
        store._save()

    # Prove the marker survives a fresh SessionStore and is not relying on the
    # in-memory object that wrote it.
    reloaded = _make_store(tmp_path)
    assert reloaded.recover_interrupted_turns(
        max_age_seconds=ACTIVE_TURN_MAX_AGE_SECONDS
    ) == 1

    recovered = _entry_for(reloaded, source)
    assert recovered.resume_pending is True
    assert recovered.resume_reason == "restart_interrupted"
    assert recovered.last_resume_marked_at is not None
    assert recovered.last_resume_marked_at > datetime.now() - timedelta(seconds=5)
    assert recovered.active_turn_token is None
    assert recovered.active_turn_started_at is None
    assert token


def test_suspended_active_turn_is_cleared_without_resume(tmp_path):
    store = _make_store(tmp_path)
    source = _make_source()
    entry = store.get_or_create_session(source)
    store.mark_turn_active(entry.session_key)

    with store._lock:
        store._entries[entry.session_key].suspended = True

    assert store.recover_interrupted_turns() == 0
    recovered = _entry_for(store, source)
    assert recovered.suspended is True
    assert recovered.resume_pending is False
    assert recovered.active_turn_token is None
    assert recovered.active_turn_started_at is None


def test_existing_resume_reason_and_freshness_are_preserved(tmp_path):
    store = _make_store(tmp_path)
    source = _make_source()
    entry = store.get_or_create_session(source)
    store.mark_turn_active(entry.session_key)
    original_mark = datetime.now() - timedelta(minutes=2)

    with store._lock:
        current = store._entries[entry.session_key]
        current.resume_pending = True
        current.resume_reason = "shutdown_timeout"
        current.last_resume_marked_at = original_mark

    assert store.recover_interrupted_turns() == 0
    recovered = _entry_for(store, source)
    assert recovered.resume_pending is True
    assert recovered.resume_reason == "shutdown_timeout"
    assert recovered.last_resume_marked_at == original_mark
    assert recovered.active_turn_token is None
    assert recovered.active_turn_started_at is None


def test_ancient_active_marker_is_cleared_without_auto_resume(tmp_path):
    store = _make_store(tmp_path)
    source = _make_source()
    entry = store.get_or_create_session(source)
    store.mark_turn_active(entry.session_key)

    with store._lock:
        current = store._entries[entry.session_key]
        current.active_turn_started_at = datetime.now() - timedelta(hours=2)

    assert store.recover_interrupted_turns(
        max_age_seconds=ACTIVE_TURN_MAX_AGE_SECONDS
    ) == 0
    recovered = _entry_for(store, source)
    assert recovered.resume_pending is False
    assert recovered.active_turn_token is None
    assert recovered.active_turn_started_at is None


def test_clean_startup_discards_orphan_markers_without_resuming(tmp_path):
    store = _make_store(tmp_path)
    source = _make_source()
    entry = store.get_or_create_session(source)
    store.mark_turn_active(entry.session_key)

    assert store.discard_active_turn_markers() == 1

    recovered = _entry_for(store, source)
    assert recovered.resume_pending is False
    assert recovered.active_turn_token is None
    assert recovered.active_turn_started_at is None


@pytest.mark.asyncio
async def test_clean_shutdown_marker_is_not_consumed_when_discard_fails(tmp_path):
    marker = tmp_path / ".clean_shutdown"
    marker.write_text("clean", encoding="utf-8")
    runner = object.__new__(GatewayRunner)
    async_store = MagicMock()
    async_store.discard_active_turn_markers = AsyncMock(
        side_effect=OSError("state store unavailable")
    )

    with patch.object(
        GatewayRunner,
        "async_session_store",
        new_callable=PropertyMock,
        return_value=async_store,
    ):
        with pytest.raises(OSError, match="state store unavailable"):
            await runner._consume_clean_shutdown_marker(marker)

    assert marker.exists()


@pytest.mark.asyncio
async def test_clean_shutdown_marker_is_unlinked_after_durable_discard(tmp_path):
    marker = tmp_path / ".clean_shutdown"
    marker.write_text("clean", encoding="utf-8")
    runner = object.__new__(GatewayRunner)
    async_store = MagicMock()
    async_store.discard_active_turn_markers = AsyncMock(return_value=2)

    with patch.object(
        GatewayRunner,
        "async_session_store",
        new_callable=PropertyMock,
        return_value=async_store,
    ):
        discarded = await runner._consume_clean_shutdown_marker(marker)

    assert discarded == 2
    assert not marker.exists()


@pytest.mark.asyncio
async def test_runner_active_turn_carrier_clears_the_exact_resolved_key():
    runner = object.__new__(GatewayRunner)
    runner.session_store = MagicMock()
    mark_active = AsyncMock(return_value="token-1")
    clear_active = AsyncMock(return_value=True)
    setattr(
        runner,
        "_async_session_store",
        SimpleNamespace(
            _store=runner.session_store,
            mark_turn_active=mark_active,
            clear_turn_active=clear_active,
        ),
    )
    event = SimpleNamespace()

    await runner._mark_durable_active_turn(
        cast(Any, event), "resolved-session-key"
    )

    assert event._gateway_active_turn_session_key == "resolved-session-key"
    assert event._gateway_active_turn_token == "token-1"

    await runner._clear_durable_active_turn(cast(Any, event))

    clear_active.assert_awaited_once_with("resolved-session-key", "token-1")
    assert not hasattr(event, "_gateway_active_turn_session_key")
    assert not hasattr(event, "_gateway_active_turn_token")


@pytest.mark.asyncio
async def test_runner_active_turn_clear_is_best_effort():
    runner = object.__new__(GatewayRunner)
    runner.session_store = MagicMock()
    clear_active = AsyncMock(
        side_effect=[OSError("disk unavailable"), True]
    )
    setattr(
        runner,
        "_async_session_store",
        SimpleNamespace(
            _store=runner.session_store,
            clear_turn_active=clear_active,
        ),
    )
    event = SimpleNamespace(
        _gateway_active_turn_session_key="resolved-session-key",
        _gateway_active_turn_token="token-1",
    )

    await runner._clear_durable_active_turn(cast(Any, event))

    assert clear_active.await_count == 2
    assert not hasattr(event, "_gateway_active_turn_session_key")
    assert not hasattr(event, "_gateway_active_turn_token")


@pytest.mark.asyncio
async def test_runner_active_turn_clear_stops_after_bounded_retries():
    runner = object.__new__(GatewayRunner)
    runner.session_store = MagicMock()
    clear_active = AsyncMock(side_effect=OSError("disk unavailable"))
    setattr(
        runner,
        "_async_session_store",
        SimpleNamespace(
            _store=runner.session_store,
            clear_turn_active=clear_active,
        ),
    )
    event = SimpleNamespace(
        _gateway_active_turn_session_key="resolved-session-key",
        _gateway_active_turn_token="token-1",
    )

    assert await runner._clear_durable_active_turn(cast(Any, event)) is False

    assert clear_active.await_count == 3
    assert not hasattr(event, "_gateway_active_turn_session_key")
    assert not hasattr(event, "_gateway_active_turn_token")


def _db_runner(tmp_path) -> tuple[GatewayRunner, SessionStore]:
    runner = object.__new__(GatewayRunner)
    runner.session_store = _make_db_store(tmp_path)
    return runner, runner.session_store


def _turn(store: SessionStore, chat_id: str, *, marked: bool, reply: str | None, **prompt: Any) -> SessionSource:
    source = _make_source(chat_id)
    entry = store.get_or_create_session(source)
    if marked:
        store.mark_turn_active(entry.session_key)
    store.append_to_transcript(entry.session_id, {"role": "user", "content": f"question {chat_id}", **prompt})
    if reply is not None:
        store.append_to_transcript(entry.session_id, {"role": "assistant", "content": reply})
    return source


@pytest.mark.asyncio
async def test_unclean_restart_resumes_only_the_turn_left_in_flight(tmp_path):
    """A kill re-arms the marked turn that had no reply yet, never a chat whose turn finished just
    before it (the removed 120 s recency sweep re-answered every recently active chat)."""
    runner, store = _db_runner(tmp_path)
    finished = _turn(store, "finished", marked=False, reply="answered and delivered")
    in_flight = _turn(store, "in-flight", marked=True, reply=None)

    assert await runner._recover_unclean_sessions() == (1, 0)

    assert not _entry_for(store, finished).resume_pending
    resumed = _entry_for(store, in_flight)
    assert (resumed.resume_pending, resumed.resume_reason) == (True, "restart_interrupted")
    _close_store_db(store)


@pytest.mark.asyncio
async def test_unclean_restart_delivers_a_persisted_unledgered_reply_instead_of_regenerating(tmp_path):
    """Killed after the reply was persisted but before it reached the delivery ledger: the stored
    reply is ledgered for this boot's sweep (marked, it may already be on screen), not resumed."""
    from gateway.delivery_ledger import sweep_recoverable

    runner, store = _db_runner(tmp_path)
    source = _turn(store, "replied", marked=True, reply="the stored answer")

    assert await runner._recover_unclean_sessions() == (0, 1)

    entry = _entry_for(store, source)
    assert (entry.resume_pending, entry.active_turn_token) == (False, None)
    rows = sweep_recoverable(deliverable_platforms={"discord"})
    assert [(r["content"], r["needs_marker"], r["chat_id"], r["thread_id"]) for r in rows] == [
        ("the stored answer", True, "replied", "thread-1")]
    _close_store_db(store)


@pytest.mark.asyncio
@pytest.mark.parametrize("owner_alive", [True, False], ids=["living-drainer", "verified-dead-owner"])
@pytest.mark.parametrize("reply", [None, "the owned final"], ids=["unfinished", "persisted-final"])
async def test_overlap_recovery_preserves_living_owner_and_never_retries_cut_work(tmp_path, monkeypatch, owner_alive, reply):
    """Absence of a clean receipt is not death proof during overlap startup."""
    import subprocess
    import sys
    from gateway.delivery_ledger import sweep_recoverable
    from gateway.generation import GenerationCoordinator, GenerationIdentity
    from gateway.status import _get_process_start_time

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    runner, store = _db_runner(tmp_path)
    runner.config = GatewayConfig(overlap_handover_enabled=True)
    source = _turn(store, "owned", marked=True, reply=reply)
    entry = _entry_for(store, source)
    token = entry.active_turn_token
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        started = _get_process_start_time(child.pid)
        assert started is not None
        db = GenerationCoordinator(tmp_path)
        old = GenerationIdentity.create(release_sha="old", label="old", pid=child.pid,
            start_fingerprint=f"{child.pid}:{started}")
        db.register(old, state="draining")
        assert db.claim_session(str(tmp_path), "discord", entry.session_key, old.id, 1, outstanding_work=1)
        if not owner_alive:
            child.terminate()
            child.wait(timeout=5)
        # A second, unowned crash must still recover while A is alive.
        unowned = _turn(store, "unowned", marked=True, reply=None)
        resumed, ledgered = await runner._recover_unclean_sessions()
        assert (resumed, ledgered) == (1, int(not owner_alive and reply is not None))
        assert _entry_for(store, unowned).resume_pending
        assert not entry.resume_pending, "cut owned work must never replay on the successor"
        assert entry.active_turn_token == (None if not owner_alive and reply is not None else token)
        rows = sweep_recoverable(deliverable_platforms={"discord"})
        assert [r["content"] for r in rows] == ([] if owner_alive or reply is None else [reply])
        # A fresh reader must see the same preserved marker: recovery of another
        # lane must not bulk-save over the living drainer's state.
        reloaded = _make_db_store(tmp_path)
        assert _entry_for(reloaded, source).active_turn_token == entry.active_turn_token
        _close_store_db(reloaded)
    finally:
        if child.poll() is None:
            child.terminate()
            child.wait(timeout=5)
        _close_store_db(store)


@pytest.mark.asyncio
@pytest.mark.parametrize("unknown_start", [False, True], ids=["known-identity", "unknown-start-is-not-death"])
@pytest.mark.parametrize("save_failure", [None, "unavailable", "failed"], ids=["saved", "no-scoped-writer", "scoped-write-failed"])
async def test_overlap_clean_receipt_cannot_clear_a_living_owners_newer_marker(tmp_path, monkeypatch, unknown_start, save_failure):
    from gateway.generation import GenerationCoordinator, GenerationIdentity
    from gateway.status import _get_process_start_time

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    config = GatewayConfig(overlap_handover_enabled=True)
    old_store = _make_db_store(tmp_path)
    source = _turn(old_store, "living", marked=True, reply="old final")
    unowned = _turn(old_store, "orphan", marked=True, reply=None)
    entry = _entry_for(old_store, source)
    old_store.mark_resume_pending(entry.session_key, reason="shutdown_timeout")
    started = _get_process_start_time(os.getpid())
    assert started is not None
    db = GenerationCoordinator(tmp_path)
    old = GenerationIdentity.create(release_sha="old", label="old",
        start_fingerprint=f"{os.getpid()}:{started}")
    # Terminal metadata and stale heartbeats must not authorize recovery either.
    db.register(old, state="exited")
    assert db.claim_session(str(tmp_path), "discord", entry.session_key, old.id, 1, outstanding_work=1)
    with db.connect() as conn:
        conn.execute("UPDATE generations SET heartbeat_at=0,verdict='failed',"
                     "verdict_at=0,verdict_evidence='fixture_terminal_metadata' WHERE id=?", (old.id,))
    runner, store = _db_runner(tmp_path)
    runner.config = config
    stale_token = _entry_for(store, source).active_turn_token
    fresh_token = old_store.mark_turn_active(entry.session_key)
    assert fresh_token != stale_token
    if unknown_start:
        monkeypatch.setattr("gateway.status._get_process_start_time", lambda pid: None)
    marker = tmp_path / ".clean_shutdown"
    marker.write_text("an older clean receipt", encoding="utf-8")
    try:
        if save_failure:
            if save_failure == "unavailable":
                monkeypatch.setattr(store, "_routing_db_method", lambda name: None)
            else:
                real_method = store._routing_db_method
                monkeypatch.setattr(store, "_routing_db_method", lambda name: (
                    MagicMock(side_effect=OSError("disk unavailable"))
                    if name == "save_gateway_routing_entry" else real_method(name)))
            with pytest.raises(RuntimeError, match="full rewrite would clobber a live owner"):
                await runner._consume_clean_shutdown_marker(marker)
            assert marker.exists(), "failed scoped cleanup must not consume the receipt"
        else:
            assert await runner._consume_clean_shutdown_marker(marker) == 1
            assert not marker.exists()
        assert _entry_for(store, source).active_turn_token == stale_token
        assert _entry_for(store, unowned).active_turn_token is None
        assert runner._resume_pending_candidates(record_boot=False) == []
        reloaded = _make_db_store(tmp_path)
        assert _entry_for(reloaded, source).active_turn_token == fresh_token
        assert _entry_for(reloaded, source).resume_pending
        _close_store_db(reloaded)
    finally:
        _close_store_db(store)
        _close_store_db(old_store)


_WAKE = {"display_kind": "internal_notification"}


@pytest.mark.asyncio
@pytest.mark.parametrize(("reply", "prompt", "owed"), [
    ("[SILENT]", _WAKE, []),
    ("NO_REPLY", _WAKE, []),
    ("disk is 91% full", {**_WAKE, "display_metadata": {"notification_category": "diagnostic"}}, []),
    ("NO_REPLY", {}, ["⚠️ The model returned only a silence marker for a message that needed a reply. "
                      "Try again or rephrase."]),
    ("NO_REPLY", {"display_metadata": {"reply_expected": False}}, []),
])
async def test_unclean_restart_never_redelivers_a_reply_live_delivery_suppressed(tmp_path, reply, prompt, owed):
    """A crash-left reply is owed exactly what live delivery would have sent: nothing for a silence
    marker on a machinery turn, a muted diagnostic wake or a message the adapter reported as not
    addressed to the bot (and the finished turn is not resumed), the unexpected-silence notice for
    any other human turn, never the raw marker."""
    from gateway.delivery_ledger import sweep_recoverable

    (Path(os.environ["HERMES_HOME"]) / "config.yaml").write_text("display: {suppress_warning_notifications: true}\n", encoding="utf-8")
    runner, store = _db_runner(tmp_path)
    source = _turn(store, "quiet", marked=True, reply=reply, **prompt)

    assert await runner._recover_unclean_sessions() == (0, len(owed))

    entry = _entry_for(store, source)
    assert (entry.resume_pending, entry.active_turn_token) == (False, None)
    assert [r["content"] for r in sweep_recoverable(deliverable_platforms={"discord"})] == owed
    _close_store_db(store)


@pytest.mark.asyncio
@pytest.mark.skipif(not hasattr(time, "tzset"), reason="needs a POSIX process timezone switch")
async def test_turn_marker_start_survives_a_timezone_change_across_the_crash(tmp_path):
    """The dead process's local zone is not the new one's (DST, container vs unit TZ): the marked
    turn still resumes, and the previous turn's answer persisted a minute before it is not re-sent
    (a naive wall-clock marker read in the new zone was 7 h off and dropped the turn as stale)."""
    from gateway.delivery_ledger import sweep_recoverable

    runner, store = _db_runner(tmp_path)
    source = _make_source("tz")
    entry = store.get_or_create_session(source)
    store.append_to_transcript(entry.session_id, {"role": "user", "content": "earlier question"})
    store.append_to_transcript(entry.session_id, {"role": "assistant", "content": "earlier answer",
                                                  "timestamp": time.time() - 60})
    original_tz = os.environ.get("TZ")
    try:
        os.environ["TZ"] = "Etc/GMT+7"  # UTC-7 when the turn starts ...
        time.tzset()
        store.mark_turn_active(entry.session_key)
        os.environ["TZ"] = "UTC"  # ... UTC when the gateway comes back
        time.tzset()
        assert await runner._recover_unclean_sessions() == (1, 0)
    finally:
        if original_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = original_tz
        time.tzset()

    assert _entry_for(store, source).resume_pending is True
    assert sweep_recoverable(deliverable_platforms={"discord"}) == []
    _close_store_db(store)


@pytest.mark.asyncio
@pytest.mark.parametrize("reason", ["shutdown_timeout", "restart_timeout"])
@pytest.mark.parametrize("successor", [False, True])
async def test_cleanly_released_claim_does_not_block_later_recovery(tmp_path, monkeypatch, reason, successor):
    from gateway.generation import GenerationCoordinator, GenerationIdentity

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    runner, store = _db_runner(tmp_path)
    runner.config = GatewayConfig(overlap_handover_enabled=True)
    source = _turn(store, "released", marked=True, reply=None)
    entry = _entry_for(store, source)
    store.mark_resume_pending(entry.session_key, reason=reason)
    coordinator = GenerationCoordinator(tmp_path)
    old = GenerationIdentity.create(release_sha="old", label="old", pid=456789)
    coordinator.register(old, state="exited")
    coordinator.claim_session(str(tmp_path), "discord", entry.session_key, old.id, 1)
    monkeypatch.setattr("gateway.status._pid_exists", lambda pid: pid != old.pid)
    if successor:
        from gateway.status import _get_process_start_time
        current = GenerationIdentity.create(release_sha="current", label="current",
            start_fingerprint=f"{os.getpid()}:{_get_process_start_time(os.getpid())}")
        coordinator.register(current, state="serving")
        coordinator.acquire_lease("active_generation", current.id)
    coordinator.release_exited_owner(old.id)
    marker = tmp_path / ".clean_shutdown"
    marker.write_text("clean", encoding="utf-8")
    try:
        if successor:
            # Reassignment is not release by the current living owner. Preserve
            # it even at zero work: startup cannot prove that owner is finished.
            assert await runner._consume_clean_shutdown_marker(marker) == 0
            assert runner._resume_pending_candidates(record_boot=False) == []
            assert entry.active_turn_token is not None
            return
        assert await runner._consume_clean_shutdown_marker(marker) == 1
        assert runner._resume_pending_candidates(record_boot=False) == [entry]
        # A later, unrelated crash in this same lane is not a historical owner's cut.
        store.clear_resume_pending(entry.session_key)
        store.mark_turn_active(entry.session_key)
        assert await runner._recover_unclean_sessions() == (1, 0)
        assert entry.resume_pending
        assert entry.active_turn_token is None
    finally:
        _close_store_db(store)


@pytest.mark.asyncio
@pytest.mark.parametrize("claim", ["live", "dead-unfinished", "dead-interrupted", "dead-ambiguous", "dead-pending", "interrupted", "other-home", "other-transport"])
async def test_recovery_fence_matches_current_claim_scope_and_cut_evidence(tmp_path, monkeypatch, claim):
    from gateway.generation import GenerationCoordinator, GenerationIdentity
    from gateway.status import _get_process_start_time

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    runner, store = _db_runner(tmp_path)
    runner.config = GatewayConfig(overlap_handover_enabled=True)
    source = _turn(store, "scoped", marked=True, reply=None)
    entry = _entry_for(store, source)
    token = entry.active_turn_token
    coordinator = GenerationCoordinator(tmp_path)
    owner = GenerationIdentity.create(release_sha="owner", label="owner",
        start_fingerprint=f"{os.getpid()}:{_get_process_start_time(os.getpid())}")
    coordinator.register(owner, state="exited" if claim == "dead-unfinished" else "serving")
    if claim == "dead-unfinished":
        with coordinator.connect() as db:
            db.execute("UPDATE generations SET verdict='failed',verdict_at=heartbeat_at,"
                       "verdict_evidence='fixture_dead_unfinished' WHERE id=?", (owner.id,))
    home = str(tmp_path / "other") if claim == "other-home" else str(tmp_path)
    transport = "telegram" if claim == "other-transport" else "discord"
    coordinator.claim_session(home, transport, entry.session_key, owner.id, 1, outstanding_work=1)
    if claim.startswith("dead-"):
        monkeypatch.setattr("gateway.status._pid_exists", lambda pid: False)
    if claim in {"interrupted", "dead-interrupted"}:
        # The cut tombstone survives even after a released claim moves to a live owner.
        with coordinator.connect() as db:
            db.execute("UPDATE sessions SET state='interrupted',outstanding_work=0")
    if claim == "dead-ambiguous":
        with coordinator.connect() as db:
            db.execute("UPDATE sessions SET outstanding_work=0")
            # A previous-release writer still publishes its legacy retirement state.
            db.execute("UPDATE generations SET state='failed'")
            retired = db.execute("SELECT state,verdict FROM generations WHERE id=?", (owner.id,)).fetchone()
            assert (retired["state"], retired["verdict"]) == ("exited", "failed")
    if claim == "dead-pending":
        epoch = coordinator.acquire_lease("active_generation", owner.id)
        coordinator.enqueue(home, transport, entry.session_key, "pending", "message",
                            b'{"version":1,"authorized":true,"sender":"user"}', b"work", owner.id, epoch)
        with coordinator.connect() as db:
            db.execute("UPDATE sessions SET outstanding_work=0")
            db.execute("UPDATE generations SET state='exited'")
    try:
        expected = int(claim in {"other-home", "other-transport"})
        assert await runner._recover_unclean_sessions() == (expected, 0)
        assert entry.resume_pending == bool(expected)
        assert entry.active_turn_token == (None if expected else token)
        store.mark_resume_pending(entry.session_key, reason="shutdown_timeout")
        assert runner._resume_pending_candidates(record_boot=False) == ([entry] if expected else [])
    finally:
        _close_store_db(store)


def test_startup_owner_probe_uses_read_snapshot_without_blocking_coordinator_writes(tmp_path, monkeypatch):
    from gateway.generation import GenerationCoordinator, GenerationIdentity
    from gateway.owned_admission import OwnedAdmissionMixin
    from gateway.run_startup_recovery import startup_recovery_fences
    from gateway.status import _get_process_start_time

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    runner, store = _db_runner(tmp_path)
    runner.config = GatewayConfig(overlap_handover_enabled=True)
    source = _turn(store, "read-snapshot", marked=True, reply=None)
    entry = _entry_for(store, source)
    coordinator = GenerationCoordinator(tmp_path)
    owner = GenerationIdentity.create(release_sha="owner", label="owner",
        start_fingerprint=f"{os.getpid()}:{_get_process_start_time(os.getpid())}")
    coordinator.register(owner, state="serving")
    coordinator.claim_session(str(tmp_path), "discord", entry.session_key, owner.id, 1, outstanding_work=1)
    original = OwnedAdmissionMixin._owner_is_dead
    probed = []

    def probe(row):
        # OS identity lookup must not monopolize the single coordinator writer.
        with coordinator.connect() as db:
            db.execute("PRAGMA busy_timeout=50")
            db.execute("BEGIN IMMEDIATE")
            db.execute("UPDATE generations SET heartbeat_at=heartbeat_at WHERE id=?", (owner.id,))
            db.rollback()
        probed.append(row["id"])
        return original(row)

    monkeypatch.setattr(OwnedAdmissionMixin, "_owner_is_dead", staticmethod(probe))
    try:
        assert startup_recovery_fences(runner) == (frozenset({entry.session_key}), frozenset({entry.session_key}))
        assert probed == [owner.id]
    finally:
        _close_store_db(store)


def test_recovery_scope_follows_runtime_profile_home_a_b_a(tmp_path, monkeypatch):
    from dataclasses import replace
    from gateway.generation import GenerationCoordinator, GenerationIdentity
    from gateway.run_startup_recovery import startup_recovery_fences
    from gateway.status import _get_process_start_time

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    homes = {name: tmp_path / name for name in ("alpha", "beta")}
    for home in homes.values():
        home.mkdir()
    monkeypatch.setattr("hermes_cli.profiles.get_profile_dir", lambda name: homes.get(name, tmp_path))
    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda name: name in homes)
    runner, store = _db_runner(tmp_path)
    runner.config = store.config = GatewayConfig(overlap_handover_enabled=True, multiplex_profiles=True)
    coordinator = GenerationCoordinator(tmp_path)
    owner = GenerationIdentity.create(release_sha="owner", label="owner",
        start_fingerprint=f"{os.getpid()}:{_get_process_start_time(os.getpid())}")
    coordinator.register(owner, state="serving")
    try:
        for name in ("alpha", "beta", "alpha"):
            source = replace(_make_source("profile-lane"), profile=name)
            entry = store.get_or_create_session(source)
            # The same session key recorded for another home does not own this lane.
            coordinator.claim_session(str(homes["beta"]), "discord", entry.session_key,
                                      owner.id, 1, outstanding_work=1)
            live, unsafe = startup_recovery_fences(runner)
            assert (entry.session_key in live) == (name == "beta")
            assert (entry.session_key in unsafe) == (name == "beta")
            assert runner._resolve_profile_home_for_source(source) == homes[name]
    finally:
        _close_store_db(store)
