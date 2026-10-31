"""A live owner whose runtime epoch was taken says so: CRITICAL once, and readiness reports it.

Without this the only symptom is every turn failing with ``stale_epoch`` until someone restarts.
"""

import logging
import sqlite3
from types import SimpleNamespace

import pytest

from gateway import status
from gateway.readiness import _probe_runtime_epoch
from gateway.runtime_ownership import ProfileOwnership
from gateway.session_epoch_watch import check_runtime_epochs
from hermes_state import SessionDB
from hermes_state_runtime import begin_runtime_epoch


@pytest.fixture
def owned(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    db = SessionDB(db_path=home / "state.db")
    owner = ProfileOwnership()
    owner.reserve([home])
    authority = SimpleNamespace(db=db, epoch=begin_runtime_epoch(db, instance_id="gateway"), retired=False)
    published = []
    monkeypatch.setattr(status, "publish_runtime_status", lambda **fields: published.append(fields))
    yield SimpleNamespace(home=home, db=db, owner=owner, runner=SimpleNamespace(session_authority=authority),
                          authority=authority, published=published)
    owner.release(home)
    db.close()


def _take_epoch(db, holder="owner"):
    """A writer outside ``begin_runtime_epoch``'s ownership check (raw SQL)."""
    conn = sqlite3.connect(db.db_path)
    with conn:
        conn.execute("UPDATE runtime_epoch SET epoch=epoch+1, instance_id=? WHERE singleton=1", (holder,))
    conn.close()


def _criticals(caplog):
    return [r for r in caplog.records if r.levelno == logging.CRITICAL and r.name == "gateway.session_epoch_watch"]


def test_taken_epoch_logs_critical_once_and_publishes_lost(owned, caplog):
    assert check_runtime_epochs(owned.runner) == []
    assert owned.published == [{"runtime_epoch": {"status": "ok"}}]
    _take_epoch(owned.db)
    caplog.set_level(logging.CRITICAL)
    for _ in range(3):
        check_runtime_epochs(owned.runner)
    held = owned.authority.epoch
    assert len(_criticals(caplog)) == 1
    assert "'owner'" in _criticals(caplog)[0].getMessage()
    assert owned.published[1:] == [{"runtime_epoch": {"status": "lost", "held": held, "store": held + 1}}]
    assert _probe_runtime_epoch({"runtime_epoch": status._coerce_runtime_epoch(owned.published[-1]["runtime_epoch"])}) \
        == {"status": "unavailable", "detail": "lost"}


def test_released_owner_is_shutting_down_not_robbed(owned, caplog):
    owned.owner.release(owned.home)
    _take_epoch(owned.db)
    caplog.set_level(logging.CRITICAL)
    assert check_runtime_epochs(owned.runner) == []
    assert _criticals(caplog) == []


def test_readiness_without_the_field_stays_ok():
    """Older gateways publish no ``runtime_epoch``."""
    assert _probe_runtime_epoch({}) == {"status": "ok"}



@pytest.fixture
def successor(owned, monkeypatch):
    """A zero-cut handover this gateway accepted; the sibling slot identifies as ``successor-b``."""
    owned.runner._zero_cut_handover_state = {"accepted": True}
    asked = []

    def query(home, verb, *, slot=None, timeout=None, **_):
        asked.append((verb, slot))
        return {"instance_id": "successor-b"} if verb == "identify" and slot == "b" else None
    monkeypatch.setattr("gateway.control_socket.query_gateway_control", query)
    return asked


def test_handed_over_epoch_is_the_promoted_sibling_not_a_theft(owned, successor, caplog):
    """Slot b's promotion begins the next epoch while slot a finishes its in-flight turns: the new
    holder is the instance slot b identifies as, so the move logs INFO once and readiness stays ok."""
    _take_epoch(owned.db, holder="successor-b")
    caplog.set_level(logging.INFO, logger="gateway.session_epoch_watch")
    for _ in range(3):
        assert check_runtime_epochs(owned.runner) == []
    assert _criticals(caplog) == []
    infos = [r for r in caplog.records if r.levelno == logging.INFO and "zero-cut successor" in r.getMessage()]
    assert len(infos) == 1
    assert successor == [("identify", "b")], "the sibling is asked once per epoch move"
    assert owned.published == [{"runtime_epoch": {"status": "ok"}}]


_HOLDER = r"""
import fcntl, sys
handle = open(sys.argv[1], "a+")
fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
print("held", flush=True)
sys.stdin.read()
"""


@pytest.fixture
def live_sibling(owned):
    """Slot b alive in another process, holding its runtime lock."""
    import subprocess
    import sys
    proc = subprocess.Popen([sys.executable, "-c", _HOLDER, str(owned.home / "gateway.b.lock")],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    assert proc.stdout.readline().strip() == "held"
    yield proc
    proc.stdin.close()
    proc.wait(timeout=30)


def test_a_handover_does_not_excuse_a_different_holder(owned, successor, live_sibling, caplog):
    """The probe's ``instance_id='owner'`` during an accepted handover, with slot b alive, is still
    a theft: only the instance slot b identifies as is its promotion."""
    _take_epoch(owned.db, holder="owner")
    caplog.set_level(logging.CRITICAL)
    assert len(check_runtime_epochs(owned.runner)) == 1
    assert len(_criticals(caplog)) == 1


def test_a_failed_handover_does_not_excuse_the_move(owned, successor, live_sibling, caplog):
    owned.runner._zero_cut_handover_state = {"accepted": True, "failed": "token lock lost"}
    _take_epoch(owned.db, holder="successor-b")
    caplog.set_level(logging.CRITICAL)
    assert len(check_runtime_epochs(owned.runner)) == 1
    assert len(_criticals(caplog)) == 1


def test_an_unanswered_sibling_is_pending_then_escalates(owned, caplog):
    """A promoting slot b too busy to answer one 0.5 s identify is not a theft yet: slot a rechecks
    each tick and reports the move only after HANDOVER_PENDING_TICKS unanswered ticks."""
    from gateway.session_epoch_watch import HANDOVER_PENDING_TICKS
    owned.runner._zero_cut_handover_state = {"accepted": True}
    _take_epoch(owned.db, holder="successor-b")
    caplog.set_level(logging.CRITICAL)
    for _ in range(HANDOVER_PENDING_TICKS - 1):
        assert check_runtime_epochs(owned.runner) == []
    assert _criticals(caplog) == []
    assert owned.published == [{"runtime_epoch": {"status": "ok"}}], "readiness dropped mid-handover"
    assert len(check_runtime_epochs(owned.runner)) == 1
    assert len(_criticals(caplog)) == 1
