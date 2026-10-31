"""Strict loop ticks cannot cross a fresh-session reset or block later human input.

Use real authority admission, drains, reset callers and transcript-pin checks. Only model
execution and platform sends are stand-ins. Events below reproduce the producer race: the
scheduler claims an idle tick, then a human turn starts before its route wake is admitted.
"""
import asyncio

import pytest

from tests.waiting import async_wait_until


async def _drains_idle(authority, adapter=None):
    # Native ingress and /new guard cleanup run in adapter tasks too. The authority
    # alone can look idle before a buffered follower has even reached admission.
    await async_wait_until(
        lambda: all(live.task is None or live.task.done() for live in authority.sessions.values())
        and (adapter is None or not adapter._background_tasks),
        timeout=15, what="authority drains and native delivery tasks to settle")


def _tick(runner, source, key, sid):
    from hermes_cli.loops import LoopManager
    from hermes_cli.session_controls import loop_wake_metadata

    manager = LoopManager(session_id=sid)
    manager.set("WAKE", interval_seconds=300, times=3)
    generation = runner._current_session_run_generation(key)
    assert manager.fire_tick(generation) is not None
    state = manager.state
    assert state is not None and state.awaiting_response
    event = runner._synthetic_prompt_event(source, "WAKE", internal=True, reply_expected=False)
    event.metadata.update(loop_wake_metadata(sid, state))
    event.metadata.update(gateway_session_key=key, gateway_session_id=sid,
                          gateway_session_strict=True)
    tick = (state.created_at, state.ticks_fired)
    event.metadata.update(hermes_loop_wakeup=True, gateway_loop_tick=list(tick))
    event._gateway_loop_execution_fence = (sid, tick, generation, state.control_generation)
    return manager, event


@pytest.mark.asyncio
@pytest.mark.parametrize("reset,superseded", [("new", False), ("exhaustion", False), ("exhaustion", True)])
async def test_native_reset_cancels_strict_tick_and_unblocks_human(monkeypatch, reset, superseded, caplog):
    from gateway.config import GatewayConfig, Platform, PlatformConfig
    from gateway.platforms.base import SendResult
    from gateway.platforms.event import MessageEvent
    from gateway.run import GatewayRunner
    from gateway.session_authority import initialize_session_authority
    from gateway.session_automation import admit_route_wake
    from gateway.session_results import execution_result
    from gateway.session_runtime_workers import mutation_tasks
    from hermes_state_runtime import list_session_admissions
    from plugins.platforms.discord.adapter import DiscordAdapter

    monkeypatch.setenv("DISCORD_ALLOWED_USERS", "42")
    runner = GatewayRunner(GatewayConfig())
    adapter = DiscordAdapter(PlatformConfig(enabled=True, token="fixture-token", typing_indicator=False))

    async def send(*args, **kwargs):
        return SendResult(success=True, message_id="sent")

    monkeypatch.setattr(adapter, "send", send)
    adapter.gateway_runner = runner
    runner.adapters = {Platform.DISCORD: adapter}
    authority = await initialize_session_authority(runner, profile_id="default", instance_id="owner")
    runner._wire_adapter_handlers(adapter)
    source = adapter.build_source(chat_id="42", chat_type="dm", user_id="42")
    key = runner.session_store._generate_session_key(source)
    old = runner.session_store.get_or_create_session(source).session_id
    await runner._warm_goals_session_db("loop reset fixture")
    manager, tick = _tick(runner, source, key, old)
    running, finish = asyncio.Event(), asyncio.Event()
    executed, resolved = [], []

    class Agent:
        def interrupt(self, *args, **kwargs):
            pass

    async def turn(event, source, quick_key, run_generation):
        entry = await runner._hmwa_resolve_session(event, source)
        resolved.append((event.text, entry is not None, dict(event.metadata or {})))
        if entry is None:
            return None
        executed.append((event.text, entry[1].session_id))
        captured = execution_result.get()
        if event.text == "A":
            runner._session_state(quick_key).turn.agent = Agent()
            running.set()
            await finish.wait()
            if reset == "exhaustion":
                result = {"failed": True, "compression_exhausted": True, "messages": []}
                captured["result"] = result
                await runner._hmwa_compression_exhaustion_reset(
                    result, "context exhausted", entry[1], quick_key, source)
                return "context exhausted"
        captured["result"] = {"final_response": "reply", "messages": []}
        return "reply"

    monkeypatch.setattr(runner, "_handle_message_with_agent", turn)
    try:
        await adapter.handle_message(MessageEvent(text="A", source=source, message_id="A"))
        await asyncio.wait_for(running.wait(), 10)
        assert await admit_route_wake(runner, adapter, source, "WAKE", "loop-tick", event=tick)
        queued = list_session_admissions(authority.db, session_id=old, pending_only=False)
        wake = next(row for row in queued if row["payload"].get("native_text_v1", {}).get("synthetic"))
        assert wake["status"] == "queued"
        if superseded:
            # Cancelling an old descriptor must not release a newer definition's claim.
            manager.set("REPLACEMENT", interval_seconds=300, times=3)
            assert manager.fire_tick(runner._current_session_run_generation(key)) is not None
        if reset == "new":
            await adapter.handle_message(MessageEvent(text="/new", source=source, message_id="new"))
        finish.set()
        await _drains_idle(authority, adapter)
        new = runner.session_store.peek_session_id(key)
        assert new != old
        await adapter.handle_message(MessageEvent(text="B", source=source, message_id="B"))
        await _drains_idle(authority, adapter)
        rows = [row for sid in (old, new) for row in list_session_admissions(
            authority.db, session_id=sid, pending_only=False)]
        actual = next(row for row in rows if row["admission_id"] == wake["admission_id"])
        from hermes_cli.loops import load_loop
        state = load_loop(old)
        assert state is not None
        observed = dict(reset=reset, executed=executed, resolved=resolved,
                        wake=(actual["status"], actual["outcome"]),
                        admissions=[(row["payload"].get("text"), row["status"], row["outcome"]) for row in rows],
                        awaiting=state.awaiting_response, log=caplog.text)
        print("RESET_OBSERVED", observed)
        assert ("B", new) in executed, observed
        assert not any(text == "WAKE" for text, _ in executed), observed
        assert (actual["status"], actual["outcome"]) == ("terminal", "cancelled"), observed
        assert state.awaiting_response is superseded, observed
        assert len([row for row in rows if row["request_id"] == wake["request_id"]]) == 1, rows
        assert "admission_conflict" not in caplog.text, observed
        # A repeated reset-move observation is idempotent and cannot settle a replacement tick.
        from gateway.session_automation import move_route_wakes
        snapshot = state.to_json()
        await move_route_wakes(runner, key, old)
        after = load_loop(old)
        assert after is not None and after.to_json() == snapshot
    finally:
        finish.set()
        await _drains_idle(authority, adapter)
        await asyncio.gather(*mutation_tasks(authority))
        authority.db.close()
        runner.session_store.close_all_db_handles()


