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
