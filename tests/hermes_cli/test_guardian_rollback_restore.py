"""A guardian rollback never leaves the gateway down for a release it cannot start."""
import json
import os
import plistlib
import subprocess

import pytest

import hermes_cli.gateway  # noqa: F401  (load the facade before patching gateway_launchd)
from hermes_cli import gateway_guardian as guardian
from tests.hermes_cli.test_gateway_guardian import layout


def _op_on(tmp_path):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    op = bindir / "op"
    op.write_text("#!/bin/sh\nexit 0\n")
    op.chmod(0o755)
    return str(bindir)


def _service(tmp_path, monkeypatch, *, path):
    home, plist, label, a, b = layout(tmp_path)
    definition = plistlib.loads(plist.read_bytes())
    definition["EnvironmentVariables"]["PATH"] = path
    plist.write_bytes(plistlib.dumps(definition))
    (home / "config.yaml").write_text("secrets:\n  onepassword:\n    enabled: true\n")
    calls = []

    def run(argv, **kwargs):
        calls.append(argv[1])
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    monkeypatch.setattr(guardian.subprocess, "run", run)
    from hermes_cli import gateway_launchd
    monkeypatch.setattr(gateway_launchd, "_launchctl_supervised_pid", lambda name, **_: None)
    return home, plist, label, a, b, calls


def _receipts(home):
    return [json.loads(p.read_text()) for p in sorted((home / "logs/guardian").glob("*.json"))]


@pytest.mark.platforms("macos")
def test_rollback_that_does_not_come_up_restores_the_running_release(tmp_path, monkeypatch):
    home, plist, label, a, b, calls = _service(tmp_path, monkeypatch, path=_op_on(tmp_path))
    running_body = plist.read_bytes()

    def rollback(home_, *, plist_path, plist_body, reload_callback):
        plist_path.write_bytes(plist_body)
        (home / "current").unlink()
        (home / "current").symlink_to(a)
        reload_callback()
        return {"reload_pending": False}

    activations = []

    def activate(home_, candidate, **kwargs):
        activations.append((candidate, kwargs))
        kwargs["plist_path"].write_bytes(kwargs["plist_body"])
        (home / "current").unlink()
        (home / "current").symlink_to(candidate)
        kwargs["reload_callback"]()
        return {"current": str(candidate)}

    monkeypatch.setattr(guardian, "rollback", rollback)
    monkeypatch.setattr("hermes_cli.immutable_releases.activate_release", activate)
    # The rolled-back release exits at startup; the release that was serving comes back.
    monkeypatch.setattr(guardian, "healthy", lambda home_, label_, release, *rest: release == b)

    monkeypatch.setattr(guardian, "ROLLBACK_SECONDS", 2)
    deadline = guardian.time.monotonic() + 2
    assert guardian.rollback_switch(home, plist, label, a, domain=f"gui/{os.getuid()}",
                                    deadline=deadline) is False

    [(candidate, kwargs)] = activations
    assert candidate == b and kwargs["operation"] == "restore"
    assert kwargs["plist_body"] == running_body  # the definition it last started with
    assert plist.read_bytes() == running_body and (home / "current").resolve() == b
    assert calls.count("bootstrap") == 2
    assert [(r["action"], r["outcome"]) for r in _receipts(home)] == [
        ("restore", "attempt"), ("restore", "restored")]


@pytest.mark.platforms("macos")
def test_rollback_definition_without_op_is_refused_before_the_gateway_stops(tmp_path, monkeypatch):
    home, plist, label, a, b, calls = _service(tmp_path, monkeypatch, path="/usr/bin:/bin:/usr/sbin:/sbin")
    monkeypatch.setattr("agent.secret_sources.onepassword.shutil.which",
                        lambda cmd, path=None: None)
    rollbacks = []
    monkeypatch.setattr(guardian, "rollback", lambda *a_, **k: rollbacks.append(k))

    with pytest.raises(RuntimeError, match="1Password op CLI"):
        guardian.rollback_switch(home, plist, label, a, domain=f"gui/{os.getuid()}")

    assert rollbacks == [] and "bootout" not in calls
    assert (home / "current").resolve() == b


