"""Durable async-delegation ledger transactions, pruning and completion outbox."""

from __future__ import annotations

import logging
import os
import sqlite3
from typing import Any, Callable, Optional


logger = logging.getLogger('tools.async_delegation')

_SCHEMA_RECONCILED: set[tuple[str, int]] = set()


def _schema_key(conn: sqlite3.Connection) -> Optional[tuple[str, int]]:
    try:
        path = next(row[2] for row in conn.execute("PRAGMA database_list") if row[1] == "main")
        return (path, os.stat(path).st_ino) if path else None
    except (sqlite3.Error, OSError, StopIteration):
        return None


def _db_path():
    from tools.async_delegation import get_hermes_home
    return get_hermes_home() / "state.db"


def _connect() -> sqlite3.Connection:
    from tools.async_delegation import _db_path, _initialize_schema
    from agent.runtime_session_store import WorkerPersistenceError, is_worker_process
    if is_worker_process():
        raise WorkerPersistenceError('worker_delegation_ledger_unavailable')
    from hermes_cli.sqlite_util import open_db
    # Same state.db as hermes_state.SessionDB -- reuse its owner-only (0600)
    # hardening so this writer doesn't create/leave the file (and its WAL
    # sidecars) at the process umask. See hermes_state._secure_state_db_files.
    from hermes_constants import mkdir_under_hermes_home
    from hermes_state import _secure_state_db_files

    path = _db_path()
    # A late replay or writer must not resurrect a removed named profile (#123265).
    mkdir_under_hermes_home(path.parent)
    _secure_state_db_files(path, create_main=True)
    # wal=False: SessionDB owns state.db's journal mode (_initialize_schema applies the barriers).
    conn = open_db(path, db_label="state.db (async_delegation)", busy_timeout_ms=10_000,
                   wal=False, row_factory=None, initialize=_initialize_schema)
    _secure_state_db_files(path)
    return conn


def _initialize_schema(conn: sqlite3.Connection) -> None:
    from hermes_state_repair import apply_durability_barriers
    from hermes_state_schema import reconcile_state_schema
    # Preserve the journal mode SessionDB configured on state.db: forcing WAL from
    # every short-lived connection collides with live transcript/FTS writers.
    apply_durability_barriers(conn)
    # Single durable-shape authority: the canonical SCHEMA_SQL drives both
    # table creation and column backfill (reconcile_state_schema replays the
    # canonical DDL and reuses SessionDB's declarative reconciliation). This
    # module previously carried its own CREATE TABLE + ALTER column list,
    # which drifted from SCHEMA_SQL — same-name columns with different
    # nullability/defaults depending on which authority touched the database
    # first (#94691).
    # Each ledger transaction opens a connection, often while the registry lock is held.
    # Replaying DDL there stalls the loop's readers. Reconcile once per database generation.
    key = _schema_key(conn)
    if key is not None and key in _SCHEMA_RECONCILED:
        return
    reconcile_state_schema(conn)
    if key is not None:
        _SCHEMA_RECONCILED.add(key)


def _transaction(*, immediate: bool = False):
    from tools.async_delegation import _connect
    from hermes_cli.sqlite_util import transaction

    return transaction(_connect(), immediate=immediate)


def _capture_routing_origin() -> dict[str, Any]:
    """Snapshot scope_id/user_id/user_name on the PARENT thread (the daemon worker
    has no contextvars) so a restart-replayed completion can rebuild a SessionSource.
    Best-effort: empty values are omitted."""
    from tools.async_delegation import _ROUTING_KEYS
    try:
        from gateway.session_context import get_session_env
        return {k: v for k in _ROUTING_KEYS if (v := get_session_env(f"HERMES_SESSION_{k.upper()}", ""))}
    except Exception:
        return {}


