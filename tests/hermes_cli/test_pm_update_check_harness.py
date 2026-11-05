"""The PM E2E provider config must retain its local-origin update channel.

Regression for nightly run 42034618511: configure() erased the installer's
main record, sending the managed-environment check to GitHub's shared quota.
"""
from contextlib import redirect_stdout
from io import StringIO
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from urllib.error import HTTPError
import urllib.request

import pytest


@pytest.mark.platforms("posix")
@pytest.mark.real_release_channels
def test_pm_configure_keeps_update_check_local_when_github_is_rate_limited(tmp_path, monkeypatch, capsys):
    # The upgrade harness imports pwd and uses POSIX sandboxes. No real install,
    # updater apply, service manager, or dependency download is needed here.
    from tests.e2e.core.upgrade import _install_helpers as I
    from tests.e2e.core.upgrade.pm import _pm as P
    from hermes_cli import github_api, main, update_cmd
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    root = tmp_path / "checkout"
    origin = tmp_path / "origin"
    home = tmp_path / "home" / ".hermes"
    home.mkdir(parents=True)
    env = {**os.environ, "HOME": str(home.parent), "HERMES_HOME": str(home),
           "GIT_CONFIG_GLOBAL": str(tmp_path / "gitconfig"), "GIT_CONFIG_NOSYSTEM": "1"}

    def git(*args, cwd=tmp_path):
        return subprocess.run(
            ["git", "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
             "-c", "commit.gpgsign=false", *args], cwd=cwd, env=env,
            check=True, capture_output=True, text=True,
        ).stdout.strip()

    git("init", "-b", "main", str(origin))
    git("commit", "--allow-empty", "-m", "fixture", cwd=origin)
    git("clone", str(origin), str(root))
    official = "https://github.com/NousResearch/hermes-agent.git"
    git("remote", "set-url", "origin", official, cwd=root)
    git("config", f"url.{origin}.insteadOf", official, cwd=root)
    before = git("rev-parse", "HEAD", cwd=root)
    monkeypatch.setattr(main, "PROJECT_ROOT", root)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_MANAGED", raising=False)
    monkeypatch.setattr(github_api, "github_token", lambda: None)

    requests = []

    def rate_limited(request, **kwargs):
        url = request.full_url
        requests.append(url)
        raise HTTPError(url, 403, "Forbidden", {"x-ratelimit-remaining": "0"}, None)

    monkeypatch.setattr(urllib.request, "urlopen", rate_limited)
    config = home / "config.yaml"
    # Exactly the config the old configure() wrote: no install-channel record.
    config.write_text(I.provider_config("http://127.0.0.1:9/v1", DEFAULT_CONFIG["_config_version"]),
                      encoding="utf-8")
    with pytest.raises(SystemExit) as failed:
        update_cmd._cmd_update_check()
    assert failed.value.code == 1
    output = capsys.readouterr().out
    assert "Update channel: stable" in output
    assert "GitHub API rate limit reached (HTTP 403)" in output
    assert any(url.startswith("https://api.github.com/") for url in requests)
    requests.clear()

    # Run configure's real version-query subprocess and its real metadata CLI
    # handler. Only the subprocess transport is adapted to this minimal checkout;
    # the harness, channel persistence, Git fetch, and update check remain real.
    def run(argv, **kwargs):
        return subprocess.run(argv, cwd=Path(__file__).resolve().parents[2], env=env,
                              capture_output=True, text=True, check=False, **kwargs)

    def cli(*argv, **kwargs):
        from hermes_cli.update_channel import handle_metadata_args

        assert argv == ("update", "--set-channel", "main")
        output = StringIO()
        with redirect_stdout(output):
            assert handle_metadata_args(SimpleNamespace(set_channel="main"), root)
        return subprocess.CompletedProcess(argv, 0, output.getvalue(), "")

    sb = SimpleNamespace(python=sys.executable, hermes_home=home, run=run, cli=cli)
    P.configure(sb, "http://127.0.0.1:9/v1", extra="custom_fixture: preserved\n",
                env_extra="FIXTURE_ONLY=value\n")
    pinned = config.read_bytes()
    update_cmd._cmd_update_check()
    output = capsys.readouterr().out
    assert "Update channel: main" in output
    assert "Already up to date." in output
    assert requests == [], "the pinned main check must not consult the release HTTP transport"
    assert config.read_bytes() == pinned, "--check must not rewrite the configuration"
    assert "custom_fixture: preserved" in config.read_text(encoding="utf-8")
    assert "FIXTURE_ONLY=value" in (home / ".env").read_text(encoding="utf-8")
    assert git("rev-parse", "HEAD", cwd=root) == before
