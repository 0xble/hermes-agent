"""Source-level routes for opt-in zero-cut lifecycle handover."""
from __future__ import annotations

import asyncio
import os
from types import SimpleNamespace

import pytest

from gateway.config import GatewayConfig
from hermes_cli import gateway_handover as handover, gateway_launchd as launchd


@pytest.fixture
def routed_handover(monkeypatch, tmp_path):
    """Use the real planned router and orchestration with service effects replaced."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        "gateway:\n  zero_cut_handover:\n    enabled: true\n",
        encoding="utf-8",
    )
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
                return {
                    "accepted": state["released"],
                    "updates_settled": state["released"],
                    "token_lock_released": state["released"],
                }
            return {"standby": False}
        if not state["booted"]:
            return None
        if verb in {"identify", "status"}:
            return {"standby": not state["promoted"], "ready": True, "pid": os.getpid()}
        if verb == "arm-handover":
            return {"armed": True}
        if verb == "promote":
            assert state["released"]
            state["promoted"] = True
            return {"promoted": True}
        return {}

    effects = handover.HandoverEffects(
        query=query,
        bootstrap=lambda *args: state.update(booted=True),
        bootout=lambda *args: pytest.fail("a planned router must not kill either live owner"),
        process_exists=lambda pid: True,
        write_plist=lambda *args: None,
    )
    monkeypatch.setattr(handover, "_effects", lambda _: effects)
    monkeypatch.setattr(launchd, "generate_launchd_plist", lambda **kw: "isolated definition")
    monkeypatch.setattr(handover, "_slot_plist_path", lambda label: tmp_path / f"{label}.plist")
    return calls, state


def test_disabled_router_returns_none_without_touching_launchd(monkeypatch):
    monkeypatch.setattr(handover, "is_zero_cut_handover_enabled", lambda config: False)
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: {})
    assert handover.planned_handover() is None


def test_launchd_restart_routes_to_handover_before_service_manager(monkeypatch):
    monkeypatch.setattr(handover, "planned_handover", lambda: True)
    monkeypatch.setattr(launchd, "configured_launchd_domain", lambda: pytest.fail("legacy restart path"))
    assert launchd.launchd_restart() is None


def test_stopped_gateway_falls_back_to_normal_launchd_start(monkeypatch):
    calls = []
    monkeypatch.setattr(handover, "planned_handover", lambda: False)
    monkeypatch.setattr("gateway.status.get_running_pid", lambda: None)
    monkeypatch.setattr(
        launchd,
        "_launchd_restart_locked",
        lambda **kwargs: calls.append(kwargs) or "started",
    )
    assert launchd.launchd_restart() == "started"
    assert calls == [{"use_planned_handover": False}]


def test_stopped_gateway_start_uses_lifetime_lock(monkeypatch):
    events = []

    class Lock:
        def __enter__(self):
            events.append("enter")

        def __exit__(self, *_):
            events.append("exit")

    monkeypatch.setattr(handover, "planned_handover", lambda: False)
    monkeypatch.setattr("gateway.status.get_running_pid", lambda: None)
    monkeypatch.setattr(launchd, "configured_launchd_domain", lambda: "gui/501")
    monkeypatch.setattr(launchd, "_gw", lambda: SimpleNamespace(get_hermes_home=lambda: "home"))
    monkeypatch.setattr(
        launchd,
        "_launchd_restart_locked",
        lambda **kwargs: events.append(("restart", kwargs)) or "started",
    )
    monkeypatch.setattr(
        "hermes_cli.gateway_launchd_lifetime.lifetime_lock",
        lambda *args, **kwargs: Lock(),
    )
    assert launchd.launchd_restart() == "started"
    assert events == ["enter", ("restart", {"use_planned_handover": False}), "exit"]


def test_stopped_gateway_refresh_reloads_the_written_definition_directly(monkeypatch, tmp_path):
    plist = tmp_path / "gateway.plist"
    plist.write_text("old", encoding="utf-8")
    calls = []
    fake_gateway = SimpleNamespace(
        get_launchd_plist_path=lambda: plist,
        launchd_plist_is_current=lambda: False,
        generate_launchd_plist=lambda: "new",
        get_hermes_home=lambda: tmp_path,
        _prepare_service_launcher=lambda: None,
    )
    monkeypatch.setattr(launchd, "_gw", lambda: fake_gateway)
    monkeypatch.setattr("gateway.status.get_running_pid", lambda: None)
    monkeypatch.setattr(
        "hermes_cli.gateway_service_owner.definition_belongs_to_home",
        lambda *args: True,
    )
    monkeypatch.setattr(
        "hermes_cli.gateway_service_owner.refuse_temp_home_service_write",
        lambda *args: False,
    )
    monkeypatch.setattr(
        launchd,
        "_reload_installed_launchd_plist",
        lambda path, **kwargs: calls.append((path, kwargs)) or True,
    )

    assert launchd.refresh_launchd_plist_if_needed(use_planned_handover=False) is True
    assert calls == [(plist, {"use_planned_handover": False})]
    assert plist.read_text(encoding="utf-8") == "new"


@pytest.mark.parametrize(
    ("running_pid", "expected"),
    [(None, {"use_planned_handover": False}), (1234, {"use_planned_handover": True})],
)
def test_gateway_start_selects_reload_route_from_liveness(
    monkeypatch, tmp_path, running_pid, expected
):
    plist = tmp_path / "gateway.plist"
    plist.write_text("current", encoding="utf-8")
    calls = []
    fake_gateway = SimpleNamespace(
        get_hermes_home=lambda: tmp_path,
        get_launchd_plist_path=lambda: plist,
        get_launchd_label=lambda: "ai.hermes.gateway",
        refresh_launchd_plist_if_needed=lambda **kwargs: calls.append(kwargs) or False,
        _clear_launchd_unsupported_marker=lambda: None,
    )
    monkeypatch.setattr(launchd, "_gw", lambda: fake_gateway)
    monkeypatch.setattr("gateway.status.get_running_pid", lambda: running_pid)
    monkeypatch.setattr(
        "hermes_cli.gateway_service_owner.refuse_foreign_launchd_plist",
        lambda *_: None,
    )
    monkeypatch.setattr("hermes_cli.gateway_guardian.set_intent", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(launchd, "_launchctl_kickstart_current", lambda *_: None)
    monkeypatch.setattr(launchd, "_launchd_ok", lambda *_: None)

    launchd._launchd_start_locked()

    assert calls == [expected]


def test_stopped_restart_forwards_direct_reload_to_the_locked_route(monkeypatch, tmp_path):
    plist = tmp_path / "gateway.plist"
    plist.write_text("current", encoding="utf-8")
    calls = []
    fake_gateway = SimpleNamespace(
        get_hermes_home=lambda: tmp_path,
        get_launchd_plist_path=lambda: plist,
        get_launchd_label=lambda: "ai.hermes.gateway",
        _launchd_domain=lambda: "gui/501",
        refresh_launchd_plist_if_needed=lambda **kwargs: calls.append(kwargs) or True,
        _wait_for_api_server_port_free=lambda: None,
        _CAPTURE_TEXT={},
        _clear_launchd_unsupported_marker=lambda: None,
    )
    monkeypatch.setattr(launchd, "_gw", lambda: fake_gateway)
    monkeypatch.setattr("gateway.status.get_running_pid", lambda: None)
    monkeypatch.setattr(
        "hermes_cli.gateway_service_owner.refuse_foreign_launchd_plist",
        lambda *_: None,
    )
    monkeypatch.setattr("hermes_cli.gateway_guardian.set_intent", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(launchd.subprocess, "run", lambda *args, **kwargs: None)
    monkeypatch.setattr(launchd, "_launchd_ok", lambda *_: None)

    launchd._launchd_restart_locked(use_planned_handover=False)

    assert calls == [{"use_planned_handover": False}]


def test_live_gateway_handover_refusal_does_not_fall_back(monkeypatch):
    monkeypatch.setattr(handover, "planned_handover", lambda: False)
    monkeypatch.setattr("gateway.status.get_running_pid", lambda: 1234)
    monkeypatch.setattr(launchd, "_launchd_restart_locked", lambda: pytest.fail("unsafe fallback"))
    with pytest.raises(RuntimeError, match="planned handover refused"):
        launchd.launchd_restart()


def test_update_fleet_route_uses_one_handover_for_slot_labels(monkeypatch):
    from hermes_cli import update_cmd_fleet

    routed = []
    monkeypatch.setattr(
        "hermes_cli.config.load_config_readonly",
        lambda: {"gateway": {"zero_cut_handover": {"enabled": True}}},
    )
    monkeypatch.setattr("hermes_cli.gateway.get_launchd_label", lambda: "ai.hermes.gateway")
    monkeypatch.setattr(
        "hermes_cli.gateway.get_launchd_slot_labels",
        lambda: ["ai.hermes.gateway", "ai.hermes.gateway.b"],
    )
    monkeypatch.setattr(handover, "planned_handover", lambda: routed.append(True) or True)
    monkeypatch.setattr("hermes_cli.update_cmd_posix_pause.already_restarted", lambda: {"labels": []})
    monkeypatch.setattr("hermes_cli.gateway.launchd_gateway_labels_for_install", lambda: [])
    monkeypatch.setattr("hermes_cli.gateway.legacy_launchd_labels_for_install", lambda exclude: [])

    restarted, failed = [], []
    update_cmd_fleet._restart_macos_launchd_gateways(
        restarted,
        failed,
        0,
        acknowledged_label="not-a-slot",
        require_supervision=False,
    )
    assert routed == [True]
    assert restarted == ["ai.hermes.gateway"]
    assert not failed


def test_normal_update_reload_routes_through_actual_handover(monkeypatch, tmp_path, routed_handover):
    calls, state = routed_handover
    monkeypatch.setattr(
        launchd, "_spawn_deferred_launchd_reload", lambda **kw: pytest.fail("destructive reload")
    )
    assert launchd._reload_installed_launchd_plist(tmp_path / "gateway.plist") is True
    assert ("a", "handover") in calls
    assert state["promoted"] is True


def test_immutable_reload_keeps_the_recorded_launchd_label(monkeypatch, tmp_path):
    called = []
    monkeypatch.setattr(handover, "planned_handover", lambda: called.append(True) or pytest.fail("slot handover"))
    fake_gateway = SimpleNamespace(
        get_launchd_label=lambda: "ai.hermes.gateway",
        _launchd_domain=lambda: "gui/501",
        get_hermes_home=lambda: tmp_path,
        _get_restart_drain_timeout=lambda: 0,
    )
    monkeypatch.setattr(launchd, "_gw", lambda: fake_gateway)
    monkeypatch.setattr("gateway.status.get_running_pid", lambda: None)
    monkeypatch.setattr(launchd, "_bootout_drain_and_rebootstrap", lambda *args: True)
    assert launchd._reload_installed_launchd_plist(tmp_path / "gateway.plist", use_planned_handover=False) is True
    assert not called


def test_update_fleet_handover_failure_does_not_fall_back_to_drain(monkeypatch):
    from hermes_cli import update_cmd_fleet_handover

    monkeypatch.setattr(
        "hermes_cli.config.load_config_readonly",
        lambda: {"gateway": {"zero_cut_handover": {"enabled": True}}},
    )
    monkeypatch.setattr(
        "hermes_cli.gateway_launchd.get_launchd_slot_labels",
        lambda: ["ai.hermes.gateway.b"],
    )
    monkeypatch.setattr("hermes_cli.gateway_handover.planned_handover", lambda: False)
    restarted, failed = [], []
    protected = update_cmd_fleet_handover.restart_invoking_gateway(
        restarted, failed, "ai.hermes.gateway", set(), None, self_restart_pending=None,
    )
    assert not restarted
    assert failed == ["ai.hermes.gateway"]
    assert "ai.hermes.gateway" in protected


@pytest.mark.asyncio
async def test_external_restart_request_routes_without_stopping_original(routed_handover):
    from gateway.run_shutdown import GatewayShutdownMixin

    runner = SimpleNamespace(
        config=GatewayConfig(zero_cut_handover_enabled=True),
        _restart_task_started=False,
        _draining=False,
    )
    assert GatewayShutdownMixin.request_restart(runner, reason="external") is True
    await asyncio.wait_for(runner._restart_task, 5)
    assert routed_handover[1]["promoted"] is True
    assert runner._draining is False
    assert not getattr(runner, "_restart_requested", False)


@pytest.mark.asyncio
async def test_builtin_restart_request_uses_same_handover_route(routed_handover):
    from gateway.run_shutdown import GatewayShutdownMixin

    runner = SimpleNamespace(
        config=GatewayConfig(zero_cut_handover_enabled=True),
        _restart_task_started=False,
        _draining=False,
    )
    assert GatewayShutdownMixin.request_restart(runner, reason="/restart") is True
    await asyncio.wait_for(runner._restart_task, 5)
    assert routed_handover[1]["promoted"] is True
    assert not getattr(runner, "_restart_requested", False)