def _persist_dispatch(record: dict[str, Any]) -> None:
    """Insert a new record exactly once, in durable ``queued`` state.

    This is intentionally not ``INSERT OR REPLACE``: replacing a row can erase
    a terminal event written by a racing cancellation.  The caller holds
    ``_records_lock`` while this commits, so cancellation cannot transition the
    in-memory record until the row exists.
    """
    from tools.async_delegation import (
        _DB_LOCK, _ROUTING_KEYS, _prune_durable_records, _transaction, json, os, time, worker_ledger,
    )
    if remote := worker_ledger():
        return remote.dispatch(record)
    now = time.time()
    try:
        from gateway.status import get_process_start_time
        owner_started_at = get_process_start_time(os.getpid())
    except Exception:
        owner_started_at = None
    task_payload = {
        key: record.get(key)
        for key in ("goal", "goals", "context", "toolsets", "role", "model", "is_batch", "task_indexes", "task_contexts", "task_transcripts", "cron_execution_id", "cron_job_id", "cron_job_name", "cron_deliver", *_ROUTING_KEYS)
        if key in record}
    if child_sessions := record.get("child_sessions"):
        task_payload["child_execution"] = {
            str(index): {"child_session_id": sid, "checkpoint": "admitted", "admitted_at": now}
            for index, sid in child_sessions.items() if isinstance(sid, str) and sid
        }
    try:  # where the children's terminals started; lets recovery add a git-state hint
        task_payload["owner_cwd"] = os.getcwd()
    except OSError:
        pass
    from tools.delegation_resume import (
        RetryAdmissionError, link_replacement, replacement_admission_error,
    )
    with _DB_LOCK, _transaction(immediate=True) as conn:
        admission_error = replacement_admission_error(conn, record, json.dumps(task_payload))
        if admission_error is None:
            changed = conn.execute("""INSERT INTO async_delegations
                   (delegation_id, origin_session, origin_ui_session_id,
                    parent_session_id, state, dispatched_at, updated_at,
                    delivery_state, delivery_attempts, owner_pid,
                    owner_started_at, task_json, origin_session_id)
                   SELECT ?, ?, ?, ?, 'queued', ?, ?, 'pending', 0, ?, ?, ?, ?
                   WHERE NOT EXISTS (SELECT 1 FROM async_delegations WHERE delegation_id=?)""",
                (record["delegation_id"], record.get("session_key", ""), record.get("origin_ui_session_id", ""),
                 record.get("parent_session_id"), record["dispatched_at"], now, os.getpid(), owner_started_at,
                 json.dumps(task_payload), record.get("origin_session_id", ""), record["delegation_id"])).rowcount
            if changed != 1:
                raise RuntimeError(f"async delegation {record['delegation_id']} already exists or was not inserted")
            # Only messaging gateway origins have a durable retry driver. CLI/TUI stay on main's path.
            from tools.delegation_resume import gateway_retry_origin
            retry_state = "armed" if gateway_retry_origin(record) else "none"
            conn.execute("UPDATE async_delegations SET retry_state=?, retry_root=?, retry_root_started_at=? "
                         "WHERE delegation_id=?", (retry_state, record["delegation_id"], record["dispatched_at"], record["delegation_id"]))
            link_replacement(conn, record, json.dumps(task_payload))
    if admission_error:
        raise RetryAdmissionError(admission_error)
    try:
        _prune_durable_records()
    except Exception:
        # The durable INSERT is already committed. Retention housekeeping must
        # not turn a legitimate queued dispatch into an in-memory-only failure.
        logger.warning("Async delegation %s: post-insert durable pruning failed", record["delegation_id"], exc_info=True)


def _persist_transition(delegation_id: str, expected: str, new: str) -> int:
    """Conditionally persist one lifecycle transition and return rowcount."""
    from tools.async_delegation import _DB_LOCK, _LIFECYCLE_STATES, _transaction, time
    if expected not in _LIFECYCLE_STATES or new not in _LIFECYCLE_STATES:
        raise ValueError(f"invalid async delegation transition {expected!r} -> {new!r}")
    with _DB_LOCK, _transaction() as conn:
        return conn.execute(
            f"UPDATE async_delegations SET state='{new}', updated_at=? "
            f"WHERE delegation_id=? AND state='{expected}'",
            (time.time(), delegation_id),
        ).rowcount


