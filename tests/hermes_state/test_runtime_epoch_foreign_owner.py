"""``begin_runtime_epoch`` refuses while another process owns the profile's gateway runtime.

The live gateway holds ``<home>/gateway.lock`` and fences every write with its runtime epoch. A
process outside that lock that begins an epoch (a probe driving ``initialize_session_authority``
against the live store) stales every later settlement and admission of the live owner until a
restart. The child below stands in for the live gateway by holding the lock.
"""

import os
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


@pytest.fixture(params=["gateway.lock", "gateway.b.lock"], ids=["slot-a", "slot-b"])
def live_owner(store, request):
    """Another process holding the profile's runtime lock, as the live gateway does (either slot:
    a zero-cut standby or a promoted slot b serves from ``gateway.b.lock``)."""
    home = store[0]
    proc = subprocess.Popen([sys.executable, "-c", _HOLDER, str(home / request.param)],
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


@pytest.mark.skipif(hasattr(os, "geteuid") and os.geteuid() == 0, reason="root opens any lock file")
def test_an_unopenable_lock_counts_as_held(store):
    """Unknown ownership never grants the store: a lock file this user cannot open is foreign."""
    home, db, live_epoch = store
    lock = home / "gateway.lock"
    lock.touch()
    lock.chmod(0)
    try:
        with pytest.raises(OwnershipConflict):
            begin_runtime_epoch(db, instance_id="owner")
    finally:
        lock.chmod(0o600)
    assert _store_epoch(db) == (live_epoch, "live-gateway")



def _reserve_as(monkeypatch, home, slot):
    """This process reserves *home* as gateway slot *slot*, the way its runtime startup does."""
    monkeypatch.setattr("gateway.gateway_slot.effective_gateway_slot", lambda config=None: slot)
    owner = ProfileOwnership()
    owner.reserve([home])
    return owner


def _sibling_answers(monkeypatch, answer):
    monkeypatch.setattr("gateway.control_socket.query_gateway_control",
                        lambda home, verb, *, slot=None, timeout=None, **_: answer)


@pytest.mark.parametrize("answer", [None, {"standby": False}], ids=["no-answer", "active"])
def test_a_slot_b_reservation_cannot_begin_over_a_serving_slot_a(store, monkeypatch, answer):
    """Slot b that booted active (its role query gave up) holds its own lock only: slot a still
    serves and owns the epoch, so b's begin is refused unless a answers as a standby."""
    home, db, live_epoch = store
    holder = subprocess.Popen([sys.executable, "-c", _HOLDER, str(home / "gateway.lock")],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held"
        _sibling_answers(monkeypatch, answer)
        owner = _reserve_as(monkeypatch, home, "b")
        try:
            with pytest.raises(OwnershipConflict):
                begin_runtime_epoch(db, instance_id="slot-b")
            assert _store_epoch(db) == (live_epoch, "live-gateway")
            # Only a zero-cut promotion passes the successor allowance.
            assert begin_runtime_epoch(db, instance_id="slot-b", successor=True) == live_epoch + 1
        finally:
            owner.release(home)
    finally:
        holder.stdin.close()
        holder.wait(timeout=30)


def test_an_active_slot_may_begin_beside_an_unpromoted_standby(store, monkeypatch):
    """Slot a restarting while a zero-cut standby waits in slot b: the standby begins no epoch."""
    home, db, live_epoch = store
    holder = subprocess.Popen([sys.executable, "-c", _HOLDER, str(home / "gateway.b.lock")],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "held"
        _sibling_answers(monkeypatch, {"standby": True})
        owner = _reserve_as(monkeypatch, home, "a")
        try:
            assert begin_runtime_epoch(db, instance_id="slot-a") == live_epoch + 1
        finally:
            owner.release(home)
    finally:
        holder.stdin.close()
        holder.wait(timeout=30)


def test_the_successor_allowance_needs_a_reservation(store, live_owner):
    """A lock-less probe gains nothing by claiming to be a successor."""
    _home, db, live_epoch = store
    with pytest.raises(OwnershipConflict):
        begin_runtime_epoch(db, instance_id="owner", successor=True)
    assert _store_epoch(db) == (live_epoch, "live-gateway")
