"""Cleanup follows the launch-time harness locator and Hermes profile owner."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_constants import (
    get_hermes_home, reset_hermes_home_override, set_hermes_home_override,
)
from tools import browser_tool, browser_use_cli as cli


@pytest.fixture
def harness(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for key in ("BH_RUNTIME_DIR", "BH_RUNTIME_DIR_SHARED", "BH_HOME",
                "BROWSER_HARNESS_HOME", "XDG_CONFIG_HOME", "BU_CDP_URL", "BU_CDP_WS"):
        monkeypatch.delenv(key, raising=False)
    for name in ("_browser_exec_identity_bindings", "_browser_exec_identity_daemons",
                 "_browser_exec_identity_daemon_homes", "_browser_exec_identity_daemon_envs"):
        if hasattr(cli, name):
            monkeypatch.setattr(cli, name, {})
    monkeypatch.setattr(cli, "_find_cli", lambda: ["fixture-browser-use"])
    # Only browser/daemon external operations are replaced. Routing, environment,
    # identity claims, persisted records and cleanup are the production path.
    monkeypatch.setattr(browser_tool, "_real_profile_cdp", lambda *a, **kw: ("ws://127.0.0.1:9929/fixture", None))
    alive, reloads = set(), []

    def execute(cmd, code, env, timeout):
        alive.add(cli._daemon_pid_path(env["BU_NAME"], env))
        return SimpleNamespace(returncode=0, stdout="fixture", stderr="")

    def reload(cmd, **kwargs):
        assert cmd == ["fixture-browser-use", "--reload"]
        env = kwargs["env"]
        pid = cli._daemon_pid_path(env["BU_NAME"], env)
        reloads.append((pid, get_hermes_home(), dict(env)))
        alive.discard(pid)  # A wrong-directory reload may report successful no-op.
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(cli, "_run_cli_killing_process_group", execute)
    monkeypatch.setattr(cli.subprocess, "run", reload)

    def launch(home, locator):
        home.mkdir(exist_ok=True)
        (home / "config.yaml").write_text(json.dumps({"browser": {
            "backend": "browser-use", "use_real_profile": True,
            "real_profile_identities": {"work": {"browser": "chrome", "source_profile": "Default"}},
        }}))
        monkeypatch.setenv("HERMES_HOME", str(home))
        for key in ("BH_RUNTIME_DIR", "BH_RUNTIME_DIR_SHARED", "BH_HOME",
                    "BROWSER_HARNESS_HOME", "XDG_CONFIG_HOME"):
            monkeypatch.delenv(key, raising=False)
        for key, value in locator.items():
            monkeypatch.setenv(key, value)
        monkeypatch.setenv("BROWSER_USE_API_KEY", "fixture-secret-" + home.name)
        result = json.loads(cli.browser_exec("print(1)", session="same", identity="work", local=True))
        assert result["success"], result
        return cli._browser_exec_durable_binding_dir("same")

    return launch, alive, reloads


@pytest.mark.parametrize("locator_key", ["BH_RUNTIME_DIR", "BH_HOME", "BROWSER_HARNESS_HOME", "XDG_CONFIG_HOME"])
@pytest.mark.parametrize("restart", [False, True])
def test_cleanup_retains_each_launch_locator_and_clears_only_own_markers(harness, tmp_path, monkeypatch, locator_key, restart):
    launch, alive, reloads = harness
    home_a, home_b = tmp_path / "profile-a", tmp_path / "profile-b"
    claim_a = launch(home_a, {locator_key: str(tmp_path / "runtime-a")})
    claim_b = launch(home_b, {"BH_RUNTIME_DIR": str(tmp_path / "runtime-b"), "BH_RUNTIME_DIR_SHARED": "1"})
    expected = set(alive)
    assert len(expected) == 2
    # Keep a sibling marker that cleanup has no authority to remove.
    sibling = home_b / "browser-profile" / "browser-use-bindings" / "unrelated"
    sibling.mkdir()
    (sibling / "daemon").write_text("unrelated\n")
    if restart:
        for name in ("_browser_exec_identity_bindings", "_browser_exec_identity_daemons",
                     "_browser_exec_identity_daemon_homes", "_browser_exec_identity_daemon_envs"):
            if hasattr(cli, name):
                getattr(cli, name).clear()
        # Each profile is recovered when visited, then process exit happens in B.
        token = set_hermes_home_override(home_a)
        try:
            cli._recover_browser_exec_daemons(home_key=str(home_a))
        finally:
            reset_hermes_home_override(token)
    cli._close_all_browser_exec_identity_daemons(all_profiles=True)
    assert not alive
    assert {entry[0] for entry in reloads} == expected
    assert {entry[1] for entry in reloads} == {home_a, home_b}
    assert not (claim_a / "daemon").exists()
    assert not (claim_b / "daemon").exists()
    assert (sibling / "daemon").read_text() == "unrelated\n"
    assert get_hermes_home() == home_b
    for _, home, env in reloads:
        assert "BROWSER_USE_API_KEY" not in env
        assert "BU_CDP_URL" not in env and "BU_CDP_WS" not in env
        assert env["HERMES_HOME"] == str(home)
    for claim in (claim_a, claim_b):
        for path in claim.iterdir():
            assert "fixture-secret" not in path.read_text()
    assert cli._browser_exec_identity_daemons == {}


@pytest.mark.parametrize("record", ["missing", "corrupt", "reload-failed"])
def test_unproven_cleanup_keeps_owning_record(harness, tmp_path, monkeypatch, record):
    launch, alive, reloads = harness
    home_a, home_b = tmp_path / "profile-a", tmp_path / "profile-b"
    claim_a = launch(home_a, {"BH_RUNTIME_DIR": str(tmp_path / "runtime-a")})
    # Simulate a pre-locator record, then recover it while visiting its owner.
    if record != "reload-failed":
        for path in claim_a.iterdir():
            if path.name not in ("owner", "daemon"):
                path.unlink()
        if record == "corrupt":
            (claim_a / "runtime.json").write_text('{"BROWSER_USE_API_KEY":"untrusted"}')
    for name in ("_browser_exec_identity_daemons", "_browser_exec_identity_daemon_homes", "_browser_exec_identity_daemon_envs"):
        if hasattr(cli, name):
            getattr(cli, name).clear()
    cli._recover_browser_exec_daemons(home_key=str(home_a))
    launch(home_b, {"BH_RUNTIME_DIR": str(tmp_path / "runtime-b")})
    if record == "reload-failed":
        real_fake_reload = cli.subprocess.run

        def fail_owner_a(cmd, **kwargs):
            if get_hermes_home() == home_a:
                return SimpleNamespace(returncode=1, stderr="fixture refusal")
            return real_fake_reload(cmd, **kwargs)

        monkeypatch.setattr(cli.subprocess, "run", fail_owner_a)
    cli._close_all_browser_exec_identity_daemons(all_profiles=True)
    assert len(alive) == 1
    assert len(reloads) == 1 and reloads[0][1] == home_b
    assert (claim_a / "daemon").exists()
    assert len(cli._browser_exec_identity_daemons) == 1