def _persist_transition_group(delegation_ids: list[str], expected: str, new: str) -> bool:
    """Persist one lifecycle transition for every sibling in one transaction.

    A queued completion-unit group is one admission decision: either every
    conditional update commits or SQLite rolls the whole group back.  The
    caller must not change any in-memory state until this returns successfully.
    """
    from tools.async_delegation import _DB_LOCK, _LIFECYCLE_STATES, _transaction, time
    if expected not in _LIFECYCLE_STATES or new not in _LIFECYCLE_STATES:
        raise ValueError(f"invalid async delegation transition {expected!r} -> {new!r}")
    if not delegation_ids:
        return True
    with _DB_LOCK, _transaction() as conn:
        for delegation_id in delegation_ids:
            changed = conn.execute(
                f"UPDATE async_delegations SET state='{new}', updated_at=? "
                f"WHERE delegation_id=? AND state='{expected}'",
                (time.time(), delegation_id),
            ).rowcount
            if changed != 1:
                raise RuntimeError(
                    f"async delegation {delegation_id} expected durable state {expected!r} for group transition"
                )
    return True


def _durable_state(delegation_id: str) -> Optional[dict[str, Any]]:
    """Read the authoritative lifecycle row for an explicit reconcile path."""
    from tools.async_delegation import _DB_LOCK, _transaction, json
    with _DB_LOCK, _transaction() as conn:
        row = conn.execute(
            "SELECT state, event_json, result_json FROM async_delegations WHERE delegation_id=?",
            (delegation_id,),
        ).fetchone()
    if row is None:
        return None
    state, event_json, result_json = row
    return {
        "state": state,
        "event": json.loads(event_json) if event_json else None,
        "result": json.loads(result_json) if result_json else None,
    }


def _log_pruned_ids(kind: str, ids: list[str]) -> None:
    """Make retention decisions auditable without dumping a whole high-churn ledger."""
    from tools.async_delegation import (
        _PRUNE_ID_LOG_LIMIT,
    )
    if ids:
        shown = ids[:_PRUNE_ID_LOG_LIMIT]
        suffix = "..." if len(ids) > len(shown) else ""
        logger.info("Pruned async delegation %s rows: count=%d ids=%s%s",
                    kind, len(ids), ",".join(shown), suffix)


