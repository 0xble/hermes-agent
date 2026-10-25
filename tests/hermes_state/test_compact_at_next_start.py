"""One-shot ``hermes sessions optimize --at-next-start`` request, honored by the next gateway start.

A live gateway always holds state.db, so ``hermes sessions optimize`` refuses and automatic VACUUM
skips; after a bulk delete the freed pages stay on disk forever. The request only records intent;
the next gateway start rewrites the store before anything else in the process opens it. It must be
honored once and cleared, deferred (and kept) under a foreign holder or short disk, and never raise.
"""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from argparse import Namespace
from collections import namedtuple
from unittest.mock import AsyncMock

import pytest

import hermes_startup_watchdog as sw
import hermes_state_compaction as compaction

pytestmark = pytest.mark.platforms("posix")  # holder scan is unavailable on Windows

_HOLDER = (
    "import sqlite3, sys\n"
    "conn = sqlite3.connect(sys.argv[1])\n"
    "conn.execute('SELECT count(*) FROM sqlite_master')\n"
    "print('ready', flush=True)\n"
    "sys.stdin.readline()\n"
)


@pytest.fixture
def bloated_db():
    """A WAL state.db in the test's isolated HERMES_HOME with most of its pages on the freelist."""
    from hermes_constants import get_hermes_home
    from hermes_state import SessionDB

    db_path = get_hermes_home() / "state.db"
    db = SessionDB(db_path=db_path)
    payload = "x" * 60_000
    for index in range(10):
        sid = f"s{index}"
        db.create_session(sid, "cli")
        for _ in range(4):
            db.append_message(sid, "user", payload)
        db.end_session(sid, "done")
    for index in range(8):
        db.delete_session(f"s{index}")
    db.close()
    return db_path


def _sizes(db_path):
    import sqlite3

    conn = sqlite3.connect(db_path)
    try:
        return tuple(conn.execute(f"PRAGMA {name}").fetchone()[0] for name in ("page_count", "freelist_count"))
    finally:
        conn.close()


@pytest.fixture
def armed_watchdog():
    sw._reset_for_tests()
    handle = sw.arm_startup_watchdog(timeout_s=300.0)
    yield handle
    sw._reset_for_tests()


def test_request_is_honored_once_then_cleared(bloated_db, armed_watchdog, caplog):
    pages_before, free_before = _sizes(bloated_db)
    assert free_before > pages_before // 4
    compaction.request_compaction(bloated_db)

    with caplog.at_level(logging.INFO, logger="hermes_state_compaction"):
        result = compaction.run_pending_compaction(bloated_db)

    assert result["status"] == "compacted"
    pages_after, free_after = _sizes(bloated_db)
    assert free_after == 0 and pages_after < pages_before
    assert result["after"] < result["before"]
    assert not compaction.request_path(bloated_db).exists()
    assert any("compaction complete" in r.getMessage() and "GiB" in r.getMessage() for r in caplog.records)
    # The rewrite held a startup-watchdog lease, so a multi-minute VACUUM is not read as a deadlock.
    assert armed_watchdog._lease_phase == compaction.LEASE_PHASE
    # One-shot: the next start finds nothing to do.
    assert compaction.run_pending_compaction(bloated_db) == {"status": "none"}


