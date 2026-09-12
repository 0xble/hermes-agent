"""Dispatcher attempt → executed transcript evidence, never handoff metadata.

The quiet worker launch owns binding; model-visible tools can only read it. This
is runtime provenance, not confinement of code with direct SQLite/file access.
"""
from __future__ import annotations

import json
import logging
import os
import sqlite3
from contextlib import closing
from pathlib import Path
from typing import Any, Protocol

from hermes_state import SessionDB

from agent.delegation_context import is_dispatcher_owned_worker_context
from hermes_cli import kanban_db_connect as kbc
from hermes_cli.goals import collect_tool_evidence
from hermes_cli.profiles import resolve_profile_env
from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)
_MAX_MESSAGES = 512


class _WorkerAgent(Protocol):
    session_id: str
    _session_db: SessionDB | None


def _current_run(conn: sqlite3.Connection, task_id: str, expected_run_id: int | None) -> sqlite3.Row | None:
    row = conn.execute(
        "SELECT r.* FROM task_runs r JOIN tasks t ON t.current_run_id = r.id "
        "AND t.id = r.task_id WHERE t.id = ? AND t.status = 'running' "
        "AND r.status = 'running' AND r.claim_lock = t.claim_lock",
        (task_id,),
    ).fetchone()
    if row is None or (expected_run_id is not None and row["id"] != expected_run_id):
        return None
    return row


def bind_worker_session(agent: _WorkerAgent) -> bool:
    """Bind once, before the dispatched goal worker's first turn.

    A retry in the same worker preserves the watermark. A different session,
    profile, stale claim, delegated child or historical transcript cannot bind.
    Missing provenance is not a reason to fabricate evidence or abort manual CLI.
    """
    tid = os.environ.get("HERMES_KANBAN_TASK")
    if not tid or not is_dispatcher_owned_worker_context():
        return False
    db = getattr(agent, "_session_db", None)
    sid = getattr(agent, "session_id", None)
    if db is None or not isinstance(sid, str) or not sid:
        return False
    try:
        run_id = int(os.environ.get("HERMES_KANBAN_RUN_ID", ""))
        claim = os.environ.get("HERMES_KANBAN_CLAIM_LOCK")
        with kbc.connect_closing() as conn, kbc.write_txn(conn):
            row = _current_run(conn, tid, run_id)
            if row is None or not claim or claim != row["claim_lock"]:
                return False
            home = Path(resolve_profile_env(row["profile"])).resolve()
            if home != get_hermes_home().resolve() or Path(db.db_path).resolve() != home / "state.db":
                return False
            if row["worker_session_id"] is not None:
                return row["worker_session_id"] == sid and row["worker_home"] == str(home)
            # Dispatcher workers are fresh sessions. Refuse resume/branch history:
            # compression could otherwise replay pre-attempt tool rows past a watermark.
            if db.get_messages(sid, include_inactive=True, limit=1):
                return False
            session = db.get_session(sid)
            if session and session.get("parent_session_id"):
                return False
            if conn.execute("SELECT 1 FROM task_runs WHERE worker_session_id = ? AND worker_home = ?",
                            (sid, str(home))).fetchone():
                return False
            start = db._read_one("SELECT COALESCE(MAX(id), 0) AS id FROM messages")["id"]
            conn.execute(
                "UPDATE task_runs SET worker_session_id = ?, worker_home = ?, worker_start_message_id = ? "
                "WHERE id = ? AND worker_session_id IS NULL", (sid, str(home), start, run_id),
            )
            return True
    except (OSError, ValueError, sqlite3.Error):
        logger.warning("Kanban worker transcript binding unavailable", exc_info=True)
        return False


def collect_kanban_evidence(task_id: str, *, expected_run_id: int | None = None) -> list[dict[str, Any]]:
    """Read a bounded snapshot of the current attempt's actual tool results.

    Manual/historical handoffs without a launch binding have no evidence. The
    profile is resolved from the run, not from a supplied path/session/summary.
    Recheck ownership after reading so a concurrent reclaim cannot donate proof.
    """
    if not is_dispatcher_owned_worker_context():
        return []
    env_tid = os.environ.get("HERMES_KANBAN_TASK")
    if env_tid and env_tid != task_id:
        return []
    try:
        if env_tid:
            live_run_id = int(os.environ.get("HERMES_KANBAN_RUN_ID", ""))
            if expected_run_id is not None and expected_run_id != live_run_id:
                return []
            expected_run_id = live_run_id
        with kbc.connect_closing() as conn:
            row = _current_run(conn, task_id, expected_run_id)
            if row is None or not row["worker_session_id"] or row["worker_start_message_id"] is None:
                return []
            if env_tid and os.environ.get("HERMES_KANBAN_CLAIM_LOCK") != row["claim_lock"]:
                return []
            home = Path(resolve_profile_env(row["profile"])).resolve()
            if str(home) != row["worker_home"]:
                return []
            with closing(SessionDB(home / "state.db", read_only=True)) as db:
                # Follow only compression lineage, never the generic resume resolver
                # (which also follows /new). Bound both traversal and message work.
                owned: list[str] = []
                sid = row["worker_session_id"]
                for _ in range(16):
                    session = db.get_session(sid)
                    if not session or session.get("source") == "tool" or sid in owned:
                        break
                    config = session.get("model_config") or {}
                    if isinstance(config, str):
                        config = json.loads(config)
                    if not isinstance(config, dict) or any(config.get(key) for key in
                            ("_reset_from", "_branched_from", "_delegate_from")):
                        break
                    owned.append(sid)
                    if session.get("end_reason") != "compression":
                        break
                    children = db._read_all(
                        "SELECT id FROM sessions WHERE parent_session_id = ? "
                        "AND source != 'tool' ORDER BY started_at DESC LIMIT 2", (sid,))
                    # Ambiguous successors must not donate evidence. Ordinary in-place
                    # compaction needs no successor at all.
                    if len(children) != 1:
                        break
                    sid = children[0]["id"]
                if not owned:
                    return []
                placeholders = ",".join("?" for _ in owned)
                # One SQL snapshot fixes the upper boundary. Include archived actual
                # outcomes after compaction, not summaries or Undo/Rewind-only rows.
                rows = db._read_all(
                    f"SELECT * FROM messages WHERE session_id IN ({placeholders}) "
                    "AND id > ? AND (active = 1 OR compacted = 1) AND _compressed_summary = 0 "
                    "AND role IN ('assistant', 'tool') ORDER BY id DESC LIMIT ?",
                    [*owned, row["worker_start_message_id"], _MAX_MESSAGES],
                )
                messages = [db._row_to_message_dict(m, warn_context="kanban evidence", summary_flag=True)
                            for m in reversed(rows)]
            current = _current_run(conn, task_id, row["id"])
            if current is None or current["claim_lock"] != row["claim_lock"]:
                return []
            # Compression may retain a tool row in both the old and new session.
            # Keep its first occurrence in this bounded snapshot, rather than
            # counting a copied result as another executed check.
            seen: set[str] = set()
            unique = []
            for message in messages:
                call_id = message.get("tool_call_id")
                if message.get("role") == "tool" and call_id:
                    if call_id in seen:
                        continue
                    seen.add(call_id)
                unique.append(message)
            return collect_tool_evidence({"messages": unique})
    except (OSError, ValueError, sqlite3.Error):
        logger.warning("Kanban executed evidence unavailable", exc_info=True)
        return []
