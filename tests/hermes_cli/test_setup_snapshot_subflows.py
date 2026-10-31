"""Wizard subflows may write independently without invalidating their owner snapshot."""
import copy

import pytest

from hermes_cli import config
from hermes_cli.config_write_guards import ConfigWriteConflict


@pytest.mark.parametrize("flow", ["quick", "gateway"])
def test_wizard_refresh_preserves_revision_and_pending_edits(tmp_path, monkeypatch, flow):
    from hermes_cli import setup, gateway, gateway_setup_service, setup_platforms
    from hermes_cli.setup_quick import _reload_config_into

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    path = tmp_path / "config.yaml"
    path.write_text("timezone: UTC\n", encoding="utf-8")
    wizard = config.load_config()
    stale = copy.deepcopy(wizard)
    if flow == "quick":
        config.set_config_value("timezone", "Pacific/Honolulu")
        _reload_config_into(wizard)
        assert wizard["timezone"] == "Pacific/Honolulu"
    else:
        wizard["display"]["skin"] = "mono"
        platforms = [{"key": "synthetic", "label": "Synthetic", "emoji": ""}]
        monkeypatch.setattr(gateway, "_all_platforms", lambda: platforms)
        monkeypatch.setattr(gateway, "_platform_status", lambda _: "not configured")
        monkeypatch.setattr(setup, "prompt_checklist", lambda *args: [0])
        monkeypatch.setattr(gateway, "_configure_platform", lambda _: config.write_platform_config_field(
            "synthetic", "unauthorized_dm_behavior", "ignore"))
        monkeypatch.setattr(gateway, "_is_service_running", lambda: False)
        monkeypatch.setattr(gateway, "supports_systemd_services", lambda: False)
        monkeypatch.setattr(gateway_setup_service, "ensure_gateway_service", lambda **kwargs: None)
        setup_platforms.setup_gateway(wizard)
        assert wizard["display"]["skin"] == "mono"
        assert wizard["platforms"]["synthetic"]["unauthorized_dm_behavior"] == "ignore"
    config.save_config(wizard)
    latest = path.read_bytes()
    with pytest.raises(ConfigWriteConflict):
        config.save_config(stale)
    assert path.read_bytes() == latest


def test_gateway_cancel_keeps_entry_checkpoint_and_says_so(tmp_path, monkeypatch, capsys):
    from hermes_cli import gateway, setup, setup_platforms

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    path = tmp_path / "config.yaml"
    path.write_text("timezone: UTC\n", encoding="utf-8")
    wizard = config.load_config()
    wizard["display"]["skin"] = "mono"
    platforms = [{"key": "synthetic", "label": "Synthetic", "emoji": ""}]
    monkeypatch.setattr(gateway, "_all_platforms", lambda: platforms)
    monkeypatch.setattr(gateway, "_platform_status", lambda _: "not configured")

    def cancel(*args):
        raise setup._SetupCancelled()

    monkeypatch.setattr(setup, "prompt_checklist", cancel)
    monkeypatch.setattr(setup, "_run_setup_wizard_impl", lambda _: setup_platforms.setup_gateway(wizard))
    setup.run_setup_wizard(None)
    assert config.read_user_config_raw()["display"]["skin"] == "mono"
    output = capsys.readouterr().out
    assert "earlier checkpoints are kept" in output
    assert "Remaining sections were not changed" not in output
