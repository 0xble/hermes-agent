"""The per-write replaced/generation probe must not stat() under the writer lock on a healthy store.

On 2026-10-11 (11:52) the SessionDB writer-lock holder sat in ``stat_db_file_identity`` inside
``_raise_if_db_replaced`` while every other writer queued behind it: a stat() of state.db stalls
in the kernel on a saturated disk. The lock-free pre-probe clears the healthy path; any doubt
still defers to the authoritative in-lock probe (#105567's close race), so detection is intact.
"""

from __future__ import annotations

import threading

import pytest

import hermes_state
from hermes_state import SessionDB


@pytest.fixture
def db(tmp_path):
    d = SessionDB(db_path=tmp_path / "state.db")
    d.set_meta("warm", "1")  # records the WAL generation the probe compares against
    yield d
    d.close()


def test_a_stalled_identity_stat_does_not_block_other_writers(db, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    real_stat = hermes_state._stat_db_file_identity

    def stalled_stat(path):
        if threading.current_thread().name == "stalled-writer":
            entered.set()
            assert release.wait(10), "stat never released"
        return real_stat(path)

    monkeypatch.setattr(hermes_state, "_stat_db_file_identity", stalled_stat)
    stalled = threading.Thread(target=db.set_meta, args=("stalled", "1"), name="stalled-writer")
    stalled.start()
    try:
        assert entered.wait(10), "the write never probed the file identity"
        other = threading.Thread(target=db.set_meta, args=("other", "1"))
        other.start()
        other.join(3)
        assert not other.is_alive(), "a write waited on another writer's identity stat"
    finally:
        release.set()
        stalled.join(10)
    assert db.get_meta("stalled") == "1" and db.get_meta("other") == "1"


def test_a_doubtful_pre_probe_defers_to_the_locked_probe(db, monkeypatch):
    """A lock-free mismatch never raises by itself: only the in-lock probe decides."""
    locked_probes = []
    real = db._halt_if_db_generation_changed
    monkeypatch.setattr(db, "_db_generation_probe_clean", lambda: False)
    monkeypatch.setattr(db, "_halt_if_db_generation_changed",
                        lambda: (locked_probes.append(db._lock.locked()), real())[1])
    db.set_meta("k", "v")
    assert locked_probes == [True]
    assert db.get_meta("k") == "v"
