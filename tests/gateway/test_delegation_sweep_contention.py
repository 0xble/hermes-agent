"""Real SQLite contention defers durable claims and remains visible if repeated."""

import logging
import math
import sqlite3
import time

import pytest

from gateway.run import GatewayRunner
from tools import async_delegation as ad
from tools import delegation_resume as dr


@pytest.mark.parametrize("busy_profile", ["default", "secondary"])
def test_busy_sweep_retains_claims_recovers_and_escalates(tmp_path, monkeypatch, caplog, busy_profile):
    from gateway.run import _profile_runtime_scope
    from tools.async_delegation import ORPHAN_SWEEP_INTERVAL_S

    homes = {name: tmp_path / name for name in ("default", "secondary")}
    monkeypatch.setenv("HERMES_HOME", str(homes["default"]))
    monkeypatch.setattr("hermes_cli.profiles.get_active_profile_name", lambda: "default")
    now = time.time()
    for name, home in homes.items():
        with _profile_runtime_scope(home, {}), ad._transaction() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("""INSERT INTO async_delegations
                (delegation_id,origin_session,parent_session_id,state,dispatched_at,updated_at,
                 task_json,result_json,delivery_state,retry_state,retry_due_at,retry_root_started_at)
                VALUES(?,?,'parent','error',?,?,?, '{}','delivered','scheduled',?,?)""",
                (name, "agent:main:telegram:dm:1", now - 100, now - 100,
                 '{"goal":"recover work"}', now - 1, now - 100))
    connect = ad._connect

    def short_connect():
        conn = connect()
        conn.execute("PRAGMA busy_timeout=20")
        return conn

    monkeypatch.setattr(ad, "_connect", short_connect)
    runner = object.__new__(GatewayRunner)
    runner._served_profile_homes = homes
    runner._primary_profile_name = "default"
    threshold = math.ceil(dr.RETRY_CLAIM_TTL_S / ORPHAN_SWEEP_INTERVAL_S)
    writer = sqlite3.connect(homes[busy_profile] / "state.db")
    caplog.set_level(logging.INFO, logger="gateway.run")
    try:
        writer.execute("BEGIN IMMEDIATE")
        actions = []
        for count in range(1, threshold + 1):
            caplog.clear()
            actions.extend(runner._collect_delegation_retry_actions())
            records = [r for r in caplog.records if "Delegation retry sweep" in r.message]
            assert len(records) == 1
            assert "deferred: database busy; retrying next sweep" in records[0].message
            assert records[0].levelno == (logging.WARNING if count >= threshold else logging.INFO)
            row = writer.execute("SELECT retry_state,retry_claim,retry_notices,resume_attempts "
                                 "FROM async_delegations WHERE delegation_id=?", (busy_profile,)).fetchone()
            assert row == ("scheduled", None, 0, 0)
        assert [a["delegation_id"] for a in actions] == [next(n for n in homes if n != busy_profile)]
    finally:
        writer.close()
    caplog.clear()
    recovered = runner._collect_delegation_retry_actions()
    assert [a["delegation_id"] for a in recovered] == [busy_profile]
    assert recovered[0]["claim"]
    assert runner._collect_delegation_retry_actions() == []
    assert "recovered" in caplog.text
    # Non-busy storage errors stay warnings, rather than becoming deferrals.
    def broken_sweep():
        raise sqlite3.OperationalError("no such table: async_delegations")
    monkeypatch.setattr(dr, "sweep_retries", broken_sweep)
    # Failure to enter another profile's scope also stays isolated and visible.
    runner._served_profile_homes["secondary"] = object()
    caplog.clear()
    assert runner._collect_delegation_retry_actions() == []
    records = [r for r in caplog.records if "Delegation retry sweep failed" in r.message]
    assert len(records) == 2
    assert all(r.levelno == logging.WARNING and r.exc_info for r in records)


def test_primary_sweep_failure_does_not_skip_secondary(tmp_path, monkeypatch, caplog):
    from gateway.run import _profile_runtime_scope
    from hermes_constants import get_hermes_home

    homes = {name: tmp_path / name for name in ("default", "secondary")}
    monkeypatch.setenv("HERMES_HOME", str(homes["default"]))
    monkeypatch.setattr("hermes_cli.profiles.get_active_profile_name", lambda: "default")
    for home in homes.values():
        home.mkdir()
    with _profile_runtime_scope(homes["default"], {}):
        runner = object.__new__(GatewayRunner)
        runner._served_profile_homes = homes
        runner._primary_profile_name = "default"

        calls = []

        def sweep():
            profile = get_hermes_home().name
            calls.append(profile)
            if profile == "default":
                raise ValueError("primary sweep failed")
            return [{"delegation_id": "secondary-action"}]

        monkeypatch.setattr(dr, "sweep_retries", sweep)
        caplog.set_level(logging.WARNING, logger="gateway.run")
        actions = runner._collect_delegation_retry_actions()

    assert calls == ["default", "secondary"]
    assert [action["delegation_id"] for action in actions] == ["secondary-action"]
    records = [r for r in caplog.records if "Delegation retry sweep failed" in r.message]
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    assert records[0].exc_info


def test_secondary_sweep_failure_preserves_actions_and_later_profiles(tmp_path, monkeypatch, caplog):
    from gateway.run import _profile_runtime_scope
    from hermes_constants import get_hermes_home

    homes = {name: tmp_path / name for name in ("default", "broken", "later")}
    monkeypatch.setenv("HERMES_HOME", str(homes["default"]))
    monkeypatch.setattr("hermes_cli.profiles.get_active_profile_name", lambda: "default")
    for home in homes.values():
        home.mkdir()
    with _profile_runtime_scope(homes["default"], {}):
        runner = object.__new__(GatewayRunner)
        runner._served_profile_homes = homes
        runner._primary_profile_name = "default"

        def sweep():
            profile = get_hermes_home().name
            if profile == "broken":
                raise KeyError(profile)
            return [{"delegation_id": f"{profile}-action"}]

        monkeypatch.setattr(dr, "sweep_retries", sweep)
        caplog.set_level(logging.WARNING, logger="gateway.run")
        actions = runner._collect_delegation_retry_actions()

    assert [action["delegation_id"] for action in actions] == ["default-action", "later-action"]
    records = [r for r in caplog.records if "Delegation retry sweep failed" in r.message]
    assert len(records) == 1
    assert records[0].levelno == logging.WARNING
    assert records[0].exc_info
