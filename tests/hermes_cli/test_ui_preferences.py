"""The machine's UI preferences store (#145213): one file beside the ROOT Hermes home, shared by
native Desktop and every browser that opens this machine's Webapp.

Contracts: a profile's HERMES_HOME resolves the same file as the root; the one-time localStorage
import never overwrites an existing store; writes merge per key (concurrent writers keep each
other's keys); only Desktop-prefixed string keys are stored; the route exists only on the Webapp
and only past the session-token gate.
"""
from __future__ import annotations

import json
import multiprocessing
import os
import threading
import time

import pytest
from fastapi.testclient import TestClient

from hermes_cli import ui_preferences, web_server
from tests.waiting import wait_until


def test_a_profile_home_and_the_root_home_resolve_the_same_store(monkeypatch, tmp_path):
    root = tmp_path / "custom-root"
    (root / "profiles" / "coder").mkdir(parents=True)

    monkeypatch.setenv("HERMES_HOME", str(root))
    from_root = ui_preferences.store_path()
    monkeypatch.setenv("HERMES_HOME", str(root / "profiles" / "coder"))
    from_profile = ui_preferences.store_path()

    assert from_root == from_profile == root / "ui-preferences.json"


def test_a_custom_home_under_the_native_home_is_its_own_root(monkeypatch, tmp_path):
    """Native Desktop keeps an explicit ``HERMES_HOME`` that is not a profile as the root; the
    Webapp's server must resolve the same file, not collapse it to the native home."""
    sandbox = tmp_path / ".hermes" / "sandbox"
    sandbox.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(sandbox))

    assert ui_preferences.store_path() == sandbox.resolve() / "ui-preferences.json"


def test_the_seed_import_only_adds_keys_and_never_creates_an_empty_store(tmp_path):
    """A client's first contact fills what the store lacks: the client that has data fills a
    store an empty client reached first, and nothing already stored is overwritten."""
    path = tmp_path / "ui-preferences.json"
    empty = ui_preferences.write_preferences({}, seed=True, path=path)
    first = ui_preferences.write_preferences({"hermes.desktop.sessionListDensity": "detailed"},
                                             seed=True, path=path)
    second = ui_preferences.write_preferences({"hermes.desktop.sessionListDensity": "compact",
                                               "hermes-desktop-mode-v1": "dark"}, seed=True, path=path)

    assert empty == {"exists": False, "revision": 0, "values": {}}
    assert first["values"] == {"hermes.desktop.sessionListDensity": "detailed"}
    assert second["values"] == {"hermes.desktop.sessionListDensity": "detailed", "hermes-desktop-mode-v1": "dark"}
    assert second["generation"] == first["generation"]
    assert json.loads(path.read_text(encoding="utf-8-sig"))["values"] == second["values"]


@pytest.mark.platforms("linux", "macos")
def test_an_unreadable_or_corrupt_store_is_never_merged_over(tmp_path):
    """Only a missing file is no store. An unreadable one refuses the write (the client retries)
    and stays intact; a corrupt one is kept aside and replaced by a NEW generation, which every
    open window follows whatever revision it had seen."""
    path = tmp_path / "ui-preferences.json"
    kept = ui_preferences.write_preferences({"hermes-desktop-theme-v2": "nous", "hermes.desktop.keybinds": "{}"},
                                            path=path)
    before = path.read_bytes()
    path.chmod(0)
    try:
        if os.access(path, os.R_OK):
            pytest.skip("running as a user that reads mode-000 files (root)")
        with pytest.raises(ui_preferences.PreferencesBusy):
            ui_preferences.write_preferences({"hermes-desktop-mode-v1": "dark"}, path=path)
    finally:
        path.chmod(0o600)
    assert path.read_bytes() == before

    torn = before[:-5]
    path.write_bytes(torn)
    assert ui_preferences.read_preferences(path)["exists"] is False
    after = ui_preferences.write_preferences({"hermes-desktop-mode-v1": "dark"}, path=path)

    backups = list(tmp_path.glob("ui-preferences.json.corrupt-*"))
    assert [backup.read_bytes() for backup in backups] == [torn]
    assert after["values"] == {"hermes-desktop-mode-v1": "dark"}
    assert after["generation"] != kept["generation"]


