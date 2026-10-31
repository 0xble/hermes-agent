"""``begin_runtime_epoch`` refuses while another process owns the profile's gateway runtime.

The live gateway holds ``<home>/gateway.lock`` and fences every write with its runtime epoch. A
process outside that lock that begins an epoch (a probe driving ``initialize_session_authority``
against the live store) stales every later settlement and admission of the live owner until a
restart. The child below stands in for the live gateway by holding the lock.
"""

import subprocess
import sys

import pytest

from gateway.runtime_ownership import OwnershipConflict, ProfileOwnership, exclusive_maintenance
from hermes_state import SessionDB
from hermes_state_runtime import begin_runtime_epoch

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX flock stand-in for the live owner")

_HOLDER = r"""
import fcntl, sys
handle = open(sys.argv[1], "a+")
fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
print("held", flush=True)
sys.stdin.read()
"""


@pytest.fixture
def store(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    db = SessionDB(db_path=home / "state.db")
    live_epoch = begin_runtime_epoch(db, instance_id="live-gateway")
    yield home, db, live_epoch
    db.close()


@pytest.fixture
def live_owner(store):
    """Another process holding the profile's runtime lock, as the live gateway does."""
    home = store[0]
    proc = subprocess.Popen([sys.executable, "-c", _HOLDER, str(home / "gateway.lock")],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert proc.stdout.readline().strip() == "held"
        yield proc
    finally:
        proc.stdin.close()
        proc.wait(timeout=30)


def _store_epoch(db):
    with db._read_ctx() as conn:
        return tuple(conn.execute("SELECT epoch, instance_id FROM runtime_epoch WHERE singleton=1").fetchone())


def test_foreign_begin_is_refused_and_leaves_the_live_epoch(store, live_owner):
    _home, db, live_epoch = store
    with pytest.raises(OwnershipConflict, match="another process owns"):
        begin_runtime_epoch(db, instance_id="owner")
    assert _store_epoch(db) == (live_epoch, "live-gateway")


def test_begin_succeeds_once_the_owner_is_gone(store):
    _home, db, live_epoch = store
    assert begin_runtime_epoch(db, instance_id="next-gateway") == live_epoch + 1


def test_a_reservation_in_this_process_is_not_foreign(store):
    home, db, live_epoch = store
    owner = ProfileOwnership()
    owner.reserve([home])
    try:
        assert begin_runtime_epoch(db, instance_id="gateway") == live_epoch + 1
    finally:
        owner.release(home)


def test_offline_maintenance_reservation_may_begin(store):
    """The dashboard's offline store edit begins its epoch under ``exclusive_maintenance``."""
    home, db, live_epoch = store
    with exclusive_maintenance([home]):
        assert begin_runtime_epoch(db, instance_id="dashboard-offline") == live_epoch + 1