@pytest.mark.platforms("macos")
def test_pinned_op_binary_satisfies_the_preflight(tmp_path, monkeypatch):
    bindir = _op_on(tmp_path)
    (tmp_path / "svc").mkdir()
    home, plist, *_ = _service(tmp_path / "svc", monkeypatch, path="/usr/bin:/bin")
    monkeypatch.setattr("agent.secret_sources.onepassword.shutil.which", lambda cmd, path=None: None)
    assert guardian.secret_preflight(home, plist.read_bytes()) is not None
    (home / "config.yaml").write_text(
        f"secrets:\n  onepassword:\n    enabled: true\n    binary_path: {bindir}/op\n")
    assert guardian.secret_preflight(home, plist.read_bytes()) is None
    (home / "config.yaml").write_text("secrets:\n  onepassword:\n    enabled: false\n")
    assert guardian.secret_preflight(home, plist.read_bytes()) is None


@pytest.mark.platforms("macos")
def test_failure_before_the_gateway_stops_still_fails_closed(tmp_path, monkeypatch):
    home, plist, label, a, b, calls = _service(tmp_path, monkeypatch, path=_op_on(tmp_path))

    def rollback(*args, **kwargs):
        raise RuntimeError("rollback gateway is not owned by the selected launchd service")

    activations = []
    monkeypatch.setattr(guardian, "rollback", rollback)
    monkeypatch.setattr("hermes_cli.immutable_releases.activate_release",
                        lambda *a_, **k: activations.append(k))
    with pytest.raises(RuntimeError, match="not owned"):
        guardian.rollback_switch(home, plist, label, a, domain=f"gui/{os.getuid()}")
    assert activations == [] and calls == []


def _failing_rollback(home, a, *, raise_after_bootout=False):
    def rollback(home_, *, plist_path, plist_body, reload_callback):
        plist_path.write_bytes(plist_body)
        (home / "current").unlink()
        (home / "current").symlink_to(a)
        reload_callback()
        if raise_after_bootout:
            raise RuntimeError("bootstrap of the rollback target failed")
        return {"reload_pending": False}
    return rollback


def _record_activations(monkeypatch, home):
    activations = []

    def activate(home_, candidate, **kwargs):
        activations.append((candidate, kwargs))
        (home / "current").unlink()
        (home / "current").symlink_to(candidate)
        return {"current": str(candidate)}

    monkeypatch.setattr("hermes_cli.immutable_releases.activate_release", activate)
    return activations


@pytest.mark.platforms("macos")
def test_exception_after_bootout_restores_the_serving_release(tmp_path, monkeypatch):
    home, plist, label, a, b, calls = _service(tmp_path, monkeypatch, path=_op_on(tmp_path))
    monkeypatch.setattr(guardian, "rollback", _failing_rollback(home, a, raise_after_bootout=True))
    activations = _record_activations(monkeypatch, home)
    monkeypatch.setattr(guardian, "healthy", lambda home_, label_, release, *rest: release == b)
    monkeypatch.setattr(guardian, "ROLLBACK_SECONDS", 2)
    assert guardian.rollback_switch(home, plist, label, a, domain=f"gui/{os.getuid()}",
                                    deadline=guardian.time.monotonic() + 2) is False
    assert [c for c, _ in activations] == [b] and (home / "current").resolve() == b


@pytest.mark.platforms("macos")
def test_slow_rollback_target_gets_its_own_startup_window(tmp_path, monkeypatch):
    """A rollback target still starting when the run deadline ends is not booted out."""
    home, plist, label, a, b, calls = _service(tmp_path, monkeypatch, path=_op_on(tmp_path))
    from hermes_cli import gateway_launchd
    monkeypatch.setattr(gateway_launchd, "_launchctl_supervised_pid", lambda name, **_: 123)
    monkeypatch.setattr(guardian, "rollback", _failing_rollback(home, a))
    activations = _record_activations(monkeypatch, home)
    window = {"open": False}
    real_await = guardian._await_healthy

    def await_healthy(home_, label_, release, deadline):
        if release == a and not window["open"]:
            window["open"] = True  # the run's own deadline expires first
            return False
        return release == a or real_await(home_, label_, release, deadline)

    monkeypatch.setattr(guardian, "_await_healthy", await_healthy)
    monkeypatch.setattr(guardian, "healthy", lambda home_, label_, release, *rest: release == b)
    monkeypatch.setattr(guardian, "STARTUP_SECONDS", 1)
    assert guardian.rollback_switch(home, plist, label, a, domain=f"gui/{os.getuid()}",
                                    deadline=guardian.time.monotonic() + 1) is True
    assert activations == [] and (home / "current").resolve() == a


