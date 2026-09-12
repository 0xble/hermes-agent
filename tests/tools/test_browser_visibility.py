"""Managed visibility identity, opt-in and transport contracts."""
import json
import subprocess
import sys
from typing import Any
from unittest.mock import Mock
import pytest
from hermes_cli.browser_identity import BrowserIdentity
from tools import browser_handoff as life, browser_use_cli as cli
from tools.browser_handoff_cdp import HandoffCDP, CdpHandoffError

@pytest.fixture
def identity(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    value = BrowserIdentity("work", "chrome", "Default", "fixture_identity")
    monkeypatch.setattr(cli, "_read_browser_cfg", lambda: {"visibility_handoff": True})
    monkeypatch.setattr(cli, "_real_profile_consented", lambda: True)
    monkeypatch.setattr("hermes_cli.browser_identity.resolve_browser_identity", lambda name: value if name else None)
    monkeypatch.setattr("hermes_cli.browser_identity.configured_identity_aliases", lambda cfg: ("work",))
    monkeypatch.setattr(cli, "_has_cdp_env", lambda env: False)
    monkeypatch.setattr("tools.browser_tool._get_cdp_override_raw", lambda: None)
    return value

def invoke(**overrides):
    args: dict[str, Any] = dict(code="# reveal", identity="work", session="visibility-test", local=True, handoff="reveal")
    args.update(overrides)
    return cli.browser_exec(**args)

def test_gate(identity, monkeypatch):
    monkeypatch.setattr(cli, "_read_browser_cfg", lambda: {})
    assert "disabled" in invoke()
    assert "handoff" not in cli.BROWSER_EXEC_SCHEMA["parameters"]["properties"]
    assert "handoff" not in cli._dynamic_schema_overrides()["parameters"]["properties"]

def test_schema(identity):
    assert cli._dynamic_schema_overrides()["parameters"]["properties"]["handoff"]["enum"] == ["reveal", "minimize"]

@pytest.mark.parametrize("overrides", [{"code": "print(1)"}, {"identity": ""}, {"local": False}, {"handoff": "restart"}])
def test_invalid_request(identity, monkeypatch, overrides):
    action = Mock()
    monkeypatch.setattr(life, "handoff", action)
    assert "error" in invoke(**overrides)
    action.assert_not_called()

def test_unbound_does_not_claim(identity):
    assert "not verified as bound" in invoke()
    assert not cli._browser_exec_durable_binding_dir("visibility-test").exists()
    assert not life._state_path(identity).exists()

def test_wrong_binding(identity):
    cli._claim_browser_exec_durable_binding("visibility-test", "wrong")
    assert "not verified as bound" in invoke()
    assert cli._read_browser_exec_durable_binding("visibility-test") == "wrong"

def test_pending_blocks_visibility_not_retry(identity, monkeypatch):
    cli._claim_browser_exec_durable_binding("visibility-test", cli._browser_exec_runtime_owner(identity))
    with life.activity(identity):
        life.mark_executing(identity, "loopback")
    assert "unfinished or uncertain" in invoke()
    assert life.mark_executing(identity, "another") is False
    assert life._read_state(identity)["pending"] == "loopback"
    execute = Mock(return_value="retry admitted")
    monkeypatch.setattr(cli, "_browser_exec", execute)
    assert invoke(handoff=None) == "retry admitted"


def test_launch_failure_releases_only_its_activity_marker(identity, monkeypatch):
    """No CLI process started, so this invocation can prove its marker is stale."""
    monkeypatch.setattr(cli, "_find_cli", lambda: ["browser-use"])
    monkeypatch.setattr(
        cli, "_resolve_real_profile_cdp",
        lambda env, **_kwargs: (env.update({"BU_CDP_URL": "http://127.0.0.1:9222", cli._REAL_PROFILE_SENTINEL: "1"}) or None),
    )
    monkeypatch.setattr(cli, "_resolve_backend_cdp", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli.subprocess, "run", Mock(side_effect=OSError("not executable")))

    result = cli._browser_exec("print(1)", session="launch-failure", identity="work")

    assert "Failed to launch" in result
    assert life._read_state(identity) == {}


def test_timeout_remains_pending_until_exact_daemon_reload(identity, monkeypatch):
    owner = cli._browser_exec_runtime_owner(identity)
    daemon = "rp_fixture"
    with life.activity(identity):
        assert life.mark_executing(
            identity, "loopback", daemon_name=daemon, runtime_owner=owner,
            daemon_identity=(41, 1.0),
        )

    assert life.clear_pending_after_daemon_reload(owner, "rp_other", (41, 1.0)) is False
    assert life._read_state(identity)["pending"] == "loopback"
    assert life.clear_pending_after_daemon_reload(owner, daemon, (41, 1.0)) is True
    assert life._read_state(identity) == {}


def test_successful_targeted_daemon_reload_recovers_timeout_marker(identity, monkeypatch):
    owner = cli._browser_exec_runtime_owner(identity)
    daemon = "rp_fixture"
    with life.activity(identity):
        assert life.mark_executing(
            identity, "loopback", daemon_name=daemon, runtime_owner=owner,
            daemon_identity=(41, 1.0),
        )
    cli._browser_exec_identity_daemons[daemon] = owner
    cli._browser_exec_identity_daemon_homes[daemon] = __import__("hermes_constants").hermes_home_key()
    monkeypatch.setattr(cli, "_find_cli", lambda: ["browser-use"])
    monkeypatch.setattr(cli, "_base_subprocess_env", lambda: {})
    monkeypatch.setattr(cli.subprocess, "run", lambda *_args, **_kwargs: Mock(returncode=0, stderr=""))
    monkeypatch.setattr(cli, "_daemon_process_identity", lambda *_args: (41, 1.0))
    monkeypatch.setattr(cli, "_process_identity_is_live", lambda *_args: False)

    assert cli._reload_browser_exec_daemons_for_runtime(owner) is True
    assert life._read_state(identity) == {}


@pytest.mark.parametrize("before,after", [((41, 1.0), True), (None, False), ((42, 2.0), False)])
def test_zero_exit_reload_without_exact_termination_proof_keeps_timeout_marker(
    identity, monkeypatch, before, after,
):
    owner = cli._browser_exec_runtime_owner(identity)
    daemon = "rp_fixture"
    with life.activity(identity):
        assert life.mark_executing(
            identity, "loopback", daemon_name=daemon, runtime_owner=owner,
            daemon_identity=(41, 1.0),
        )
    cli._browser_exec_identity_daemons[daemon] = owner
    cli._browser_exec_identity_daemon_homes[daemon] = __import__("hermes_constants").hermes_home_key()
    run = Mock(return_value=Mock(returncode=0, stderr=""))
    monkeypatch.setattr(cli, "_find_cli", lambda: ["browser-use"])
    monkeypatch.setattr(cli, "_base_subprocess_env", lambda: {})
    monkeypatch.setattr(cli.subprocess, "run", run)
    monkeypatch.setattr(cli, "_daemon_process_identity", lambda *_args: before)
    monkeypatch.setattr(cli, "_process_identity_is_live", lambda *_args: after)
    monkeypatch.setattr(cli, "_process_identity_verified_gone", lambda *_args: False)

    assert cli._reload_browser_exec_daemons_for_runtime(owner) is False
    assert life._read_state(identity)["pending"] == "loopback"
    if before is None or before != (41, 1.0):
        run.assert_not_called()


@pytest.mark.parametrize("observed", [None, (42, 2.0)])
def test_reload_clears_marker_whose_recorded_daemon_is_verifiably_dead(
    identity, monkeypatch, observed,
):
    """A timed-out daemon that already exited must not leave the marker stuck forever."""
    owner = cli._browser_exec_runtime_owner(identity)
    daemon = "rp_fixture"
    with life.activity(identity):
        assert life.mark_executing(
            identity, "loopback", daemon_name=daemon, runtime_owner=owner,
            daemon_identity=(41, 1.0),
        )
    cli._browser_exec_identity_daemons[daemon] = owner
    cli._browser_exec_identity_daemon_homes[daemon] = __import__("hermes_constants").hermes_home_key()
    run = Mock(return_value=Mock(returncode=0, stderr=""))
    monkeypatch.setattr(cli, "_find_cli", lambda: ["browser-use"])
    monkeypatch.setattr(cli, "_base_subprocess_env", lambda: {})
    monkeypatch.setattr(cli.subprocess, "run", run)
    monkeypatch.setattr(cli, "_daemon_process_identity", lambda *_args: observed)
    checked = []
    monkeypatch.setattr(
        cli, "_process_identity_verified_gone", lambda ident: checked.append(ident) or True,
    )

    assert cli._reload_browser_exec_daemons_for_runtime(owner) is True
    assert checked == [(41, 1.0)]
    assert life._read_state(identity) == {}
    run.assert_called_once()
    assert daemon not in cli._browser_exec_identity_daemons


def test_reload_keeps_marker_when_recorded_pid_is_alive_with_other_start_time(
    identity, monkeypatch,
):
    """A reused PID is not termination proof: the marker must stay fail-closed."""
    import os
    import psutil

    pid = os.getpid()
    live_created = psutil.Process(pid).create_time()
    owner = cli._browser_exec_runtime_owner(identity)
    daemon = "rp_fixture"
    with life.activity(identity):
        assert life.mark_executing(
            identity, "loopback", daemon_name=daemon, runtime_owner=owner,
            daemon_identity=(pid, live_created - 1000.0),
        )
    cli._browser_exec_identity_daemons[daemon] = owner
    cli._browser_exec_identity_daemon_homes[daemon] = __import__("hermes_constants").hermes_home_key()
    run = Mock(return_value=Mock(returncode=0, stderr=""))
    monkeypatch.setattr(cli, "_find_cli", lambda: ["browser-use"])
    monkeypatch.setattr(cli, "_base_subprocess_env", lambda: {})
    monkeypatch.setattr(cli.subprocess, "run", run)
    monkeypatch.setattr(cli, "_daemon_process_identity", lambda *_args: (pid, live_created))

    assert cli._reload_browser_exec_daemons_for_runtime(owner) is False
    assert life._read_state(identity)["pending"] == "loopback"
    run.assert_not_called()
    assert cli._browser_exec_identity_daemons[daemon] == owner


def test_reload_leaves_live_matching_daemon_marker_to_ordinary_recovery(identity, monkeypatch):
    owner = cli._browser_exec_runtime_owner(identity)
    daemon = "rp_fixture"
    with life.activity(identity):
        assert life.mark_executing(
            identity, "loopback", daemon_name=daemon, runtime_owner=owner,
            daemon_identity=(41, 1.0),
        )
    cli._browser_exec_identity_daemons[daemon] = owner
    cli._browser_exec_identity_daemon_homes[daemon] = __import__("hermes_constants").hermes_home_key()
    run = Mock(return_value=Mock(returncode=0, stderr=""))
    monkeypatch.setattr(cli, "_find_cli", lambda: ["browser-use"])
    monkeypatch.setattr(cli, "_base_subprocess_env", lambda: {})
    monkeypatch.setattr(cli.subprocess, "run", run)
    monkeypatch.setattr(cli, "_daemon_process_identity", lambda *_args: (41, 1.0))
    monkeypatch.setattr(cli, "_process_identity_is_live", lambda *_args: True)
    gone = Mock(return_value=True)
    monkeypatch.setattr(cli, "_process_identity_verified_gone", gone)

    assert cli._reload_browser_exec_daemons_for_runtime(owner) is False
    gone.assert_not_called()
    assert life._read_state(identity)["pending"] == "loopback"
    run.assert_called_once()


def test_process_identity_verified_gone_requires_missing_pid():
    import os
    import subprocess as sp

    import psutil

    child = sp.Popen([sys.executable, "-c", "pass"])
    child.wait()
    assert cli._process_identity_verified_gone((child.pid, 1.0)) is True
    me = os.getpid()
    created = psutil.Process(me).create_time()
    assert cli._process_identity_verified_gone((me, created)) is False
    assert cli._process_identity_verified_gone((me, created - 1000.0)) is False


def test_dead_daemon_recovery_ignores_malformed_recorded_identity(identity, monkeypatch):
    owner = cli._browser_exec_runtime_owner(identity)
    daemon = "rp_fixture"
    with life.activity(identity):
        assert life.mark_executing(identity, "loopback", daemon_name=daemon, runtime_owner=owner)
    states = life.pending_daemon_recovery_state(owner, daemon)
    gone = Mock(return_value=True)
    monkeypatch.setattr(cli, "_process_identity_verified_gone", gone)

    assert cli._clear_pending_for_gone_daemon(owner, daemon, states, None) == states
    gone.assert_not_called()
    assert life._read_state(identity)["pending"] == "loopback"


def test_daemon_recovery_does_not_clear_newer_marker(identity):
    owner = cli._browser_exec_runtime_owner(identity)
    daemon = "rp_fixture"
    with life.activity(identity):
        assert life.mark_executing(
            identity, "newer", daemon_name=daemon, runtime_owner=owner,
            daemon_identity=(41, 1.0),
        )
    generation = life._read_state(identity)["generation"]

    assert life.clear_pending_after_daemon_reload(
        owner, daemon, (41, 1.0), expected_generation="older-generation",
    ) is False
    assert life._read_state(identity) == {
        "pending": "newer", "daemon": daemon, "runtime_owner": owner,
        "daemon_pid": 41, "daemon_created": 1.0, "generation": generation,
    }

def test_cross_process_lock(identity):
    program = "from hermes_cli.browser_identity import BrowserIdentityProcessLock, BrowserIdentityError\ntry:\n with BrowserIdentityProcessLock('fixture_identity-visibility', timeout=0): pass\nexcept BrowserIdentityError:\n print('blocked')\n"
    with life.activity(identity):
        result = subprocess.run([sys.executable, "-c", program], text=True, capture_output=True, timeout=15)
    assert result.returncode == 0
    assert result.stdout.strip() == "blocked"

def test_ownership_and_headless(identity, monkeypatch):
    cli._claim_browser_exec_durable_binding("visibility-test", cli._browser_exec_runtime_owner(identity))
    monkeypatch.setattr("tools.browser_tool_real_profile._owned_profile_cdp", lambda path: None)
    assert "No verified" in invoke()
    monkeypatch.setattr("tools.browser_tool_real_profile._owned_profile_cdp", lambda path: "http://127.0.0.1:9222")
    monkeypatch.setattr("tools.browser_tool_real_profile._read_real_profile_headed_mode", lambda path: False)
    assert "not headed" in invoke()

def test_registry(identity, monkeypatch):
    from tools.registry import registry
    action = Mock(return_value={"window_state": "normal"})
    monkeypatch.setattr(life, "handoff", action)
    result = registry._tools["browser_exec"].handler(dict(code="# reveal", identity="work", session="visibility-test", local=True, handoff="reveal"))
    assert json.loads(result)["window_state"] == "normal"
    action.assert_called_once()

@pytest.fixture
def cdp():
    connection = Mock()
    with HandoffCDP("ws://127.0.0.1:9222/devtools/browser/fixture", ws_factory=lambda *a, **k: connection) as client:
        yield client, connection
    connection.close.assert_called_once()

@pytest.mark.parametrize("endpoint", ["ws://example.com/a", "https://localhost/a", "file:///tmp/a"])
def test_remote_endpoint(endpoint):
    factory = Mock()
    with pytest.raises(CdpHandoffError):
        HandoffCDP(endpoint, ws_factory=factory)
    factory.assert_not_called()

def test_transport(cdp):
    client, connection = cdp
    connection.recv.return_value = json.dumps({"id": 1, "result": {"ok": True}})
    assert client.call("Target.getTargets") == {"ok": True}
    assert 0 < connection.recv.call_args.kwargs["timeout"] <= 3
    connection.recv.side_effect = TimeoutError
    with pytest.raises(CdpHandoffError, match="timed out"):
        client.call("Target.getTargets")

@pytest.mark.parametrize("arguments", [[], ["--user-data-dir=/other"], ["--user-data-dir=/tmp/owned", "--headless=new"], None])
def test_command_ownership(cdp, arguments, monkeypatch):
    client, _ = cdp
    client.call = Mock(return_value={"processInfo": [{"type": "browser", "id": 123}]})
    monkeypatch.setattr("psutil.Process", lambda pid: Mock(cmdline=lambda: arguments))
    with pytest.raises(CdpHandoffError):
        client.assert_owned_headed_command_line("/tmp/owned")

@pytest.mark.parametrize("action,state", [("reveal", "normal"), ("minimize", "minimized")])
def test_readback(cdp, action, state):
    client, _ = cdp
    client.call = Mock(side_effect=[{"targetInfos": [{"type": "page", "targetId": "one"}]}, {"windowId": 1}, {}, {"bounds": {"windowState": state}}])
    assert client.set_visibility(action)["window_state"] == state

def test_multiple_windows(cdp):
    client, _ = cdp
    client.call = Mock(side_effect=[{"targetInfos": [{"type": "page", "targetId": "one"}, {"type": "page", "targetId": "two"}]}, {"windowId": 1}, {"windowId": 2}])
    with pytest.raises(CdpHandoffError, match="multiple"):
        client.set_visibility("reveal")
    assert all(c.args[0] != "Browser.setWindowBounds" for c in client.call.call_args_list)


def test_cold_execution_handshake_and_pending_refusal(identity, monkeypatch):
    monkeypatch.setattr(cli, "_find_cli", lambda: ["browser-use"])
    monkeypatch.setattr(cli, "_base_subprocess_env", lambda: {})
    monkeypatch.setattr(cli, "_resolve_real_profile_cdp", lambda env, **_: (
        env.update({"BU_CDP_URL": "http://fake", cli._REAL_PROFILE_SENTINEL: "1"}) or None))
    monkeypatch.setattr(cli, "_resolve_backend_cdp", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli, "_attach_vault_supervisor", lambda *_: None)
    monkeypatch.setattr(cli, "_workspace_dir", lambda _: None)
    calls = []
    clock = [0.0]
    daemon = [None]
    monkeypatch.setattr(cli.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(cli, "_daemon_process_identity", lambda *_: daemon[0])
    def run(cmd, code, env, timeout):
        calls.append((code, dict(env), timeout))
        if code == "pass\n":
            daemon[0] = (41, 1.0)
            clock[0] += 2
            return subprocess.CompletedProcess(cmd, 0, "", "")
        raise subprocess.TimeoutExpired(cmd, timeout)
    monkeypatch.setattr(cli, "_run_cli_killing_process_group", run)
    result = cli.browser_exec("print('user')", identity="work", session="cold", timeout_s=10)
    assert "timed out" in result
    assert len(calls) == 2
    assert calls[0][0] == "pass\n"
    assert calls[1][1]["BH_REQUIRE_EXISTING_DAEMON"] == "1"
    assert calls[1][2] < calls[0][2]
    pending = life._read_state(identity)
    assert pending["daemon_pid"] == 41
    assert pending["daemon_created"] == 1.0
    result = cli.browser_exec("print('another')", identity="work", session="cold")
    assert "unfinished or uncertain" in result
    assert len(calls) == 2
    assert life._read_state(identity) == pending
