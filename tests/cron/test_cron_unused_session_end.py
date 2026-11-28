"""A canonical cron fire that ends before any agent turn closes the session it owned.

The gateway creates the fire's session before the job's script gate runs. A false wake gate (or a
script failure) returned without an agent, nothing ended that row, and it stayed open until the
orphan reaper; every boot restored it meanwhile.
"""

import sqlite3
from unittest.mock import patch

import pytest

from cron.scheduler_unused_session import NO_AGENT_END_REASON


@pytest.fixture
def cron_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


def _cron_rows(home):
    conn = sqlite3.connect(home / "state.db")
    try:
        return conn.execute(
            "SELECT id, ended_at IS NOT NULL, end_reason FROM sessions WHERE source = 'cron'").fetchall()
    finally:
        conn.close()


def _job(script="gate.py"):
    return {"id": "job_unused_session", "name": "unused-session", "prompt": "Do a thing",
            "schedule": "*/5 * * * *", "script": script}


@pytest.mark.parametrize("script_result", [
    (True, '{"wakeAgent": false}'),  # the silent watcher tick
    (False, "boom"),                  # a failed gate script
])
def test_fire_without_an_agent_ends_its_owned_session(cron_home, cron_owner, script_result):
    from cron import scheduler
    from cron import scheduler_script

    with patch.object(scheduler_script, "_run_job_script", return_value=script_result), \
         patch("run_agent.AIAgent") as agent_cls:
        scheduler.run_job(_job())

    agent_cls.assert_not_called()
    rows = _cron_rows(cron_home)
    assert len(rows) == 1
    _sid, ended, reason = rows[0]
    assert ended and reason == NO_AGENT_END_REASON


def test_legacy_fire_without_an_owned_session_only_releases_the_store(tmp_path):
    from cron.scheduler_unused_session import release_unused_session

    calls = []
    db = type("DB", (), {"end_session": lambda self, *a: calls.append(("end", a))})()
    with patch("hermes_state_registry.release_or_close", lambda d: calls.append(("release", d))):
        release_unused_session(db, "job", None, lambda fn, **kw: fn())
    assert calls == [("release", db)]
