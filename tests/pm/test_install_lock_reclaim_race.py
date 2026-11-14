"""Reclaiming an orphan install state never leaves two holders of its install lock
(``pm/install_states.py``, ``hermes_cli.runtime_state.runtime_lock``)."""

from __future__ import annotations

import contextlib
import os
import threading
from pathlib import Path

import pm.environments
import pm.install_states
import pytest
from hermes_cli.runtime_state import runtime_lock
from pm.environments import install_key, install_state_lock_path
from pm.install_states import collect_orphan_install_states
from pm.filesystem import lock_path


def _lock_file(checkout, lock_kind):
    state = pm.environments.install_state_dir(checkout)
    return install_state_lock_path(state) if lock_kind == "stable" else state / ".install.lock"


@contextlib.contextmanager
def _launcher_lock(checkout, lock_kind, *, timeout):
    if lock_kind == "stable":
        with runtime_lock(checkout, timeout=timeout) as held:
            yield held
        return
    # Legacy launchers still use the collector's deletable lock. Keep the real
    # inode-validation retry path under the same adversarial interleaving.
    fd = lock_path(_lock_file(checkout, lock_kind), timeout=timeout, create_dir=True)
    try:
        yield fd is not None
    finally:
        if fd is not None:
            os.close(fd)


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("lock_kind", ["stable", "legacy"])
def test_launcher_waiting_on_a_reclaimed_state_lock_excludes_later_launchers(tmp_path, monkeypatch, lock_kind):
    """A launcher from a checkout re-created at the recorded path opens the lock while the
    collector holds it, then gets it when the collector lets go. The stable lock survives
    reclamation; a legacy waiter retries on the replacement inode. Either holder must
    still exclude the next launcher, or two installers write one state."""
    installs = tmp_path / "home" / "installs"
    checkout = tmp_path / "home" / "cache" / "scratch" / "clone"  # provably deleted: reclaimed
    state = installs / install_key(checkout)
    (state / "inputs").mkdir(parents=True)
    (state / "inputs" / ".project-root").write_text(str(checkout), encoding="utf-8")
    (state / "environments" / "gen" / "venv").mkdir(parents=True)
    monkeypatch.setattr(pm.environments, "installs_root", lambda: installs)
    lock = _lock_file(checkout, lock_kind)

    opened, held, done = threading.Event(), threading.Event(), threading.Event()
    real_open = os.open

    def observed_open(path, *args, **kwargs):
        fd = real_open(path, *args, **kwargs)
        if threading.current_thread().name == "launcher-a" and os.fspath(path) == os.fspath(lock):
            opened.set()
        return fd

    def launcher_a():
        with _launcher_lock(checkout, lock_kind, timeout=None) as got:
            if got:
                held.set()
            done.wait(30)

    a = threading.Thread(target=launcher_a, name="launcher-a", daemon=True)
    real_leased = pm.install_states._leased

    def checkout_recreated_mid_reclaim(path):
        # Called under the collector's lock, after its orphan re-check.
        checkout.mkdir(parents=True)
        a.start()
        assert opened.wait(30)
        return real_leased(path)

    monkeypatch.setattr(os, "open", observed_open)
    monkeypatch.setattr(pm.install_states, "_leased", checkout_recreated_mid_reclaim)
    try:
        collect_orphan_install_states(installs)
        assert held.wait(30)
        with _launcher_lock(checkout, lock_kind, timeout=0) as launcher_b:
            assert launcher_b is False, "two launchers hold the same state's install lock"
    finally:
        done.set()
        a.join(30)


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("lock_kind", ["stable", "legacy"])
def test_lock_file_removal_never_lands_after_a_waiting_launcher_got_the_lock(tmp_path, monkeypatch, lock_kind):
    """Worst interleaving for the collector's lock-file removal: if it happens once the
    collector has let go, a launcher already waiting on that file gets the lock first, and the
    removal then hands the next launcher a fresh, unheld file. Removal has to happen while
    the collector still holds the lock."""
    installs = tmp_path / "home" / "installs"
    checkout = tmp_path / "home" / "cache" / "scratch" / "clone"
    state = installs / install_key(checkout)
    (state / "inputs").mkdir(parents=True)
    (state / "inputs" / ".project-root").write_text(str(checkout), encoding="utf-8")
    (state / "environments" / "gen" / "venv").mkdir(parents=True)
    monkeypatch.setattr(pm.environments, "installs_root", lambda: installs)
    lock = _lock_file(checkout, lock_kind)

    opened, held, released, done = (threading.Event() for _ in range(4))
    collector = threading.current_thread()
    fence_name = "_install_fence" if lock_kind == "stable" else "_install_lock"
    real_open, real_unlink = os.open, Path.unlink
    real_install_lock = getattr(pm.install_states, fence_name)

    def observed_open(path, *args, **kwargs):
        fd = real_open(path, *args, **kwargs)
        if threading.current_thread().name == "launcher-a" and os.fspath(path) == os.fspath(lock):
            opened.set()
        return fd

    @contextlib.contextmanager
    def observed_install_lock(path):
        with real_install_lock(path) as locked:
            yield locked
        released.set()

    def unlink_late(self, *args, **kwargs):
        if threading.current_thread() is collector and self == lock and released.is_set():
            assert held.wait(30)  # the lock is free: the waiting launcher takes it first
        return real_unlink(self, *args, **kwargs)

    def launcher_a():
        with _launcher_lock(checkout, lock_kind, timeout=None) as got:
            if got:
                held.set()
            done.wait(30)

    a = threading.Thread(target=launcher_a, name="launcher-a", daemon=True)
    real_leased = pm.install_states._leased

    def checkout_recreated_mid_reclaim(path):
        checkout.mkdir(parents=True)
        a.start()
        assert opened.wait(30)
        return real_leased(path)

    monkeypatch.setattr(os, "open", observed_open)
    monkeypatch.setattr(Path, "unlink", unlink_late)
    monkeypatch.setattr(pm.install_states, fence_name, observed_install_lock)
    monkeypatch.setattr(pm.install_states, "_leased", checkout_recreated_mid_reclaim)
    try:
        collect_orphan_install_states(installs)
        assert held.wait(30)
        with _launcher_lock(checkout, lock_kind, timeout=0) as launcher_b:
            assert launcher_b is False, "two launchers hold the same state's install lock"
    finally:
        done.set()
        a.join(30)


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("lock_kind", ["stable", "legacy"])
def test_unopenable_lock_path_fails_instead_of_retrying(tmp_path, monkeypatch, lock_kind):
    """A lock path that can never be opened (a dangling symlink) raises at once; only a parent
    directory removed mid-open is worth retrying, and never past the timeout."""
    installs = tmp_path / "installs"
    checkout = tmp_path / "clone"
    monkeypatch.setattr(pm.environments, "installs_root", lambda: installs)
    lock = _lock_file(checkout, lock_kind)
    lock.parent.mkdir(parents=True)
    lock.symlink_to(tmp_path / "gone" / "target")
    outcome: list[BaseException] = []

    def launcher():
        try:
            with _launcher_lock(checkout, lock_kind, timeout=0):
                pass
        except BaseException as exc:
            outcome.append(exc)

    t = threading.Thread(target=launcher, daemon=True)
    t.start()
    t.join(30)
    assert not t.is_alive(), "lock_path retries an open that can never succeed"
    assert len(outcome) == 1 and isinstance(outcome[0], FileNotFoundError)
