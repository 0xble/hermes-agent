"""launchd plist refresh must not report success when launchd never registered the service (#12866)."""
import subprocess
from types import SimpleNamespace
from unittest.mock import MagicMock

from hermes_cli import gateway as gw
from hermes_cli import gateway_launchd as launchd
from hermes_cli import gateway_launchd_lifetime as lifetime
from hermes_cli import gateway_service_owner

def _stale_plist(tmp_path, monkeypatch, *, registered: bool):
    plist_path = tmp_path / "com.hermes.plist"
    plist_path.write_text("<old/>", encoding="utf-8")
    monkeypatch.setattr(gw, "get_launchd_plist_path", lambda: plist_path)
    monkeypatch.setattr(gw, "launchd_plist_is_current", lambda: False)
    monkeypatch.setattr(gw, "generate_launchd_plist", lambda: "<new/>")
    monkeypatch.setattr(gateway_service_owner, "refuse_temp_home_service_write", lambda *a: False)
    monkeypatch.setattr(gw, "get_launchd_label", lambda: "com.hermes.agent")
    monkeypatch.setattr(gw, "_launchd_domain", lambda: "gui/501")
    monkeypatch.setattr(gw, "_append_launchd_reload_log", lambda msg: None)
    monkeypatch.setattr("gateway.status.get_running_pid", lambda: None)
    monkeypatch.setattr(gw, "_retry_launchctl_bootstrap_until_registered", lambda *a, **k: registered)
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: MagicMock(returncode=0))

def test_refresh_reports_registration_outcome(tmp_path, monkeypatch, capsys):
    _stale_plist(tmp_path, monkeypatch, registered=False)
    assert gw.refresh_launchd_plist_if_needed() is False
    assert "Updated" not in capsys.readouterr().out

    _stale_plist(tmp_path, monkeypatch, registered=True)
    assert gw.refresh_launchd_plist_if_needed() is True
    assert "Updated" in capsys.readouterr().out


def test_disabled_zero_cut_retires_stale_standby_definition(tmp_path, monkeypatch):
    standby = tmp_path / "ai.hermes.gateway.b.plist"
    standby.write_text("<old-standby/>", encoding="utf-8")
    logs = []
    facade = SimpleNamespace(
        get_hermes_home=lambda: tmp_path,
        get_launchd_plist_path=lambda: tmp_path / "ai.hermes.gateway.plist",
        _append_launchd_reload_log=logs.append,
    )
    monkeypatch.setattr(launchd, "_gw", lambda: facade)
    monkeypatch.setattr(launchd, "get_launchd_slot_labels", lambda: ("ai.hermes.gateway", "ai.hermes.gateway.b"))
    monkeypatch.setattr(launchd, "_probe_launchd_domain_for_label", lambda _label: "gui/501")
    monkeypatch.setattr(lifetime, "_state", lambda *args: (False, None))
    monkeypatch.setattr(launchd.gateway_service_owner, "definition_belongs_to_home", lambda *args: True)
    monkeypatch.setattr("gateway.config.is_zero_cut_handover_enabled", lambda _config: False)
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", dict)

    assert launchd._retire_disabled_standby_launchd() is True
    assert not standby.exists()
    assert logs == ["retired disabled standby definition ai.hermes.gateway.b"]
