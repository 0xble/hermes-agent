"""Additional fake-boundary regressions for lifetime rollback and stop races."""
import plistlib
import json
import os
from datetime import UTC, datetime
import subprocess
from pathlib import Path
from types import SimpleNamespace

import psutil
import pytest

from hermes_cli import gateway, gateway_guardian, gateway_launchd_lifetime as lifetime
from hermes_cli.gateway_launchd_login import login_bootstrap, login_plist

pytestmark = pytest.mark.platforms("linux", "macos")


def test_real_writer_must_drain_after_wrapper_exit(tmp_path, monkeypatch):
    class Writer:
        pid = 7711
        def wait(self, timeout):
            raise psutil.TimeoutExpired(timeout, self.pid)
    monkeypatch.setattr(gateway_guardian, "_rollback_owners", lambda *a: (7711, [Writer()]))
    monkeypatch.setattr(gateway, "_wait_for_pid_exit", lambda *a: True)
    loaded = True
    def run(argv, **kwargs):
        nonlocal loaded
        if argv[1] == "bootout":
            loaded = False
        return SimpleNamespace(returncode=0 if loaded or argv[1] == "bootout" else 113,
                               stdout="pid = 7710\n", stderr="")
    with pytest.raises(RuntimeError, match="writer PID 7711"):
        lifetime._unload(run, "user/501", "ai.hermes.gateway", home=tmp_path)


def test_stop_after_probe_prevents_login_bootstrap(tmp_path):
    path = tmp_path / "ai.hermes.gateway.plist"
    path.write_bytes(plistlib.dumps({"Label": path.stem, "LimitLoadToSessionType": "Background",
                                    "EnvironmentVariables": {"HERMES_HOME": str(tmp_path)}}))
    calls, logs = [], []
    def run(argv, **kwargs):
        calls.append(argv)
        assert argv[1] == "print"
        if argv[2].startswith("gui/"):
            gateway_guardian.set_intent(tmp_path, stopped=True)
        return SimpleNamespace(returncode=113, stdout="", stderr="")
    login_bootstrap(tmp_path, path, runner=run, log=logs.append)
    assert logs == ["ai.hermes.gateway: skipped: stopped intent"]


def test_guardian_repair_and_stop_share_lifetime_lock(tmp_path):
    with lifetime.lifetime_lock(tmp_path):
        assert gateway_guardian.run_once(tmp_path, tmp_path / "gateway.plist", "ai.hermes.gateway") == "locked"
    (tmp_path / ".gateway-planned-stop.json").write_text(json.dumps({"target_pid": os.getpid(), "written_at": datetime.now(UTC).isoformat()}))
    assert gateway_guardian.run_once(tmp_path, tmp_path / "gateway.plist", "ai.hermes.gateway") == "stopped"


def test_immutable_login_trigger_uses_stable_current_script(tmp_path):
    (tmp_path / "current").mkdir()
    body = plistlib.loads(login_plist(tmp_path, tmp_path / "gateway.plist", "ai.hermes.gateway"))
    assert body["ProgramArguments"][:3] == [str(tmp_path / "current/.venv/bin/python"), "-I",
                                           str(tmp_path / "current/hermes_cli/gateway_launchd_login.py")]