@pytest.mark.asyncio
@pytest.mark.parametrize("superseded", [False, True])
async def test_local_exhaustion_cancels_strict_tick_and_settles_claim(monkeypatch, tmp_path, caplog, superseded):
    from gateway.config import GatewayConfig
    from gateway.run import GatewayRunner
    from gateway.session_authority import initialize_session_authority
    from gateway.session_contract import Submission
    from gateway.session_controls import AuthorityConnection
    from gateway.session_local import create_local_session
    from gateway.session_results import execution_result
    from gateway.session_runtime_workers import mutation_tasks
    from hermes_constants import get_hermes_home
    from gateway import run
    from hermes_state_runtime import list_session_admissions

    (get_hermes_home() / "config.yaml").write_text(
        'platform_toolsets: {cli: []}\nmodel: {provider: custom, default: m, base_url: "http://127.0.0.1:9/v1"}\n',
        encoding="utf-8")
    monkeypatch.setattr(run, "_hermes_home", get_hermes_home())
    runner = GatewayRunner(GatewayConfig())
    authority = await initialize_session_authority(runner, profile_id="default", instance_id="owner")
    connection = AuthorityConnection(authority, object(), {"user_id": "owner"})
    ref = create_local_session(authority, connection.actor, dict(
        request_id="loop-reset", source="cli", cwd=str(tmp_path), toolsets=[], model="m"))
    live = authority.sessions[ref.session_id]
    source, key = live.source, live.route
    adapter = runner.adapters[source.platform]
    await runner._warm_goals_session_db("local loop reset fixture")
    manager, tick = _tick(runner, source, key, ref.session_id)
    running, finish = asyncio.Event(), asyncio.Event()
    executed, resolved = [], []

    async def turn(event, source, quick_key, run_generation):
        entry = await runner._hmwa_resolve_session(event, source)
        resolved.append((event.text, entry is not None, dict(event.metadata or {})))
        if entry is None:
            return None
        executed.append((event.text, entry[1].session_id))
        captured = execution_result.get()
        if event.text == "A":
            running.set()
            await finish.wait()
            result = {"failed": True, "compression_exhausted": True, "messages": []}
            captured["result"] = result
            await runner._hmwa_compression_exhaustion_reset(
                result, "context exhausted", entry[1], quick_key, source)
            return "context exhausted"
        captured["result"] = {"final_response": "reply", "messages": []}
        return "reply"

    monkeypatch.setattr(runner, "_handle_message_with_agent", turn)
    try:
        await authority.submit(connection.actor, Submission("A", ref, {"text": "A"}, "queue"))
        await asyncio.wait_for(running.wait(), 10)
        from gateway.session_automation import admit_route_wake
        assert await admit_route_wake(runner, adapter, source, "WAKE", "loop-tick", event=tick)
        rows = list_session_admissions(authority.db, session_id=ref.session_id, pending_only=False)
        wake = next(row for row in rows if "local_automation_v1" in row["payload"])
        assert wake["status"] == "queued"
        if superseded:
            manager.set("REPLACEMENT", interval_seconds=300, times=3)
            assert manager.fire_tick(runner._current_session_run_generation(key)) is not None
        finish.set()
        await _drains_idle(authority)
        new = runner.session_store.peek_session_id(key)
        assert new != ref.session_id
        await authority.submit(connection.actor, Submission("B", ref, {"text": "B"}, "queue"))
        await _drains_idle(authority)
        rows = list_session_admissions(authority.db, session_id=ref.session_id, pending_only=False)
        actual = next(row for row in rows if row["admission_id"] == wake["admission_id"])
        from hermes_cli.loops import load_loop
        state = load_loop(ref.session_id)
        assert state is not None
        observed = dict(executed=executed, resolved=resolved,
                        wake=(actual["status"], actual["outcome"]),
                        awaiting=state.awaiting_response, log=caplog.text)
        print("LOCAL_RESET_OBSERVED", observed)
        assert ("B", new) in executed, observed
        assert not any(text == "WAKE" for text, _ in executed), observed
        assert (actual["status"], actual["outcome"]) == ("terminal", "cancelled"), observed
        assert state.awaiting_response is superseded, observed
        assert "admission_conflict" not in caplog.text, observed
    finally:
        finish.set()
        await _drains_idle(authority)
        await asyncio.gather(*mutation_tasks(authority))
        await connection.close()
        authority.db.close()
        runner.session_store.close_all_db_handles()
