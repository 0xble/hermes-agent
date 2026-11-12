"""Lifetime opt-in must not change automatic installs or leak expected CLI failures."""
import plistlib
import subprocess
from types import SimpleNamespace

import pytest

from hermes_cli import gateway, gateway_guardian, gateway_launchd
from hermes_cli import gateway_launchd_lifetime as lifetime

pytestmark = pytest.mark.platforms("posix")


def layout(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    path = tmp_path / "ai.hermes.gateway.plist"
    monkeypatch.setattr(gateway, "get_hermes_home", lambda: home)
    monkeypatch.setattr(gateway, "get_launchd_plist_path", lambda: path)
    monkeypatch.setattr(gateway, "get_launchd_label", lambda: path.stem)
    monkeypatch.setattr(gateway_launchd, "get_launchd_label", lambda: path.stem)
    return home, path


def definition(home, label):
    return plistlib.dumps({"Label": label, "EnvironmentVariables": {"HERMES_HOME": str(home)}})


def test_automatic_uninstall_preserves_login_profile_and_guardian(tmp_path, monkeypatch):
    home, path = layout(tmp_path, monkeypatch)
    path.write_bytes(definition(home, path.stem))
    sibling = path.with_name("ai.hermes.gateway-login.plist")
    sibling_body = definition(home / "profiles/login", sibling.stem)
    sibling.write_bytes(sibling_body)
    guardian = path.with_name(f"{gateway_guardian.GUARDIAN_LABEL}.plist")
    guardian_body = definition(home, guardian.stem)
    guardian.write_bytes(guardian_body)
    calls = []
    monkeypatch.setattr(gateway_launchd, "_launchd_domain", lambda: "gui/501")
    monkeypatch.setattr(gateway_launchd.subprocess, "run", lambda argv, **kw: calls.append(argv) or
                        subprocess.CompletedProcess(argv, 113, stdout="", stderr=""))
    gateway_launchd.launchd_uninstall()
    assert not path.exists()
    assert sibling.read_bytes() == sibling_body
    assert guardian.read_bytes() == guardian_body
    assert calls == [["launchctl", "bootout", "gui/501/ai.hermes.gateway"]]


def test_automatic_uninstall_cleans_partial_migration_login_trigger(tmp_path, monkeypatch):
    home, path = layout(tmp_path, monkeypatch)
    path.write_bytes(definition(home, path.stem))
    trigger = path.with_name("ai.hermes.login-bootstrap.plist")
    trigger.write_bytes(definition(home, trigger.stem))
    uid = gateway_launchd.os.getuid()
    loaded = {f"gui/{uid}/{trigger.stem}"}
    monkeypatch.setattr(gateway_launchd, "_launchd_domain", lambda: f"gui/{uid}")
    def run(argv, **kwargs):
        if argv[1] == "bootout":
            loaded.discard(argv[2])
        return subprocess.CompletedProcess(argv, 0 if argv[2] in loaded else 113, stdout="", stderr="")
    monkeypatch.setattr(gateway_launchd.subprocess, "run", run)
    monkeypatch.setattr("gateway.status.live_gateway_pid_for_home", lambda *a: None)
    gateway_launchd.launchd_uninstall()
    assert not trigger.exists()
    assert not path.exists()
    assert not loaded


@pytest.mark.parametrize("strict", [False, True])
def test_explicit_pid_probe_failure_obeys_strict_contract(tmp_path, monkeypatch, strict):
    _, path = layout(tmp_path, monkeypatch)
    path.write_bytes(plistlib.dumps({"LimitLoadToSessionType": "Background"}))
    monkeypatch.setattr(gateway_launchd.subprocess, "run", lambda argv, **kw:
                        subprocess.CompletedProcess(argv, 5, stdout="", stderr="I/O error"))
    if strict:
        with pytest.raises(RuntimeError, match="cannot probe launchd supervised PID"):
            gateway_launchd._launchctl_supervised_pid(path.stem, strict=True)
    else:
        assert gateway_launchd._launchctl_supervised_pid(path.stem) is None


@pytest.mark.parametrize("failure,expected", [
    ("system", "macOS per-user option"),
    ("run-as-user", "macOS per-user option"),
    ("wrong-backend", "macOS per-user option"),
    ("no-login", "login registration"),
    ("preflight", "cannot inspect"),
    ("busy", "busy"),
])
def test_lifetime_install_errors_exit_cleanly(tmp_path, monkeypatch, capsys, failure, expected):
    home, path = layout(tmp_path, monkeypatch)
    args = SimpleNamespace(gateway_command="install", launchd_domain="user", start_now=False,
                           start_on_login=False if failure == "no-login" else None,
                           system=failure == "system", run_as_user="other" if failure == "run-as-user" else None)
    for name in ("_refuse_forward_only_leftovers", "_guard_named_profile_under_multiplexer"):
        monkeypatch.setattr(gateway, name, lambda **kw: None)
    monkeypatch.setattr(gateway, "is_managed", lambda: False)
    monkeypatch.setattr(gateway, "_service_mgmt_blocked", lambda: False)
    monkeypatch.setattr(gateway, "_service_backend", lambda: "systemd" if failure == "wrong-backend" else "launchd")
    monkeypatch.setattr(gateway, "_gateway_command_inner", gateway._cmd_install)
    monkeypatch.setattr(gateway, "generate_launchd_plist", lambda **kw: definition(home, path.stem).decode())
    monkeypatch.setattr("hermes_cli.gateway_service_owner.refuse_temp_home_service_write", lambda *a: False)
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 5, stdout="", stderr="I/O error")
    monkeypatch.setattr(lifetime.subprocess, "run", run)
    from contextlib import nullcontext
    with lifetime.lifetime_lock(home) if failure == "busy" else nullcontext():
        with pytest.raises(SystemExit) as exc:
            gateway.gateway_command(args)
    assert exc.value.code == 1
    output = capsys.readouterr()
    assert expected in output.out + output.err
    assert "Traceback" not in output.out + output.err
    assert all(call[1] == "print" for call in calls)
    assert not path.exists()