def test_immutable_renderer_keeps_current_working_directory(tmp_path, monkeypatch):
    from hermes_cli import gateway_launchd
    release = tmp_path / "releases" / ("a" * 40)
    (release / ".venv/bin").mkdir(parents=True)
    (release / ".venv/bin/python").touch()
    (tmp_path / "current").symlink_to(release, target_is_directory=True)
    monkeypatch.setattr(gateway, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(gateway, "get_launchd_plist_path", lambda: tmp_path / "gateway.plist")
    body = plistlib.loads(gateway_launchd.generate_launchd_plist(release_target=release, domain="user").encode())
    assert body["WorkingDirectory"] == str(tmp_path / "current")
    assert str(tmp_path / "current/.venv/bin/python") in " ".join(body["ProgramArguments"])


def test_explicit_reload_helper_cannot_revive_operator_stop(tmp_path, monkeypatch):
    import ast
    import shlex
    import sys
    from hermes_cli import gateway_launchd
    original_run = subprocess.run
    calls = []
    monkeypatch.setattr(gateway_launchd, "configured_launchd_domain", lambda *a: "user/501")
    monkeypatch.setattr(gateway, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(gateway, "get_python_path", lambda: sys.executable)
    monkeypatch.setattr(gateway_launchd, "_mark_planned_gateway_restart", lambda *a: None)
    monkeypatch.setattr(gateway_launchd.subprocess, "run", lambda argv, **kw: calls.append(argv))
    assert gateway_launchd._spawn_deferred_launchd_reload(plist_path=tmp_path / "gateway.plist",
                                                          label="ai.hermes.gateway", domain="user/501",
                                                          target="user/501/ai.hermes.gateway", gateway_pid=9911)
    wrapper_args = shlex.split(calls[0][-1])
    assert wrapper_args[:3] == [sys.executable, "-I", "-c"]
    wrapper = wrapper_args[3]
    ast.parse(wrapper)
    assert "fcntl.LOCK_EX" in wrapper
    assert "launchctl list" not in wrapper
    assert "launchctl print user/501/ai.hermes.gateway" in wrapper
    assert "    break;   fi;   sleep 1; done;" not in wrapper
    (tmp_path / "logs").mkdir(exist_ok=True)
    gateway_guardian.set_intent(tmp_path, stopped=True)
    bin_path = tmp_path / "bin"
    bin_path.mkdir()
    output = tmp_path / "launchctl-calls"
    fake = bin_path / "launchctl"
    fake.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >> ' + shlex.quote(str(output)) + '\n')
    fake.chmod(0o700)
    result = original_run(wrapper_args, env={**os.environ, "PATH": str(bin_path)}, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    assert output.read_text().startswith("remove ai.hermes.gateway.reload.")
    assert "bootstrap" not in output.read_text()


def test_explicit_restart_holds_same_lifetime_lock_as_operator_stop(tmp_path, monkeypatch):
    from hermes_cli import gateway_launchd
    monkeypatch.setattr(gateway_launchd, "configured_launchd_domain", lambda: "user/501")
    monkeypatch.setattr(gateway, "get_hermes_home", lambda: tmp_path)
    def restart():
        with lifetime.lifetime_lock(tmp_path, blocking=False) as acquired:
            assert acquired is False
        return "serialized"
    monkeypatch.setattr(gateway_launchd, "_launchd_restart_locked", restart)
    assert gateway_launchd.launchd_restart() == "serialized"


def test_reinstall_preserves_explicit_migration_path(monkeypatch):
    from hermes_cli import gateway_launchd
    calls = []
    monkeypatch.setattr(gateway_launchd, "configured_launchd_domain", lambda: "user/501")
    monkeypatch.setattr(gateway_launchd, "migrate_launchd_domain", lambda *a, **kw: calls.append((a, kw)))
    gateway_launchd.launchd_install(start_now=False)
    assert calls == [(("user",), {"start_now": False})]


@pytest.mark.parametrize("uid, domain", [(501, "user/501"), (501, "gui/501"), (0, "system")])
def test_signal_stop_budget_follows_actual_launchd_domain(uid, domain):
    from gateway.restart import read_launchd_exit_timeout_s, resolve_launchd_capped_drain
    calls = []
    def run(argv, **kwargs):
        calls.append(argv)
        loaded = argv[2] == f"{domain}/ai.hermes.gateway"
        return subprocess.CompletedProcess(argv, 0 if loaded else 113,
                                           stdout="exit timeout = 60\n" if loaded else "")
    budget = read_launchd_exit_timeout_s("ai.hermes.gateway", uid=uid, platform="darwin", run=run)
    assert resolve_launchd_capped_drain(180.0, budget) == 50.0
    expected = [] if domain != "user/501" else [["launchctl", "print", "gui/501/ai.hermes.gateway"]]
    assert calls == expected + [["launchctl", "print", f"{domain}/ai.hermes.gateway"]]
