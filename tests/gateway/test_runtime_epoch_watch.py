"""A live owner whose runtime epoch was taken says so: CRITICAL once, and readiness reports it.

Without this the only symptom is every turn failing with ``stale_epoch`` until someone restarts.
"""

import logging
import sqlite3
import sys
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


def _take_epoch(db):
    """A writer outside ``begin_runtime_epoch``'s ownership check (raw SQL)."""
    conn = sqlite3.connect(db.db_path)
    with conn:
        conn.execute("UPDATE runtime_epoch SET epoch=epoch+1, instance_id='owner' WHERE singleton=1")
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


_HOLDER = r"""
import fcntl, sys
handle = open(sys.argv[1], "a+")
fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
print("held", flush=True)
sys.stdin.read()
"""


@pytest.fixture
def sibling_slot(owned):
    """A zero-cut sibling (slot b) alive in another process."""
    import subprocess
    import sys
    proc = subprocess.Popen([sys.executable, "-c", _HOLDER, str(owned.home / "gateway.b.lock")],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    assert proc.stdout.readline().strip() == "held"
    yield proc
    proc.stdin.close()
    proc.wait(timeout=30)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX flock stand-in for the sibling slot")
def test_handed_over_epoch_is_the_promoted_sibling_not_a_theft(owned, sibling_slot, caplog):
    """Slot a accepted a zero-cut handover; slot b's promotion begins the next epoch while a finishes
    its in-flight turns. That move logs INFO once, never CRITICAL, and readiness stays ok."""
    owned.runner._zero_cut_handover_state = {"accepted": True}
    _take_epoch(owned.db)
    caplog.set_level(logging.INFO, logger="gateway.session_epoch_watch")
    for _ in range(2):
        assert check_runtime_epochs(owned.runner) == []
    assert _criticals(caplog) == []
    infos = [r for r in caplog.records if r.levelno == logging.INFO and "zero-cut successor" in r.getMessage()]
    assert len(infos) == 1
    assert owned.published == [{"runtime_epoch": {"status": "ok"}}]


def test_accepted_handover_without_a_live_sibling_is_still_lost(owned, caplog):
    """Accepting a handover does not excuse a move nobody beside this gateway could have made."""
    owned.runner._zero_cut_handover_state = {"accepted": True}
    _take_epoch(owned.db)
    caplog.set_level(logging.CRITICAL)
    assert len(check_runtime_epochs(owned.runner)) == 1
    assert len(_criticals(caplog)) == 1
