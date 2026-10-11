"""Camofox tab lease failure and cleanup behavior."""

import os

import pytest


def test_lease_root_follows_explicit_process_home_and_preserves_default(tmp_path, monkeypatch):
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    from tools import browser_camofox_leases as leases
    import runpy
    # The tools autouse fixture replaces _lease_root for ordinary tests. Load the
    # resolver without that patch; all its paths still point into this sandbox.
    lease_root = runpy.run_path(leases.__file__)["_lease_root"]
    monkeypatch.delenv("HERMES_HOME", raising=False)
    monkeypatch.setattr(leases.Path, "home", lambda: tmp_path)
    assert lease_root() == tmp_path / ".local/state/hermes/camofox-tab-leases"
    monkeypatch.setenv("LEASE_TEST_ROOT", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", "$LEASE_TEST_ROOT/process")
    token = set_hermes_home_override(tmp_path / "task-profile")
    try:
        assert lease_root() == tmp_path / "process/state/camofox-tab-leases"
    finally:
        reset_hermes_home_override(token)


def test_first_claim_prunes_only_unlocked_residue_once(tmp_path, monkeypatch):
    from tools import browser_camofox_leases as leases
    root = tmp_path / "leases"
    root.mkdir()
    monkeypatch.setattr(leases, "_lease_root", lambda: root)
    monkeypatch.setattr(leases, "_pruned", False)
    idle = leases._lease_path(("server", "user", "idle"))
    held = leases._lease_path(("server", "user", "held"))
    idle.touch()
    with open(held, "w+b") as holder:
        assert leases._try_lock(holder)
        assert leases.claim("server", "user", "first")
        try:
            assert not idle.exists()
            assert held.exists()
            idle.touch()  # no rescan on a subsequent claim
            assert leases.claim("server", "user", "second")
            assert idle.exists()
        finally:
            leases.release("server", "user", "first")
            leases.release("server", "user", "second")


@pytest.mark.skipif(os.name != "posix", reason="POSIX inode/flock unlink race")
def test_pruning_never_unlinks_a_replacement_inode(tmp_path, monkeypatch):
    from tools import browser_camofox_leases as leases
    root = tmp_path / "leases"
    root.mkdir()
    monkeypatch.setattr(leases, "_lease_root", lambda: root)
    monkeypatch.setattr(leases, "_pruned", False)
    path = leases._lease_path(("server", "user", "old"))
    path.touch()
    original_try_lock = leases._try_lock
    replacement = []

    def replace_before_prune_unlink(handle):
        assert original_try_lock(handle)
        path.unlink()
        holder = open(path, "w+b")
        replacement.append(holder)
        assert original_try_lock(holder)
        return True

    monkeypatch.setattr(leases, "_try_lock", replace_before_prune_unlink)
    try:
        leases._prune_idle_files(root)
        assert path.exists()
        assert os.stat(path).st_ino == os.fstat(replacement[0].fileno()).st_ino
    finally:
        for holder in replacement:
            holder.close()


def test_release_unlinks_idle_lease_file(tmp_path, monkeypatch):
    from tools import browser_camofox_leases as leases

    root = tmp_path / "leases"
    monkeypatch.setattr(leases, "_lease_root", lambda: root)
    key = ("http://camofox", "user", "tab")

    assert leases.claim(*key)
    files = list(root.iterdir())
    assert len(files) == 1
    leases.release(*key)
    assert list(root.iterdir()) == []


def test_state_directory_failure_degrades_to_process_local_ownership(tmp_path, monkeypatch, caplog):
    from tools import browser_camofox_leases as leases

    blocker = tmp_path / "not-a-directory"
    blocker.write_text("file blocks lease directory creation")
    monkeypatch.setattr(leases, "_lease_root", lambda: blocker / "leases")
    key = ("http://camofox", "user", "tab")
    leases._fallback_warned = False

    with caplog.at_level("WARNING"):
        assert leases.claim(*key)
        assert leases.claim(*key)

    assert key in leases._local_claims
    assert sum("process-local ownership" in record.message for record in caplog.records) == 1
    leases.release(*key)
    assert key not in leases._local_claims


def test_lock_contention_still_refuses_claim(tmp_path, monkeypatch):
    from tools import browser_camofox_leases as leases

    monkeypatch.setattr(leases, "_lease_root", lambda: tmp_path / "leases")
    key = ("http://camofox", "user", "tab")
    assert leases.claim(*key)
    path = leases._lease_path(key)
    other_handle = open(path, "r+b")
    try:
        assert leases._try_lock(other_handle) is False
    finally:
        other_handle.close()
    leases.release(*key)


@pytest.mark.parametrize("failure", [OSError("lock unavailable"), ImportError("no portalocker")])
def test_missing_lock_provider_falls_back_and_does_not_leave_files(tmp_path, monkeypatch, caplog, failure):
    from tools import browser_camofox_leases as leases

    root = tmp_path / "leases"
    monkeypatch.setattr(leases, "_lease_root", lambda: root)
    monkeypatch.setattr(leases, "_fallback_warned", False)

    def unavailable(_handle):
        raise failure

    monkeypatch.setattr(leases, "_try_lock", unavailable)
    with caplog.at_level("WARNING"):
        assert leases.claim("server", "user", "first")
        assert leases.claim("server", "user", "second")
    assert len(leases._local_claims) == 2
    assert list(root.iterdir()) == []
    assert sum("process-local ownership" in record.message for record in caplog.records) == 1


@pytest.mark.skipif(os.name != "posix", reason="POSIX inode/flock unlink race")
def test_claim_rejects_preopened_orphan_inode_when_replacement_is_locked(tmp_path, monkeypatch):
    from tools import browser_camofox_leases as leases

    monkeypatch.setattr(leases, "_lease_root", lambda: tmp_path / "leases")
    key = ("server", "user", "tab")
    path = leases._lease_path(key)
    original_try_lock = leases._try_lock
    replacement = []
    calls = []

    def replace_during_claim(handle):
        calls.append(handle.fileno())
        if len(calls) == 1:
            # The claimant already opened the old inode just as its owner released it.
            path.unlink()
            current = open(path, "w+b")
            replacement.append(current)
            assert original_try_lock(current)
        return original_try_lock(handle)

    monkeypatch.setattr(leases, "_try_lock", replace_during_claim)
    try:
        assert leases.claim(*key) is False
        assert len(calls) == 2
        assert key not in leases._handles and key not in leases._local_claims
    finally:
        for handle in replacement:
            handle.close()
