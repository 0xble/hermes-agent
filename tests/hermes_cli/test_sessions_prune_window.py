"""`hermes sessions prune` deletes only inside ``maintenance.deletion_window`` unless asked to run now.

Regression for 2026-11-07: a daytime operator prune of a 107 GB state.db stalled the live
gateway's writers for five hours. The CLI and the owner's ``session.prune`` both refuse outside
the window, naming it; a dry run still previews, and an explicit ``--now`` runs anyway.
"""
import asyncio
import time
from argparse import Namespace
from contextlib import closing
from types import SimpleNamespace

import pytest

import hermes_cli.maintenance_window
from hermes_cli import sessions_cmd
from hermes_state import SessionDB
from hermes_state_runtime import RuntimeStoreError


@pytest.fixture
def closed_window_store(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("maintenance:\n  deletion_window: '05:00-06:00'\n")
    monkeypatch.setattr(hermes_cli.maintenance_window, "deletion_window_open", lambda: False)
    monkeypatch.setattr("hermes_cli.sessions_owner.live_owner", lambda: None)
    at = time.time() - 200 * 86400
    with closing(SessionDB(tmp_path / "state.db")) as db:
        db.create_session("old", source="cron")
        db.append_message("old", "user", "old", timestamp=at)
        db.end_session("old", "done")
        db._execute_write(lambda conn: conn.execute(
            "UPDATE sessions SET started_at=?, ended_at=?, last_activity_at=? WHERE id='old'", (at, at, at)))
    return tmp_path / "state.db"


def _prune(**overrides):
    args = Namespace(sessions_action="prune", dry_run=False, yes=True, force=True, never_active=False,
                     include_archived=False, include_pinned=False, now=False,
                     **{name: None for name in sessions_cmd._FILTER_ARGS})
    args.older_than = "90"
    for name, value in overrides.items():
        setattr(args, name, value)
    return sessions_cmd.cmd_sessions(args)


def _exists(path):
    with closing(SessionDB(path)) as db:
        return db.get_session("old") is not None


def test_cli_prune_outside_the_window_refuses_previews_and_runs_only_with_now(closed_window_store, capsys):
    assert _prune() == 1
    out = capsys.readouterr().out
    assert "maintenance.deletion_window (05:00-06:00" in out and "--now" in out, out
    assert _exists(closed_window_store)

    assert _prune(dry_run=True) in (None, 0)
    assert "Dry run" in capsys.readouterr().out
    assert _exists(closed_window_store)

    assert _prune(now=True) in (None, 0)
    assert "Pruned 1 session(s)." in capsys.readouterr().out
    assert not _exists(closed_window_store)


def test_owner_session_prune_outside_the_window_is_refused_before_any_work(closed_window_store):
    from gateway import session_prune

    def no_work():
        raise AssertionError("a refused prune must not reach the store")
    authority = SimpleNamespace(profile_id="p", _require_admission_open=no_work)
    connection = SimpleNamespace(native_owner=True, authority=authority,
                                 actor=SimpleNamespace(profile_id="p", capabilities={"session:operator"}))

    with pytest.raises(RuntimeStoreError) as refused:
        asyncio.run(session_prune.prune(connection, None, {"filters": {"older_than_days": 90}}))
    assert refused.value.reason == "outside_deletion_window"
    assert "05:00-06:00" in refused.value.detail