def test_lock_waits_for_a_live_holder_and_keeps_the_lock_unchanged_until_release(tmp_path, monkeypatch):
    """A live holder must release a waiter; contention is not data loss."""
    path = tmp_path / "ui-preferences.json"
    lock_dir = path.with_name(path.name + ".lock")
    owner = lock_dir / "owner"
    lock_dir.mkdir()
    owner.write_text(str(os.getpid()), encoding="utf-8")
    seen_existing = threading.Event()
    polls = []
    original_break_if_stale = ui_preferences._break_if_stale

    def observe_break(existing_lock_dir):
        polls.append(existing_lock_dir)
        seen_existing.set()
        return original_break_if_stale(existing_lock_dir)

    monkeypatch.setattr(ui_preferences, "_break_if_stale", observe_break)
    errors = []

    def wait_write():
        try:
            ui_preferences.write_preferences({"hermes.desktop.waiter": "released"}, path=path)
        except BaseException as error:  # pragma: no cover - only used to surface thread failures
            errors.append(error)

    waiter = threading.Thread(target=wait_write)
    waiter.start()
    assert seen_existing.wait(10)
    # Several unchanged polls: the waiter has looked at the live lock 3 times and kept waiting.
    wait_until(lambda: len(polls) >= 3, timeout=10, what="3 polls of the live lock")
    assert waiter.is_alive()
    assert lock_dir.is_dir()
    assert owner.read_text(encoding="utf-8-sig") == str(os.getpid())
    assert not errors

    owner.unlink()
    lock_dir.rmdir()
    waiter.join(10)

    assert not waiter.is_alive()
    assert not errors
    assert ui_preferences.read_preferences(path)["values"] == {"hermes.desktop.waiter": "released"}


def test_lock_reclaims_a_dead_pid(tmp_path):
    path = tmp_path / "ui-preferences.json"
    lock_dir = path.with_name(path.name + ".lock")
    lock_dir.mkdir()
    (lock_dir / "owner").write_text("2147483647-dead", encoding="utf-8")

    written = ui_preferences.write_preferences({"hermes.desktop.reclaimed": "yes"}, path=path)

    assert written["values"] == {"hermes.desktop.reclaimed": "yes"}
    assert not lock_dir.exists()


def test_lock_reclaims_an_ownerless_lock_dir_once_it_is_stale(tmp_path):
    """A writer that died between mkdir and the owner write leaves no owner file; it must age out."""
    path = tmp_path / "ui-preferences.json"
    lock_dir = path.with_name(path.name + ".lock")
    lock_dir.mkdir()
    old = time.time() - ui_preferences._LOCK_STALE_S - 1
    os.utime(lock_dir, (old, old))

    written = ui_preferences.write_preferences({"hermes.desktop.reclaimed": "ownerless"}, path=path)

    assert written["values"] == {"hermes.desktop.reclaimed": "ownerless"}
    assert not lock_dir.exists()


def test_a_fresh_ownerless_lock_dir_is_left_for_its_writer(tmp_path):
    """A contender between mkdir and its owner write must not be broken while the dir is fresh."""
    path = tmp_path / "ui-preferences.json"
    lock_dir = path.with_name(path.name + ".lock")
    lock_dir.mkdir()

    ui_preferences._break_if_stale(lock_dir)

    assert lock_dir.is_dir()


def test_lock_reclaims_a_live_pid_when_the_owner_age_backstop_expires(tmp_path):
    path = tmp_path / "ui-preferences.json"
    lock_dir = path.with_name(path.name + ".lock")
    owner = lock_dir / "owner"
    lock_dir.mkdir()
    owner.write_text(str(os.getpid()), encoding="utf-8")
    old = time.time() - ui_preferences._LOCK_MAX_AGE_S - 1
    os.utime(owner, (old, old))

    written = ui_preferences.write_preferences({"hermes.desktop.reclaimed": "old"}, path=path)

    assert written["values"] == {"hermes.desktop.reclaimed": "old"}
    assert not lock_dir.exists()


