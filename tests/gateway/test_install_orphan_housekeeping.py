"""Dependency installs of deleted checkouts are collected by the gateway housekeeping tick.

Every agent worktree that runs Hermes commits a full environment under ``<root>/installs/<key>``.
Only a release publish collected them, so a host whose agents make a worktree per task piled up
hundreds of orphaned environments between publishes.
"""

import os
import time

import gateway.run as gateway_run
from pm.environments import install_key, record_install_use

HOUR = 60 * 60


def _install(home, checkout, idle_seconds, *, deleted):
    checkout.mkdir()
    record_install_use(checkout)
    state = home / "installs" / install_key(checkout)
    old = time.time() - idle_seconds
    os.utime(state / "install.json", (old, old))
    if deleted:
        checkout.rmdir()
    return state


def test_housekeeping_collects_installs_of_checkouts_deleted_and_idle_for_a_day(monkeypatch, tmp_path):
    home = tmp_path / "home"
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(gateway_run, "_install_orphan_thread", None)
    idle_orphan = _install(home, tmp_path / "removed-yesterday", 25 * HOUR, deleted=True)
    recent_orphan = _install(home, tmp_path / "removed-today", 23 * HOUR, deleted=True)
    live = _install(home, tmp_path / "live-worktree", 48 * HOUR, deleted=False)

    gateway_run._housekeeping_install_orphans()
    gateway_run._install_orphan_thread.join(timeout=30)

    assert not gateway_run._install_orphan_thread.is_alive()
    assert not idle_orphan.exists()
    assert recent_orphan.is_dir()
    assert live.is_dir()
    assert not list((home / "installs").glob(".gc-*"))
