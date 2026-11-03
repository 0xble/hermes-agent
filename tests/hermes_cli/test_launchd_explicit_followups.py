"""Explicit lifetime operations must preserve other profiles and operator intent."""
import plistlib
import subprocess
from pathlib import Path

import pytest

from hermes_cli import gateway as gw, gateway_guardian as guardian
from hermes_cli import gateway_launchd as launchd, gateway_launchd_lifetime as lifetime
from hermes_cli.gateway_launchd_login import _login_trigger_label, login_bootstrap

pytestmark = pytest.mark.platforms("posix")


def definition(home, label, session):
    return plistlib.dumps({"Label": label, "EnvironmentVariables": {"HERMES_HOME": str(home)},
                          "LimitLoadToSessionType": session})


@pytest.fixture
def layout(tmp_path, monkeypatch):
    home = tmp_path / "profiles/work"
    home.mkdir(parents=True)
    path = tmp_path / "ai.hermes.gateway-work.plist"
    label = path.stem
    uid = launchd.os.getuid()
    gui, user = f"gui/{uid}", f"user/{uid}"
    loaded, calls = {}, []

    def run(argv, **kwargs):
        calls.append(argv)
        action = argv[1]
        if action == "print":
            if argv[2] in {gui, user}:
                return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")
            domain, _, name = argv[2].rpartition("/")
            pid = loaded.get((domain, name))
            return subprocess.CompletedProcess(argv, 0 if (domain, name) in loaded else 113,
                                               stdout=f"pid = {pid}\n" if pid else "", stderr="")
        if action == "bootout":
            domain, _, name = argv[2].rpartition("/")
            loaded.pop((domain, name), None)
        elif action == "bootstrap":
            domain, source = argv[2:]
            name = plistlib.loads(Path(source).read_bytes())["Label"]
            assert not any(key[1] == name for key in loaded), "duplicate writer"
            loaded[(domain, name)] = 211
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(gw, "get_hermes_home", lambda: home)
    monkeypatch.setattr(gw, "get_launchd_plist_path", lambda: path)
    monkeypatch.setattr(gw, "get_launchd_label", lambda: label)
    monkeypatch.setattr(gw, "_resolved_launchd_domain", None)
    monkeypatch.setattr(launchd, "get_launchd_label", lambda: label)
    monkeypatch.setattr(gw, "_prepare_service_launcher", lambda: None)
    monkeypatch.setattr(gw, "launchd_gateway_labels_for_install", lambda: [label])
    monkeypatch.setattr(gw, "legacy_launchd_labels_for_install", lambda: [])
    monkeypatch.setattr(gw, "_wait_for_pid_exit", lambda *a: True)
    monkeypatch.setattr(guardian, "_rollback_owners", lambda *a: (None, []))
    monkeypatch.setattr("gateway.status.get_running_pid", lambda **kw: None)
    monkeypatch.setattr("gateway.status.live_gateway_pid_for_home", lambda *a: None)
    monkeypatch.setattr("hermes_cli.gateway_service_owner.refuse_temp_home_service_write", lambda *a: False)
    monkeypatch.setattr(lifetime.subprocess, "run", run)
    return home, path, label, gui, user, loaded, calls, run


@pytest.mark.parametrize("operation", ["uninstall", "migrate", "login"])
def test_profile_lifetime_leaves_shared_guardian_with_its_owner(layout, operation):
    home, path, label, gui, user, loaded, calls, run = layout
    path.write_bytes(definition(home, label, "Background" if operation != "migrate" else "Aqua"))
    shared = path.with_name(f"{guardian.GUARDIAN_LABEL}.plist")
    original = definition(home.parent.parent, guardian.GUARDIAN_LABEL, "Aqua")
    shared.write_bytes(original)
    loaded[(gui, guardian.GUARDIAN_LABEL)] = 110
    if operation == "uninstall":
        loaded[(user, label)] = 111
        lifetime.uninstall_launchd_lifetime(runner=run)
        assert not path.exists()
    elif operation == "migrate":
        loaded[(gui, label)] = 111
        lifetime.migrate_launchd_domain("user", start_now=True, runner=run)
        assert (user, label) in loaded
    else:
        login_bootstrap(home, path, runner=run)
        assert (user, label) in loaded
    assert shared.read_bytes() == original
    assert loaded[(gui, guardian.GUARDIAN_LABEL)] == 110
    assert all(guardian.GUARDIAN_LABEL not in " ".join(call) for call in calls)


def test_missing_plist_start_preserves_explicit_domain_from_login_trigger(layout):
    home, path, label, _, user, loaded, _, run = layout
    from hermes_cli.gateway_launchd_login import login_plist
    trigger = path.with_name(f"{_login_trigger_label(label)}.plist")
    trigger.write_bytes(login_plist(home, path, label))
    launchd.launchd_start()
    assert plistlib.loads(path.read_bytes())["LimitLoadToSessionType"] == "Background"
    assert (user, label) in loaded
    assert trigger.exists()


