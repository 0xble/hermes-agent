"""Camofox tab lease failure and cleanup behavior."""

from pathlib import Path


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

    monkeypatch.setattr(leases, "_lease_root", lambda: Path("/definitely/not/a/lease/directory"))
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
