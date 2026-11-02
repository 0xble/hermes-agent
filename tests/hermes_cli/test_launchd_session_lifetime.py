"""An explicit Background install owns one label in one domain, including rollback."""
import plistlib
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import gateway, gateway_guardian, gateway_launchd

pytestmark = pytest.mark.platforms("linux", "macos")


def definition(home, label="ai.hermes.gateway", session=None):
    data = {"Label": label, "EnvironmentVariables": {"HERMES_HOME": str(home)}}
    if session:
        data["LimitLoadToSessionType"] = session
    return plistlib.dumps(data)


@pytest.mark.parametrize("mode,session", [("user", "Background"), ("gui", "Aqua")])
def test_explicit_domain_survives_definition_refresh(tmp_path, monkeypatch, mode, session):
    home = tmp_path / "home"
    home.mkdir()
    path = tmp_path / "ai.hermes.gateway.plist"
    monkeypatch.setattr(gateway, "get_hermes_home", lambda: home)
    monkeypatch.setattr(gateway, "get_launchd_plist_path", lambda: path)
    monkeypatch.setattr(gateway, "get_launchd_label", lambda: "ai.hermes.gateway")
    path.write_text(gateway_launchd.generate_launchd_plist(domain=mode))
    refreshed = plistlib.loads(gateway_launchd.generate_launchd_plist().encode())
    assert refreshed["LimitLoadToSessionType"] == session
    assert refreshed["ProgramArguments"][0] == "/usr/bin/osascript"
    assert refreshed["ProcessType"] == "Interactive"
    domain = f"{mode}/{gateway_launchd.os.getuid()}"
    guardian = plistlib.loads(gateway_guardian.guardian_plist(home, path, "ai.hermes.gateway", domain=domain))
    assert guardian["LimitLoadToSessionType"] == session
    assert guardian["ProgramArguments"][0] == str(home / "current/.venv/bin/python")
    assert gateway_launchd.configured_launchd_domain() == domain


@pytest.mark.parametrize("failure", [None, "bootstrap", "bootstrap-stopped", "bootstrap-parked", "guardian-bootstrap", "drain", "foreign", "duplicate", "legacy", "pending-reload", "uninspectable"])
def test_domain_migration_never_overlaps_and_rolls_back(tmp_path, monkeypatch, failure):
    home = tmp_path / "home"
    home.mkdir()
    path = tmp_path / "ai.hermes.gateway.plist"
    guardian_path = path.with_name("ai.hermes.gateway-guardian.plist")
    label, guard = "ai.hermes.gateway", "ai.hermes.gateway-guardian"
    uid = gateway_launchd.os.getuid()
    old, new = f"gui/{uid}", f"user/{uid}"
    original = definition(home if failure != "foreign" else tmp_path / "other", session="Aqua")
    path.write_bytes(original)
    guardian_original = definition(home, guard, "Aqua")
    guardian_path.write_bytes(guardian_original)
    loaded: dict[tuple[str, str], int | None] = {(old, label): 110, (old, guard): 111}
    if failure == "bootstrap-stopped":
        gateway_guardian.set_intent(home, stopped=True)
    if failure == "bootstrap-parked":
        loaded[(old, label)] = None
    if failure == "duplicate":
        loaded[(new, label)] = 112
    if failure == "legacy":
        loaded[(old, "ai.hermes")] = 113
    calls = []
    failed = False

    def run(argv, **kwargs):
        nonlocal failed
        calls.append(argv)
        action = argv[1]
        if action == "print":
            if argv[2] in {old, new}:
                return SimpleNamespace(returncode=1 if failure == "uninspectable" else 0,
                                       stdout=f"{label}.reload.9876" if failure == "pending-reload" else "", stderr="")
            domain, _, target = argv[2].rpartition("/")
            pid = loaded.get((domain, target))
            return SimpleNamespace(returncode=0 if (domain, target) in loaded else 113,
                                   stdout=f"pid = {pid}\n" if pid else "", stderr="")
        if action == "bootout":
            domain, _, target = argv[2].rpartition("/")
            loaded.pop((domain, target), None)
        elif action == "bootstrap":
            domain, file = argv[2:]
            target = plistlib.loads(Path(file).read_bytes())["Label"]
            assert not any(key[1] == target for key in loaded), "two writers were bootstrapped"
            if ((failure in {"bootstrap", "bootstrap-stopped", "bootstrap-parked"} or failure == "guardian-bootstrap" and target == guard)
                    and domain == new and not failed):
                failed = True
                raise subprocess.CalledProcessError(5, argv)
            loaded[(domain, target)] = 210
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(gateway, "get_hermes_home", lambda: home)
    monkeypatch.setattr(gateway, "get_launchd_plist_path", lambda: path)
    monkeypatch.setattr(gateway, "get_launchd_label", lambda: label)
    monkeypatch.setattr(gateway, "_prepare_service_launcher", lambda: None)
    monkeypatch.setattr(gateway_guardian, "_rollback_owners", lambda *a: (None, []))
    monkeypatch.setattr("hermes_cli.gateway_service_owner.refuse_temp_home_service_write", lambda *a: False)
    monkeypatch.setattr(gateway, "launchd_gateway_labels_for_install", lambda: [label])
    monkeypatch.setattr(gateway, "legacy_launchd_labels_for_install", lambda: ["ai.hermes"] if failure == "legacy" else [])
    monkeypatch.setattr(gateway, "_wait_for_pid_exit", lambda *a: failure != "drain")
    monkeypatch.setattr(gateway, "generate_launchd_plist", lambda **kw: definition(home, session="Background" if kw["domain"] == "user" else "Aqua").decode())
    monkeypatch.setattr("gateway.status.get_running_pid", lambda **kw: None)
    monkeypatch.setattr("gateway.status.live_gateway_pid_for_home", lambda *a: None)
    if failure:
        with pytest.raises((RuntimeError, subprocess.CalledProcessError)):
            gateway_launchd.migrate_launchd_domain("user", start_now=True, runner=run)
        assert path.read_bytes() == original
        assert guardian_path.read_bytes() == guardian_original
        if failure != "duplicate":
            assert not any(key[0] == new for key in loaded)
        if failure in {"bootstrap", "guardian-bootstrap"}:
            assert set(loaded) == {(old, label), (old, guard)}
        if failure in {"bootstrap-stopped", "bootstrap-parked"}:
            assert set(loaded) == {(old, guard)}
            assert gateway_guardian.intent_path(home).exists()
        if failure in {"pending-reload", "uninspectable"}:
            assert all(call[1] == "print" for call in calls)
        if failure == "foreign":
            assert not calls
    else:
        gateway_launchd.migrate_launchd_domain("user", start_now=True, runner=run)
        assert set(loaded) == {(new, label), (new, guard)}
        shim = path.with_name("ai.hermes.gateway-login.plist")
        shim_body = plistlib.loads(shim.read_bytes())
        assert shim_body["RunAtLoad"] is True
        assert shim_body["LimitLoadToSessionType"] == "Aqua"
        assert "hermes_cli.gateway_launchd_login" in " ".join(shim_body["ProgramArguments"])
        assert not gateway_guardian.intent_path(home).exists()
        before = list(calls)
        gateway_launchd.migrate_launchd_domain("user", start_now=True, runner=run)
        assert all(call[1] == "print" for call in calls[len(before):]), "idempotent install restarted a writer"
        gateway_launchd.migrate_launchd_domain("gui", start_now=False, runner=run)
        assert (new, label) not in loaded
        assert not shim.exists()
        assert gateway_guardian.intent_path(home).exists()
        assert plistlib.loads(path.read_bytes())["LimitLoadToSessionType"] == "Aqua"
        before = list(calls)
        gateway_launchd.migrate_launchd_domain("gui", start_now=False, runner=run)
        assert all(call[1] == "print" for call in calls[len(before):])
        assert gateway_guardian.intent_path(home).exists()
        gateway_launchd.uninstall_launchd_lifetime(runner=run)
        assert not loaded
        assert not path.exists() and not guardian_path.exists()


