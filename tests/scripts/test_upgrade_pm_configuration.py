"""PM upgrade fixtures follow their local commit origin, not published releases."""

import subprocess
from unittest.mock import Mock

import pytest


@pytest.mark.platforms("linux", "macos")
def test_provider_configuration_keeps_the_main_channel(tmp_path, monkeypatch):
    import hermes_yaml as yaml
    from hermes_cli.update_channel import install_id, resolve_update_channel
    from tests.e2e.core.upgrade.pm import _pm as P

    checkout = tmp_path / "checkout"
    (checkout / ".git").mkdir(parents=True)
    (checkout / ".git" / "config").write_text(
        '[remote "origin"]\n  url = https://github.com/NousResearch/hermes-agent.git\n',
        encoding="utf-8",
    )
    record = {"update": {"installs": {install_id(checkout): {
        "path": str(checkout), "channel": "main",
    }}}}
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump(record), encoding="utf-8")
    assert resolve_update_channel({}, checkout) == "stable"
    assert resolve_update_channel(record, checkout) == "main"

    def cli(*args, **kwargs):
        # Execute the metadata-only channel writer, never the real updater.
        assert args == ("update", "--set-channel", "main")
        from hermes_cli.update_channel import set_install_channel
        monkeypatch.setenv("HERMES_HOME", str(tmp_path))
        set_install_channel("main", checkout)
        return subprocess.CompletedProcess(args, 0, stdout="Update channel for fixture: main\n", stderr="")

    sb = Mock(
        hermes_home=tmp_path, checkout=checkout, python="fixture-python", cli=cli,
        run=lambda argv: subprocess.CompletedProcess(argv, 0, stdout="47\n", stderr=""),
    )
    P.configure(sb, "http://127.0.0.1:12345/v1", extra="compression:\n  enabled: false\n",
                env_extra="FIXTURE_EXTRA=value\n")

    configured = yaml.safe_load(config.read_text(encoding="utf-8"))
    assert resolve_update_channel(configured, checkout) == "main", "provider setup dropped the fixture's update channel"
    assert configured["model"]["base_url"] == "http://127.0.0.1:12345/v1"
    assert configured["compression"]["enabled"] is False
    assert "FIXTURE_EXTRA=value\n" in (tmp_path / ".env").read_text(encoding="utf-8")