@pytest.mark.platforms("macos")
def test_running_but_unhealthy_target_is_not_replaced(tmp_path, monkeypatch):
    home, plist, label, a, b, calls = _service(tmp_path, monkeypatch, path=_op_on(tmp_path))
    from hermes_cli import gateway_launchd
    monkeypatch.setattr(gateway_launchd, "_launchctl_supervised_pid", lambda name, **_: 123)
    monkeypatch.setattr(guardian, "rollback", _failing_rollback(home, a))
    activations = _record_activations(monkeypatch, home)
    monkeypatch.setattr(guardian, "healthy", lambda home_, label_, release, *rest: release == b)
    monkeypatch.setattr(guardian, "STARTUP_SECONDS", 1)
    assert guardian.rollback_switch(home, plist, label, a, domain=f"gui/{os.getuid()}",
                                    deadline=guardian.time.monotonic() + 1) is False
    assert activations == []
    assert _receipts(home)[-1]["reason"] == "rollback target still running"


@pytest.mark.platforms("macos")
def test_never_proven_serving_release_is_not_restored(tmp_path, monkeypatch):
    """An expired, never-acknowledged switch may never have started: do not reinstate it."""
    home, plist, label, a, b, calls = _service(tmp_path, monkeypatch, path=_op_on(tmp_path))
    monkeypatch.setattr(guardian, "rollback", _failing_rollback(home, a))
    activations = _record_activations(monkeypatch, home)
    monkeypatch.setattr(guardian, "healthy", lambda *args: False)
    assert guardian.rollback_switch(home, plist, label, a, domain=f"gui/{os.getuid()}",
                                    deadline=guardian.time.monotonic() + 1) is False
    assert activations == []
    assert _receipts(home)[-1]["reason"] == "serving release never proven"


@pytest.mark.platforms("macos")
def test_restore_leaves_an_operator_rollback_available(tmp_path, monkeypatch):
    """Real rollback() and activate_release(): a restore is not a completed rollback."""
    from datetime import datetime, timezone
    from hermes_cli import immutable_releases as releases
    home, plist, label, a, b, calls = _service(tmp_path, monkeypatch, path=_op_on(tmp_path))
    serving = str(b.resolve())

    def acknowledge(home_, **_):
        # Only the serving release's gateway ever comes up.
        paths = releases.ReleasePaths.for_home(home_)
        record = releases._read_txn(paths)
        if not record or record.get("candidate") != serving:
            return False
        record["reload_ack"] = {"at": datetime.now(timezone.utc).isoformat(), "gateway_pid": 1}
        record["reload_done"] = True
        releases._write_txn(paths, record)
        releases._finish_txn(paths, record)
        return True

    monkeypatch.setattr(releases, "acknowledge_running_release", acknowledge)
    monkeypatch.setattr(guardian, "healthy", lambda home_, label_, release, *rest: release == b)
    monkeypatch.setattr(guardian, "ROLLBACK_SECONDS", 2)
    assert guardian.rollback_switch(home, plist, label, a, domain=f"gui/{os.getuid()}",
                                    deadline=guardian.time.monotonic() + 2) is False
    assert (home / "current").resolve() == b.resolve()
    assert json.loads((home / "release-last-txn.json").read_text())["operation"] == "restore"
    assert [(r["action"], r["outcome"]) for r in _receipts(home)][-1] == ("restore", "restored")

    # The operator's own `hermes update --rollback` still goes back.
    result = releases.rollback(home, plist_path=plist, plist_body=plist.read_bytes(),
                               reload_callback=lambda: True)
    assert result["current"] == str(a.resolve()) and (home / "current").resolve() == a.resolve()
