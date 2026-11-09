"""Root-scoped admission and disarm through the production delivery/replay owners."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway import run_checkins
from gateway.platforms.base import SendResult, _thread_metadata_for_event, _thread_metadata_for_source
from gateway.platforms.event import MessageEvent
from gateway.session_results import execution_result
from hermes_cli.checkins import CheckinConfig, CheckinManager
from hermes_cli.config import atomic_config_replace
from hermes_constants import get_hermes_home
from tests.gateway import test_supervisor_checkins as supervisor_fixtures

fixture = supervisor_fixtures.fixture

_LOAD_CONFIG = CheckinConfig.load.__func__


def configure(monkeypatch, **settings):
    monkeypatch.setattr(CheckinConfig, "load", classmethod(_LOAD_CONFIG))
    atomic_config_replace(get_hermes_home() / "config.yaml", {"checkins": settings})


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", [None, [], ["owner"], ["other"], "owner", {}, [""], ["owner", 7], "invalid-yaml", "invalid-section"])
async def test_scope_admission_execution_and_hot_idle_rearm(fixture, tmp_path, monkeypatch, scope):
    f = fixture
    settings = {} if scope is None else {"only_roots": scope}
    if scope == "invalid-yaml":
        configure(monkeypatch)
        assert CheckinConfig.load().enabled
        (get_hermes_home() / "config.yaml").write_text("checkins: [unterminated\n")
    elif scope == "invalid-section":
        monkeypatch.setattr(CheckinConfig, "load", classmethod(_LOAD_CONFIG))
        atomic_config_replace(get_hermes_home() / "config.yaml", {"checkins": "invalid"})
    else:
        configure(monkeypatch, **settings)
    allowed = scope is None or scope == [] or scope == ["owner"]
    await run_checkins.record_visible(f.runner, f.adapter, "42", "3")
    await run_checkins.poll_checkins(f.runner)
    assert bool(f.manager.state) is allowed
    f.clock.now = 1300
    await run_checkins.poll_checkins(f.runner)
    assert f.authority.admit_automation.await_count == int(allowed)
    stale = MessageEvent(text="tick", source=f.source, internal=True,
                         metadata={"checkin": ["owner", 1300], "display_kind": "supervisor_checkin"})
    if allowed:
        # Remove scope after admission but before any attempt fence has been spent.
        configure(monkeypatch, only_roots=["other"])
        assert not await run_checkins.prepare_checkin(f.runner, stale, f.entry, start=True)
        configure(monkeypatch, **settings)
        await run_checkins.reconcile_turn(f.runner, f.source, f.entry.session_key)
    assert await run_checkins.prepare_checkin(f.runner, stale, f.entry, start=True) is allowed

    # Disarm an existing due state and its already-admitted descriptor without a human turn.
    if scope == "invalid-yaml":
        # A malformed fixture must be repaired before the guarded writer will replace it.
        (get_hermes_home() / "config.yaml").write_text("checkins: {}\n")
    configure(monkeypatch, only_roots=["other"])
    assert not await run_checkins.prepare_checkin(f.runner, stale, f.entry, start=True)
    await run_checkins.poll_checkins(f.runner)
    assert f.manager.state is None
    f.authority.admit_automation.reset_mock()

    # Real compression and a reopened DB preserve the root, never the physical child id.
    f.db.end_session("owner", "compression")
    f.db.create_session("compressed", source="telegram", parent_session_id="owner")
    f.entry.session_id = "compressed"
    await run_checkins.poll_checkins(f.runner)
    configure(monkeypatch, enabled=False, only_roots=["owner"])
    await run_checkins.poll_checkins(f.runner)
    configure(monkeypatch, enabled=True, only_roots=["owner"])
    await run_checkins.poll_checkins(f.runner)
    assert f.manager.state is not None  # The same idle session was already scanned while disabled.
    due = f.manager.state.due_at
    f.clock.now = due
    await run_checkins.poll_checkins(f.runner)
    event = f.authority.admit_automation.await_args.args[1]
    assert event.metadata["checkin"] == ["owner", due]
    assert event.source.thread_id == "3"
    from hermes_state import SessionDB
    reopened = SessionDB(db_path=f.db.db_path)
    try:
        assert CheckinManager(reopened, "compressed", CheckinConfig.load()).root == "owner"
        assert CheckinManager(reopened, "compressed", CheckinConfig.load()).state.due_at == due
    finally:
        reopened.close()
    configure(monkeypatch, only_roots=["compressed"])
    assert not await run_checkins.prepare_checkin(f.runner, event, f.entry, start=True)
    if scope is None:
        await _assert_source_profiles(fixture, tmp_path, monkeypatch)


@pytest.mark.asyncio
@pytest.mark.parametrize("disarm", ["disable", "remove"])
@pytest.mark.parametrize("terminal", ["pending", "human", "complete", "unknown", "failed"])
@pytest.mark.parametrize("lane", ["normalization", "transport", "outbox", "ledger", "crash", "local", "handler", "voice"])
async def test_inflight_disarm_fences_pending_delivery_and_replay(fixture, monkeypatch, disarm, terminal, lane):
    f = fixture
    configure(monkeypatch, only_roots=["owner"])
    await run_checkins.reconcile_turn(f.runner, f.source, f.entry.session_key)
    f.clock.now = 1300
    await run_checkins.poll_checkins(f.runner)
    event = f.authority.admit_automation.await_args.args[1]
    event._gateway_active_turn_token = "exact-turn"
    assert await run_checkins.prepare_checkin(f.runner, event, f.entry, start=True)
    from gateway.response_filters import display_metadata_for_event
    f.db.append_message("owner", role="user", content=event.text, timestamp=1300,
                        display_kind="supervisor_checkin", display_metadata=display_metadata_for_event(event))
    f.db.end_session("owner", "compression")
    f.db.create_session("compressed", source="telegram", parent_session_id="owner")
    f.entry.session_id = "compressed"
    result = {"completed": True, "final_response": "The work is still pending.", "messages": []}
    result.update({"human": {"queued_terminal_display_kind": None, "final_response": "Here is the answer you requested."},
                   "complete": {"final_response": "Verified the last result. The work is complete."},
                   "failed": {"failed": True, "completed": False}}.get(terminal, {}))
    if terminal == "complete":
        f.work.clear()
    if terminal == "unknown":
        def unavailable(*_):
            raise OSError("roster unavailable")
        monkeypatch.setattr(run_checkins, "outstanding_work", unavailable)
    keep = terminal in {"human", "complete"}
    text = result["final_response"]
    captured = {"result": dict(result)}
    token = execution_result.set(captured)
    def disarm_now():
        configure(monkeypatch, enabled=disarm != "disable",
                  only_roots=["other"] if disarm == "remove" else ["owner"])
    try:
        if lane == "normalization":
            disarm_now()
        normalized = await run_checkins.silence_fallback(f.runner, event, f.entry, result)
        assert bool(normalized["final_response"]) is (keep or lane != "normalization")
        if lane == "normalization":
            assert bool(captured["result"]["final_response"]) is keep
            return
        f.db.append_message(f.entry.session_id, role="assistant", content=text, timestamp=1301)
        sends = AsyncMock(return_value=SendResult(success=True, message_id="ack"))
        async def send(chat_id, content, reply_to=None, metadata=None):
            return await sends(chat_id, content, metadata=metadata)
        f.adapter.send = send
        f.adapter.gateway_runner = f.runner
        run_checkins.install_visible_observer(f.runner, f.adapter)
        metadata = _thread_metadata_for_event(event)
        disarm_now()  # Config changes after result shaping, before its actual transport/replay.
        f.runner._event_thread_metadata = lambda event, _: _thread_metadata_for_event(event)
        f.runner._thread_metadata_for_source = _thread_metadata_for_source
        if lane == "handler":
            from gateway.run_turn_persistence import GatewayTurnPersistenceMixin
            f.runner._hmwa_deliver_turn_response = AsyncMock(return_value=text)
            response = await GatewayTurnPersistenceMixin._deliver_turn_response_then_limit_offer(
                f.runner, event, f.source, f.entry, f.entry.session_key, 1, normalized, [], text, None, False)
            assert bool(response) is keep
            assert f.runner._hmwa_deliver_turn_response.await_count == int(keep)
            return
        if lane == "voice":
            from gateway.run_voice import GatewayVoiceMixin
            f.runner._get_guild_id = lambda _: None
            f.runner._reply_anchor_for_event = lambda _: None
            f.adapter.send_voice = AsyncMock(return_value=SendResult(success=True, message_id="voice"))
            await GatewayVoiceMixin._deliver_voice_reply(f.runner, event, ["fixture.ogg"])
            assert f.adapter.send_voice.await_count == int(keep)
            return
        if lane == "local":
            from gateway import session_settlement_recovery as settlement
            from gateway.session_results import prepare_result
            captured = {"result": normalized}
            committed = {}
            def commit(authority, live, ref, row, response, outcome, captured, target):
                target["settled"] = {"outcome": outcome}
                target["response"] = response
                committed.update(prepare_result(row, response, outcome, captured)[1])
            monkeypatch.setattr(settlement, "commit_and_publish", commit)
            monkeypatch.setattr("gateway.session_ingress_media.release_admission_media_later", lambda *_: None)
            monkeypatch.setattr("gateway.session_runtime_workers.track_mutation", lambda _, coro: asyncio.create_task(coro))
            await settlement.settle_with_retry(
                SimpleNamespace(runner=f.runner), SimpleNamespace(source=f.source), None,
                {"admission_id": "local", "payload": {"local_automation_v1": {"display_kind": "supervisor_checkin"}}},
                text, "completed", captured, committed)
            assert bool(committed["response"]) is keep
            if not keep:
                assert captured["result"]["final_response"] == ""
            return
        if lane == "transport":
            await f.adapter.send("42", text, metadata=metadata)
        elif lane == "outbox":
            from gateway.outbox import Outbox, recover
            store = Outbox(get_hermes_home())
            store.enqueue("original-turn", "send", {"chat_id": "42", "content": text, "metadata": metadata})
            await recover(store, f.adapter)
            assert store.pending() == []
        elif lane == "ledger":
            from gateway.run_startup import GatewayStartupMixin
            from gateway.delivery_ledger import record_obligation, mark_attempting
            record_obligation(obligation_id="owed", session_key=f.entry.session_key, platform="telegram",
                              chat_id="42", thread_id="3", content=text, resume_turn_id="exact-turn")
            mark_attempting("owed")
            f.runner._obligation_adapter = AsyncMock(return_value=f.adapter)
            f.runner._redelivery_restart_note_event = AsyncMock(return_value=None)
            f.runner._arm_flood_timers_for_waiting_rows = AsyncMock()
            await GatewayStartupMixin._redeliver_claimed_obligations(f.runner, [{
                "obligation_id": "owed", "session_key": f.entry.session_key, "platform": "telegram",
                "profile": "default", "chat_id": "42", "thread_id": "3", "content": text,
                "resume_turn_id": "exact-turn", "attempts": 1}])
            if not keep:
                from gateway import delivery_ledger
                with delivery_ledger._connect() as conn:
                    assert conn.execute("SELECT state FROM delivery_obligations WHERE obligation_id='owed'").fetchone()[0] == "abandoned"
        else:
            from datetime import UTC, datetime
            from gateway.run_startup import GatewayStartupMixin
            f.runner._snapshot_active_turns_for_recovery = lambda: [(
                f.entry.session_key, f.entry.session_id, "exact-turn", datetime.fromtimestamp(1300, UTC), f.source, "default")]
            f.runner.async_session_store = SimpleNamespace(
                load_transcript=AsyncMock(return_value=f.db.get_messages(f.entry.session_id)), clear_turn_active=AsyncMock(return_value=True))
            f.runner._crash_left_reply = lambda *_: text
            f.runner._crash_left_parts = lambda *_: [("crash:exact-turn", text)]
            monkeypatch.setattr("gateway.run_startup.time.time", lambda: 1400)
            ledgered = await GatewayStartupMixin._ledger_crash_left_replies(f.runner, 7200)
            assert bool(ledgered) is keep
            return
        assert sends.await_count == int(keep)
        if not keep:
            assert not getattr(f.runner, "_checkin_visible", {})
    finally:
        execution_result.reset(token)


async def _assert_source_profiles(fixture, tmp_path, monkeypatch):
    from pathlib import Path
    from contextlib import asynccontextmanager
    from agent.secret_scope import set_multiplex_active
    from gateway.run import _profile_runtime_scope
    from gateway.session import SessionSource
    f = fixture
    home = tmp_path / ".hermes"
    secondary = home / "profiles" / "secondary"
    secondary.mkdir(parents=True)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(CheckinConfig, "load", classmethod(_LOAD_CONFIG))
    for path, roots in [(home, ["owner"]), (secondary, ["other"])]:
        atomic_config_replace(path / "config.yaml", {"checkins": {"only_roots": roots}})
    @asynccontextmanager
    async def scope(source):
        with _profile_runtime_scope(secondary if source.profile else home, hydrate_secrets=False):
            yield
    f.runner._async_profile_scope_for_source = scope
    set_multiplex_active(True)
    try:
        await run_checkins.poll_checkins(f.runner)
        assert f.manager.state is not None
        f.source.profile = "secondary"
        await run_checkins.poll_checkins(f.runner)
        assert f.manager.state is None
        f.source.profile = None
        await run_checkins.poll_checkins(f.runner)
        assert f.manager.state is not None
        f.clock.now = f.manager.state.due_at
        await run_checkins.poll_checkins(f.runner)
        event = f.authority.admit_automation.await_args.args[1]
        await run_checkins.silence_fallback(f.runner, event, f.entry,
                                          {"completed": True, "final_response": "Pending"})
        from gateway.checkin_delivery import permits_metadata
        metadata = _thread_metadata_for_event(event)
        assert await permits_metadata(f.runner, metadata)
        from dataclasses import replace
        other = replace(event, source=SessionSource.from_dict({**event.source.to_dict(), "profile": "secondary"}))
        other._checkin_pending_root = "owner"
        assert not await permits_metadata(f.runner, _thread_metadata_for_event(other))
        assert await permits_metadata(f.runner, metadata)
    finally:
        set_multiplex_active(False)
