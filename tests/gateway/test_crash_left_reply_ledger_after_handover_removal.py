"""Regression: crash-left replies are still ledgered after the forward-only handover removal.

Syncing the off-loop startup work onto the handover removal left ``_ledger_crash_left_replies``
passing a no-longer-defined exclusion-set variable to ``_snapshot_active_turns_for_recovery``.
The resulting ``NameError`` is swallowed by ``_recover_unclean_sessions``' suppressed-warning
guard, so the stored reply was silently resumed (regenerated) instead of ledgered.
"""

import logging

import pytest

from gateway.delivery_ledger import sweep_recoverable
from tests.gateway.test_active_turn_recovery import _close_store_db, _db_runner, _entry_for, _turn


@pytest.mark.asyncio
async def test_unclean_restart_ledgers_crash_left_reply_without_a_swallowed_failure(tmp_path, caplog):
    runner, store = _db_runner(tmp_path)
    source = _turn(store, "crash-left", marked=True, reply="persisted before the crash")

    with caplog.at_level(logging.DEBUG, logger="gateway"):
        assert await runner._recover_unclean_sessions() == (0, 1)

    assert not [r for r in caplog.records if "Crash-left reply recovery on startup failed" in r.getMessage()]
    entry = _entry_for(store, source)
    assert (entry.resume_pending, entry.active_turn_token) == (False, None)
    assert [r["content"] for r in sweep_recoverable(deliverable_platforms={"discord"})] == [
        "persisted before the crash"]
    _close_store_db(store)


def test_active_turn_snapshot_takes_no_exclusion_set(tmp_path):
    """Every marked, unsuspended turn with an origin is a recovery candidate; no session is fenced."""
    runner, store = _db_runner(tmp_path)
    marked = [_turn(store, "a", marked=True, reply=None), _turn(store, "b", marked=True, reply="done")]
    _turn(store, "c", marked=False, reply="done")

    rows = runner._snapshot_active_turns_for_recovery()

    assert sorted(row[0] for row in rows) == sorted(store._generate_session_key(s) for s in marked)
    _close_store_db(store)
