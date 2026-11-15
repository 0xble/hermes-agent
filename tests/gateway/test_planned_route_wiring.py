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