def _prune_durable_records() -> None:
    """Bound terminal history without deleting rows still eligible for recovery.

    Restart-cut/retry rows are exempt from the ordinary history cap and have a
    separate hard ceiling; pending completions are bounded last.
    """
    from tools.async_delegation import (
        _DB_LOCK, _DURABLE_RETENTION_SECONDS, _MAX_DURABLE_PENDING, _MAX_RETAINED_COMPLETED,
        _MAX_RETAINED_RESUMABLE, _RESUMABLE_RETENTION_SQL, _RETENTION_OWED_SQL, _log_pruned_ids,
        _transaction, require_ledger_owner, time,
    )
    require_ledger_owner()
    cutoff = time.time() - _DURABLE_RETENTION_SECONDS
    from tools.delegation_resume import classify_armed
    classify_armed()
    with _DB_LOCK, _transaction() as conn:
        old_ids = [row[0] for row in conn.execute(
            f"""SELECT delegation_id FROM async_delegations
                WHERE delivery_state IN ('delivered','superseded') AND updated_at < ?
                  AND NOT {_RETENTION_OWED_SQL}""", (cutoff,))]
        if old_ids:
            marks = ",".join("?" for _ in old_ids)
            conn.execute(f"DELETE FROM async_delegations WHERE delegation_id IN ({marks})", old_ids)
            _log_pruned_ids("expired", old_ids)

        ordinary_count = conn.execute(
            f"""SELECT COUNT(*) FROM async_delegations
                WHERE state NOT IN ('queued','admitted','running','stalling','finalizing')
                  AND delivery_state != 'pending' AND NOT {_RESUMABLE_RETENTION_SQL}""", (cutoff,)
        ).fetchone()[0]
        ordinary = [row[0] for row in conn.execute(
            f"""SELECT delegation_id FROM async_delegations
                WHERE state NOT IN ('queued','admitted','running','stalling','finalizing')
                  AND delivery_state != 'pending' AND NOT {_RESUMABLE_RETENTION_SQL}
                ORDER BY CASE delivery_state
                           WHEN 'superseded' THEN 0
                           WHEN 'delivered' THEN 1
                           ELSE 2
                         END,
                         updated_at ASC, delegation_id ASC
                LIMIT ?""", (cutoff, max(0, ordinary_count - _MAX_RETAINED_COMPLETED))
        )]
        if ordinary:
            marks = ",".join("?" for _ in ordinary)
            conn.execute(f"DELETE FROM async_delegations WHERE delegation_id IN ({marks})", ordinary)
            _log_pruned_ids("ordinary-cap", ordinary)

        protected_count = conn.execute(
            f"""SELECT COUNT(*) FROM async_delegations
                WHERE state NOT IN ('queued','admitted','running','stalling','finalizing')
                  AND {_RESUMABLE_RETENTION_SQL}""", (cutoff,)
        ).fetchone()[0]
        protected = [row[0] for row in conn.execute(
            f"""SELECT delegation_id FROM async_delegations
                WHERE state NOT IN ('queued','admitted','running','stalling','finalizing')
                  AND {_RESUMABLE_RETENTION_SQL}
                  AND delivery_state != 'pending' AND NOT {_RETENTION_OWED_SQL}
                ORDER BY updated_at ASC, delegation_id ASC
                LIMIT ?""", (cutoff, max(0, protected_count - _MAX_RETAINED_RESUMABLE))
        )]
        if protected:
            marks = ",".join("?" for _ in protected)
            conn.execute(f"DELETE FROM async_delegations WHERE delegation_id IN ({marks})", protected)
            _log_pruned_ids("resumable-cap", protected)

        pending_count = conn.execute("""SELECT COUNT(*) FROM async_delegations
               WHERE state NOT IN ('queued','admitted','running','stalling','finalizing') AND delivery_state='pending'""").fetchone()[0]
        if pending_count > _MAX_DURABLE_PENDING:
            from tools.delegation_resume import RESULT_UNDELIVERABLE, SUCCESS_REASONS
            evict = [r[0] for r in conn.execute(f"""SELECT delegation_id FROM async_delegations
                     WHERE state NOT IN ('queued','admitted','running','stalling','finalizing') AND delivery_state='pending'
                       AND NOT {_RESUMABLE_RETENTION_SQL}
                     ORDER BY updated_at ASC, delegation_id ASC LIMIT ?""", (cutoff, pending_count - _MAX_DURABLE_PENDING))]
            marks = ",".join("?" for _ in evict)
            if evict:
                conn.execute(f"""UPDATE async_delegations SET delivery_state='dropped', retry_state='terminal',
                                 retry_reason=?, retry_due_at=NULL, updated_at=?
                                 WHERE delegation_id IN ({marks}) AND retry_state='none'
                                   AND retry_reason IN ({','.join('?' for _ in SUCCESS_REASONS)})""",
                             (RESULT_UNDELIVERABLE, time.time(), *evict, *SUCCESS_REASONS))
                conn.execute(f"DELETE FROM async_delegations WHERE delegation_id IN ({marks}) AND NOT {_RETENTION_OWED_SQL}",
                             evict)
                _log_pruned_ids("pending-cap", evict)
        conn.execute("DELETE FROM async_delegation_child_recovery WHERE NOT EXISTS "
                     "(SELECT 1 FROM async_delegations a WHERE a.delegation_id="
                     "async_delegation_child_recovery.delegation_id)")
        conn.execute(
            """DELETE FROM async_delegation_events
               WHERE delivery_state IN ('delivered','dropped') AND updated_at < ?""",
            (cutoff,),
        )


def _persist_completion(
    event: dict[str, Any], result: dict[str, Any], delivery_state: str = "pending",
    *, expected_state: Optional[str] = None,
) -> bool:
    """Conditionally persist a terminal payload without replacing the row.

    Checkpoint reconciliation mutates the caller's ``event``/``result`` in place (the producer
    syncs the reconciled payload into its live event); only the stored copies are force-redacted."""
    from tools.async_delegation import (
        _DB_LOCK, _transaction, json, reconcile_checkpoint, redact_child_result, redact_completion_event,
        time, worker_ledger,
    )
    if remote := worker_ledger():
        return remote.complete(redact_completion_event(event), redact_child_result(result))
    now = time.time()
    expected_state = expected_state or event.get("expected_state") or "queued"
    with _DB_LOCK, _transaction() as conn:
        conn.execute("BEGIN IMMEDIATE")
        reconcile_checkpoint(conn, event, result, expected_state)
        stored_event, stored_result = redact_completion_event(event), redact_child_result(result)
        changed = conn.execute("""UPDATE async_delegations SET state=?, completed_at=?, updated_at=?,
               event_json=?, result_json=?, delivery_state=?
               WHERE delegation_id=? AND state=?""",
            (event.get("status", "completed"), event.get("completed_at", now), now,
             json.dumps(stored_event), json.dumps(stored_result), delivery_state, event["delegation_id"],
             expected_state)).rowcount
    if changed != 1:
        logger.error("Async delegation %s terminal transition %s -> %s changed %s rows",
                     event.get("delegation_id"), expected_state, event.get("status"), changed)
        return False
    return True


