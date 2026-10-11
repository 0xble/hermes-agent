"""Camofox tab lease failure and cleanup behavior."""

import os

import pytest


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
