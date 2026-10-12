"""Bounded, profile-local receipts for completed terminal processes.

Receipts are read through process_manage, never replayed as notifications or
adopted as live PIDs. Each producer writes its own file so independent one-shot
parents cannot overwrite each other's results in the running-PID checkpoint.
"""

import json
import logging
import re
import sqlite3
import time

from hermes_constants import get_hermes_home
from utils import atomic_json_write

logger = logging.getLogger("tools.process_registry")

RESULT_RETENTION_SECONDS = 7 * 24 * 60 * 60
CUT_RECEIPT_RETENTION_SECONDS = 2 * 24 * 60 * 60
MAX_RETAINED_RESULTS = 64
MAX_RETAINED_CUT_RESULTS = 256
_CUT_COMPLETION_REASONS = frozenset({"cut_pending"})
_RESULT_FIELDS = (
    "id", "command", "cwd", "task_id", "owner_task_id", "session_key",
    "parent_session_id", "started_at", "exit_code", "completion_reason",
    "termination_source", "notify_on_complete",
)


def _result_paths():
    """Prune normal and pre-kill receipts independently by completion time."""
    directory = get_hermes_home() / "logs" / "process-results"
    now = time.time()
    retained = {"normal": [], "cut": []}
    for path in directory.glob("proc_*.json"):
        try:
            modified = path.stat().st_mtime
            try:
                record = json.loads(path.read_text(encoding="utf-8-sig"))
            except (OSError, ValueError, TypeError):
                record = {}
            kind = "cut" if (
                record.get("completion_reason") in _CUT_COMPLETION_REASONS
                or (record.get("completion_reason") == "killed" and record.get("termination_source"))
            ) else "normal"
            retention = CUT_RECEIPT_RETENTION_SECONDS if kind == "cut" else RESULT_RETENTION_SECONDS
            if modified < now - retention:
                path.unlink(missing_ok=True)
            else:
                retained[kind].append((modified, path))
        except FileNotFoundError:
            continue  # Another producer pruned it.
    retained["normal"].sort(key=lambda item: (item[0], item[1].name), reverse=True)
    retained["cut"].sort(key=lambda item: (item[0], item[1].name), reverse=True)
    for _, path in retained["normal"][MAX_RETAINED_RESULTS:]:
        path.unlink(missing_ok=True)
    for _, path in retained["cut"][MAX_RETAINED_CUT_RESULTS:]:
        path.unlink(missing_ok=True)
    return [
        path
        for _, path in sorted(
            (*retained["normal"][:MAX_RETAINED_RESULTS], *retained["cut"][:MAX_RETAINED_CUT_RESULTS]),
            key=lambda item: (item[0], item[1].name),
            reverse=True,
        )
    ]


def save_completed_result(
    session, *, completion_reason: str | None = None, termination_source: str | None = None,
) -> None:
    from agent.redact import redact_sensitive_text, redact_terminal_output
    from tools.process_registry import MAX_OUTPUT_CHARS

    with session._lock:
        record = {key: getattr(session, key) for key in _RESULT_FIELDS}
        record["output"] = session.output_buffer[-MAX_OUTPUT_CHARS:]
    if completion_reason is not None:
        record["completion_reason"] = completion_reason
    if termination_source is not None:
        record["termination_source"] = termination_source
    # Live-output opt-out must not persist raw credentials in durable receipts.
    record["output"] = redact_terminal_output(record["output"], record["command"], force=True)
    record["command"] = redact_sensitive_text(record["command"], code_file=True, force=True)
    directory = get_hermes_home() / "logs" / "process-results"
    try:
        from hermes_constants import assert_named_profile_home_live
        assert_named_profile_home_live(directory)
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        atomic_json_write(directory / f"{session.id}.json", record, mode=0o600)
        _result_paths()
    except OSError:
        # Preserve live delivery on disk failure, but never silently claim durability.
        logger.warning("Could not retain completed process result %s", session.id, exc_info=True)


def save_cut_pending_result(session, source: str) -> None:
    """Persist the last live snapshot before a restart or bounded sweep can cut it."""
    save_completed_result(
        session,
        completion_reason="cut_pending",
        termination_source=source,
    )


def _owns_result(owner: str, parent: str | None) -> bool:
    if not parent:
        return False
    if owner == parent:
        return True
    from hermes_state import SessionDB

    # Pure lineage read on the hot path of every retained-result load; a writable open here
    # was one more writer handle per call inside the gateway (#100896).
    db = SessionDB(read_only=True)
    try:
        return db.get_compression_tip(parent) == owner
    finally:
        db.close()


def load_completed_results(prefix: str = "") -> dict:
    """Restore read-only snapshots; no process handles, watchers, or queue events."""
    from tools.process_registry import ProcessSession

    from gateway.session_context import get_session_env

    owner = get_session_env("HERMES_SESSION_ID", "")
    if not owner:
        return {}
    results = {}
    try:
        paths = _result_paths()
    except OSError:
        logger.warning("Could not read retained process results", exc_info=True)
        return results
    for path in paths:
        if not path.stem.startswith(prefix):
            continue
        try:
            record = json.loads(path.read_text(encoding="utf-8-sig"))
            if record["id"] != path.stem or not re.fullmatch(r"proc_[\w]+", record["id"]):
                continue
            if not _owns_result(owner, record.get("parent_session_id")):
                continue
            session = ProcessSession(
                **{key: record[key] for key in _RESULT_FIELDS},
                exited=True, output_buffer=record["output"],
            )
            session._completion_event.set()
            results[session.id] = session
        except (OSError, ValueError, KeyError, TypeError, sqlite3.Error):
            logger.debug("Skipping unreadable process result %s", path.name, exc_info=True)
    return results
