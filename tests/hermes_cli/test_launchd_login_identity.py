"""Login bootstraps are not gateway fleet members or profile service identities."""
import plistlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import gateway, gateway_launchd_login, update_cmd_fleet

pytestmark = pytest.mark.platforms("linux", "macos")


@pytest.mark.parametrize("suffix", ["", "-work"])
def test_fleet_restart_does_not_kickstart_installed_login_trigger(tmp_path, monkeypatch, suffix):
    import pwd

    home = tmp_path / "hermes"
    home.mkdir()
    agents = tmp_path / "Library/LaunchAgents"
    agents.mkdir(parents=True)
    label = f"ai.hermes.gateway{suffix}"
    path = agents / f"{label}.plist"
    monkeypatch.setattr(gateway, "get_python_path", lambda: "/usr/bin/python3")
    trigger = gateway_launchd_login.login_plist(home, path, label)
    trigger_label = plistlib.loads(trigger)["Label"]
    (agents / f"{trigger_label}.plist").write_bytes(trigger)
    monkeypatch.setattr(pwd, "getpwuid", lambda _: SimpleNamespace(pw_dir=str(tmp_path)))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(gateway, "get_launchd_label", lambda: label)
    monkeypatch.setattr(gateway, "get_launchd_plist_path", lambda: path)
    monkeypatch.setattr(gateway, "launchd_gateway_labels_for_install", lambda: [label])
    monkeypatch.setattr("hermes_cli.update_cmd_posix_pause.already_restarted", lambda: {"labels": set()})
    monkeypatch.setattr(gateway, "_wait_for_launchd_service_pid", lambda *a, **kw: False)
    calls = []

    def launchctl(argv, **kwargs):
        calls.append(argv)
        assert argv[0] == "launchctl"
        if argv[1] == "print":
            loaded = argv[2].startswith("gui/") and argv[2].endswith(f"/{trigger_label}")
            return SimpleNamespace(returncode=0 if loaded else 113, stdout="", stderr="")
        assert argv[1] == "kickstart"
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(gateway.subprocess, "run", launchctl)
    restarted, failed = [], []
    update_cmd_fleet._restart_macos_launchd_gateways(
        restarted, failed, drain_budget=0.0, acknowledged_label=label,
    )
    assert [call for call in calls if call[1] == "kickstart"] == []
    assert restarted == [label]
    assert failed == []


def test_login_profile_and_root_trigger_have_distinct_labels_and_paths(tmp_path, monkeypatch):
    root = tmp_path / ".hermes"
    root.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(gateway, "_launchd_account_home", lambda: tmp_path)
    monkeypatch.setattr(gateway, "_home_owns_bare_service_name", lambda home: home == root)
    monkeypatch.setattr(gateway, "get_python_path", lambda: "/usr/bin/python3")
    monkeypatch.setenv("HERMES_HOME", str(root))
    root_path = gateway.get_launchd_plist_path()
    trigger = plistlib.loads(gateway_launchd_login.login_plist(root, root_path, gateway.get_launchd_label()))
    trigger_path = root_path.with_name(f"{trigger['Label']}.plist")

    profile = root / "profiles/login"
    profile.mkdir(parents=True)
    (profile / "config.yaml").write_text("{}\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(profile))
    profile_label = gateway.get_launchd_label()
    profile_path = gateway.get_launchd_plist_path()
    assert trigger["Label"] != profile_label
    assert trigger_path != profile_path
    profile_trigger = plistlib.loads(gateway_launchd_login.login_plist(profile, profile_path, profile_label))
    assert profile_trigger["Label"] not in {trigger["Label"], profile_label}


@pytest.mark.parametrize("profile", ["", "-login"])
def test_full_uninstall_removes_login_bootstrap(tmp_path, monkeypatch, profile):
    from hermes_cli import uninstall

    agents = tmp_path / "LaunchAgents"
    agents.mkdir()
    gateway_path = agents / f"ai.hermes.gateway{profile}.plist"
    trigger = agents / f"ai.hermes.login-bootstrap{profile}.plist"
    trigger.write_bytes(plistlib.dumps({"Label": trigger.stem}))
    unrelated = agents / "com.example.other.plist"
    unrelated.write_bytes(plistlib.dumps({"Label": unrelated.stem}))
    monkeypatch.setattr(gateway, "get_launchd_plist_path", lambda: gateway_path)
    calls = []

    def launchctl(argv, **kwargs):
        calls.append(argv)
        assert argv[0] == "launchctl"
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(uninstall.subprocess, "run", launchctl)
    assert uninstall._remove_launchd_gateway() is True
    assert not trigger.exists()
    assert unrelated.exists()
    assert any(call[1] == "bootout" and call[2].endswith(f"/{trigger.stem}") for call in calls)


def test_lifetime_uninstall_removes_registered_login_trigger(tmp_path, monkeypatch):
    from hermes_cli import gateway_launchd_lifetime as lifetime

    home = tmp_path / "hermes"
    home.mkdir()
    label = "ai.hermes.gateway"
    path = tmp_path / f"{label}.plist"
    path.write_bytes(plistlib.dumps({"Label": label, "LimitLoadToSessionType": "Background",
                                    "EnvironmentVariables": {"HERMES_HOME": str(home)}}))
    trigger = tmp_path / "ai.hermes.login-bootstrap.plist"
    trigger.write_bytes(plistlib.dumps({"Label": trigger.stem,
                                       "EnvironmentVariables": {"HERMES_HOME": str(home)}}))
    loaded = {f"gui/{gateway.os.getuid()}/{trigger.stem}"}
    monkeypatch.setattr(gateway, "get_hermes_home", lambda: home)
    monkeypatch.setattr(gateway, "get_launchd_label", lambda: label)
    monkeypatch.setattr(gateway, "get_launchd_plist_path", lambda: path)
    monkeypatch.setattr(gateway, "launchd_gateway_labels_for_install", lambda: [label])
    monkeypatch.setattr(gateway, "legacy_launchd_labels_for_install", lambda **kw: [])
    monkeypatch.setattr("hermes_cli.gateway_service_owner.refuse_temp_home_service_write", lambda *a: False)
    monkeypatch.setattr("gateway.status.get_running_pid", lambda **kw: None)
    monkeypatch.setattr("gateway.status.live_gateway_pid_for_home", lambda *a: None)

    def launchctl(argv, **kwargs):
        assert argv[0] == "launchctl"
        if argv[1] == "bootout":
            loaded.remove(argv[2])
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        assert argv[1] == "print"
        return SimpleNamespace(returncode=0 if argv[2] in loaded or argv[2].count("/") == 1 else 113,
                               stdout="", stderr="")

    lifetime.uninstall_launchd_lifetime(runner=launchctl)
    assert not loaded
    assert not trigger.exists()
    assert not path.exists()
