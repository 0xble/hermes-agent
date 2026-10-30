"""Contracts for the opt-in zero-cut gateway handover seams."""

import asyncio
import threading
from types import SimpleNamespace

from hermes_cli.config_defaults import DEFAULT_CONFIG
from hermes_cli import gateway_launchd
from gateway.config import GatewayConfig, Platform, is_zero_cut_handover_enabled
from gateway.config_loader import bridge_toplevel_keys


def test_zero_cut_handover_defaults_off():
    assert DEFAULT_CONFIG["gateway"]["zero_cut_handover"]["enabled"] is False
    assert GatewayConfig.from_dict({}).zero_cut_handover_enabled is False
    assert GatewayConfig.from_dict(
        {"gateway": {"zero_cut_handover": {"enabled": True}}}
    ).zero_cut_handover_enabled is True


def test_raw_mapping_flag_uses_config_bool_coercion():
    assert is_zero_cut_handover_enabled({"gateway": {"zero_cut_handover": {"enabled": "false"}}}) is False
    assert is_zero_cut_handover_enabled({"zero_cut_handover": {"enabled": "true"}}) is True
    assert is_zero_cut_handover_enabled(None) is False


def test_real_yaml_gateway_section_is_bridged_before_schema_parse():
    gw_data = {}
    bridge_toplevel_keys(
        {"gateway": {"zero_cut_handover": {"enabled": True}}},
        {"zero_cut_handover": {"enabled": True}},
        gw_data,
    )
    assert GatewayConfig.from_dict(gw_data).zero_cut_handover_enabled is True


def test_launchd_slot_labels_reuse_default_label_convention(monkeypatch, tmp_path):
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.setattr(
        gateway_launchd,
        "_gw",
        lambda: SimpleNamespace(_profile_suffix=lambda: ""),
    )
    assert gateway_launchd.get_launchd_slot_labels() == (
        "ai.hermes.gateway",
        "ai.hermes.gateway.b",
    )

    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "hermes-zero-cut"))
    from hermes_cli import gateway
    monkeypatch.setattr(gateway_launchd, "_gw", lambda: gateway)
    active, standby = gateway_launchd.get_launchd_slot_labels()
    assert active == gateway.get_launchd_label()
    assert active.startswith("ai.hermes.gateway-")
    assert standby == f"{active}.b"