@pytest.mark.parametrize("mode", ["user", "gui"])
def test_unloaded_job_uses_persisted_domain_not_shell(tmp_path, monkeypatch, mode):
    path = tmp_path / "ai.hermes.gateway.plist"
    path.write_bytes(definition(tmp_path, session="Background" if mode == "user" else "Aqua"))
    monkeypatch.setattr(gateway, "get_launchd_plist_path", lambda: path)
    monkeypatch.setattr(gateway, "get_launchd_label", lambda: "ai.hermes.gateway")
    def absent(argv, **kwargs):
        if kwargs.get("check"):
            raise subprocess.CalledProcessError(113, argv)
        return SimpleNamespace(returncode=113, stdout="", stderr="")
    monkeypatch.setattr(gateway_launchd.subprocess, "run", absent)
    assert gateway_launchd._probe_launchd_domain_for_label("ai.hermes.gateway") == f"{mode}/{gateway_launchd.os.getuid()}"


def test_domain_argument_is_validated():
    with pytest.raises(ValueError, match="domain"):
        gateway_launchd.generate_launchd_plist(domain="system")
    with pytest.raises(ValueError, match="domain"):
        gateway_launchd.migrate_launchd_domain("system", start_now=False)


def test_guardian_keeps_background_owner_when_gui_domain_disappears(monkeypatch):
    from hermes_cli import gateway_launchd_lifetime
    uid = gateway_launchd.os.getuid()
    def run(argv, **kwargs):
        if argv[2].startswith("gui/"):
            return SimpleNamespace(returncode=112, stdout="", stderr=f"Bad request.\nCould not find domain for user gui: {uid}\n")
        return SimpleNamespace(returncode=0, stdout="pid = 42\n", stderr="")
    monkeypatch.setattr(gateway_guardian.subprocess, "run", run)
    assert gateway_guardian._gateway_domain("ai.hermes.gateway", f"user/{uid}") == f"user/{uid}"
    assert gateway_launchd_lifetime._state(run, f"gui/{uid}", "ai.hermes.gateway") == (False, None)
