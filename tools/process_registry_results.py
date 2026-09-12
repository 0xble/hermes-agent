"""Profile-local completed receipts; unresolved owner fences outlive bounded history.

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
MAX_RETAINED_RESULTS = 64
_RESULT_FIELDS = (
    "id", "command", "cwd", "task_id", "owner_task_id", "session_key",
    "parent_session_id", "started_at", "exit_code", "completion_reason",
    "termination_source", "notify_on_complete",
)


def _ordinary_conversation(parent, db, cache):
    """Prove non-delegated ownership from existing durable session lineage.

    Missing metadata is not proof. Legacy child rows and unknown sources remain
    fences; an ended child is not thereby reconciled.
    """
    if not parent or db is None:
        return False
    if parent in cache:
        return cache[parent]
    cache[parent] = False  # Also terminates malformed/cyclic ancestry.
    row = db.execute("SELECT source, parent_session_id, model_config, end_reason FROM sessions WHERE id = ?",
                     (parent,)).fetchone()
    if row is None:
        return False
    source, ancestor, config, _ = row
    config = json.loads(config) if config else {}
    if (not source or source in {"subagent", "tool"} or not isinstance(config, dict)
            or any(key.startswith("_delegation_") for key in config)):
        return False
    if ancestor:
        previous = db.execute("SELECT source, end_reason FROM sessions WHERE id = ?", (ancestor,)).fetchone()
        ordinary = (previous == (source, "compression") and _ordinary_conversation(ancestor, db, cache))
    else:
        ordinary = True
    cache[parent] = ordinary
    return ordinary


def _result_paths():
    """Maintain bounded ordinary history outside the registry lock.

    Corruption is local: preserve its file and let owner-scoped lookup decide
    whether its uncertainty affects the caller. No malformed file is pruned.
    """
    directory = get_hermes_home() / "logs" / "process-results"
    cutoff = time.time() - RESULT_RETENTION_SECONDS
    retained, unresolved = [], []
    db, cache = None, {}
    try:
        # Retention must not create/migrate a DB to classify an owner.
        db = sqlite3.connect((get_hermes_home() / "state.db").as_uri() + "?mode=ro", uri=True)
    except sqlite3.Error:
        pass
    try:
        for path in directory.glob("proc_*.json"):
            try:
                modified = path.stat().st_mtime
                record = json.loads(path.read_text(encoding="utf-8"))
                session = restore_completed_result(record)
                if session.id != path.stem:
                    raise ValueError("Completed receipt filename identity mismatch")
                if record.get("owner_task_id") and record.get("result_observed") is not True:
                    ordinary = False
                    try:
                        ordinary = _ordinary_conversation(record.get("parent_session_id"), db, cache)
                    except (sqlite3.Error, ValueError, TypeError, RecursionError):
                        pass
                    if not ordinary:
                        unresolved.append(path)
                        continue
                if modified < cutoff:
                    path.unlink(missing_ok=True)
                else:
                    retained.append((modified, path))
            except FileNotFoundError:
                continue  # Another producer pruned it.
            except (OSError, ValueError, KeyError, TypeError, AttributeError):
                unresolved.append(path)
    finally:
        if db is not None:
            db.close()
    retained.sort(key=lambda item: (item[0], item[1].name), reverse=True)
    for _, path in retained[MAX_RETAINED_RESULTS:]:
        path.unlink(missing_ok=True)
    return unresolved + [path for _, path in retained[:MAX_RETAINED_RESULTS]]


def prune_completed_results():
    """Best-effort history maintenance, never part of publication success."""
    try:
        _result_paths()
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        logger.warning("Could not prune process history", exc_info=True)


def completed_result_record(session) -> dict:
    """The same bounded, force-redacted snapshot backs receipts and checkpoint fallback."""
    from agent.redact import redact_sensitive_text, redact_terminal_output
    from tools.process_registry import MAX_OUTPUT_CHARS

    with session._lock:
        record = {key: getattr(session, key) for key in _RESULT_FIELDS}
        record["output"] = session.output_buffer[-MAX_OUTPUT_CHARS:]
        record["result_observed"] = session._result_observed
    # Live-output opt-out must not persist raw credentials in durable receipts.
    record["output"] = redact_terminal_output(record["output"], record["command"], force=True)
    record["command"] = redact_sensitive_text(record["command"], code_file=True, force=True)
    return record


def save_completed_result(session) -> bool:
    record = completed_result_record(session)
    directory = get_hermes_home() / "logs" / "process-results"
    try:
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        atomic_json_write(directory / f"{session.id}.json", record, mode=0o600)
        session._result_persist_failed = False
        # Callers maintain history only after releasing the registry lock.
        return True
    except (OSError, ValueError):
        # Preserve live delivery on disk failure, but never silently claim durability.
        session._result_persist_failed = True
        logger.warning("Could not retain completed process result %s", session.id, exc_info=True)
        return False


def restore_completed_result(record):
    from tools.process_registry import ProcessSession

    if not isinstance(record, dict) or not re.fullmatch(r"proc_[\w]+", str(record.get("id", ""))):
        raise ValueError("Invalid completed process receipt identity")
    # Dataclasses do not enforce annotations. Invalid owner/output types must
    # not become ordinary history or be mistaken for an unrelated owner.
    for key in ("id", "owner_task_id", "task_id", "output"):
        if not isinstance(record.get(key), str):
            raise ValueError("Invalid completed process receipt identity/output")
    if record.get("parent_session_id") is not None and not isinstance(record["parent_session_id"], str):
        raise ValueError("Invalid completed process receipt parent")
    session = ProcessSession(
        **{key: record[key] for key in _RESULT_FIELDS},
        exited=True, output_buffer=record["output"],
        _result_observed=record.get("result_observed") is True,
    )
    session._completion_event.set()
    return session


def restore_checkpoint_result(record):
    """Recover an unresolved fallback, never an exited PID or notification.

    A newer exact receipt wins over a stale checkpoint left by a failed cleanup
    write. Never republish the stale unobserved snapshot over observation proof.
    """
    session = restore_completed_result(record)
    if not session.owner_task_id:
        raise ValueError("Completed checkpoint has no raw owner")
    session._result_observed = False
    session._result_persist_failed = True
    path = get_hermes_home() / "logs" / "process-results" / f"{session.id}.json"
    try:
        saved = json.loads(path.read_text(encoding="utf-8"))
        if all(saved.get(key) == record.get(key) for key in
               ("id", "owner_task_id", "parent_session_id", "started_at")):
            return restore_completed_result(saved)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        pass  # The checkpoint remains the conservative recovery source.
    return session


def _owns_result(owner: str, parent: str | None) -> bool:
    if not parent:
        return False
    if owner == parent:
        return True
    from hermes_state import SessionDB

    db = SessionDB()
    try:
        return db.get_compression_tip(parent) == owner
    finally:
        db.close()


def checkpoint_entry_owner(entry):
    """Localize uncertainty only from exact, nonempty owner metadata.

    Completed fallbacks carry a raw spawning owner separately from task_id,
    which may be a shared container key. Only legacy LIVE entries retain the
    task_id compatibility fallback used by live checkpoint recovery.
    """
    if not isinstance(entry, dict) or not isinstance(entry.get("session_id"), str):
        return None
    identity = entry.get("completed_result", entry)
    if not isinstance(identity, dict) or identity.get("id", entry["session_id"]) != entry["session_id"]:
        return None
    owner = identity.get("owner_task_id")
    if "completed_result" not in entry:
        owner = owner or identity.get("task_id")
    return owner if isinstance(owner, str) and owner else None


def _corrupt_result_owner(path, record):
    """Use intact exact identity metadata only; never guess a truncated owner.

    A mismatched filename/id has two uncertain identities and stays global.
    Existing checkpoint metadata can localize a damaged payload, without a new
    index or rewriting its source. Conflicting metadata stays unknown.
    """
    owners = set()
    if isinstance(record, dict):
        if record.get("id") != path.stem:
            return None
        owner = record.get("owner_task_id")
        if isinstance(owner, str) and owner:
            owners.add(owner)
    from tools.process_registry import CHECKPOINT_PATH
    try:
        entries = json.loads(CHECKPOINT_PATH.read_text(encoding="utf-8"))
        for entry in entries:
            if not isinstance(entry, dict) or entry.get("session_id") != path.stem:
                continue
            owner = checkpoint_entry_owner(entry)
            if owner is None:
                return None
            owners.add(owner)
    except (OSError, ValueError, TypeError):
        pass
    return next(iter(owners)) if len(owners) == 1 else None


def load_completed_results(prefix: str = "", *, unresolved_owners=None) -> dict:
    """Restore read-only snapshots; no process handles, watchers, or queue events."""
    from gateway.session_context import get_session_env

    owner = get_session_env("HERMES_SESSION_ID", "")
    if unresolved_owners is None and not owner:
        return {}
    results = {}
    try:
        paths = _result_paths()
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        if unresolved_owners is not None:
            raise  # A broken owner fence must block resume, never look empty.
        logger.warning("Could not read retained process results", exc_info=True)
        return results
    for path in paths:
        if not path.stem.startswith(prefix):
            continue
        record = None
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            if record["id"] != path.stem:
                raise ValueError("Completed receipt filename identity mismatch")
            # Validate before filtering; missing owner/observation is not proof.
            session = restore_completed_result(record)
            if unresolved_owners is not None:
                if record.get("owner_task_id") not in unresolved_owners or record.get("result_observed") is True:
                    continue
            elif not _owns_result(owner, record.get("parent_session_id")):
                continue
            results[session.id] = session
        except FileNotFoundError:
            continue  # Concurrent retention removed settled history only.
        except (OSError, ValueError, KeyError, TypeError, AttributeError, sqlite3.Error):
            if unresolved_owners is not None:
                affected_owner = _corrupt_result_owner(path, record)
                if affected_owner is None or affected_owner in unresolved_owners:
                    raise ValueError(f"Unreadable process result {path.stem}: unresolved owner") from None
            logger.debug("Skipping unreadable process result %s", path.name, exc_info=True)
    return results
