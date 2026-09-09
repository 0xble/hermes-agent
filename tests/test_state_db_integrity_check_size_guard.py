"""``_db_opens_cleanly`` must not walk every b-tree page of a huge store.

``PRAGMA integrity_check`` is O(database). On Brian's 30 GB state.db the startup
``state_db_data_migrations`` phase sat in SQLite's ``checkTreePage`` for minutes with the
gateway unable to reach Telegram (observed 2026-09-03, 09-07, 09-09). ``hermes_cli/backup.py``
already caps the same PRAGMA at ``DEFAULT_INTEGRITY_CHECK_MAX_BYTES`` (#70553); this probe had
no cap. The cheap probes that detect the corruption class this function exists for (#66724
partial FTS5 shadow damage, where reads and ``integrity_check`` both pass) still run at any size.
"""
import sqlite3

import pytest

import hermes_state_repair as repair
from hermes_cli.backup import DEFAULT_INTEGRITY_CHECK_MAX_BYTES


def _sparse_db(path, size_bytes):
    """A real SQLite DB padded to an apparent size, without writing the bytes."""
    with sqlite3.connect(path) as conn:
        conn.execute("create table t(x)")
    with open(path, "r+b") as fh:
        fh.truncate(size_bytes)
    return path


def test_small_store_is_still_integrity_checked(tmp_path):
    assert repair._integrity_check_is_affordable(_sparse_db(tmp_path / "small.db", 1024))


def test_store_above_the_cap_skips_the_walk(tmp_path):
    big = _sparse_db(tmp_path / "big.db", DEFAULT_INTEGRITY_CHECK_MAX_BYTES + 1)
    assert repair._integrity_check_is_affordable(big) is False


def test_cap_matches_the_backup_path(tmp_path):
    """Both paths must agree on 'too big to walk', or one blocks while the other skips."""
    at_cap = _sparse_db(tmp_path / "at.db", DEFAULT_INTEGRITY_CHECK_MAX_BYTES)
    assert repair._integrity_check_is_affordable(at_cap) is True


def test_env_override_can_restore_the_unconditional_check(tmp_path, monkeypatch):
    big = _sparse_db(tmp_path / "big.db", DEFAULT_INTEGRITY_CHECK_MAX_BYTES + 1)
    monkeypatch.setenv("HERMES_INTEGRITY_CHECK_MAX_BYTES", "0")
    assert repair._integrity_check_is_affordable(big) is True


def test_unstatable_path_does_not_mask_a_real_open_error(tmp_path):
    """A missing file must fall through to the normal probe, not be declared 'too big'."""
    assert repair._integrity_check_is_affordable(tmp_path / "absent.db") is True


def test_oversized_store_still_reports_healthy_and_returns_promptly(tmp_path):
    """The guard skips the walk without turning a healthy store into a repair candidate."""
    db = tmp_path / "state.db"
    with sqlite3.connect(db) as conn:
        conn.execute("create table sessions(session_id text primary key)")
    with open(db, "r+b") as fh:
        fh.truncate(DEFAULT_INTEGRITY_CHECK_MAX_BYTES + 1)
    assert repair._integrity_check_is_affordable(db) is False
