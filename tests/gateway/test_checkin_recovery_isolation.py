"""One unreadable provenance store must not strand other profiles' owed replies."""

import asyncio
import sqlite3
from contextlib import asynccontextmanager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway import delivery_ledger
from gateway.config import Platform
from gateway.platforms.base import SendResult
from gateway.run import _profile_runtime_scope
from gateway.run_startup import GatewayStartupMixin
from gateway.session import SessionSource
from hermes_cli.checkins import CheckinConfig
from hermes_state import SessionDB


@pytest.fixture
def recovery_batch(tmp_path, monkeypatch):
    homes = {name: tmp_path / name for name in ("alpha", "beta")}
    dbs = {}
    for name, home in homes.items():
        home.mkdir()
        dbs[name] = SessionDB(db_path=home / "state.db")
    monkeypatch.setattr(delivery_ledger, "_db_path", lambda: tmp_path / "ledger.db")
    monkeypatch.setattr(CheckinConfig, "load", classmethod(lambda cls: CheckinConfig(enabled=False)))
    started = (datetime.now(UTC) - timedelta(seconds=10)).timestamp()
    entries, histories, rows = {}, {}, []
    for kind in ("unavailable", "ordinary", "human", "complete", "pending"):
        profile = "alpha" if kind == "unavailable" else "beta"
        source = SessionSource(platform=Platform.TELEGRAM, chat_id=kind, profile=profile)
        route, token = f"agent:{profile}:telegram:dm:{kind}", f"turn-{kind}"
        db = dbs[profile]
        db.create_session(kind, source="telegram", session_key=route)
        metadata = {"checkin_turn_id": token, "checkin_root": kind,
                    "checkin_pending_root": kind if kind == "pending" else None}
        supervisor = kind in {"human", "complete", "pending"}
        db.append_message(kind, role="user", content="inspect" if supervisor else "question",
                          timestamp=started, display_kind="supervisor_checkin" if supervisor else None,
                          display_metadata=metadata if supervisor else None)
        db.append_message(kind, role="assistant", content=f"answer-{kind}", timestamp=started + 1)
        entries[route] = SimpleNamespace(session_id=kind, origin=source)
        histories[kind] = db.get_messages(kind)
        rows.append({"obligation_id": f"owed-{kind}", "session_key": route, "platform": "telegram",
                     "profile": profile, "chat_id": kind, "thread_id": None, "content": f"answer-{kind}",
                     "resume_turn_id": token, "attempts": 1})
    runner = object.__new__(GatewayStartupMixin)
    runner.session_store = SimpleNamespace(
        _db_for_key=lambda route: dbs[entries[route].origin.profile],
        lookup_by_session_key=entries.get)
    runner._run_in_executor_with_context = asyncio.to_thread
    runner._restored_source = lambda entry: entry.origin

    @asynccontextmanager
    async def scope(source):
        with _profile_runtime_scope(homes[source.profile], hydrate_secrets=False):
            yield

    runner._async_profile_scope_for_source = scope
    runner._media_delivery_scope_for_source = lambda source: _profile_runtime_scope(
        homes[source.profile], hydrate_secrets=False)
    adapter = SimpleNamespace(send=AsyncMock(return_value=SendResult(success=True, message_id="ack")))
    runner._obligation_adapter = AsyncMock(return_value=adapter)
    runner._redelivery_restart_note_event = AsyncMock(return_value=None)
    runner._arm_flood_timers_for_waiting_rows = AsyncMock()
    active = {row["session_key"]: row for row in rows}
    runner._snapshot_active_turns_for_recovery = lambda: [
        (route, entries[route].session_id, row["resume_turn_id"], datetime.fromtimestamp(started, UTC),
         entries[route].origin, row["profile"]) for route, row in active.items()]

    async def clear(route, token):
        assert active[route]["resume_turn_id"] == token
        del active[route]
        return True

    runner.async_session_store = SimpleNamespace(
        load_transcript=AsyncMock(side_effect=lambda sid: histories[sid]),
        clear_turn_active=AsyncMock(side_effect=clear),
        # Fork per-room silence (33cf2037) reads the room decision before judging the reply.
        get_session_metadata=AsyncMock(return_value={}),
        recover_interrupted_turns=AsyncMock(side_effect=lambda **_: len(active)))
    try:
        yield SimpleNamespace(runner=runner, adapter=adapter, rows=rows, dbs=dbs, active=active)
    finally:
        for db in dbs.values():
            db.close()


