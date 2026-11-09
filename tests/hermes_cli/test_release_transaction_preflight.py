"""Preflight refusal must not strand a recorded release switch."""
import json
import plistlib

import pytest

from hermes_cli import immutable_release_preflight as preflight
from hermes_cli import immutable_releases as releases
from tests.hermes_cli.test_release_extras_preflight import package, release, selected


def transition(tmp_path, monkeypatch):
    home = tmp_path / "home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr("pm.paths.repo_root", lambda: tmp_path / "frozen-source")
    z, a, b = [release(home, name, {"widget": "widget-client"}) for name in "ZAB"]
    for root in (z, a):
        package(selected(root, tmp_path, ["widget"]), "widget-client", "widget_client")
    releases.promote(home, z)
    releases.promote(home, a)
    return home, z, a, b


def crash(*args, **kwargs):
    raise RuntimeError("injected crash")


def source_checkout(root):
    import subprocess
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    subprocess.run(["git", "-C", str(root), "add", "pyproject.toml"], check=True)
    subprocess.run(["git", "-C", str(root), "commit", "-qm", "fixture"], check=True)


@pytest.mark.parametrize("fault", ["verify", "reload"])
@pytest.mark.parametrize("route", ["recover", "update-finish", "rollback"])
def test_applied_forward_replay_is_advisory_and_keeps_rollback_usable(
        tmp_path, monkeypatch, caplog, fault, route):
    home, _z, a, b = transition(tmp_path, monkeypatch)
    kwargs = {}
    if fault == "reload":
        plist = home / "fixture.plist"
        body = plistlib.dumps({"Label": "fixture"})
        plist.write_bytes(body)
        kwargs = dict(plist_path=plist, plist_body=body, reload_callback=crash)
    with monkeypatch.context() as context:
        # The initial budget/probe failure is advisory. Real probes on replay
        # will establish a definite regression against the healthy origin.
        context.setattr(preflight, "_check_activation", lambda *args: crash())
        if fault == "verify":
            context.setattr(releases, "_verify_transaction", crash)
        with pytest.raises(RuntimeError, match="injected crash"):
            releases.activate_release(home, b, **kwargs)
    assert releases.read_pointer(home / "current") == b
    assert (home / "release-txn.json").exists()

    def acknowledge(home, **kwargs):
        paths = releases.ReleasePaths.for_home(home)
        record = releases._read_txn(paths)
        releases._verify_transaction(paths, record)
        releases._finish_txn(paths, record)
        return True

    # Service observation is independent of dependency preflight. No real
    # supervisor is touched, and a replay must not issue another reload.
    monkeypatch.setattr(releases, "acknowledge_running_release", acknowledge)
    if route == "update-finish":
        from hermes_cli.update_cmd import _finish_pending_release_transaction
        _finish_pending_release_transaction(home)
    elif route == "rollback":
        releases.rollback(home)
    else:
        releases.recover_pending_transaction(home)
    assert not (home / "release-txn.json").exists()
    assert "already applied" in caplog.text
    assert "widget-client" in caplog.text
    releases.rollback(home)
    assert releases.read_pointer(home / "current") == a


def test_unapplied_replay_refuses_without_leaving_wal(tmp_path, monkeypatch):
    home, z, a, b = transition(tmp_path, monkeypatch)
    plist = home / "fixture.plist"
    body = plistlib.dumps({"Label": "fixture"})
    plist.write_bytes(body)
    with monkeypatch.context() as context:
        context.setattr(preflight, "_check_activation", lambda *args: crash())
        context.setattr(releases, "_run_transaction", crash)
        with pytest.raises(RuntimeError, match="injected crash"):
            releases.activate_release(home, b, plist_path=plist, plist_body=body)
    record = releases._read_txn(releases.ReleasePaths.for_home(home))
    backup = record["plist"]["backup"]
    with pytest.raises(RuntimeError, match="refused"):
        releases.recover_pending_transaction(home)
    assert releases.read_pointer(home / "current") == a
    assert releases.read_pointer(home / "previous") == z
    assert not (home / "release-txn.json").exists()
    from pathlib import Path
    assert not Path(backup).exists()
    assert plist.read_bytes() == body
    releases.rollback(home)
    assert releases.read_pointer(home / "current") == z


