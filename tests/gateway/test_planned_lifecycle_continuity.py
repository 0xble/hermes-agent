"""Planned lifecycle contracts at the router, owner, and real work-registry boundaries.

Native supervisors and Telegram transport are replaced, not the lifecycle routers or
retirement policy. This is not a native or live continuity acceptance drill.
"""
from __future__ import annotations

import asyncio
import os
import shlex
import sys
import threading
from types import SimpleNamespace

import pytest

from gateway.config import GatewayConfig, Platform
from gateway import zero_cut_handover_drain as drain, zero_cut_standby as standby
from hermes_cli import gateway_handover as handover, gateway_launchd as launchd
from tests.waiting import async_wait_until


@pytest.fixture
def routed_handover(monkeypatch, tmp_path):
    """Use the real planned router and orchestration, with only service effects replaced."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text('gateway:\n  zero_cut_handover:\n    enabled: true\n')
    calls = []
    state = {"booted": False, "released": False, "promoted": False}

    def query(home, verb, *, slot, timeout=2, params=None):
        calls.append((slot, verb))
        if slot == "a":
            if verb == "identify":
                return {"standby": False, "pid": os.getpid()}
            if verb == "handover":
                state["released"] = True
                return {"accepted": True}
            if verb == "handover-status":
                return {"accepted": state["released"], "updates_settled": state["released"],
                        "token_lock_released": state["released"]}
            return {"standby": False}
        if not state["booted"]:
            return None
        if verb in {"identify", "status"}:
            return {"standby": not state["promoted"], "ready": True, "pid": os.getpid()}
        if verb == "arm-handover":
            assert params == {"predecessor_slot": "a", "predecessor_pid": os.getpid()}
            return {"armed": True}
        if verb == "promote":
            assert state["released"]
            state["promoted"] = True
            return {"promoted": True}
        return {}

    fx = handover.HandoverEffects(
        query=query,
        bootstrap=lambda *args: state.update(booted=True),
        bootout=lambda *args: pytest.fail("a planned router must not kill either live owner"),
        process_exists=lambda pid: True,
        write_plist=lambda *args: None,
    )
    monkeypatch.setattr(handover, "_effects", lambda _: fx)
    monkeypatch.setattr(launchd, "generate_launchd_plist", lambda **kw: "isolated definition")
    monkeypatch.setattr(handover, "_slot_plist_path", lambda label: tmp_path / f"{label}.plist")
    return calls, state


def test_normal_update_reload_routes_through_actual_handover(monkeypatch, tmp_path, routed_handover):
    calls, state = routed_handover
    monkeypatch.setattr(launchd, "_spawn_deferred_launchd_reload", lambda **kw: pytest.fail("destructive reload"))
    assert launchd._reload_installed_launchd_plist(tmp_path / "gateway.plist") is True
    assert ("a", "handover") in calls
    assert state["promoted"] is True


@pytest.mark.asyncio
async def test_in_band_restart_routes_without_stopping_original(routed_handover):
    from gateway.run_shutdown import GatewayShutdownMixin
    runner = SimpleNamespace(config=GatewayConfig(zero_cut_handover_enabled=True),
                             _restart_task_started=False, _draining=False)
    assert GatewayShutdownMixin.request_restart(runner, reason="planned") is True
    await asyncio.wait_for(runner._restart_task, 5)
    assert routed_handover[1]["promoted"] is True
    assert runner._draining is False
    assert not getattr(runner, "_restart_requested", False)


@pytest.mark.asyncio
async def test_retirement_waits_for_real_command_delegation_and_delivery(monkeypatch, tmp_path):
    from gateway.run import GatewayRunner
    from gateway import delivery_ledger
    from tools import async_delegation, process_registry as process_module
    from tools.process_registry import ProcessRegistry

    registry = ProcessRegistry()
    monkeypatch.setattr(process_module, "process_registry", registry)
    monkeypatch.setattr(process_module, "_find_shell", lambda: "/bin/sh")
    runner = GatewayRunner(GatewayConfig(zero_cut_handover_enabled=True, sessions_dir=tmp_path / "sessions"))
    runner._zero_cut_handover_state = {
        "accepted": True, "updates_settled": True, "token_lock_released": True,
        "idle": False, "failed": None,
    }
    stopped = asyncio.Event()
    async def stop():
        stopped.set()
    runner.stop = stop
    release = threading.Event()
    started = threading.Event()
    def work():
        started.set()
        assert release.wait(10)
        return {"status": "completed", "summary": "owned result"}

    command = registry.spawn_local(
        f"{shlex.quote(sys.executable)} -c " + shlex.quote("import sys; sys.stdin.read(); print('owned command')"),
        cwd=str(tmp_path),
    )
    handle = async_delegation.dispatch_async_delegation(
        goal="finite owned work", context=None, toolsets=None, role=None, model=None,
        session_key="telegram:test", runner=work, interrupt_fn=lambda: pytest.fail("work interrupted"),
    )
    delivery_ledger.record_obligation(obligation_id="owed", session_key="telegram:test", platform="telegram",
                                      chat_id="test", thread_id=None, content="original response")
    task = asyncio.create_task(drain._wait_for_idle(runner))
    try:
        assert await asyncio.to_thread(started.wait, 5)
        snapshot = drain.status(runner)
        assert snapshot.get("active_work", snapshot["active_turns"]) >= 3
        assert not stopped.is_set()
        registry.close_stdin(command.id)
        result = await asyncio.to_thread(registry.wait, command.id, 5)
        assert result["exit_code"] == 0
        assert "owned command" in result["output"]
        release.set()
        await async_wait_until(lambda: async_delegation.get_durable_delegation(handle["delegation_id"])["state"] == "completed", timeout=5)
        # Completion delivery itself is still owed, even after the callable returned.
        assert not stopped.is_set()
        async_delegation.mark_completion_delivered(handle["delegation_id"])
        registry.drain_notifications()
        snapshot = drain.status(runner)
        assert snapshot.get("active_work", snapshot["active_turns"]) > 0
        delivery_ledger.mark_delivered("owed")
        await asyncio.wait_for(stopped.wait(), 5)
        await task
    finally:
        release.set()
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        registry.kill_all(source="test_cleanup")


@pytest.mark.asyncio
async def test_handover_drain_cannot_be_cleared_by_external_marker_watcher():
    from gateway.run_shutdown import GatewayShutdownMixin
    runner = SimpleNamespace(_external_drain_active=True, _draining=False, _running=True,
                             _zero_cut_handover_state={"accepted": True},
                             _update_runtime_status=lambda *args: None, _serving_state=lambda: "running")
    GatewayShutdownMixin._exit_external_drain(runner)
    assert runner._external_drain_active is True


def test_active_slot_identifies_accepted_handover(monkeypatch):
    from gateway.control_socket import build_identify_payload
    runner = SimpleNamespace(_zero_cut_handover_state={"accepted": True})
    monkeypatch.setattr(standby, "_runner", runner)
    assert build_identify_payload()["handover_accepted"] is True


@pytest.mark.asyncio
async def test_failed_successor_connection_is_not_a_successful_promotion(monkeypatch):
    standby.reset_for_tests()
    monkeypatch.setattr(standby, "is_standby", lambda: True)
    monkeypatch.setattr(standby, "_ready", True)
    runner = SimpleNamespace(
        adapters={}, _standby_deferred={(None, Platform.TELEGRAM): object()}, _failed_platforms={},
        _create_adapter=lambda *args: object(), _wire_adapter_handlers=lambda *args: None,
        _connect_initial_adapter_with_timeout=lambda *args: asyncio.sleep(0, result=False),
        _startup_retry_entry=lambda *args: {}, _start_prime_session_db_after_ready=lambda: asyncio.sleep(0),
        _start_finish_wiring=lambda *args: asyncio.sleep(0),
        _start_spawn_background_watchers=lambda: None,
    )
    try:
        monkeypatch.setattr("gateway.run._start_gateway_start_cron_and_housekeeping", lambda r: (None,) * 4)
        monkeypatch.setattr(standby, "_start_adopting", lambda *args: None)
        result = await standby.promote(runner)
        assert result["promoted"] is False
        assert result["failed"] == ["telegram"]
        assert runner._standby_deferred  # a later owner can retry without replaying a turn
    finally:
        standby.reset_for_tests()


@pytest.mark.asyncio
@pytest.mark.parametrize("initiator", ["command", pytest.param("plugin", marks=pytest.mark.skipif(
    not os.environ.get("HERMES_TEST_REQUEST_RESTART_PLUGIN"),
    reason="installed request-restart bridge proof is opt-in and read-only",
))])
async def test_marker_bearing_restart_promotes_and_sends_only_request_completion_receipt(
        monkeypatch, tmp_path, routed_handover, initiator):
    from unittest.mock import AsyncMock, Mock
    from gateway import run
    from gateway.platforms.base import MessageEvent
    from gateway.session import SessionSource
    from gateway.platforms.base import SendResult

    monkeypatch.setattr(run, "_hermes_home", tmp_path)
    owner = run.GatewayRunner(GatewayConfig(zero_cut_handover_enabled=True, sessions_dir=tmp_path / "sessions"))
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="123", user_id="456", chat_type="dm")
    if initiator == "plugin":
        import importlib.util
        import json
        spec = importlib.util.spec_from_file_location("isolated_request_restart_bridge",
                                                       os.environ["HERMES_TEST_REQUEST_RESTART_PLUGIN"])
        plugin = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(plugin)
        monkeypatch.setattr(plugin, "_is_child", lambda: False)  # this fixture is the owning parent
        monkeypatch.setattr(run, "_gateway_runner_ref", lambda: owner)
        owner._gateway_loop = asyncio.get_running_loop()
        result = json.loads(plugin.request_restart({"reason": "isolated continuity regression"},
                                                     platform="telegram", chat_id="123"))
        assert result["success"] and result["routed"]
        await async_wait_until(lambda: owner._restart_task is not None)
    else:
        await owner._handle_restart_command(MessageEvent("/restart", source=source, message_id="m", platform_update_id=77))
    # Exercise the real initiating writer, not a bare runner method with no receipt marker.
    assert (tmp_path / ".restart_notify.json").exists()
    await asyncio.wait_for(owner._restart_task, 5)
    assert routed_handover[1]["promoted"]
    assert not (tmp_path / ".restart_pending.json").exists()

    successor = run.GatewayRunner(GatewayConfig(zero_cut_handover_enabled=True, sessions_dir=tmp_path / "sessions"))
    # Keep actual promotion, finish-wiring, bounded boot-send, and restart-receipt methods.
    # Replace service starts and unrelated discovery, not lifecycle decisions or marker handling.
    for name in ("_start_prime_session_db_after_ready", "_start_post_connect_services",
                 "_recover_pending_shutdown_flush_off_loop", "_await_mcp_discovery",
                 "_finish_startup_restore", "_send_session_db_warning_notifications",
                 "_redeliver_claimed_obligations"):
        monkeypatch.setattr(successor, name, AsyncMock())
    monkeypatch.setattr(successor, "_claim_pending_obligations", AsyncMock(return_value=[]))
    monkeypatch.setattr(successor, "_resume_pending_candidates_async", AsyncMock(return_value=[]))
    monkeypatch.setattr(successor, "_schedule_resume_pending_sessions", Mock())
    monkeypatch.setattr(successor, "_schedule_auto_resume_delegations", Mock())
    monkeypatch.setattr(successor, "_schedule_update_notification_watch", Mock())
    monkeypatch.setattr(successor, "_start_spawn_background_watchers", Mock())
    monkeypatch.setattr("gateway.cron_store_notices.install_cron_store_notices", Mock())
    monkeypatch.setattr(run, "_start_gateway_start_cron_and_housekeeping", lambda r: (None,) * 4)
    monkeypatch.setattr(standby, "_start_adopting", Mock())
    monkeypatch.setattr(standby, "_acknowledge_release", AsyncMock())
    monkeypatch.setattr(standby, "effective_gateway_standby", lambda: True)
    transport = SimpleNamespace(adapter=object(), send=AsyncMock(return_value=SendResult(success=True)))
    monkeypatch.setattr("gateway.delivery.resolve_delivery_transport", lambda *args, **kw: transport)
    monkeypatch.setattr(successor, "_pending_marker_metadata", lambda *args: {})
    monkeypatch.setattr(successor, "_send_interrupted_turn_notes", AsyncMock())
    standby.reset_for_tests()
    standby.mark_ready()
    try:
        assert (await standby.promote(successor))["promoted"]
        assert transport.send.await_count == 1
        assert transport.send.await_args.args[:2] == (Platform.TELEGRAM, "123")
        from agent.i18n import t
        assert transport.send.await_args.args[2] == t("gateway.startup.restarted")
        successor._send_interrupted_turn_notes.assert_not_called()
        assert not (tmp_path / ".restart_notify.json").exists()
        assert not (tmp_path / ".restart_pending.json").exists()
        assert (tmp_path / ".restart_last_processed.json").exists() == (initiator == "command")
    finally:
        standby.reset_for_tests()


@pytest.mark.asyncio
async def test_real_control_server_resolves_original_waiting_approval_without_turn_replay(monkeypatch, tmp_path):
    from unittest.mock import AsyncMock
    from gateway import run
    from gateway.control_socket import query_gateway_control
    from gateway.session import SessionSource
    from hermes_state import SessionDB
    from tools.approval_gateway_wait import _await_gateway_decision
    from tools.approval import pending_gateway_approval_count, unregister_gateway_notify

    monkeypatch.setattr(run, "_hermes_home", tmp_path)
    runner = run.GatewayRunner(GatewayConfig(zero_cut_handover_enabled=True, sessions_dir=tmp_path / "sessions"))
    runner.session_store._db = SessionDB(tmp_path / "state.db")
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="123", user_id="456", chat_type="dm")
    entry = runner.session_store.get_or_create_session(source)
    token = runner.session_store.mark_turn_active(entry.session_key)
    runner.on_message = AsyncMock(side_effect=AssertionError("control must not replay a model turn"))
    runner._deliver_approval_confirmation = AsyncMock(return_value="approved")
    notified = threading.Event()
    waiter = asyncio.create_task(asyncio.to_thread(
        _await_gateway_decision, entry.session_key, lambda data: notified.set(),
        {"command": "test command", "session_key": entry.session_key},
    ))
    server = await run._start_gateway_start_control_socket(runner)
    try:
        assert server is not None
        assert await asyncio.to_thread(notified.wait, 5)
        assert pending_gateway_approval_count() == 1
        assert drain._owned_work(runner) > 0
        answer = await asyncio.to_thread(query_gateway_control, tmp_path, "handover-control",
                                         slot="a", params={"source": source.to_dict(), "text": "/approve"})
        assert answer == {"owned": True, "response": "approved"}
        outcome = await asyncio.wait_for(waiter, 5)
        assert outcome["resolved"] and outcome["choice"] == "once"
        assert pending_gateway_approval_count() == 0
        runner.on_message.assert_not_called()
    finally:
        unregister_gateway_notify(entry.session_key)
        await asyncio.wait_for(waiter, 5)
        runner.session_store.clear_turn_active(entry.session_key, token)
        if server is not None:
            await server.stop()
        runner.session_store._db.close()
        standby.reset_for_tests()


def test_live_unanswered_predecessor_is_not_released_or_exited(tmp_path):
    fx = handover.HandoverEffects(query=lambda *args, **kw: None, process_exists=lambda pid: True)
    assert not handover._handover_released(fx, tmp_path, [None], "a", os.getpid())
    assert not handover._old_active_exited(fx, tmp_path, os.getpid(), "a")


def test_repeated_update_refuses_when_both_slots_are_occupied(tmp_path, monkeypatch):
    monkeypatch.setattr(launchd, "get_launchd_slot_labels", lambda: ("isolated-a", "isolated-b"))
    fx = handover.HandoverEffects(
        query=lambda home, verb, **kw: {"standby": False, "handover_accepted": kw["slot"] == "a"},
        bootstrap=lambda *args: pytest.fail("no third slot"),
        bootout=lambda *args: pytest.fail("no destructive fallback"),
    )
    assert handover.run_handover(home=tmp_path, timeout=1, effects=fx, wait_for_exit=False) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("barrier", ["accepted", "released", "promoted"])
async def test_successor_owns_promotion_and_ack_without_updater(monkeypatch, barrier):
    from unittest.mock import AsyncMock
    standby.reset_for_tests()
    monkeypatch.setattr(standby, "effective_gateway_standby", lambda: True)
    monkeypatch.setattr(standby, "_PREDECESSOR_POLL_S", 0.001)
    standby.mark_ready()
    runner = SimpleNamespace()
    calls = []
    async def serve(r):
        assert standby.readiness()["standby"] is True  # never ACK while connecting
        calls.append("serve")
        return {"promoted": True, "failed": []}
    def query(*args, **kwargs):
        if barrier == "accepted" and not calls:
            calls.append("release-pending")
            return {"accepted": True, "updates_settled": False, "token_lock_released": False}
        return {"accepted": True, "updates_settled": True, "token_lock_released": True}
    def ack(home, **kwargs):
        assert standby.readiness()["standby"] is False
        calls.append("ack")
        return True
    monkeypatch.setattr(standby, "_promote", serve)
    monkeypatch.setattr("gateway.control_socket.query_gateway_control", query)
    monkeypatch.setattr("hermes_cli.immutable_releases.acknowledge_running_release", ack)
    try:
        if barrier == "promoted":
            await standby.promote(runner)
        else:
            assert standby.arm_handover(runner, {"predecessor_slot": "a", "predecessor_pid": os.getpid()})["armed"]
            await asyncio.wait_for(runner._zero_cut_promotion_watch, 5)
        assert calls[-2:] == ["serve", "ack"]
        assert (await standby.promote(runner))["already"]
        assert calls.count("serve") == 1
    finally:
        standby.reset_for_tests()