def _attach_retry_note(evt: dict[str, Any]) -> None:
    """Classify only an owned terminal event; replay keeps the durable plan visible."""
    from tools.async_delegation import (
        _stamp_resume_availability,
    )
    if evt.get("type") != "async_delegation" or evt.get("task_failure_notice"):
        return
    evt.pop("retry_note", None)  # cancellation may have invalidated a previously persisted promise
    try:
        from tools.delegation_resume import retry_note, retry_status, schedule_retry
        delegation_id = evt["delegation_id"]
        plan = schedule_retry(delegation_id)
        if plan is None:
            record = retry_status(delegation_id)
            if record and record["retry_state"] in {"scheduled", "terminal"}:
                plan = {"retry_state": record["retry_state"], "reason": record["retry_reason"],
                        "due_at": record["retry_due_at"], "attempt": record["retry_attempt"] + 1,
                        "delegation_id": delegation_id, "cut_reason": record["event"].get("cut_reason")}
        if note := retry_note(plan):
            evt["retry_note"] = note
    except Exception:  # the retry sweep classifies later if the ledger is unavailable
        logger.debug("Async delegation %s: retry classification deferred", evt.get("delegation_id"), exc_info=True)
    _stamp_resume_availability(evt)


def _stamp_resume_availability(evt: dict[str, Any], conn=None) -> None:
    """Off-loop builders and claimers record cut-resume admission; formatters only read it."""
    try:
        from tools.delegation_resume import stamp_resume_availability
        stamp_resume_availability(evt, conn)
    except Exception:
        evt["resume_available"] = False