@pytest.mark.parametrize("started", ["previous", "current", "reload"])
def test_each_applied_intent_signal_prevents_refusal(tmp_path, monkeypatch, started):
    home, _z, a, b = transition(tmp_path, monkeypatch)
    with monkeypatch.context() as context:
        context.setattr(preflight, "_check_activation", lambda *args: crash())
        context.setattr(releases, "_run_transaction", crash)
        with pytest.raises(RuntimeError, match="injected crash"):
            releases.promote(home, b)
    paths = releases.ReleasePaths.for_home(home)
    if started == "reload":
        record = releases._read_txn(paths)
        record["reload_issued"] = {"attempt": 1}
        releases._write_txn(paths, record)
    else:
        releases._set_pointer(getattr(paths, started), str(a if started == "previous" else b))
    releases.recover_pending_transaction(home)
    assert releases.read_pointer(home / "current") == b
    assert not (home / "release-txn.json").exists()


@pytest.mark.parametrize("started", [False, True])
def test_pending_first_migration_preflight_respects_applied_pointers(tmp_path, monkeypatch, caplog, started):
    home = tmp_path / "home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr("pm.paths.repo_root", lambda: tmp_path / "frozen-source")
    source = release(home, "source", {"widget": "widget-client"})
    source_checkout(source)
    candidate = release(home, "B", {"widget": "widget-client"})
    package(selected(source, tmp_path, ["widget"]), "widget-client", "widget_client")
    # Exercise the real migration WAL/layout writer with a fixture interpreter.
    monkeypatch.setattr(releases, "_source_python_valid", lambda *args: True)
    paths = releases.ReleasePaths.for_home(home)
    record = releases._migration_record(paths, source, None, source_python=source / ".venv/bin/python")
    releases._write_txn(paths, record)
    if started:
        releases.recover_pending_transaction(home)
        releases.activate_release(home, candidate)
        assert releases.read_pointer(home / "current") == candidate
        assert "already applied" in caplog.text
    else:
        with pytest.raises(RuntimeError, match="refused"):
            releases.activate_release(home, candidate)
    assert not (home / "release-txn.json").exists()


def test_refusal_does_not_claim_current_pointer_was_unchanged(tmp_path, monkeypatch):
    home, _z, a, b = transition(tmp_path, monkeypatch)
    releases._set_pointer(home / "current", str(b))
    with pytest.raises(RuntimeError, match="refused") as refusal:
        preflight.activation_preflight(b, a, home)
    assert "current release was not changed" not in str(refusal.value)


def test_applied_first_migration_replay_completes(tmp_path, monkeypatch, caplog):
    home = tmp_path / "home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr("pm.paths.repo_root", lambda: tmp_path / "frozen-source")
    source = release(home, "source", {"widget": "widget-client"})
    source_checkout(source)
    candidate = release(home, "B", {"widget": "widget-client"})
    package(selected(source, tmp_path, ["widget"]), "widget-client", "widget_client")
    monkeypatch.setattr(releases, "_source_python_valid", lambda *args: True)
    with monkeypatch.context() as context:
        context.setattr(preflight, "_check_activation", lambda *args: crash())
        context.setattr(releases, "_verify_transaction", crash)
        with pytest.raises(RuntimeError, match="injected crash"):
            releases.activate_release(home, candidate, source=source,
                                      source_python=source / ".venv/bin/python")
    releases.recover_pending_transaction(home)
    assert releases.read_pointer(home / "current") == candidate
    assert not (home / "release-txn.json").exists()
    assert json.loads((home / "release-layout.json").read_text(encoding="utf-8-sig"))["state"] == "done"
    assert "already applied" in caplog.text


def test_guardian_abandon_bypasses_forward_preflight(tmp_path, monkeypatch):
    home, _z, a, b = transition(tmp_path, monkeypatch)
    plist = home / "fixture.plist"
    body = plistlib.dumps({"Label": "fixture"})
    plist.write_bytes(body)
    with monkeypatch.context() as context:
        context.setattr(preflight, "_check_activation", lambda *args: crash())
        with pytest.raises(RuntimeError, match="injected crash"):
            releases.activate_release(home, b, plist_path=plist, plist_body=body, reload_callback=crash)
    releases.abandon_failed_switch(home, candidate=b, previous=a)
    releases.rollback(home)
    assert releases.read_pointer(home / "current") == a
    assert not (home / "release-txn.json").exists()
    archived = list(home.glob("release-abandoned-*.json"))
    assert len(archived) == 1
    assert json.loads(archived[0].read_text(encoding="utf-8-sig"))["candidate"] == str(b)