def _ledger_rows():
    with delivery_ledger._connect() as conn:
        return {row[0]: (row[1], row[2]) for row in conn.execute(
            "SELECT session_key, state, content FROM delivery_obligations")}


@pytest.mark.asyncio
@pytest.mark.parametrize("lane", ["ledger", "crash"])
@pytest.mark.parametrize("fault", ["missing-db", "sqlite-read"])
async def test_unavailable_provenance_does_not_abort_following_profile(recovery_batch, monkeypatch, lane, fault):
    f = recovery_batch
    first = f.rows[0]
    original_lookup = f.runner.session_store._db_for_key
    original_read = f.dbs["alpha"]._read_all
    if fault == "missing-db":
        f.runner.session_store._db_for_key = lambda route: None if route == first["session_key"] else original_lookup(route)
    else:
        def unreadable(*args, **kwargs):
            raise sqlite3.OperationalError("transient provenance read failure")
        monkeypatch.setattr(f.dbs["alpha"], "_read_all", unreadable)

    if lane == "ledger":
        for row in f.rows:
            delivery_ledger.record_obligation(
                obligation_id=row["obligation_id"], session_key=row["session_key"], platform=row["platform"],
                chat_id=row["chat_id"], thread_id=None, content=row["content"],
                adapter_profile=row["profile"], resume_turn_id=row["resume_turn_id"])
            delivery_ledger.mark_attempting(row["obligation_id"])
        await f.runner._redeliver_claimed_obligations(f.rows)
        f.runner._arm_flood_timers_for_waiting_rows.assert_awaited_once()
        sent = [call.kwargs["content"] for call in f.adapter.send.await_args_list]
        assert [text for text in sent if text != first["content"]] == [
            "answer-ordinary", "answer-human", "answer-complete"]
    else:
        await f.runner._ledger_crash_left_replies(7200)
        assert all(row["session_key"] not in f.active for row in f.rows[1:])
    states = _ledger_rows()
    for row in f.rows[1:4]:
        assert states[row["session_key"]][1] == row["content"]
        assert states[row["session_key"]][0] == ("delivered" if lane == "ledger" else "attempting")
    pending = f.rows[-1]
    if lane == "ledger":
        assert states[pending["session_key"]][0] == "abandoned"
    else:
        assert pending["session_key"] not in states
    if lane == "ledger":
        # Uncertainty can defer the first row, but must not discard its owed reply.
        assert states[first["session_key"]][0] in {"pending", "attempting", "failed", "delivered"}
        assert (states[first["session_key"]][0] == "delivered") == (first["content"] in sent)

    # A missing DB may use ordinary-final fallback. Crash-left recovery still
    # settles a saved final reply when policy inspection fails.
    if fault == "sqlite-read":
        if lane == "ledger":
            assert first["content"] not in sent
            assert states[first["session_key"]][0] in {"pending", "attempting", "failed"}
        else:
            assert first["session_key"] not in f.active
            assert states[first["session_key"]][1] == first["content"]
    else:
        if lane == "ledger":
            assert first["content"] in sent
        else:
            assert states[first["session_key"]][1] == first["content"]
    f.runner.session_store._db_for_key = original_lookup
    monkeypatch.setattr(f.dbs["alpha"], "_read_all", original_read)
    if lane == "ledger":
        if states[first["session_key"]][0] != "delivered":
            await f.runner._redeliver_claimed_obligations([first])
        assert [call.kwargs["content"] for call in f.adapter.send.await_args_list].count(first["content"]) == 1
        assert _ledger_rows()[first["session_key"]] == ("delivered", first["content"])
    else:
        await f.runner._ledger_crash_left_replies(7200)
        assert not f.active
        assert _ledger_rows()[first["session_key"]][1] == first["content"]


@pytest.mark.asyncio
async def test_startup_recovery_ledgers_saved_reply_before_resume_sweep(recovery_batch, monkeypatch):
    f = recovery_batch
    first = f.rows[0]

    def unreadable(*args, **kwargs):
        raise sqlite3.OperationalError("transient provenance read failure")

    monkeypatch.setattr(f.dbs["alpha"], "_read_all", unreadable)
    resumed, ledgered = await f.runner._recover_unclean_sessions()

    assert resumed == 0 and ledgered == 4
    f.runner.async_session_store.recover_interrupted_turns.assert_awaited_once()
    assert first["session_key"] not in f.active
    assert _ledger_rows()[first["session_key"]][1] == first["content"]