def test_lock_retries_when_an_owner_appears_during_stale_recovery(tmp_path, monkeypatch):
    path = tmp_path / "ui-preferences.json"
    lock_dir = path.with_name(path.name + ".lock")
    owner = lock_dir / "owner"
    lock_dir.mkdir()
    old = time.time() - ui_preferences._LOCK_STALE_S - 1
    os.utime(lock_dir, (old, old))
    new_owner = f"{os.getpid()}-holder"
    rename = os.rename

    def publish_owner_before_quarantine(source, target):
        if source == lock_dir:
            # The holder publishes its token after the waiter judged the directory ownerless.
            owner.write_text(new_owner, encoding="utf-8")
        rename(source, target)

    def release_on_poll(_seconds):
        assert owner.read_text(encoding="utf-8") == new_owner
        assert not path.exists()
        owner.unlink()
        lock_dir.rmdir()

    clock = iter((0.0, 0.0, ui_preferences._LOCK_WAIT_S))
    monkeypatch.setattr(os, "rename", publish_owner_before_quarantine)
    monkeypatch.setattr(ui_preferences, "_monotonic", lambda: next(clock))
    monkeypatch.setattr(ui_preferences, "_sleep", release_on_poll)

    values = {"hermes.desktop.waiter": "released"}
    ui_preferences.write_preferences(values, path=path)

    assert ui_preferences.read_preferences(path)["values"] == values
    assert not list(tmp_path.glob("ui-preferences.json.lock*"))


def test_lock_backstop_raises_preferences_busy_without_waiting(tmp_path, monkeypatch):
    path = tmp_path / "ui-preferences.json"
    lock_dir = path.with_name(path.name + ".lock")
    owner = lock_dir / "owner"
    lock_dir.mkdir()
    owner.write_text(str(os.getpid()), encoding="utf-8")
    clock = iter((0.0, ui_preferences._LOCK_WAIT_S + 1))
    monkeypatch.setattr(ui_preferences, "_monotonic", lambda: next(clock))
    monkeypatch.setattr(ui_preferences, "_sleep", lambda _seconds: None)

    with pytest.raises(ui_preferences.PreferencesBusy, match="preferences store is busy"):
        ui_preferences.write_preferences({"hermes.desktop.stuck": "yes"}, path=path)

    assert lock_dir.is_dir()
    assert owner.read_text(encoding="utf-8-sig") == str(os.getpid())


def test_stale_recovery_does_not_move_a_successor_observed_before_quarantine(tmp_path, monkeypatch):
    path = tmp_path / "ui-preferences.json"
    lock_dir = path.with_name(path.name + ".lock")
    owner = lock_dir / "owner"
    lock_dir.mkdir()
    owner.write_text("2147483647-dead", encoding="utf-8")
    old = time.time() - ui_preferences._LOCK_MAX_AGE_S - 1
    os.utime(owner, (old, old))
    successor = f"{os.getpid()}-successor"
    owner_age = ui_preferences._owner_age
    rename = os.rename

    def replace_owner_during_age_check(directory):
        age = owner_age(directory)
        owner.write_text(successor, encoding="utf-8")
        return age

    def check_rename(source, target):
        assert source != lock_dir, "stale recovery moved the fresh owner's lock"
        rename(source, target)

    def release_on_poll(_seconds):
        assert owner.read_text(encoding="utf-8") == successor
        assert not path.exists()
        owner.unlink()
        lock_dir.rmdir()

    monkeypatch.setattr(ui_preferences, "_owner_age", replace_owner_during_age_check)
    monkeypatch.setattr(os, "rename", check_rename)
    monkeypatch.setattr(ui_preferences, "_sleep", release_on_poll)

    values = {"hermes.desktop.waiter": "released"}
    assert ui_preferences.write_preferences(values, path=path)["values"] == values
    assert not lock_dir.exists()


