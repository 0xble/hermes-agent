"""Observed activation distinguishes ignored and import-affecting launch environments."""
from __future__ import annotations

import json
from types import SimpleNamespace

import psutil
import pytest

from hermes_cli import gateway_launchd, immutable_releases as releases
from hermes_cli._launchers import runtime_command
from tests.hermes_cli.test_immutable_release_transactions import _fixture


@pytest.mark.platforms("macos")
@pytest.mark.parametrize("launcher", ["generated", "module", "other-release", "foreign-code"])
@pytest.mark.parametrize("pythonpath", ["", "/selected/project:/selected/site-packages"])
def test_pending_reload_recovery_qualifies_import_environment(tmp_path, monkeypatch, launcher, pythonpath):
    home, _, plist, a, b, _, _, intended = _fixture(tmp_path, "promote")
    assert releases.activate_release(
        home, b, plist_path=plist, plist_body=intended,
        reload_callback=lambda: "deferred",
    )["reload_pending"]
    argv = runtime_command(b, ["gateway", "run", "--external-supervisor"], python=b / ".venv/bin/python")
    if launcher == "module":
        argv = [argv[0], "-m", "hermes_cli.main", "gateway", "run"]
    elif launcher == "other-release":
        argv = runtime_command(a, ["gateway", "run"], python=argv[0])
    elif launcher == "foreign-code":
        argv[3] = "import time; time.sleep(60)"
    interpreter = tmp_path / "interpreter"
    monkeypatch.setattr(releases, "_interpreter_process_executable", lambda _: interpreter)
    gateway = SimpleNamespace(
        pid=31416, cmdline=lambda: argv, exe=lambda: str(interpreter), cwd=lambda: str(b),
        environ=lambda: {"HERMES_HOME": str(home), "PYTHONPATH": pythonpath},
    )
    supervisor = SimpleNamespace(pid=31415, children=lambda **_: [gateway], cmdline=lambda: ["wrapper"])
    monkeypatch.setattr(gateway_launchd, "_launchctl_supervised_pid", lambda *_, **__: supervisor.pid)
    monkeypatch.setattr(psutil, "Process", lambda _: supervisor)

    def forbidden_reload():
        pytest.fail("recovery must observe the existing gateway, never reload it")

    result = releases.recover_pending_transaction(home, reload_callback=forbidden_reload)
    assert result is not None
    expected_ack = launcher == "generated" or (launcher == "module" and not pythonpath)
    assert bool(result.get("reload_pending")) is not expected_ack
    assert (home / "release-txn.json").exists() is not expected_ack
    if expected_ack:
        completed = json.loads((home / "release-last-txn.json").read_text())
        assert completed["reload_ack"]["gateway_pid"] == gateway.pid
        assert completed["reload_ack"]["code_sha"] == b.name
        assert completed["reload_ack"]["plist_sha256"] == completed["plist"]["intended_sha256"]
        assert completed["reload_issued"]["attempt"] == 1
        assert releases.recover_pending_transaction(home, reload_callback=forbidden_reload) is None


@pytest.mark.platforms("macos")
def test_pending_reload_recovery_skips_zombie_descendant(tmp_path, monkeypatch):
    """A gateway's zombie child, scanned first, does not abandon the acknowledgement."""
    home, _, plist, _a, b, _, _, intended = _fixture(tmp_path, "promote")
    assert releases.activate_release(
        home, b, plist_path=plist, plist_body=intended,
        reload_callback=lambda: "deferred",
    )["reload_pending"]
    argv = runtime_command(b, ["gateway", "run", "--external-supervisor"], python=b / ".venv/bin/python")
    interpreter = tmp_path / "interpreter"
    monkeypatch.setattr(releases, "_interpreter_process_executable", lambda _: interpreter)

    def zombie_cmdline():
        raise psutil.ZombieProcess(31417)

    zombie = SimpleNamespace(pid=31417, cmdline=zombie_cmdline)
    gateway = SimpleNamespace(
        pid=31416, cmdline=lambda: argv, exe=lambda: str(interpreter), cwd=lambda: str(b),
        environ=lambda: {"HERMES_HOME": str(home), "PYTHONPATH": "/selected/project:/selected/site-packages"},
    )
    supervisor = SimpleNamespace(pid=31415, children=lambda **_: [zombie, gateway], cmdline=lambda: ["wrapper"])
    monkeypatch.setattr(gateway_launchd, "_launchctl_supervised_pid", lambda *_, **__: supervisor.pid)
    monkeypatch.setattr(psutil, "Process", lambda _: supervisor)

    assert releases.acknowledge_running_release(home)
    assert not (home / "release-txn.json").exists()