def _outbox_event_id(event: dict[str, Any], event_kind: str) -> str:
    """Stable delivery identity for an event that is not the unit's terminal row."""
    from tools.async_delegation import hashlib, json
    delegation_id = str(event.get("delegation_id") or "")
    if not delegation_id:
        raise ValueError("durable async delegation events require a delegation_id")
    if event_kind == "task_failure":
        results = event.get("results") or [{}]
        task_index = results[0].get("task_index") if isinstance(results[0], dict) else None
        if task_index is not None:
            return f"{delegation_id}:task-failure:{task_index}"
        # Older callers can omit task_index. Hash the complete event instead of
        # collapsing every such notice onto one INSERT OR IGNORE identity.
        payload = dict(event)
        payload.pop("_delivery_event_id", None)
        digest = hashlib.sha256(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        return f"{delegation_id}:task-failure:{digest}"
    return f"{delegation_id}:{event_kind}"


def _persist_outbox_event(
    event: dict[str, Any], result: Optional[dict[str, Any]], *, event_kind: str,
    terminal_status: Optional[str] = None, expected_state: Optional[str] = None,
) -> str:
    """Persist one event, conditionally claiming terminal-fallback ownership.

    A fallback is one transaction: its outbox insert is committed only when the
    expected lifecycle row is still active.  A zero-row transition rolls back
    the insert, so a competing terminal writer cannot acquire a second durable
    delivery identity.
    """
    from tools.async_delegation import (
        _DB_LOCK, _outbox_event_id, _transaction, json, reconcile_checkpoint, time,
    )
    event_id = _outbox_event_id(event, event_kind)
    now = time.time()
    with _DB_LOCK, _transaction() as conn:
        if terminal_status is not None:
            conn.execute("BEGIN IMMEDIATE")
            terminal_status = reconcile_checkpoint(conn, event, result, expected_state or "running", terminal_status)
        persisted = dict(event)
        persisted["_delivery_event_id"] = event_id
        result_json = json.dumps(result) if result is not None else None
        conn.execute(
            """INSERT OR IGNORE INTO async_delegation_events
               (event_id, delegation_id, event_kind, event_json, result_json, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?)""",
            (event_id, event["delegation_id"], event_kind, json.dumps(persisted), result_json, now, now),
        )
        if terminal_status is not None:
            # The lifecycle transition is the ownership check.  Do it after the
            # insert inside the same transaction so a competing winner rolls the
            # INSERT back instead of leaving a redundant fallback row.
            changed = conn.execute(
                """UPDATE async_delegations SET state=?, completed_at=?, updated_at=?, result_json=?
                   WHERE delegation_id=? AND state=?""",
                (terminal_status, event.get("completed_at", now), now, result_json,
                 event["delegation_id"], expected_state or "running"),
            ).rowcount
            if changed != 1:
                raise RuntimeError(
                    f"async delegation {event['delegation_id']} terminal fallback lost ownership "
                    f"of expected state {expected_state or 'running'} (changed {changed} rows)"
                )
    event["_delivery_event_id"] = event_id
    return event_id


def _terminal_payload_matches(raw: Optional[str], payload: Optional[dict[str, Any]]) -> bool:
    from tools.async_delegation import json
    if raw is None or payload is None:
        return raw is None and payload is None
    try:
        return json.loads(raw) == payload
    except (TypeError, ValueError):
        return False


def _reconcile_terminal_write(
    event: dict[str, Any], result: dict[str, Any], expected_state: str,
    lifecycle_payload: Optional[dict[str, Any]] = None,
) -> str:
    """Classify an ambiguous terminal write without creating a second event.

    ``event`` is the live event and ``lifecycle_payload`` the canonical lifecycle
    payload, whose status can differ (a queued cancellation persists
    ``cancelled`` but reports ``interrupted``).  ``lifecycle`` and ``outbox``
    mean this writer's payload already won; a different terminal payload means
    another writer won.  ``active`` means no terminal owner exists yet and
    ``missing`` that no durable row exists at all.
    """
    from tools.async_delegation import (
        _DB_LOCK, _TERMINAL_STATES, _outbox_event_id, _terminal_payload_matches, _transaction,
    )
    lifecycle_event = event if lifecycle_payload is None else lifecycle_payload
    event_id = _outbox_event_id(event, "terminal_fallback")
    persisted_event = dict(event)
    persisted_event["_delivery_event_id"] = event_id
    with _DB_LOCK, _transaction() as conn:
        row = conn.execute(
            "SELECT state, event_json, result_json FROM async_delegations WHERE delegation_id=?",
            (event["delegation_id"],),
        ).fetchone()
        if row is None:
            return "missing"
        state, event_json, result_json = row
        if state in _TERMINAL_STATES:
            if _terminal_payload_matches(result_json, result):
                if event_json is not None and _terminal_payload_matches(event_json, lifecycle_event):
                    return "lifecycle"
                outbox = conn.execute(
                    "SELECT event_json, result_json FROM async_delegation_events "
                    "WHERE event_id=? AND delivery_state IN ('pending', 'delivered')",
                    (event_id,),
                ).fetchone()
                if outbox and _terminal_payload_matches(outbox[0], persisted_event) \
                        and _terminal_payload_matches(outbox[1], result):
                    return "outbox"
            return "competing"
        return "active" if state == expected_state else "competing"


def _outbox_event_id_from_claim(claim_id: str) -> Optional[str]:
    if not claim_id.startswith("outbox:"):
        return None
    value = claim_id[len("outbox:"):]
    return value.split("|", 1)[0] or None


def _claim_outbox_delivery(event_id: str, consumer: str) -> Optional[str]:
    from tools.async_delegation import _CLAIM_LEASE_S, _DB_LOCK, _transaction, os, time, uuid
    now = time.time()
    claim_id = f"outbox:{event_id}|{consumer}:{os.getpid()}:{uuid.uuid4().hex}"
    with _DB_LOCK, _transaction() as conn:
        cur = conn.execute(
            """UPDATE async_delegation_events SET delivery_claim=?, delivery_claimed_at=?,
                      delivery_attempts=delivery_attempts+1, updated_at=?
               WHERE event_id=? AND delivery_state='pending'
                 AND (delivery_claim IS NULL OR delivery_claimed_at < ?)""",
            (claim_id, now, now, event_id, now - _CLAIM_LEASE_S),
        )
    return claim_id if cur.rowcount == 1 else None


def _update_outbox_delivery(event_id: str, claim_id: str, action: str) -> bool:
    from tools.async_delegation import _DB_LOCK, _MAX_DELIVERY_ATTEMPTS, _transaction, time
    now = time.time()
    with _DB_LOCK, _transaction() as conn:
        if action == "complete":
            cur = conn.execute(
                """UPDATE async_delegation_events SET delivery_state='delivered', delivered_at=?,
                          updated_at=?, delivery_claim=NULL, delivery_claimed_at=NULL
                   WHERE event_id=? AND delivery_state='pending' AND delivery_claim=?""",
                (now, now, event_id, claim_id),
            )
        elif action == "release":
            capped = conn.execute(
                """UPDATE async_delegation_events SET delivery_state='dropped',
                          updated_at=?, delivery_claim=NULL, delivery_claimed_at=NULL
                   WHERE event_id=? AND delivery_state='pending' AND delivery_claim=?
                     AND delivery_attempts>=?""",
                (now, event_id, claim_id, _MAX_DELIVERY_ATTEMPTS),
            )
            if capped.rowcount == 1:
                logger.warning("Async delegation outbox event %s exhausted its %d delivery attempts; "
                               "marking terminally dropped.", event_id, _MAX_DELIVERY_ATTEMPTS)
                return True
            cur = conn.execute(
                """UPDATE async_delegation_events SET delivery_claim=NULL,
                          delivery_claimed_at=NULL, updated_at=?
                   WHERE event_id=? AND delivery_state='pending' AND delivery_claim=?""",
                (now, event_id, claim_id),
            )
        elif action == "defer":
            cur = conn.execute(
                """UPDATE async_delegation_events SET delivery_claim=NULL,
                          delivery_claimed_at=NULL, delivery_attempts=MAX(0, delivery_attempts-1),
                          updated_at=?
                   WHERE event_id=? AND delivery_state='pending' AND delivery_claim=?""",
                (now, event_id, claim_id),
            )
        elif action == "drop":
            cur = conn.execute(
                """UPDATE async_delegation_events SET delivery_state='dropped',
                          updated_at=?, delivery_claim=NULL, delivery_claimed_at=NULL
                   WHERE event_id=? AND delivery_state='pending' AND delivery_claim=?""",
                (now, event_id, claim_id),
            )
        else:
            raise ValueError(f"unknown outbox delivery action: {action}")
    return cur.rowcount == 1


def _outbox_delivery_rows(conn):
    return conn.execute(
        """SELECT event_id, event_json, created_at, updated_at
           FROM async_delegation_events
           WHERE delivery_state='pending' AND event_json IS NOT NULL
           ORDER BY created_at, event_id"""
    ).fetchall()


def _recovered_results(task: dict[str, Any], result_json: Optional[str], error: str) -> Optional[list[dict[str, Any]]]:
    """Per-task results for an abandoned unit: recorded children as they finished, the rest ``unknown``."""
    from tools.async_delegation import json
    partial = json.loads(result_json or "{}") or {}
    if not (task.get("is_batch") and partial.get("partial") and partial.get("results")):
        return None
    recorded = {r["task_index"]: r for r in partial["results"] if isinstance(r.get("task_index"), int)}
    indexes = task.get("task_indexes") or list(range(len(task.get("goals") or [])))
    return [recorded.get(i) or {"task_index": i, "status": "unknown", "summary": None, "error": error} for i in indexes]


def _owner_liveness() -> Optional[Callable[[Any, Any], bool]]:
    """``alive(owner_pid, owner_started_at)`` over the shared drift-tolerant start-time comparator,
    or None when the liveness probes cannot be imported."""
    try:
        from gateway.status import _pid_exists, get_process_start_time, start_time_fingerprints_match
    except Exception:
        return None

    def alive(pid, started) -> bool:
        return bool(pid) and _pid_exists(int(pid)) and (
            started is None or start_time_fingerprints_match(started, get_process_start_time(int(pid)) or 0))
    return alive