def test_writes_merge_per_key_and_concurrent_writers_keep_each_others_keys(tmp_path):
    path = tmp_path / "ui-preferences.json"
    ui_preferences.write_preferences({"hermes-desktop-mode-v1": "dark", "hermes.desktop.panesFlipped": "true"},
                                     path=path)

    # Separate processes, like native Desktop and the Webapp's server: only the directory lock
    # stands between their read-merge-replace cycles.
    keys = [f"hermes.desktop.k{n}" for n in range(8)]
    spawn = multiprocessing.get_context("spawn")
    writers = [spawn.Process(target=ui_preferences.write_preferences, args=({key: "1"},),
                             kwargs={"path": path}) for key in keys]
    for writer in writers:
        writer.start()
    for writer in writers:
        writer.join(60)
    assert [writer.exitcode for writer in writers] == [0] * len(keys)
    after = ui_preferences.write_preferences({"hermes.desktop.panesFlipped": None}, path=path)

    assert after["values"] == {"hermes-desktop-mode-v1": "dark", **{key: "1" for key in keys}}
    assert after["revision"] == 2 + len(keys)
    assert not path.with_name(path.name + ".lock").exists()


@pytest.mark.parametrize("changes", [{"hermes.agent.secret": "x"}, {"hermes.desktop.n": 3}, {"": "x"}])
def test_only_desktop_string_keys_are_stored(tmp_path, changes):
    path = tmp_path / "ui-preferences.json"

    with pytest.raises(ui_preferences.PreferencesError):
        ui_preferences.write_preferences(changes, path=path)
    assert not path.exists()


@pytest.fixture
def webapp_state(monkeypatch):
    for key in ("ui_surface", "gateway_runner", "listener_public_url", "webapp_home"):
        if hasattr(web_server.app.state, key):
            monkeypatch.delattr(web_server.app.state, key)
    monkeypatch.setattr(web_server.app.state, "auth_required", False, raising=False)
    monkeypatch.setattr(web_server.app.state, "ui_surface", "webapp", raising=False)


def _client(token: bool) -> TestClient:
    client = TestClient(web_server.app, raise_server_exceptions=False)
    if token:
        client.headers[web_server._SESSION_HEADER_NAME] = web_server._SESSION_TOKEN
    return client


def test_the_webapp_reads_and_writes_the_machine_store_behind_the_session_gate(webapp_state):
    from hermes_constants import get_default_hermes_root

    store = get_default_hermes_root() / "ui-preferences.json"
    anonymous = _client(token=False)

    assert anonymous.get("/api/ui-preferences").status_code == 401
    assert anonymous.patch("/api/ui-preferences", json={"values": {"hermes-desktop-mode-v1": "dark"}}).status_code == 401
    assert not store.exists()

    tab = _client(token=True)
    assert tab.get("/api/ui-preferences").json() == {"exists": False, "revision": 0, "values": {}}
    written = tab.patch("/api/ui-preferences", json={"values": {"hermes-desktop-mode-v1": "dark"}})
    assert written.status_code == 200
    assert json.loads(store.read_text(encoding="utf-8-sig"))["values"] == {"hermes-desktop-mode-v1": "dark"}
    assert tab.patch("/api/ui-preferences", json={"values": {"hermes.other": "x"}}).status_code == 400


@pytest.mark.parametrize("surface", [None, "serve", "dashboard"])
def test_the_route_exists_only_on_the_webapp(monkeypatch, webapp_state, surface):
    if surface is None:
        monkeypatch.delattr(web_server.app.state, "ui_surface")
    else:
        monkeypatch.setattr(web_server.app.state, "ui_surface", surface, raising=False)

    assert _client(token=True).get("/api/ui-preferences").status_code == 404