def test_force_same_explicit_domain_refreshes_running_definition(layout, monkeypatch):
    home, path, label, _, user, loaded, _, _ = layout
    path.write_bytes(definition(home, label, "Background"))
    loaded[(user, label)] = 211
    monkeypatch.setattr("gateway.status.get_running_pid", lambda **kw: 211)
    refreshed = []
    monkeypatch.setattr(gw, "_launchctl_bootstrap", lambda *args, **kw: refreshed.append(args))
    launchd.launchd_install(force=True, start_now=True, domain="user")
    assert refreshed == [(user, path, label)]
    assert "ProgramArguments" in plistlib.loads(path.read_bytes())
    assert (user, label) in loaded


def test_partial_retirement_reports_exact_unloaded_targets_and_repair(layout, capsys):
    home, path, label, gui, _, loaded, _, run = layout
    path.write_bytes(definition(home, label, "Aqua"))
    shared = path.with_name(f"{guardian.GUARDIAN_LABEL}.plist")
    shared.write_bytes(definition(home, shared.stem, "Aqua"))
    trigger = path.with_name(f"{_login_trigger_label(label)}.plist")
    trigger.write_bytes(definition(home, trigger.stem, "Aqua"))
    for name in (label, shared.stem, trigger.stem):
        loaded[(gui, name)] = 211

    def fail_guardian(argv, **kw):
        if argv[1:3] == ["bootout", f"{gui}/{shared.stem}"]:
            return subprocess.CompletedProcess(argv, 5, stdout="", stderr="I/O error")
        return run(argv, **kw)

    with pytest.raises(RuntimeError) as exc:
        lifetime.migrate_launchd_domain("user", start_now=False, runner=fail_guardian)
    report = str(exc.value) + capsys.readouterr().out
    assert f"Unloaded: {gui}/{trigger.stem}" in report
    assert "Repair:" in report and "install --launchd-domain gui" in report
    assert (gui, trigger.stem) not in loaded
    assert (gui, label) in loaded and (gui, shared.stem) in loaded
    assert (home / "gateway-guardian-stopped").exists()


@pytest.mark.parametrize("operation", ["status", "start"])
def test_unexpected_explicit_probe_code_is_cli_guidance(layout, monkeypatch, capsys, operation):
    from types import SimpleNamespace
    home, path, label, _, _, _, _, _ = layout
    path.write_bytes(definition(home, label, "Background"))
    monkeypatch.setattr(launchd.subprocess, "run", lambda argv, **kw:
                        subprocess.CompletedProcess(argv, 5, stdout="", stderr="I/O error"))
    monkeypatch.setattr(gw, "_gateway_command_inner", lambda args:
                        launchd.launchd_status() if operation == "status" else launchd._launchd_domain())
    try:
        gw.gateway_command(SimpleNamespace(gateway_command=operation))
    except SystemExit as exc:
        assert exc.code == 1
    output = capsys.readouterr().out
    assert "cannot inspect launchd target" in output
    assert "I/O error" in output
    assert "Traceback" not in output


@pytest.mark.parametrize("operation", ["start", "stop"])
def test_cli_reports_waiting_for_lifetime_lock(layout, monkeypatch, capsys, operation):
    import threading
    home, path, label, _, _, _, _, _ = layout
    path.write_bytes(definition(home, label, "Background"))
    waiting, entered, errors = threading.Event(), threading.Event(), []
    import builtins
    original_print = builtins.print

    def observe(*args, **kwargs):
        original_print(*args, **kwargs)
        if "waiting for lifetime lock" in " ".join(map(str, args)).lower():
            waiting.set()

    monkeypatch.setattr(builtins, "print", observe)
    monkeypatch.setattr(launchd, f"_launchd_{operation}_locked", entered.set)

    def invoke():
        try:
            getattr(launchd, f"launchd_{operation}")()
        except Exception as exc:
            errors.append(exc)

    with lifetime.lifetime_lock(home):
        worker = threading.Thread(target=invoke)
        worker.start()
        announced = waiting.wait(5)
        assert not entered.is_set()
    worker.join(5)
    assert not worker.is_alive() and not errors
    assert entered.is_set()
    assert announced, capsys.readouterr().out


def test_reload_drain_timeout_action_is_generated_from_fail_closed_parameter():
    action = launchd._reload_drain_timeout_action
    assert action("reload-label", fail_closed=False) == "break;"
    assert action("reload-label", fail_closed=True) == "launchctl remove reload-label 2>/dev/null; exit 1;"
    import inspect
    assert ".replace(" not in inspect.getsource(launchd._spawn_deferred_launchd_reload)