def test_foreign_holder_defers_and_keeps_the_request(bloated_db, caplog):
    compaction.request_compaction(bloated_db)
    before = _sizes(bloated_db)
    holder = subprocess.Popen([sys.executable, "-c", _HOLDER, str(bloated_db)],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "ready"
        with caplog.at_level(logging.WARNING, logger="hermes_state_compaction"):
            result = compaction.run_pending_compaction(bloated_db)
    finally:
        holder.stdin.close()
        holder.wait(timeout=10)

    assert result["status"] == "deferred_holders"
    assert compaction.request_path(bloated_db).exists()
    assert _sizes(bloated_db) == before
    assert any("deferred" in r.getMessage() for r in caplog.records if r.levelno == logging.WARNING)
    # Deferral is not an attempt: the request is still fresh for the next start.
    assert compaction.read_request(bloated_db)["attempts"] == 0
    # Control: with the holder gone, the same request now runs.
    assert compaction.run_pending_compaction(bloated_db)["status"] == "compacted"


def test_low_disk_skips_and_keeps_the_request(bloated_db, monkeypatch, caplog):
    compaction.request_compaction(bloated_db)
    before = _sizes(bloated_db)
    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr(compaction.shutil, "disk_usage", lambda path: usage(10, 10, 1024))

    with caplog.at_level(logging.WARNING, logger="hermes_state_compaction"):
        result = compaction.run_pending_compaction(bloated_db)

    assert result["status"] == "skipped_disk"
    assert compaction.request_path(bloated_db).exists()
    assert _sizes(bloated_db) == before
    assert any("free disk" in r.getMessage() for r in caplog.records)


def test_pending_release_acknowledgement_defers(bloated_db):
    compaction.request_compaction(bloated_db)
    (bloated_db.parent / "release-txn.json").write_text("{}", encoding="utf-8")
    assert compaction.run_pending_compaction(bloated_db)["status"] == "deferred_release"
    assert compaction.request_path(bloated_db).exists()


def test_failures_never_raise_and_are_bounded(bloated_db, monkeypatch):
    from hermes_state import SessionDB

    compaction.request_compaction(bloated_db)
    monkeypatch.setattr(SessionDB, "vacuum", lambda self: (_ for _ in ()).throw(RuntimeError("boom")))
    for attempt in range(1, compaction.MAX_ATTEMPTS + 1):
        result = compaction.run_pending_compaction(bloated_db)
        assert result["status"] == "error"
        assert compaction.read_request(bloated_db)["attempts"] == attempt
    # A request that keeps failing (or keeps being killed mid-rewrite) is dropped, not retried forever.
    assert compaction.run_pending_compaction(bloated_db)["status"] == "abandoned"
    assert not compaction.request_path(bloated_db).exists()

    compaction.request_compaction(bloated_db)
    monkeypatch.setattr("hermes_state_holders.foreign_state_db_holders",
                        lambda path: (_ for _ in ()).throw(OSError("scan failed")))
    assert compaction.run_pending_compaction(bloated_db)["status"] == "error"
    assert compaction.request_path(bloated_db).exists()


def test_unreadable_request_still_counts_and_missing_store_drops_it(bloated_db, tmp_path):
    compaction.request_path(bloated_db).write_text('{"attempts": "garbage"}', encoding="utf-8")
    assert compaction.run_pending_compaction(bloated_db)["status"] == "compacted"
    compaction.request_path(bloated_db).write_text("not json", encoding="utf-8")
    assert compaction.run_pending_compaction(bloated_db)["status"] == "compacted"

    missing = tmp_path / "nested" / "state.db"
    missing.parent.mkdir()
    compaction.request_path(missing).write_text("{}", encoding="utf-8")
    assert compaction.run_pending_compaction(missing)["status"] == "no_store"
    assert not compaction.request_path(missing).exists()


# -- CLI ---------------------------------------------------------------------------------------


def _optimize_args(**flags):
    return Namespace(sessions_action="optimize", force=False,
                     **{"at_next_start": False, "cancel_next_start": False, **flags})


def test_cli_records_request_beside_a_live_holder(bloated_db, capsys):
    """The live gateway always holds the store; recording intent must not hit the held-store refusal."""
    import hermes_cli.sessions_cmd as sessions_cmd

    holder = subprocess.Popen([sys.executable, "-c", _HOLDER, str(bloated_db)],
                              stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "ready"
        before = _sizes(bloated_db)
        assert sessions_cmd.cmd_sessions(_optimize_args(at_next_start=True)) == 0
        assert _sizes(bloated_db) == before  # recorded, not run
    finally:
        holder.stdin.close()
        holder.wait(timeout=10)
    record = json.loads(compaction.request_path(bloated_db).read_text(encoding="utf-8"))
    assert record["attempts"] == 0 and record["requested_at"]
    assert "next gateway start" in capsys.readouterr().out

    assert sessions_cmd.cmd_sessions(_optimize_args(cancel_next_start=True)) == 0
    assert not compaction.request_path(bloated_db).exists()


def test_cli_parser_exposes_the_flags():
    import argparse

    import hermes_cli.sessions_cmd as sessions_cmd
    from hermes_cli.subcommands.sessions import build_sessions_parser

    parser = argparse.ArgumentParser()
    build_sessions_parser(parser.add_subparsers(dest="command"), cmd_sessions=sessions_cmd.cmd_sessions)
    args = parser.parse_args(["sessions", "optimize", "--at-next-start"])
    assert args.at_next_start is True and args.cancel_next_start is False
    assert parser.parse_args(["sessions", "optimize", "--cancel-next-start"]).cancel_next_start is True


# -- gateway startup ---------------------------------------------------------------------------


def test_gateway_hook_compacts_the_active_home_store(bloated_db):
    """The real startup hook resolves the active profile's state.db and honors its request."""
    from gateway.run import _run_requested_state_db_compaction

    compaction.request_compaction(bloated_db)
    _run_requested_state_db_compaction()
    assert not compaction.request_path(bloated_db).exists()
    assert _sizes(bloated_db)[1] == 0


@pytest.mark.asyncio
async def test_start_gateway_compacts_after_pid_claim_before_anything_opens_the_store(monkeypatch, tmp_path):
    """The rewrite runs only in the authoritative gateway, before the control socket and runner.start()."""
    import gateway.run as run
    from gateway.config import GatewayConfig

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    order = []

    class _Runner:
        should_exit_cleanly = True
        exit_reason = None
        exit_code = None

        def __init__(self, config):
            self.config = config
            self.adapters = {}

        async def start(self):
            order.append("runner.start")
            return True

        async def stop(self):
            return None

    async def _control_socket(runner):
        order.append("control_socket")
        return None

    monkeypatch.setattr(run, "_host_attach_or_none", AsyncMock(return_value=None))
    monkeypatch.setattr("gateway.status.get_running_pid", lambda: None)
    monkeypatch.setattr(run, "_start_gateway_configure_logging", lambda verbosity: None)
    monkeypatch.setattr(run, "GatewayRunner", _Runner)
    monkeypatch.setattr(run, "_start_gateway_claim_pid_file",
                        lambda force=False: order.append("pid_claim") or True)
    monkeypatch.setattr(run, "_run_requested_state_db_compaction", lambda: order.append("compaction"))
    monkeypatch.setattr(run, "_start_gateway_start_control_socket", _control_socket)
    monkeypatch.setattr(run, "_refresh_host_gateway_record", lambda runner: None)
    monkeypatch.setattr(run, "_log_standalone_profiles_at_boot", lambda runner: None)
    monkeypatch.setattr(run, "_settle_and_shutdown_mcp", AsyncMock(return_value=None))
    monkeypatch.setattr(run, "_shutdown_gateway_health_export", lambda runner: None)

    assert await run.start_gateway(config=GatewayConfig(), replace=False, verbosity=None) is True
    assert order == ["pid_claim", "compaction", "control_socket", "runner.start"]


def test_gateway_hook_is_a_no_op_without_a_request():
    from gateway.run import _run_requested_state_db_compaction
    from hermes_constants import get_hermes_home

    _run_requested_state_db_compaction()
    assert not (get_hermes_home() / "state.db").exists()  # never opens (or creates) the store
