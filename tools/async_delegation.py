#!/usr/bin/env python3
"""Async (background) delegation registry behind ``delegate_task(background=true)``.

The parent dispatches a subagent on a module-level daemon executor and returns a handle
immediately. On completion a ``type="async_delegation"`` event (self-contained task-source
block) is pushed onto the SHARED ``process_registry.completion_queue`` the CLI/gateway drain
while idle, so results surface as a NEW turn (never mid-turn) and inherit its de-dup and
crash-recovery wiring. Only the async lifecycle lives here; the child run is an injected ``runner``."""

from __future__ import annotations

import inspect
import contextvars
import hashlib
import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from typing import Any, Callable, Dict, List, Optional

from hermes_constants import get_hermes_home, hermes_home_key
from tools.daemon_pool import DaemonThreadPoolExecutor
from tools.thread_context import propagate_context_to_thread

logger = logging.getLogger(__name__)

# ── Module-level state ──────────────────────────────────────────────────────
# Persistent daemon executor (never a `with ThreadPoolExecutor()` block, which
# would join on exit and defeat async); daemon workers can't hang a hard exit.
_executor: Optional[ThreadPoolExecutor] = None
_executor_lock = threading.Lock()
_executor_max_workers: int = 0

# One re-entrant lock owns every in-memory lifecycle transition.  A re-entrant
# lock is required because a Future that is already complete invokes its done
# callback synchronously from add_done_callback().
_records_lock = threading.RLock()
# delegation_id -> record dict; kept for the run plus a short completed tail.
_records: Dict[str, Dict[str, Any]] = {}

# Durable and in-memory lifecycle states.  ``finalizing`` is an in-memory claim
# only: the ledger remains in the expected prior state until the conditional
# terminal UPDATE commits.
_TERMINAL_STATES = {"completed", "failed", "error", "interrupted", "cancelled", "stalled", "unknown"}
_LIFECYCLE_STATES = {"new", "queued", "admitted", "running", "stalling", "finalizing", *_TERMINAL_STATES}
_TRANSITIONS = {
    "new": {"queued"},
    "queued": {"admitted", "cancelled", "interrupted", "finalizing"},
    "admitted": {"running", "queued", "failed", "interrupted", "cancelled", "finalizing"},
    "running": {"admitted", "queued", "stalling", "completed", "failed", "error", "interrupted", "stalled", "unknown", "finalizing"},
    "stalling": {"completed", "failed", "interrupted", "stalled", "unknown", "finalizing"},
    "finalizing": _TERMINAL_STATES,
}


def _transition_memory_locked(record: Dict[str, Any], new: str, *, expected: Optional[str] = None) -> str:
    """Apply one in-memory lifecycle transition; caller holds ``_records_lock``."""
    old = str(record.get("status") or "")
    if expected is not None and old != expected:
        raise RuntimeError(f"async delegation {record.get('delegation_id')} expected {expected}, found {old}")
    if new not in _LIFECYCLE_STATES or new not in _TRANSITIONS.get(old, set()):
        raise RuntimeError(f"invalid async delegation transition {old!r} -> {new!r}")
    if new == "running" and record.get("_future") is None:
        raise RuntimeError("running async delegation requires an attached Future")
    record["status"] = new
    return old

_DEFAULT_MAX_ASYNC_CHILDREN = 3
# Low-level callers retain the historical reject-at-capacity contract.  The
# delegate_task background path explicitly opts into bounded queueing via
# ``_get_max_queued_delegations()``; cron and other direct callers must never
# queue work that they may execute inline after a rejection.
_DEFAULT_MAX_QUEUED_DELEGATIONS = 0
# Completed records retained (in memory and in the ledger) for status queries.
_MAX_RETAINED_COMPLETED = 50
_DURABLE_RETENTION_SECONDS = 7 * 24 * 60 * 60
_MAX_DURABLE_PENDING = 1000
# Rows delegation_resume would still accept for an explicit resume (owner present,
# not yet claimed). Pruning skips them inside the retention window, so a restart
# can't make an interrupted child unrecoverable. Keep in step with
# delegation_resume._eligibility's state and owner checks. One ? is the cutoff.
# Deliberately a superset: batch/partial-result checks live in task/result JSON,
# so a few ineligible rows are kept too, bounded by the retention window.
_RESUMABLE_RETENTION_SQL = """(
    state IN ('unknown','interrupted','stalled')
    AND resume_state='none'
    AND (COALESCE(parent_session_id, '') != '' OR COALESCE(origin_session_id, '') != '')
    AND updated_at >= ?
)"""
# Cap retried deliveries so an unroutable row converges to terminal 'dropped'.
_MAX_DELIVERY_ATTEMPTS = 8
# Pending completions older than this are dropped on restart replay instead of
# re-run as a full-context turn; 48h keeps weekend results deliverable.
_MAX_COMPLETION_REPLAY_AGE_S = 48 * 3600.0
# A delivery claim older than this is abandoned and may be re-claimed.
_CLAIM_LEASE_S = 300.0
_DB_LOCK = threading.Lock()

# ── Orphaned-completion sweep ────────────────────────────────────────────────
# Startup replay runs once per process, so a completion whose owner died while THIS process was
# already running (a desktop reload) would wait for the next restart (#97202). Delivery loops (gateway
# watcher, TUI poller) sweep each home they serve at most once per interval.
ORPHAN_SWEEP_INTERVAL_S = 30.0
# Idle time before a dead owner's pending row is re-offered; keeps the sweep off a row just touched.
_ORPHAN_STALE_S = 60.0
_orphan_lock = threading.Lock()
# (home key, delegation_id) put on this process's queue by replay or sweep and not re-offered while
# that copy is alive. A consumer that discards its copy with the row still pending hands it back
# (``return_completion_offer``); the delivery claim stays the only thing that settles the row.
_offered: set = set()
_last_orphan_sweep: Dict[str, float] = {}

# ── Stale-delegation detection (progress-based, on by default) ──────────────
# A runner wedged before returning never reaches its finalizer, so it would show
# "dispatched" forever. No wall-clock timeout (heavy work must never be killed for
# taking long): one monitor thread samples per-dispatch PROGRESS via an injected
# ``progress_fn``; a frozen child is interrupted, given a grace window to unwind via
# the normal finalize path, and only force-finalized (terminal ``stalled`` event) if
# it never returns. Thresholds mirror delegate_tool's sync heartbeat monitor.
_STALE_CHECK_INTERVAL = 30.0
_STALE_IDLE_SECONDS = 450.0
_STALE_IN_TOOL_SECONDS = 1200.0
_STALL_GRACE_SECONDS = 120.0
# An admitted record without a Future is a pre-submit recovery window, not live
# work. If that window outlives this deadline, requeue it rather than leaking a
# capacity slot or relying on another completion to wake admission.
_ADMITTED_RECOVERY_SECONDS = 2.0

_monitor_lock = threading.Lock()
_monitor_thread: Optional[threading.Thread] = None
_monitor_stop = threading.Event()

_LIVE_STATES = {"queued", "admitted", "running", "stalling", "finalizing"}
_ACTIVE_STATES = ("running", "stalling")
_FINALIZABLE_STATES = {"queued", "admitted", "running", "stalling"}
_INTERRUPTIBLE_STATES = {"queued", "admitted", "running", "stalling"}
_PENDING_QUEUE = deque()
# Admission is not eligible until persistence commits, but a dispatch whose
# capacity was free still needs a provisional key so concurrent dispatchers do
# not all pass the same capacity check while its INSERT is in flight.
_PENDING_ADMISSION_SLOTS: set = set()
_BACKEND_RETIRING = "backend is retiring; reconnect to continue"
# Routing origin persisted at dispatch so a restart-recovered completion can
# reconstruct a full SessionSource (scope_id drives relay tenant egress).
_ROUTING_KEYS = ("scope_id", "user_id", "user_name")
# Structured stall metadata — additive, present only on stall finalizations.
_STALL_META_KEYS = ("stalled_after_quiet_seconds", "stall_threshold_seconds", "stall_phase", "stall_grace_seconds")
# Private stall bookkeeping on the record -> public field in list_async_delegations().
_STALL_FIELD_MAP = (("_stall_quiet_seconds", "stalled_after_quiet_seconds"),
                    ("_stall_threshold_seconds", "stall_threshold_seconds"), ("_stall_in_tool", "stall_in_tool"))


# ── Durable ledger (state.db / async_delegations) ───────────────────────────
def _db_path():
    return get_hermes_home() / "state.db"


def _connect() -> sqlite3.Connection:
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
    reconcile_state_schema(conn)


def _transaction():
    from hermes_cli.sqlite_util import transaction

    return transaction(_connect())


def _capture_routing_origin() -> Dict[str, Any]:
    """Snapshot scope_id/user_id/user_name on the PARENT thread (the daemon worker
    has no contextvars) so a restart-replayed completion can rebuild a SessionSource.
    Best-effort: empty values are omitted."""
    try:
        from gateway.session_context import get_session_env
        return {k: v for k in _ROUTING_KEYS if (v := get_session_env(f"HERMES_SESSION_{k.upper()}", ""))}
    except Exception:  # noqa: BLE001 - routing origin is additive, never fatal
        return {}


def _persist_dispatch(record: Dict[str, Any]) -> None:
    """Insert a new record exactly once, in durable ``queued`` state.

    This is intentionally not ``INSERT OR REPLACE``: replacing a row can erase
    a terminal event written by a racing cancellation.  The caller holds
    ``_records_lock`` while this commits, so cancellation cannot transition the
    in-memory record until the row exists.
    """
    now = time.time()
    try:
        from gateway.status import get_process_start_time
        owner_started_at = get_process_start_time(os.getpid())
    except Exception:
        owner_started_at = None
    task_payload = {
        key: record.get(key)
        for key in ("goal", "goals", "context", "toolsets", "role", "model", "is_batch", "task_indexes", "task_transcripts", "cron_execution_id", "cron_job_id", "cron_job_name", "cron_deliver", *_ROUTING_KEYS)
        if key in record}
    try:  # where the children's terminals started; lets recovery add a git-state hint
        task_payload["owner_cwd"] = os.getcwd()
    except OSError:
        pass
    with _DB_LOCK, _transaction() as conn:
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
    try:
        _prune_durable_records()
    except Exception:
        # The durable INSERT is already committed. Retention housekeeping must
        # not turn a legitimate queued dispatch into an in-memory-only failure.
        logger.warning("Async delegation %s: post-insert durable pruning failed", record["delegation_id"], exc_info=True)


def _persist_transition(delegation_id: str, expected: str, new: str) -> int:
    """Conditionally persist one lifecycle transition and return rowcount."""
    if expected not in _LIFECYCLE_STATES or new not in _LIFECYCLE_STATES:
        raise ValueError(f"invalid async delegation transition {expected!r} -> {new!r}")
    with _DB_LOCK, _transaction() as conn:
        return conn.execute(
            f"UPDATE async_delegations SET state='{new}', updated_at=? "
            f"WHERE delegation_id=? AND state='{expected}'",
            (time.time(), delegation_id),
        ).rowcount


def _persist_transition_group(delegation_ids: List[str], expected: str, new: str) -> bool:
    """Persist one lifecycle transition for every sibling in one transaction.

    A queued completion-unit group is one admission decision: either every
    conditional update commits or SQLite rolls the whole group back.  The
    caller must not change any in-memory state until this returns successfully.
    """
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


def _durable_state(delegation_id: str) -> Optional[Dict[str, Any]]:
    """Read the authoritative lifecycle row for an explicit reconcile path."""
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


def _prune_durable_records() -> None:
    """Bound terminal history without deleting rows still eligible for recovery.

    Delivered rows go first, then unsuccessful ones; an undelivered (pending)
    completion is the parent's only copy of a child result, so it goes last.
    """
    cutoff = time.time() - _DURABLE_RETENTION_SECONDS
    with _DB_LOCK, _transaction() as conn:
        conn.execute(
            "DELETE FROM async_delegations WHERE delivery_state IN ('delivered','superseded') AND updated_at < ?", (cutoff,))
        terminal_count = conn.execute(
            "SELECT COUNT(*) FROM async_delegations WHERE state NOT IN ('queued','admitted','running','stalling','finalizing')").fetchone()[0]
        if terminal_count > _MAX_RETAINED_COMPLETED:
            conn.execute(f"""DELETE FROM async_delegations WHERE delegation_id IN (
                     SELECT delegation_id FROM async_delegations
                     WHERE state NOT IN ('queued','admitted','running','stalling','finalizing')
                       AND NOT {_RESUMABLE_RETENTION_SQL}
                     ORDER BY CASE delivery_state WHEN 'delivered' THEN 0
                                                  WHEN 'pending' THEN 2 ELSE 1 END,
                              updated_at ASC LIMIT ?
                   )""", (cutoff, terminal_count - _MAX_RETAINED_COMPLETED))
        pending_count = conn.execute("""SELECT COUNT(*) FROM async_delegations
               WHERE state NOT IN ('queued','admitted','running','stalling','finalizing') AND delivery_state='pending'""").fetchone()[0]
        if pending_count > _MAX_DURABLE_PENDING:
            conn.execute(f"""DELETE FROM async_delegations WHERE delegation_id IN (
                     SELECT delegation_id FROM async_delegations
                     WHERE state NOT IN ('queued','admitted','running','stalling','finalizing') AND delivery_state='pending'
                       AND NOT {_RESUMABLE_RETENTION_SQL}
                     ORDER BY updated_at ASC LIMIT ?
                   )""", (cutoff, pending_count - _MAX_DURABLE_PENDING))
        conn.execute(
            """DELETE FROM async_delegation_events
               WHERE delivery_state IN ('delivered','dropped') AND updated_at < ?""",
            (cutoff,),
        )


def _persist_completion(
    event: Dict[str, Any], result: Dict[str, Any], delivery_state: str = "pending",
    *, expected_state: Optional[str] = None,
) -> bool:
    """Conditionally persist a terminal payload without replacing the row."""
    now = time.time()
    expected_state = expected_state or event.get("expected_state") or "queued"
    with _DB_LOCK, _transaction() as conn:
        changed = conn.execute("""UPDATE async_delegations SET state=?, completed_at=?, updated_at=?,
               event_json=?, result_json=?, delivery_state=?
               WHERE delegation_id=? AND state=?""",
            (event.get("status", "completed"), event.get("completed_at", now), now,
             json.dumps(event), json.dumps(result), delivery_state, event["delegation_id"], expected_state)).rowcount
    if changed != 1:
        logger.error("Async delegation %s terminal transition %s -> %s changed %s rows",
                     event.get("delegation_id"), expected_state, event.get("status"), changed)
        return False
    return True


def _outbox_event_id(event: Dict[str, Any], event_kind: str) -> str:
    """Stable delivery identity for an event that is not the unit's terminal row."""
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
    event: Dict[str, Any], result: Optional[Dict[str, Any]], *, event_kind: str,
    terminal_status: Optional[str] = None, expected_state: Optional[str] = None,
) -> str:
    """Persist one event, conditionally claiming terminal-fallback ownership.

    A fallback is one transaction: its outbox insert is committed only when the
    expected lifecycle row is still active.  A zero-row transition rolls back
    the insert, so a competing terminal writer cannot acquire a second durable
    delivery identity.
    """
    event_id = _outbox_event_id(event, event_kind)
    persisted = dict(event)
    persisted["_delivery_event_id"] = event_id
    now = time.time()
    result_json = json.dumps(result) if result is not None else None
    with _DB_LOCK, _transaction() as conn:
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


def _terminal_payload_matches(raw: Optional[str], payload: Optional[Dict[str, Any]]) -> bool:
    if raw is None or payload is None:
        return raw is None and payload is None
    try:
        return json.loads(raw) == payload
    except (TypeError, ValueError):
        return False


def _reconcile_terminal_write(
    event: Dict[str, Any], result: Dict[str, Any], expected_state: str,
    lifecycle_payload: Optional[Dict[str, Any]] = None,
) -> str:
    """Classify an ambiguous terminal write without creating a second event.

    ``event`` is the live event and ``lifecycle_payload`` the canonical lifecycle
    payload, whose status can differ (a queued cancellation persists
    ``cancelled`` but reports ``interrupted``).  ``lifecycle`` and ``outbox``
    mean this writer's payload already won; a different terminal payload means
    another writer won.  ``active`` means no terminal owner exists yet and
    ``missing`` that no durable row exists at all.
    """
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


def _replay_outbox_pending(conn, rows, target_queue, now: float) -> int:
    """Replay pending outbox events, preserving their event-specific delivery identity."""
    home, restored = hermes_home_key(get_hermes_home()), 0
    for event_id, payload, created_at, updated_at in rows:
        age_basis = updated_at or created_at
        if age_basis and (now - age_basis) > _MAX_COMPLETION_REPLAY_AGE_S:
            conn.execute(
                """UPDATE async_delegation_events SET delivery_state='dropped',
                          delivery_claim=NULL, delivery_claimed_at=NULL, updated_at=?
                   WHERE event_id=? AND delivery_state='pending'""",
                (now, event_id),
            )
            continue
        evt = json.loads(payload)
        if isinstance(evt, dict):
            evt["_delivery_event_id"] = event_id
            evt["restored"] = True
        target_queue.put(evt)
        with _orphan_lock:
            _offered.add((home, event_id))
        restored += 1
    return restored


def _outbox_delivery_rows(conn):
    return conn.execute(
        """SELECT event_id, event_json, created_at, updated_at
           FROM async_delegation_events
           WHERE delivery_state='pending' AND event_json IS NOT NULL
           ORDER BY created_at, event_id"""
    ).fetchall()


def record_unit_child(delegation_id: str, entry: Dict[str, Any]) -> None:
    """Durably record ONE finished child of a still-running multi-child unit on the unit's own row, so a crash before
    the unit joins loses only the children that had not finished. Stored in ``result_json`` (overwritten by the real
    result at finalize); ``recover_abandoned_delegations`` replays it. Best-effort: a failed write costs recovery
    fidelity, never the live result."""
    try:
        with _DB_LOCK, _transaction() as conn:
            row = conn.execute("SELECT result_json FROM async_delegations WHERE delegation_id=? AND state='running'",
                               (delegation_id,)).fetchone()
            if row is None:
                return
            partial = json.loads(row[0] or "{}") or {}
            results = [r for r in partial.get("results") or [] if r.get("task_index") != entry.get("task_index")]
            results.append(entry)
            conn.execute("UPDATE async_delegations SET result_json=?, updated_at=? WHERE delegation_id=? AND state='running'",
                         (json.dumps({"results": results, "partial": True}), time.time(), delegation_id))
    except Exception:  # noqa: BLE001 — recovery bookkeeping must never fail a live child
        logger.warning("Async delegation %s: could not record finished child %s", delegation_id, entry.get("task_index"), exc_info=True)


def _recovered_results(task: Dict[str, Any], result_json: Optional[str], error: str) -> Optional[List[Dict[str, Any]]]:
    """Per-task results for an abandoned unit: recorded children as they finished, the rest ``unknown``."""
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


def recover_abandoned_delegations() -> int:
    """Classify records whose owning process disappeared as outcome unknown; children a multi-child unit had already
    recorded (``record_unit_child``) are replayed with their real results."""
    alive = _owner_liveness()
    if alive is None:
        return 0
    now, recovered = time.time(), 0
    with _DB_LOCK, _transaction() as conn:
        rows = conn.execute("""SELECT delegation_id, origin_session, origin_ui_session_id,
                      parent_session_id, dispatched_at, owner_pid,
                      owner_started_at, task_json, origin_session_id, result_json, state
               FROM async_delegations WHERE state IN ('queued','admitted','running','finalizing')""").fetchall()
        for row in rows:
            delegation_id, session_key, origin_ui, parent_id, dispatched_at, pid, started, task_json, origin_sid, result_json, last_state = row
            if alive(pid, started):
                continue
            task = json.loads(task_json or "{}")
            cron_execution_id = task.get("cron_execution_id")
            if cron_execution_id:
                # A cron runner is merely a waiter. Its detached worker owns the
                # execution and can outlive this process. Do not classify the
                # waiter's queued/admitted handoff as never-started work.
                from cron.delivery_queue import MISSING_EXECUTION_GRACE
                from cron.executions import get_execution
                execution = get_execution(cron_execution_id)
                if execution is None:
                    if now - dispatched_at < MISSING_EXECUTION_GRACE.total_seconds():
                        if last_state in ("queued", "admitted"):
                            conn.execute("""UPDATE async_delegations SET state='running', updated_at=?
                                   WHERE delegation_id=? AND state=?""", (now, delegation_id, last_state))
                        continue
                    # A pruned row has no provable outcome. Use the generic
                    # unknown event below; never rerun or invent a completion.
                elif execution["status"] not in ("completed", "failed", "unknown"):
                    # The scheduler owns ledger recovery on its tick. A delegation
                    # sweep must not mutate the cron store under its own DB lock.
                    if last_state in ("queued", "admitted"):
                        conn.execute("""UPDATE async_delegations SET state='running', updated_at=?
                               WHERE delegation_id=? AND state=?""", (now, delegation_id, last_state))
                    continue
                else:
                    from tools.cronjob_tools import _manual_run_completion
                    completed = _manual_run_completion(
                        {}, task["cron_job_id"], task["cron_job_name"],
                        task["cron_deliver"], dispatched_at,
                        execution_id=cron_execution_id,
                    )
                    event = {
                        "type": "async_delegation", "delegation_id": delegation_id,
                        "session_key": session_key, "origin_ui_session_id": origin_ui,
                        "origin_session_id": origin_sid or "", "parent_session_id": parent_id,
                        "goal": task.get("goal", ""), "context": task.get("context"),
                        "toolsets": task.get("toolsets"), "role": task.get("role"),
                        "model": task.get("model"), **completed,
                        "dispatched_at": dispatched_at, "completed_at": now,
                        **{k: task[k] for k in _ROUTING_KEYS if task.get(k)},
                    }
                    conn.execute("""UPDATE async_delegations SET state=?, completed_at=?,
                           updated_at=?, event_json=?, result_json=?, delivery_state='pending'
                           WHERE delegation_id=? AND state IN ('queued','running','finalizing','admitted')""",
                        (completed["status"], now, now, json.dumps(event),
                         json.dumps(completed), delegation_id))
                    recovered += 1
                    continue
            if last_state in ("queued", "admitted") and not cron_execution_id:
                error = (f"Delegation owner exited while this work was {last_state}; "
                         "it was never started.")
                event = {
                    "type": "async_delegation", "delegation_id": delegation_id,
                    "session_key": session_key, "origin_ui_session_id": origin_ui,
                    "origin_session_id": origin_sid or "", "parent_session_id": parent_id,
                    "goal": task.get("goal", ""), "goals": task.get("goals"),
                    "context": task.get("context"), "toolsets": task.get("toolsets"),
                    "role": task.get("role"), "model": task.get("model"),
                    "is_batch": bool(task.get("is_batch")), "status": "interrupted",
                    "summary": None, "error": error, "exit_reason": "interrupted",
                    "dispatched_at": dispatched_at, "completed_at": now,
                    **{k: task[k] for k in _ROUTING_KEYS if task.get(k)},
                }
                result = {"status": "interrupted", "summary": None, "error": error,
                          "exit_reason": "interrupted"}
                conn.execute("""UPDATE async_delegations SET state='interrupted', completed_at=?,
                       updated_at=?, event_json=?, result_json=?, delivery_state='pending'
                       WHERE delegation_id=? AND state=?""",
                    (now, now, json.dumps(event), json.dumps(result), delegation_id, last_state))
                recovered += 1
                continue
            error = ("Cron execution record missing; outcome unknown."
                     if cron_execution_id else
                     "Delegation owner exited before recording a terminal result; outcome unknown.")
            recovered_results = _recovered_results(task, result_json, error)
            if recovered_results:
                done = sum(1 for r in recovered_results if r.get("status") != "unknown")
                error = (f"Delegation owner exited before the unit finished; {done}/{len(recovered_results)} child "
                         "results were recorded and are included below, the rest are unknown.")
            diagnostics = {"last_known_status": last_state, "task_transcripts": task.get("task_transcripts") or {}}
            # Verbatim transcript tails + a git snapshot of the owner's cwd, so the parent can
            # continue or re-dispatch from the event alone instead of opening files (#116000).
            from tools.async_delegation_recovery_hints import git_state_hint, transcript_tails
            if tails := transcript_tails(diagnostics["task_transcripts"]):
                diagnostics["transcript_tails"] = tails
            if hint := git_state_hint(task.get("owner_cwd")):
                diagnostics["git_state_hint"] = hint
            event = {
                "type": "async_delegation", "delegation_id": delegation_id, "session_key": session_key,
                "origin_ui_session_id": origin_ui, "origin_session_id": origin_sid or "",
                "parent_session_id": parent_id, "goal": task.get("goal", ""), "goals": task.get("goals"),
                "context": task.get("context"), "toolsets": task.get("toolsets"), "role": task.get("role"),
                "model": task.get("model"), "is_batch": bool(task.get("is_batch")),
                "status": "unknown", "summary": None, "error": error, **diagnostics,
                **({"results": recovered_results} if recovered_results else {}),
                "dispatched_at": dispatched_at, "completed_at": now,
                **{k: task[k] for k in _ROUTING_KEYS if task.get(k)}}
            result = {"status": "unknown", "summary": None, "error": event["error"], **diagnostics,
                      **({"results": recovered_results} if recovered_results else {})}
            conn.execute("""UPDATE async_delegations SET state='unknown', completed_at=?,
                   updated_at=?, event_json=?, result_json=?, delivery_state='pending'
                   WHERE delegation_id=?""", (now, now, json.dumps(event), json.dumps(result), delegation_id))
            recovered += 1
    return recovered


def restore_undelivered_completions(target_queue) -> int:
    """Enqueue durable pending completions as fresh turns after process start.
    Restored events are stamped ``restored=True`` in memory only: they came from a PREVIOUS
    process, so drains without an ownership filter must leave them for a consumer that can
    prove ownership. Rows older than ``_MAX_COMPLETION_REPLAY_AGE_S`` are terminally dropped
    instead of replaying a turn nobody is waiting on.

    Every restored event is stamped ``restored=True`` (in-memory only — the stamp is added after the durable
    payload is deserialized and is never persisted). Restored events originate from a *previous* process, so
    no consumer in THIS process implicitly owns them: drain paths that run without an ownership filter (the
    legacy single-session behavior) must leave them queued for a consumer that can positively prove
    ownership, otherwise a brand-new session adopts a dead session's delegation results seconds after boot
    (#64484).
    """
    if not _db_path().exists():
        return 0  # nothing to replay; a replay must not create (or migrate) the ledger (#123265)
    recover_abandoned_delegations()
    now = time.time()
    with _DB_LOCK, _transaction() as conn:
        rows = conn.execute("""SELECT delegation_id, event_json, completed_at, dispatched_at
               FROM async_delegations
               WHERE state != 'running' AND delivery_state='pending' AND event_json IS NOT NULL
               ORDER BY completed_at, delegation_id""").fetchall()
        restored = _replay_pending(conn, rows, target_queue, now)
        restored += _replay_outbox_pending(conn, _outbox_delivery_rows(conn), target_queue, now)
        return restored


def _replay_pending(conn, rows, target_queue, now: float) -> int:
    """Put each pending ``(delegation_id, event_json, completed_at, dispatched_at)`` row on ``target_queue``
    stamped ``restored``, or terminally drop it past ``_MAX_COMPLETION_REPLAY_AGE_S``. Records the offer so
    the orphan sweep skips the row until the copy is handed back (``return_completion_offer``)."""
    home, restored = hermes_home_key(get_hermes_home()), 0
    for delegation_id, payload, completed_at, dispatched_at in rows:
        age_basis = completed_at or dispatched_at
        if age_basis and (now - age_basis) > _MAX_COMPLETION_REPLAY_AGE_S:
            conn.execute("""UPDATE async_delegations SET delivery_state='dropped',
                          delivery_claim=NULL, delivery_claimed_at=NULL,
                          updated_at=?
                   WHERE delegation_id=? AND delivery_state='pending'""", (now, delegation_id))
            logger.warning("Async delegation %s: pending completion is %.1fh old "
                           "(cap %.1fh); terminally dropping the replay (result remains queryable).",
                           delegation_id, (now - age_basis) / 3600.0, _MAX_COMPLETION_REPLAY_AGE_S / 3600.0)
            continue
        evt = json.loads(payload)
        if isinstance(evt, dict):
            evt["restored"] = True
        target_queue.put(evt)
        with _orphan_lock:
            _offered.add((home, delegation_id))
        restored += 1
    return restored


def sweep_orphaned_completions(target_queue, *, now: Optional[float] = None) -> int:
    """Offer this home's completions whose owner died after THIS process started (#97202).

    Startup replay (``restore_undelivered_completions``) covers owners that died before the process
    started; this covers the rest while it runs. Abandoned in-flight rows are first classified by
    ``recover_abandoned_delegations``. A terminal row qualifies when it is pending with an event, idle
    past ``_ORPHAN_STALE_S``, not under a live delivery claim, and its owner fails the shared liveness
    check. A row is offered once per live in-memory copy: a consumer that discards the copy with the row
    still pending hands it back for the next sweep. The consumer's ``claim_completion_delivery`` stays
    the atomic cross-process gate, so two processes offering one row never both deliver it. Rows past
    the delivery budget or the replay age converge to ``dropped``. Reads the current profile's ledger:
    callers bind the owning profile first."""
    held = reoffer_unresolved_completions(target_queue)  # unproven terminal events whose retry is due
    alive = _owner_liveness()
    if alive is None or not _db_path().exists():
        return held  # never create a ledger just to sweep it
    recover_abandoned_delegations()
    now = time.time() if now is None else now
    home = hermes_home_key(get_hermes_home())
    with _orphan_lock:
        offered = {delegation_id for key, delegation_id in _offered if key == home}
    with _DB_LOCK, _transaction() as conn:
        rows = conn.execute("""SELECT delegation_id, event_json, completed_at, dispatched_at,
                      owner_pid, owner_started_at, delivery_attempts
               FROM async_delegations
               WHERE state NOT IN ('admitted','running','finalizing') AND delivery_state='pending'
                 AND event_json IS NOT NULL AND updated_at < ?
                 AND (delivery_claim IS NULL OR delivery_claimed_at < ?)
               ORDER BY completed_at, delegation_id""", (now - _ORPHAN_STALE_S, now - _CLAIM_LEASE_S)).fetchall()
        orphans = []
        for delegation_id, payload, completed_at, dispatched_at, pid, started, attempts in rows:
            if delegation_id in offered or alive(pid, started):
                continue
            if (attempts or 0) >= _MAX_DELIVERY_ATTEMPTS:
                # Its last claimant died holding the final attempt; converge like release_completion_delivery.
                conn.execute("""UPDATE async_delegations SET delivery_state='dropped',
                              delivery_claim=NULL, delivery_claimed_at=NULL, updated_at=?
                       WHERE delegation_id=? AND delivery_state='pending'""", (now, delegation_id))
                logger.warning("Async delegation %s exhausted its %d delivery attempts; "
                               "marking terminally dropped (result remains queryable).",
                               delegation_id, _MAX_DELIVERY_ATTEMPTS)
                continue
            orphans.append((delegation_id, payload, completed_at, dispatched_at))
        restored = _replay_pending(conn, orphans, target_queue, now)
        outbox_rows = conn.execute(
            """SELECT e.event_id, e.event_json, e.created_at, e.updated_at,
                      d.owner_pid, d.owner_started_at, e.delivery_attempts
               FROM async_delegation_events e
               LEFT JOIN async_delegations d ON d.delegation_id=e.delegation_id
               WHERE e.delivery_state='pending' AND e.event_json IS NOT NULL AND e.updated_at < ?
                 AND (e.delivery_claim IS NULL OR e.delivery_claimed_at < ?)
               ORDER BY e.created_at, e.event_id""",
            (now - _ORPHAN_STALE_S, now - _CLAIM_LEASE_S),
        ).fetchall()
        outbox_orphans = []
        for event_id, payload, created_at, updated_at, pid, started, attempts in outbox_rows:
            if event_id in offered or alive(pid, started):
                continue
            if (attempts or 0) >= _MAX_DELIVERY_ATTEMPTS:
                conn.execute(
                    """UPDATE async_delegation_events SET delivery_state='dropped',
                              delivery_claim=NULL, delivery_claimed_at=NULL, updated_at=?
                       WHERE event_id=? AND delivery_state='pending'""",
                    (now, event_id),
                )
                continue
            outbox_orphans.append((event_id, payload, created_at, updated_at))
        return held + restored + _replay_outbox_pending(conn, outbox_orphans, target_queue, now)


def maybe_sweep_orphaned_completions(target_queue, *, now: Optional[float] = None) -> int:
    """``sweep_orphaned_completions`` at most once per ``ORPHAN_SWEEP_INTERVAL_S`` per home (``now`` is
    monotonic), for delivery loops that tick far more often. Never raises into the loop."""
    home = hermes_home_key(get_hermes_home())
    now = time.monotonic() if now is None else now
    with _orphan_lock:
        last = _last_orphan_sweep.get(home)
        if last is not None and now - last < ORPHAN_SWEEP_INTERVAL_S:
            return 0
        _last_orphan_sweep[home] = now
    try:
        return sweep_orphaned_completions(target_queue)
    except Exception:
        logger.debug("Orphaned async delegation sweep failed", exc_info=True)
        return 0


def _update_delivery(sql: str, params: tuple) -> bool:
    """Run one UPDATE on the ledger; True iff exactly one row changed."""
    with _DB_LOCK, _transaction() as conn:
        return conn.execute(sql, params).rowcount == 1


def mark_completion_delivered(delegation_id: str) -> bool:
    """Atomically acknowledge successful injection of a durable completion."""
    now = time.time()
    return _update_delivery(
        """UPDATE async_delegations SET delivery_state='delivered', delivered_at=?, updated_at=?
           WHERE delegation_id=? AND delivery_state!='delivered'""", (now, now, delegation_id))


def claim_completion_delivery(delegation_id: str, claim_id: str) -> bool:
    """Claim one pending completion across competing consumers/processes."""
    now = time.time()
    with _DB_LOCK, _transaction() as conn:
        row = conn.execute(
            "SELECT delivery_state FROM async_delegations WHERE delegation_id=?", (delegation_id,)).fetchone()
        if row is None:
            return True  # legacy event created before durable dispatch
        cur = conn.execute("""UPDATE async_delegations SET delivery_claim=?, delivery_claimed_at=?,
                      delivery_attempts=delivery_attempts+1, updated_at=?
               WHERE delegation_id=? AND delivery_state='pending'
                 AND (delivery_claim IS NULL OR delivery_claimed_at < ?)""",
            (claim_id, now, now, delegation_id, now - _CLAIM_LEASE_S))
        return cur.rowcount == 1


# In-memory marker on a terminal event whose producer could not prove which durable identity, if
# any, owns it (reconciliation failed). Its delegation_id alone proves nothing: a competing writer
# may own the lifecycle row. Never persisted; stripped once ownership is resolved.
_TERMINAL_OWNERSHIP_KEY = "_terminal_ownership"
_OWNERSHIP_RETRY_BASE_S = 5.0
_OWNERSHIP_RETRY_MAX_S = 300.0
# (home key, delegation_id) -> unresolved event held off the queue until its retry is due.
_unresolved: Dict[tuple, Dict[str, Any]] = {}


def _hold_unresolved(evt: Dict[str, Any], meta: Dict[str, Any], exc: BaseException) -> None:
    meta["attempts"] = int(meta.get("attempts") or 0) + 1
    delay = min(_OWNERSHIP_RETRY_BASE_S * 2 ** (meta["attempts"] - 1), _OWNERSHIP_RETRY_MAX_S)
    meta["retry_at"] = time.time() + delay
    evt[_TERMINAL_OWNERSHIP_KEY] = meta
    logger.error("Async delegation %s: terminal ownership is still unproven (attempt %d, retry in %.0fs); "
                 "holding the result undelivered: %s", evt.get("delegation_id"), meta["attempts"], delay, exc)
    with _orphan_lock:
        _unresolved[(meta["home"], str(evt.get("delegation_id") or ""))] = evt


def reoffer_unresolved_completions(target_queue, *, now: Optional[float] = None) -> int:
    """Put held terminal events whose ownership retry is due back on ``target_queue``."""
    now = time.time() if now is None else now
    with _orphan_lock:
        due = [key for key, evt in _unresolved.items()
               if (evt.get(_TERMINAL_OWNERSHIP_KEY) or {}).get("retry_at", 0.0) <= now]
        events = [_unresolved.pop(key) for key in due]
    for evt in events:
        target_queue.put(evt)
    return len(events)


def _resolve_terminal_ownership(evt: Dict[str, Any], meta: Dict[str, Any]) -> str:
    """Resolve an unproven terminal event to its durable identity, acquiring the lifecycle row
    through the conditional fallback transaction when no terminal owner exists yet."""
    # The producer's snapshots, not ``evt``: consumers enrich their copy (e.g. gateway routing
    # fields) before claiming, and the durable payloads were written from the originals.
    event, persisted, result, expected = meta["event"], meta["persisted_event"], meta["result"], meta["expected_state"]
    disposition = _reconcile_terminal_write(event, result, expected, persisted)
    if disposition == "active":
        try:
            _persist_outbox_event(dict(event), result, event_kind="terminal_fallback",
                                  terminal_status=meta["terminal_status"], expected_state=expected)
            return "outbox"
        except Exception as exc:  # noqa: BLE001 — lost the CAS, or committed before raising
            logger.error("Async delegation %s: deferred terminal fallback write failed; reconciling: %s",
                         evt.get("delegation_id"), exc)
            disposition = _reconcile_terminal_write(event, result, expected, persisted)
            if disposition == "active":
                raise
    return disposition


def resolve_event_ownership(evt: Dict[str, Any]) -> bool:
    """True when ``evt`` may be delivered: it carries no unproven terminal ownership, or that
    ownership now resolves to this event. A proven loser is discarded; an event whose ledger is
    still unreadable is held and re-offered later (``reoffer_unresolved_completions``). Either way
    the caller must drop its copy without showing it."""
    meta = evt.pop(_TERMINAL_OWNERSHIP_KEY, None)
    if not meta:
        return True
    evt.pop("_delivery_event_id", None)
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override
    token = set_hermes_home_override(meta["home"])
    try:
        disposition = _resolve_terminal_ownership(evt, meta)
    except Exception as exc:  # noqa: BLE001 — unavailable ledger: ownership stays unproven
        _hold_unresolved(evt, meta, exc)
        return False
    finally:
        reset_hermes_home_override(token)
    if disposition == "outbox":
        evt["_delivery_event_id"] = _outbox_event_id(evt, "terminal_fallback")
    elif disposition not in {"lifecycle", "missing"}:
        logger.error("Async delegation %s: unproven terminal result lost ownership to a competing "
                     "result; discarding it", evt.get("delegation_id"))
        return False
    return True


def is_interim_delegation_event(evt: Dict[str, Any]) -> bool:
    """An early per-task notice for a batch that is still running.

    It has its own durable outbox identity, so claiming or acknowledging it can
    never consume the batch's terminal completion row.
    """
    return evt.get("type") == "async_delegation" and bool(evt.get("task_failure_notice"))


def claim_event_delivery(evt: Dict[str, Any], consumer: str) -> Optional[str]:
    """Claim a durable delegation event; legacy in-memory notices remain unclaimable.

    ``None`` means this copy must not be delivered: another consumer holds it, or its terminal
    ownership is unproven (held for a later offer) or lost to a competing writer."""
    if not resolve_event_ownership(evt):
        return None
    event_id = str(evt.get("_delivery_event_id") or "")
    if event_id:
        return _claim_outbox_delivery(event_id, consumer)
    if is_interim_delegation_event(evt):
        return ""
    delegation_id = str(evt.get("delegation_id") or "") if evt.get("type") == "async_delegation" else ""
    if not delegation_id:
        return ""
    claim_id = f"{consumer}:{os.getpid()}:{uuid.uuid4().hex}"
    return claim_id if claim_completion_delivery(delegation_id, claim_id) else None


def release_completion_delivery(delegation_id: str, claim_id: str) -> bool:
    """Release a failed delivery claim so another consumer may retry."""
    event_id = _outbox_event_id_from_claim(claim_id)
    if event_id:
        return _update_outbox_delivery(event_id, claim_id, "release")
    now = time.time()
    with _DB_LOCK, _transaction() as conn:
        capped = conn.execute("""UPDATE async_delegations SET delivery_state='dropped',
                      delivery_claim=NULL, delivery_claimed_at=NULL, updated_at=?
               WHERE delegation_id=? AND delivery_state='pending'
                 AND delivery_claim=? AND delivery_attempts>=?""",
            (now, delegation_id, claim_id, _MAX_DELIVERY_ATTEMPTS))
        if capped.rowcount == 1:
            logger.warning("Async delegation %s exhausted its %d delivery attempts; "
                           "marking terminally dropped (result remains queryable).",
                           delegation_id, _MAX_DELIVERY_ATTEMPTS)
            return True
        cur = conn.execute("""UPDATE async_delegations SET delivery_claim=NULL,
                      delivery_claimed_at=NULL, updated_at=?
               WHERE delegation_id=? AND delivery_state='pending'
                 AND delivery_claim=?""", (now, delegation_id, claim_id))
        return cur.rowcount == 1


def defer_completion_delivery(delegation_id: str, claim_id: str) -> bool:
    """Return an unadmitted completion to pending without spending a delivery attempt."""
    event_id = _outbox_event_id_from_claim(claim_id)
    if event_id:
        return _update_outbox_delivery(event_id, claim_id, "defer")
    return _update_delivery("""UPDATE async_delegations SET delivery_claim=NULL,
                  delivery_claimed_at=NULL, delivery_attempts=MAX(0, delivery_attempts-1),
                  updated_at=?
           WHERE delegation_id=? AND delivery_state='pending' AND delivery_claim=?""",
        (time.time(), delegation_id, claim_id))


def drop_completion_delivery(delegation_id: str, claim_id: str) -> bool:
    """Terminally drop a claimed completion whose target is permanently gone."""
    event_id = _outbox_event_id_from_claim(claim_id)
    if event_id:
        return _update_outbox_delivery(event_id, claim_id, "drop")
    return _update_delivery("""UPDATE async_delegations SET delivery_state='dropped',
                  updated_at=?, delivery_claim=NULL,
                  delivery_claimed_at=NULL
           WHERE delegation_id=? AND delivery_state='pending'
             AND delivery_claim=?""", (time.time(), delegation_id, claim_id))


def complete_completion_delivery(delegation_id: str, claim_id: str) -> bool:
    """Acknowledge acceptance for the consumer holding this claim."""
    event_id = _outbox_event_id_from_claim(claim_id)
    if event_id:
        return _update_outbox_delivery(event_id, claim_id, "complete")
    now = time.time()
    return _update_delivery("""UPDATE async_delegations SET delivery_state='delivered',
                  delivered_at=?, updated_at=?, delivery_claim=NULL,
                  delivery_claimed_at=NULL
           WHERE delegation_id=? AND delivery_state='pending'
             AND delivery_claim=?""", (now, now, delegation_id, claim_id))


def complete_event_delivery(evt: Dict[str, Any], claim_id: str) -> None:
    _event_delivery(complete_completion_delivery, evt, claim_id)


def release_event_delivery(evt: Dict[str, Any], claim_id: str) -> None:
    """Release a failed claim for a consumer that discards its copy (the TUI poller): the row is pending
    again, so it must stay eligible for the orphan sweep."""
    _event_delivery(release_completion_delivery, evt, claim_id)
    return_completion_offer(evt)


def return_completion_offer(evt: Dict[str, Any]) -> None:
    """Hand an offered completion back to the orphan sweep after its in-memory copy was discarded while
    the durable row stays pending, e.g. a TUI session that cannot prove it owns the event drops it (every
    session poller drains one process-wide queue). The next sweep may offer the row again. Delegation ids
    are unique across profiles, so this clears the offer in every home."""
    delegation_id = str(evt.get("delegation_id") or "") if evt.get("type") == "async_delegation" else ""
    event_id = str(evt.get("_delivery_event_id") or "")
    if not delegation_id and not event_id:
        return
    with _orphan_lock:
        _offered.difference_update({key for key in _offered if key[1] in {delegation_id, event_id}})


def _event_delivery(fn, evt: Dict[str, Any], claim_id: str) -> None:
    if claim_id and evt.get("type") == "async_delegation":
        fn(str(evt.get("delegation_id") or ""), claim_id)


def get_durable_delegation(delegation_id: str) -> Optional[Dict[str, Any]]:
    with _DB_LOCK, _transaction() as conn:
        row = conn.execute("""SELECT origin_session, state, dispatched_at, completed_at,
                      result_json, delivery_state, delivery_attempts,
                      origin_session_id
               FROM async_delegations WHERE delegation_id=?""", (delegation_id,)).fetchone()
    return None if row is None else {
        "delegation_id": delegation_id, "origin_session": row[0], "state": row[1], "dispatched_at": row[2],
        "completed_at": row[3], "result": json.loads(row[4]) if row[4] else None, "delivery_state": row[5],
        "delivery_attempts": row[6], "origin_session_id": row[7] or ""}


_FAILED_TASK_STATES = frozenset({"error", "failed", "failure", "timeout", "stalled", "unknown", "interrupted"})
_FAILURE_SURFACE_WINDOW_S = 24 * 3600.0


def _json_object(raw: Optional[str]) -> Dict[str, Any]:
    try:
        value = json.loads(raw) if raw else {}
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def failed_delegations_for_session(
    origin_ui_session_id: str = "", parent_session_id: str = "", *, limit: int = 20, now: Optional[float] = None,
) -> List[Dict[str, Any]]:
    """Recently failed async delegation tasks owned by a session, newest first.

    The live roster forgets a child once it ends and does not survive a renderer reload, so a failed
    delegation had nowhere to show (#97202). This reads the durable row instead: one entry per failed
    task (a batch unit that "completed" can still carry failed tasks) with ``delegation_id``,
    ``task_index``, ``goal``, ``status``, ``error``, ``dispatched_at`` and ``completed_at``. Either selector claims a row:
    the UI session id at dispatch, or the spawner's durable session id (survives a reload re-mint)."""
    selectors = [(col, val) for col, val in (
        ("origin_ui_session_id", origin_ui_session_id), ("parent_session_id", parent_session_id)) if val]
    if not selectors:
        return []
    cutoff = (now if now is not None else time.time()) - _FAILURE_SURFACE_WINDOW_S
    owner_sql = " OR ".join(f"{col}=?" for col, _ in selectors)
    with _DB_LOCK, _transaction() as conn:
        rows = conn.execute(
            f"""SELECT delegation_id, state, dispatched_at, completed_at, task_json, result_json FROM async_delegations
                WHERE ({owner_sql}) AND state NOT IN ('admitted','running','finalizing') AND completed_at >= ?
                ORDER BY completed_at DESC LIMIT ?""",
            (*(val for _, val in selectors), cutoff, limit)).fetchall()
    failed: List[Dict[str, Any]] = []
    for delegation_id, state, dispatched_at, completed_at, task_json, result_json in rows:
        task, result = _json_object(task_json), _json_object(result_json)
        goals = task.get("goals") if isinstance(task.get("goals"), list) and task["goals"] else [task.get("goal") or ""]
        goal_for = dict(zip(task.get("task_indexes") or range(len(goals)), goals))
        tasks = result["results"] if isinstance(result.get("results"), list) else [] if task.get("is_batch") else [result]
        if not tasks and str(state).lower() in _FAILED_TASK_STATES:
            tasks = [{"task_index": 0, "error": result.get("error")}]
        for entry in tasks:
            status = str(entry.get("status") or state or "").lower()
            if status not in _FAILED_TASK_STATES:
                continue
            index = entry.get("task_index") if isinstance(entry.get("task_index"), int) else 0
            error = entry.get("error") or result.get("error")
            failed.append({
                "delegation_id": delegation_id, "task_index": index, "status": status,
                "goal": str(goal_for.get(index, goals[0]) or ""), "error": str(error) if error else None,
                "dispatched_at": dispatched_at, "completed_at": completed_at})
    return failed[:limit]


# ── In-memory registry queries ──────────────────────────────────────────────
def _get_executor(max_workers: int) -> ThreadPoolExecutor:
    """Lazily create (or grow in place, never shrink) the shared daemon executor. Raising
    ``_max_workers`` is enough: the next ``submit`` spawns threads up to the new cap."""
    global _executor, _executor_max_workers
    with _executor_lock:
        if _executor is None:
            _executor = DaemonThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="async-delegate")
            _executor_max_workers = max_workers
        elif max_workers > _executor_max_workers:
            _executor._max_workers = max_workers
            _executor_max_workers = max_workers
        return _executor


def active_count() -> int:
    """Number of live async delegation UNITS (one per completion message: a task group or an ungrouped task)."""
    with _records_lock:
        return sum(1 for r in _records.values() if r.get("status") in _LIVE_STATES)


def active_records() -> List[Dict[str, Any]]:
    """Snapshot live async delegation records for shutdown and observability consumers."""
    with _records_lock:
        return [
            {"delegation_id": r.get("delegation_id"), "status": r.get("status"), "dispatched_at": r.get("dispatched_at")}
            for r in _records.values()
            if r.get("status") in _LIVE_STATES
        ]


def active_task_count() -> int:
    """Number of running child subagents (a batch of N contributes N; a batch with
    no goal list counts 1) — the truthful observability figure, unlike slots."""
    with _records_lock:
        return sum(
            len(r.get("task_indexes") or r["goals"])
            if r.get("is_batch") and isinstance(r.get("goals"), (list, tuple)) and r["goals"] else 1
            for r in _records.values() if r.get("status") in {"running", "finalizing"})


def _session_records(statuses, session_key: str, origin_ui_session_id: str, parent_session_id: str) -> list:
    """Records in ``statuses`` owned by a session: any non-empty selector claims the
    record — ``origin_ui_session_id`` (TUI tab), ``session_key`` (routing key at
    dispatch), or ``parent_session_id`` (spawner's durable id — the right one for
    gateway chats, whose session_key survives ``/new`` while the session id rotates)."""
    selectors = [(field, wanted) for field, wanted in (
        ("origin_ui_session_id", origin_ui_session_id), ("session_key", session_key),
        ("parent_session_id", parent_session_id)) if wanted]
    if not selectors:
        return []
    with _records_lock:
        return [r for r in _records.values() if r.get("status") in statuses
                and any(str(r.get(field) or "") == wanted for field, wanted in selectors)]


def has_live_for_session(session_key: str = "", origin_ui_session_id: str = "", parent_session_id: str = "") -> bool:
    """Whether a session still owns any live (running/stalling/finalizing) delegation."""
    return bool(_session_records(_LIVE_STATES, session_key, origin_ui_session_id, parent_session_id))


def _new_delegation_id() -> str:
    return f"deleg_{uuid.uuid4().hex[:8]}"


def _prune_completed_locked() -> None:
    """Drop the oldest completed records beyond the cap. Caller holds ``_records_lock``.
    ``stalling``/``finalizing`` are still live: evicting one makes the late runner return hit
    ``_finalize``'s missing-record path and silently drop a real result. A terminal record whose
    executor future is still running is also retained so its slot reservation cannot disappear.
    """
    completed = [(rid, r) for rid, r in _records.items()
                 if r.get("status") not in _LIVE_STATES and not r.get("_slot_reserved")]
    completed.sort(key=lambda kv: kv[1].get("completed_at") or kv[1].get("dispatched_at") or 0)
    for rid, _ in completed[: max(0, len(completed) - _MAX_RETAINED_COMPLETED)]:
        _records.pop(rid, None)


def _current_origin_session_id() -> str:
    """Raw session id of the ORIGINATING api_server request, or ``""``. ``HERMES_SESSION_ID``
    is unsafe here: building the child agent calls ``set_current_session_id(child.session_id)``
    just before dispatch, so the wake would self-post into the subagent's own session. The
    request-scoped ``HERMES_SESSION_CHAT_ID`` (raw X-Hermes-Session-Id on api_server) survives
    child construction; on push platforms chat_id is a chat, not a session => ``""``."""
    try:
        from gateway.session_context import get_session_env
        is_api = get_session_env("HERMES_SESSION_PLATFORM", "") == "api_server"
        return (get_session_env("HERMES_SESSION_CHAT_ID", "") or "") if is_api else ""
    except Exception:
        return ""


# ── Dispatch ────────────────────────────────────────────────────────────────
def _single_crash(error: str, duration: float) -> Dict[str, Any]:
    return {"status": "error", "summary": None, "error": error, "api_calls": 0, "duration_seconds": duration}


def _batch_crash(error: str, duration: float) -> Dict[str, Any]:
    return {"results": [], "error": error, "total_duration_seconds": duration}


def _batch_status(combined: Dict[str, Any]) -> str:
    """Batch status: completed unless every child errored/was interrupted."""
    child_results = combined.get("results") or []
    ok = ("completed", "success")
    return "error" if child_results and all(r.get("status") not in ok for r in child_results) else "completed"


def _dispatch(**kwargs) -> Dict[str, Any]:
    from hermes_cli.backend_retirement import retirement

    with retirement.work() as admitted:
        if not admitted:
            return {"status": "rejected", "accepted": False, "error": "backend is retiring; reconnect to continue"}
        return _dispatch_admitted(**kwargs)


def _queued_count_locked() -> int:
    # Count queued records, including one whose durable insert succeeded but
    # whose publication to _PENDING_QUEUE has not happened yet.  The latter
    # must reserve queue capacity without becoming admission-eligible.
    return len({r.get("slot_key") or r["delegation_id"] for r in _records.values()
                if r.get("status") == "queued"})


def _active_slots_locked() -> set:
    # A force-finalized runner remains a capacity occupant until its executor
    # future's done callback runs.  Its terminal status is intentionally still
    # reported to users immediately, so the reservation is separate from the
    # live-state accounting above.
    slots = {r.get("slot_key") or r["delegation_id"] for r in _records.values()
             if r.get("status") in _ACTIVE_STATES or r.get("_slot_reserved")}
    return slots | set(_PENDING_ADMISSION_SLOTS)


def _record_context_run(record: Dict[str, Any], fn: Callable, *args):
    ctx = record.get("_context") or contextvars.copy_context()
    return ctx.copy().run(fn, *args)


def _submit_record(record: Dict[str, Any], max_async_children: int) -> Optional[str]:
    """Submit an admitted record and make it running only while a Future exists.

    A placeholder Future is attached before invoking the executor.  That keeps
    the invariant true even for executors that run their callable before
    ``submit()`` returns; the real Future replaces the placeholder immediately
    after submission and owns normal slot release callbacks.  The caller owns
    ``_records_lock``.
    """
    from concurrent.futures import Future

    delegation_id = record["delegation_id"]
    is_batch = bool(record.get("is_batch"))
    label = " batch" if is_batch else ""
    try:
        live_units = sum(1 for r in _records.values() if r.get("status") in _LIVE_STATES)
        executor = _get_executor(max(max_async_children, live_units))
    except Exception as exc:  # noqa: BLE001 - admission must settle a failed executor lookup
        logger.warning("Async delegation %s could not create an executor: %s", delegation_id, exc)
        return f"Failed to schedule async delegation{label}: {exc}"

    def _worker() -> None:
        result: Dict[str, Any] = {}
        status = "error"
        with _records_lock:
            rec = _records.get(delegation_id)
            if rec is not None and rec.get("status") == "running":
                rec.update(_started=True, _progress_ts=time.time())
        try:
            result = record["runner"]() or {}
            status = record["classify"](result)
        except Exception as exc:  # noqa: BLE001 — must never crash the worker
            logger.exception(f"Async delegation{label} %s crashed", delegation_id)
            result = record["crash_result"](
                f"{type(exc).__name__}: {exc}", round(time.time() - record["dispatched_at"], 2)
            )
        finally:
            _finalize(delegation_id, result, status)

    from hermes_cli.backend_retirement import retirement
    if not retirement.acquire():
        return _BACKEND_RETIRING

    placeholder = Future()
    record["_future"] = placeholder
    record["_slot_release_done"] = False
    record["_submitting"] = True
    future = None

    def release_slot(done_future: Any) -> None:
        released = False
        with _records_lock:
            live = _records.get(delegation_id)
            if live is not None and not live.get("_slot_release_done"):
                live["_slot_release_done"] = True
                live["_slot_reserved"] = False
                released = True
        if released:
            # A force-finalized runner only becomes capacity-free here.
            _admit_pending()

    try:
        # The placeholder is the proof that running is legal even if a custom
        # executor starts the worker synchronously inside submit().
        changed = _record_context_run(record, _persist_transition, delegation_id, "admitted", "running")
        if changed != 1:
            raise RuntimeError(f"async delegation {delegation_id} could not transition admitted -> running")
        _transition_memory_locked(record, "running", expected="admitted")
        record["_durable_state"] = "running"
        record["_running_at"] = time.time()
        record["_started"] = False

        # Let the worker acquire the lifecycle lock if submit() starts it before
        # returning.  The placeholder Future keeps the invariant during this
        # handoff; the real Future is attached before this function returns.
        is_owned = getattr(_records_lock, "_is_owned", lambda: False)
        release_save = getattr(_records_lock, "_release_save", lambda: None)
        acquire_restore = getattr(_records_lock, "_acquire_restore", lambda _: None)
        lock_state = release_save() if is_owned() else None
        try:
            future = executor.submit(record["_worker_context_runner"], _worker)
        finally:
            if lock_state is not None:
                acquire_restore(lock_state)
        record["_future"] = future

        future.add_done_callback(release_slot)
        future.add_done_callback(lambda _: retirement.release())
        record["_submitting"] = False
    except Exception as exc:  # pragma: no cover — pool submit/transition failure is rare
        record["_submitting"] = False
        if future is not None:
            # executor.submit() succeeded, so this record owns a real Future even
            # if callback registration failed.  It is already submitted and must
            # not be rolled back with its siblings or have its Future hidden.
            record["_future"] = future
            record["_slot_release_done"] = False
            record["_slot_reserved"] = True
            logger.warning("Async delegation %s completed submit setup with a live Future: %s", delegation_id, exc)
            try:
                future.add_done_callback(release_slot)
                future.add_done_callback(lambda _: retirement.release())
            except Exception:
                # Future callback registration is not expected to fail, but the
                # worker remains the source of truth; never pretend it was not
                # submitted. The retirement lease is released by the done path
                # when possible, and the capacity reservation stays conservative.
                logger.exception("Async delegation %s could not attach completion callbacks", delegation_id)
            return None
        # No real Future exists. Remove the placeholder and leave the record for
        # the caller's settle-all path; it will restore the whole sibling group
        # to queued or terminally fail it. Do not enqueue this item here.
        record["_future"] = None
        record["_slot_release_done"] = True
        record["_slot_reserved"] = True
        if record.get("status") not in {"admitted", "running"}:
            # A nonstandard executor may run the worker and then raise from
            # submit(). There is no Future to release later, and finalization
            # already owns the terminal outcome, so do not retain the slot.
            record["_slot_reserved"] = False
            logger.warning("Async delegation %s submit failed after terminal worker completion: %s", delegation_id, exc)
            retirement.release()
            return f"Failed to schedule async delegation{label}: {exc}"
        if record.get("status") == "running":
            try:
                changed = _record_context_run(record, _persist_transition, delegation_id, "running", "admitted")
            except Exception:
                changed = 0
            if changed == 1:
                _transition_memory_locked(record, "admitted", expected="running")
                record["_durable_state"] = "admitted"
        retirement.release()
        logger.warning("Async delegation %s could not be submitted: %s", delegation_id, exc)
        return f"Failed to schedule async delegation{label}: {exc}"
    if record.get("progress_fn") is not None:
        _ensure_stale_monitor()
    return None


def _queue_selected_locked(selected: List[Dict[str, Any]]) -> None:
    """Restore a selected sibling group to queued in memory and FIFO order.

    The caller has already durably rolled the group back to ``queued`` (or is
    handling a submission-fence retry). Keeping this helper transition-based
    prevents a sibling from being left admitted without a Future.
    """
    for item in reversed(selected):
        status = item.get("status")
        if status == "admitted":
            _transition_memory_locked(item, "queued", expected="admitted")
        elif status == "running":
            _transition_memory_locked(item, "queued", expected="running")
        elif status != "queued":
            continue
        item["_durable_state"] = "queued"
        item["_slot_reserved"] = False
        item["_slot_release_done"] = True
        item["queue_reason"] = "async pool capacity"
        if item["delegation_id"] not in _PENDING_QUEUE:
            _PENDING_QUEUE.appendleft(item["delegation_id"])


def _settle_unsubmitted_locked(
    selected: List[Dict[str, Any]], submitted: List[Dict[str, Any]], error: str,
) -> None:
    """Settle selected siblings that do not hold a real Future.

    The submission loop is per record, but a failure decision is for the whole
    selected sibling group. Futures already attached are allowed to finish;
    every other record is transitioned from its actual state and then restored
    to FIFO queue order or terminally failed on retirement. The caller owns
    ``_records_lock``.
    """
    submitted_ids = {item["delegation_id"] for item in submitted if item.get("_future") is not None}
    unsettled = [
        item for item in selected
        if item["delegation_id"] not in submitted_ids
        and item.get("_future") is None
        and item.get("status") in {"admitted", "running"}
    ]
    if not unsettled:
        return

    prior_statuses = {item["delegation_id"]: item.get("status") for item in unsettled}

    def settle_group(items: List[Dict[str, Any]], target: str) -> List[Dict[str, Any]]:
        """Persist and publish one target for groups split by current state."""
        settled: List[Dict[str, Any]] = []
        for expected in ("admitted", "running"):
            state_items = [item for item in items if item.get("status") == expected]
            if not state_items:
                continue
            ids = [item["delegation_id"] for item in state_items]
            try:
                _record_context_run(state_items[0], _persist_transition_group, ids, expected, target)
            except Exception:
                logger.exception("Could not settle unsubmitted sibling group %s (%s -> %s)", ids, expected, target)
                _ensure_stale_monitor()
                continue
            for item in state_items:
                _transition_memory_locked(item, target, expected=expected)
                item["_durable_state"] = target
                settled.append(item)
        return settled

    if error == _BACKEND_RETIRING:
        settled = settle_group(unsettled, "finalizing")
        for item in settled:
            item["_terminal_state"] = "failed"
            item["completed_at"] = time.time()
            item["_slot_reserved"] = False
            item["_slot_release_done"] = True
            item["interrupt_fn"] = None
            item["progress_fn"] = None
            snapshot = dict(item)
            snapshot["_claimed_prior_status"] = prior_statuses[item["delegation_id"]]
            _record_context_run(
                item,
                lambda item=item, snapshot=snapshot: _finalize(
                    item["delegation_id"], item["crash_result"](error, 0.0), "failed", _claimed_snapshot=snapshot,
                ),
            )
        return

    queued = [item for item in unsettled if item.get("_initially_queued")]
    rejected = [item for item in unsettled if not item.get("_initially_queued")]
    settled_queued = settle_group(queued, "queued")
    if settled_queued:
        order = {item["delegation_id"]: index for index, item in enumerate(selected)}
        settled_queued.sort(key=lambda item: order[item["delegation_id"]])
        _queue_selected_locked(settled_queued)
        _ensure_stale_monitor()
    for item in settle_group(rejected, "failed"):
        item["_terminal_state"] = "failed"
        item["_schedule_error"] = error
        item["_slot_reserved"] = False
        item["_slot_release_done"] = True
        item["completed_at"] = time.time()


def _admit_pending() -> None:
    """Promote durable queued records while capacity is available.

    Selection, sibling-group admission, slot reservation, cancellation
    exclusion, durable transitions, and submission are lock-owned. Admission is
    transactional, while submission settles the entire selected sibling group:
    records with Futures are left running and every record without one is
    requeued (or terminally failed when retirement closed).
    """
    while True:
        with _records_lock:
            active_slots = _active_slots_locked()
            selected: List[Dict[str, Any]] = []
            selected_slot = None
            for delegation_id in list(_PENDING_QUEUE):
                record = _records.get(delegation_id)
                if record is None or record.get("status") != "queued":
                    try:
                        _PENDING_QUEUE.remove(delegation_id)
                    except ValueError:
                        pass
                    continue
                slot_key = record.get("slot_key") or delegation_id
                if slot_key not in active_slots and len(active_slots) >= record["max_async_children"]:
                    continue
                selected_slot = slot_key
                for sibling_id in list(_PENDING_QUEUE):
                    sibling = _records.get(sibling_id)
                    if sibling is None or sibling.get("status") != "queued":
                        continue
                    if (sibling.get("slot_key") or sibling_id) != selected_slot:
                        continue
                    _PENDING_QUEUE.remove(sibling_id)
                    selected.append(sibling)
                break
            if not selected:
                return

            selected_ids = [item["delegation_id"] for item in selected]
            try:
                _record_context_run(selected[0], _persist_transition_group, selected_ids, "queued", "admitted")
            except Exception:
                logger.exception("Could not persist all-or-nothing admission of queued siblings %s", selected_ids)
                # The group transaction rolls back, so every durable row is
                # still queued. Restore every in-memory queue entry together;
                # no sibling may remain admitted or consume a slot.
                _queue_selected_locked(selected)
                _ensure_stale_monitor()
                return

            now = time.time()
            for item in selected:
                _transition_memory_locked(item, "admitted", expected="queued")
                item["_durable_state"] = "admitted"
                item["_admitted_at"] = now
                item["_slot_reserved"] = True
                item["_slot_release_done"] = False

            submitted: List[Dict[str, Any]] = []
            submission_error: Optional[str] = None
            try:
                for item in selected:
                    try:
                        error = _record_context_run(item, _submit_record, item, item["max_async_children"])
                    except Exception as exc:  # noqa: BLE001 - settle every sibling on any submit path
                        logger.exception("Async delegation %s submission raised", item["delegation_id"])
                        error = f"Failed to schedule async delegation: {exc}"
                    if error:
                        submission_error = error
                        break
                    submitted.append(item)
            finally:
                if submission_error is not None:
                    _settle_unsubmitted_locked(selected, submitted, submission_error)

            if submission_error is not None:
                # A non-retirement failure restored queued entries and a
                # retirement failure terminally settled them.  In both cases a
                # later loop must not touch this partially submitted group.
                return
            # Loop again: a done callback may have released a slot while this
            # batch was being submitted, and the queue should fill capacity.
def _dispatch_admitted(
    *, delegation_id: str, goal: str, goals: Optional[List[str]], context: Optional[str],
    toolsets: Optional[List[str]], role: str, model: Optional[str], session_key: str,
    parent_session_id: Optional[str], runner: Callable[[], Dict[str, Any]], origin_ui_session_id: str,
    origin_session_id: str, interrupt_fn: Optional[Callable[[], None]], max_async_children: int,
    progress_fn: Optional[Callable[[], tuple]], capacity_error: str, slot_key: Optional[str] = None,
    task_indexes: Optional[List[int]] = None,
    task_transcripts: Optional[Dict[str, str]] = None,
    cron_execution: Optional[Dict[str, str]] = None,
    max_queued_delegations: int = _DEFAULT_MAX_QUEUED_DELEGATIONS,
    cancel_fn: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    """Register and submit one async unit without ever running its runner inline.

    A full pool places the unit in a bounded FIFO when possible. Only the queue
    overflow is rejected; callers that can receive detached completions must not
    turn capacity pressure into synchronous work.
    """
    is_batch = goals is not None
    label = " batch" if is_batch else ""
    classify = _batch_status if is_batch else (lambda r: r.get("status") or "completed")
    crash_result = _batch_crash if is_batch else _single_crash
    dispatched_at = time.time()
    record: Dict[str, Any] = {
        "delegation_id": delegation_id, "goal": goal, **({"goals": list(goals)} if is_batch else {}),
        "context": context, "toolsets": list(toolsets) if toolsets else None, "role": role, "model": model,
        "session_key": session_key, "origin_ui_session_id": origin_ui_session_id,
        "origin_session_id": origin_session_id, "parent_session_id": parent_session_id,
        **_capture_routing_origin(), "status": "queued", "_lifecycle_state": "new", "_durable_state": None,
        "dispatched_at": dispatched_at, "completed_at": None,
        "interrupt_fn": interrupt_fn, "cancel_fn": cancel_fn, "runner": runner, "classify": classify,
        "crash_result": crash_result, **({"is_batch": True} if is_batch else {}), "progress_fn": progress_fn,
        "slot_key": slot_key or delegation_id, "max_async_children": max_async_children,
        **(cron_execution or {}), **({"task_transcripts": dict(task_transcripts)} if task_transcripts else {}),
        **({"task_indexes": list(task_indexes)} if task_indexes is not None else {}),
        "_context": contextvars.copy_context(),
        "_worker_context_runner": propagate_context_to_thread(lambda worker: worker()), "_progress_token": None,
        "_progress_ts": dispatched_at, "_interrupted_at": None, "_started": False,
        "_slot_reserved": False, "_slot_release_done": True,
        "_persisting": True, "_persist_done": threading.Event(),
    }
    with _records_lock:
        existing = _records.get(delegation_id)
        if existing is not None:
            return {"status": "rejected", "accepted": False,
                    "error": f"Async delegation {delegation_id} already exists in memory; refusing duplicate id."}
        try:
            durable = _durable_state(delegation_id)
        except Exception:
            # A failed preflight read must not turn a fresh insert failure into
            # an uncaught dispatch error; _persist_dispatch remains authoritative
            # for accepting or rejecting the new durable row.
            durable = None
        if durable is not None and durable.get("state") not in _TERMINAL_STATES:
            return {"status": "rejected", "accepted": False,
                    "error": f"Async delegation {delegation_id} already exists durably in non-terminal state "
                             f"{durable.get('state')!r}; refusing duplicate id."}
        active_slots = _active_slots_locked()
        slot = record["slot_key"]
        initially_queued = slot not in active_slots and len(active_slots) >= max_async_children
        record["_initially_queued"] = initially_queued
        if initially_queued:
            queued_same_slot = any(
                r.get("status") == "queued"
                and (r.get("slot_key") or r["delegation_id"]) == slot
                for r in _records.values()
            )
            if _queued_count_locked() >= max(0, int(max_queued_delegations)) and not queued_same_slot:
                return {"status": "rejected", "at_capacity": True, "queue_full": True,
                        "accepted": False,
                        "error": capacity_error + " The bounded pending queue is also full; nothing was started."}
            record["queue_reason"] = "async pool capacity"
            record["queued_at"] = time.time()
        else:
            _PENDING_ADMISSION_SLOTS.add(slot)
        _records[delegation_id] = record
    try:
        # Publish no admission-eligible queue entry until this insert commits.
        # Cancellation observes ``_persisting`` and waits for this same record
        # to become durable before taking the terminal transition.
        _persist_dispatch(record)
    except Exception as exc:
        with _records_lock:
            if _records.get(delegation_id) is record:
                _records.pop(delegation_id, None)
                _PENDING_ADMISSION_SLOTS.discard(record["slot_key"])
        record["_persist_done"].set()
        logger.error("Failed to persist new async delegation %s", delegation_id, exc_info=True)
        return {"status": "rejected", "accepted": False, "error": f"Failed to persist async delegation{label}: {exc}"}
    cancel_after_persist = False
    with _records_lock:
        record["_persisting"] = False
        record["_persist_done"].set()
        record["_lifecycle_state"] = "persisted"
        record["_durable_state"] = "queued"
        _PENDING_ADMISSION_SLOTS.discard(record["slot_key"])
        if record.get("_cancel_requested"):
            cancel_after_persist = True
        else:
            _PENDING_QUEUE.append(delegation_id)
            _admit_pending()
        status = record.get("status")
    if cancel_after_persist:
        _interrupt_records([record], "interrupt_delegation", record.get("_cancel_reason", "cancelled"),
                           "Interrupted %d async delegation(s) (%s)")
        return {"status": "cancelled", "delegation_id": delegation_id, "accepted": True}
    with _records_lock:
        if record.get("_schedule_error"):
            return {"status": "rejected", "accepted": False, "error": record["_schedule_error"]}
        if status == "queued" or record.get("_initially_queued"):
            _ensure_stale_monitor()
            return {"status": "queued", "accepted": True, "delegation_id": delegation_id,
                    "queue_reason": record.get("queue_reason", "async pool capacity")}
        if status in _TERMINAL_STATES:
            return {"status": status, "accepted": True, "delegation_id": delegation_id}
        return {"status": "dispatched", "accepted": True, "delegation_id": delegation_id}


def dispatch_async_delegation(
    *, goal: str, context: Optional[str], toolsets: Optional[List[str]], role: str, model: Optional[str],
    session_key: str, parent_session_id: Optional[str] = None, runner: Callable[[], Dict[str, Any]],
    origin_ui_session_id: str = "", origin_session_id: str = "", interrupt_fn: Optional[Callable[[], None]] = None,
    max_async_children: int = _DEFAULT_MAX_ASYNC_CHILDREN, max_queued_delegations: int = _DEFAULT_MAX_QUEUED_DELEGATIONS,
    progress_fn: Optional[Callable[[], tuple]] = None, delegation_id: Optional[str] = None,
    cron_execution: Optional[Dict[str, str]] = None, cancel_fn: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    """Spawn ``runner`` on the daemon executor and return a handle immediately.
    ``session_key``/``parent_session_id`` are captured on the parent thread (the worker carries
    no contextvars) and route the completion back to the spawning session.
    ``progress_fn() -> (token, in_tool)`` enables stale monitoring; omitted = unmonitored.
    Returns ``{"status": "dispatched", "delegation_id"}`` or ``{"status": "rejected", "error"}``."""
    delegation_id = delegation_id or _new_delegation_id()
    handle = _dispatch(
        delegation_id=delegation_id, goal=goal, goals=None, context=context,
        toolsets=toolsets, role=role, model=model, session_key=session_key,
        parent_session_id=parent_session_id, runner=runner,
        origin_ui_session_id=origin_ui_session_id, origin_session_id=origin_session_id,
        interrupt_fn=interrupt_fn, max_async_children=max_async_children, progress_fn=progress_fn,
        max_queued_delegations=max_queued_delegations, cancel_fn=cancel_fn,
        cron_execution=cron_execution,
        capacity_error=(
            f"Async delegation capacity reached ({max_async_children} running). Wait for one to finish "
            "(its result will re-enter the chat), or run this task synchronously (background=false). "
            "Raise delegation.max_concurrent_children in config.yaml to allow more concurrent background subagents."))
    if handle["status"] == "dispatched":
        logger.info("Dispatched async delegation %s (session_key=%s): %s",
                    delegation_id, session_key or "<cli>", (goal or "")[:80])
    return handle


def dispatch_async_delegation_batch(
    *, goals: List[str], context: Optional[str], toolsets: Optional[List[str]], role: str, model: Optional[str],
    session_key: str, parent_session_id: Optional[str] = None, runner: Callable[[], Dict[str, Any]],
    origin_ui_session_id: str = "", origin_session_id: str = "", interrupt_fn: Optional[Callable[[], None]] = None,
    max_async_children: int = _DEFAULT_MAX_ASYNC_CHILDREN, max_queued_delegations: int = _DEFAULT_MAX_QUEUED_DELEGATIONS,
    delegation_id: Optional[str] = None, progress_fn: Optional[Callable[[], tuple]] = None, slot_key: Optional[str] = None,
    task_indexes: Optional[List[int]] = None,
    task_transcripts: Optional[Dict[str, str]] = None, cancel_fn: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    """Dispatch a fan-out unit (a whole batch, or one ``group`` of a delegate_task call) as ONE
    background unit: ``runner`` runs its tasks and returns the combined ``{"results": [...],
    "total_duration_seconds": N}`` dict. The unit occupies ONE async slot — or joins the slot named
    by ``slot_key`` (in-unit parallelism is bounded separately) — and produces a SINGLE completion
    event carrying per-task ``results``."""
    delegation_id = delegation_id or _new_delegation_id()
    # ``goals`` is the whole call (result task_index indexes it); the unit's own goals label the record.
    unit_goals = [goals[i] for i in task_indexes] if task_indexes is not None else list(goals)
    n = len(unit_goals)
    combined_goal = unit_goals[0] if n == 1 else f"{n} parallel subagents: " + "; ".join(g[:40] for g in unit_goals)
    handle = _dispatch(
        delegation_id=delegation_id, goal=combined_goal, goals=goals, context=context,
        toolsets=toolsets, role=role, model=model, session_key=session_key,
        parent_session_id=parent_session_id, runner=runner,
        origin_ui_session_id=origin_ui_session_id, origin_session_id=origin_session_id,
        interrupt_fn=interrupt_fn, max_async_children=max_async_children, progress_fn=progress_fn, slot_key=slot_key,
        max_queued_delegations=max_queued_delegations, cancel_fn=cancel_fn,
        task_indexes=task_indexes, task_transcripts=task_transcripts,
        capacity_error=(
            f"Async delegation capacity reached ({max_async_children} running). Wait for one to finish "
            "(its result will re-enter the chat), or raise delegation.max_concurrent_children in "
            "config.yaml to allow more concurrent background units."))
    if handle["status"] == "dispatched":
        logger.info("Dispatched async delegation batch %s (%d task(s), session_key=%s)",
                    delegation_id, n, session_key or "<cli>")
    return handle


# ── Finalization + completion events ────────────────────────────────────────
# Everything that decides which parent a completion re-enters. A replacement must match all of it: CLI/TUI units
# share an empty session_key, and a gateway session_key outlives a conversation reset.
_PARENT_IDENTITY_KEYS = ("session_key", "origin_ui_session_id", "origin_session_id", "parent_session_id",
                         *_ROUTING_KEYS)


def supersede_delegation(delegation_id: str, replacement_id: str, reason: str = "") -> bool:
    """Mark a still-running single-task unit as replaced by ``replacement_id``, an admitted delegation that reports
    the same work. Its completion is then recorded (``delivery_state='superseded'``) but never wakes the parent.

    For owners that retry a failed child elsewhere, e.g. a reviewer rate-limited on one route and re-dispatched on
    the next from its ``subagent_stop`` hook, which runs before this unit finalizes. Refused (False) unless this
    unit is still active and runs one task, and the replacement is a known delegation reporting to the same parent
    (every routing field in ``_PARENT_IDENTITY_KEYS``), so the parent always hears about the work once."""
    if not delegation_id or not replacement_id or delegation_id == replacement_id:
        return False
    with _records_lock:
        record, replacement = _records.get(delegation_id), _records.get(replacement_id)
        if record is None or replacement is None or record.get("status") not in _ACTIVE_STATES:
            return False
        if any((replacement.get(k) or "") != (record.get(k) or "") for k in _PARENT_IDENTITY_KEYS):
            return False  # the replacement would report to a different parent
        tasks = record.get("task_indexes")
        if len(tasks if tasks is not None else (record.get("goals") or [record.get("goal")])) != 1:
            return False
        record["superseded_by"] = replacement_id
        record["superseded_reason"] = str(reason or "")[:500]
    return True


def _finalize(delegation_id: str, result: Any, status: str, *, _claimed_snapshot: Optional[Dict[str, Any]] = None) -> None:
    """Claim one terminal transition and publish its result.

    The user-visible terminal state may precede executor completion, but the
    capacity reservation remains set until the Future done callback releases it.
    """
    if _claimed_snapshot is None:
        with _records_lock:
            record = _records.get(delegation_id)
            if record is None or record.get("status") not in _FINALIZABLE_STATES:
                return
            prior_status = record.get("status")
            expected_state = record.get("_durable_state") or prior_status
            was_queued = prior_status == "queued"
            was_admitted_unstarted = prior_status == "admitted" or (prior_status == "running" and not record.get("_started"))
            _transition_memory_locked(record, "finalizing")
            record["_durable_state"] = expected_state
            record["completed_at"] = time.time()
            # Do not infer reservation from Future.done(): only its callback may
            # release a submitted slot, and the callback may not have run yet.
            if record.get("_future") is None and prior_status in {"queued", "admitted"}:
                record["_slot_reserved"] = False
                record["_slot_release_done"] = True
            record["interrupt_fn"] = None
            record["progress_fn"] = None
            snapshot = dict(record)
    else:
        snapshot = _claimed_snapshot
        prior_status = snapshot.get("_claimed_prior_status") or snapshot.get("_durable_state") or "queued"
        was_queued = prior_status == "queued"
        was_admitted_unstarted = prior_status == "admitted" or (prior_status == "running" and not snapshot.get("_started"))
    if (was_queued or was_admitted_unstarted) and snapshot.get("cancel_fn") is not None:
        try:
            snapshot["cancel_fn"]("queued delegation cancelled")
        except Exception:
            logger.debug("Queued delegation %s cleanup failed", delegation_id, exc_info=True)
    _push_completion_event(snapshot, result(snapshot) if callable(result) else result, status)
    with _records_lock:
        live = _records.get(delegation_id)
        if live is not None:
            _transition_memory_locked(live, status, expected="finalizing")
            live["_durable_state"] = live.get("_terminal_state") or status
        _prune_completed_locked()
    # A terminal event does not free a submitted slot; only release_slot does.


def _push_completion_event(record: Dict[str, Any], result: Dict[str, Any], status: str) -> None:
    """Push a type='async_delegation' event onto the shared completion queue. Batch records
    (``is_batch``) carry the per-task ``results`` list (plus live transcript paths, the
    full-fidelity record of each child's run) instead of a single summary. Best-effort: failure
    must not crash the worker, but it WOULD mean a silently-lost result, so we log loudly."""
    is_batch = bool(record.get("is_batch"))
    label = " batch" if is_batch else ""
    try:
        from tools.process_registry import process_registry
    except Exception as exc:  # pragma: no cover
        logger.error(f"Async delegation{label} %s finished but process_registry import failed; "
                     "result lost: %s", record.get("delegation_id"), exc)
        return
    dispatched_at = record.get("dispatched_at") or time.time()
    completed_at = record.get("completed_at") or time.time()
    if is_batch:
        payload = {
            "is_batch": True, "results": result.get("results") or [],
            "live_transcripts": result.get("live_transcripts"), "error": result.get("error"),
            "total_duration_seconds": result.get("total_duration_seconds"),
            **({"group": result["group"]} if result.get("group") is not None else {})}
    else:
        payload = {
            "summary": result.get("summary"), "error": result.get("error"), "api_calls": result.get("api_calls", 0),
            "duration_seconds": result.get("duration_seconds", round(completed_at - dispatched_at, 2))}
    evt = {
        "type": "async_delegation", "delegation_id": record.get("delegation_id"),
        # session_key routes back to the originating gateway session; "" => CLI.
        "session_key": record.get("session_key", ""),
        "origin_ui_session_id": record.get("origin_ui_session_id", ""),
        "origin_session_id": record.get("origin_session_id", ""),
        "parent_session_id": record.get("parent_session_id"),
        "goal": record.get("goal", ""), **({"goals": record.get("goals")} if is_batch else {}),
        "context": record.get("context"), "toolsets": record.get("toolsets"), "role": record.get("role"),
        "model": record.get("model") if is_batch else (result.get("model") or record.get("model")),
        "status": status, **payload, "dispatched_at": dispatched_at, "completed_at": completed_at,
        **({} if is_batch else {"exit_reason": result.get("exit_reason")}),
        **{k: record[k] for k in _ROUTING_KEYS if record.get(k)},
        **{k: result[k] for k in _STALL_META_KEYS if k in result}}
    if record.get("superseded_by"):
        # The owner already replaced this unit with a running delegation whose own completion will report the
        # outcome. Record the result durably, but never wake the parent for an attempt it no longer waits on.
        evt.update(superseded_by=record["superseded_by"], superseded_reason=record.get("superseded_reason") or "")
        try:
            _persist_completion(evt, result, delivery_state="superseded",
                                expected_state=record.get("_durable_state") or "running")
        except Exception as exc:  # noqa: BLE001 — the replacement still reports; only the audit row is lost
            logger.error("Async delegation %s: superseded completion write failed: %s", record.get("delegation_id"), exc)
        logger.info("Async delegation%s %s superseded by %s; completion recorded, not delivered",
                    label, record.get("delegation_id"), record["superseded_by"])
        return
    expected_state = record.get("_durable_state") or "running"
    terminal_status = record.get("_terminal_state") or status
    try:
        persist_evt = dict(evt)
        persist_evt["status"] = terminal_status
        ok = _persist_completion(persist_evt, result, expected_state=expected_state)
    except Exception as exc:  # noqa: BLE001 — reconcile an ambiguous terminal write
        logger.error("Async delegation%s %s: durable terminal row write failed; reconciling fallback ownership: %s",
                     label, record.get("delegation_id"), exc)
        ok = False
    queue_event = ok
    if not ok:
        def reconcile():
            try:
                return _record_context_run(
                    record, _reconcile_terminal_write, evt, result, expected_state, persist_evt)
            except Exception:
                logger.error("Async delegation%s %s: terminal ownership reconciliation failed",
                             label, record.get("delegation_id"), exc_info=True)
                return "unavailable"

        disposition = reconcile()
        if disposition == "active":
            # An active read is not ownership: both raised and zero-row writes
            # must acquire the lifecycle row in the fallback transaction.
            try:
                _persist_outbox_event(
                    evt, result, event_kind="terminal_fallback",
                    terminal_status=terminal_status, expected_state=expected_state,
                )
            except Exception as fallback_exc:
                logger.error("Async delegation%s %s: durable terminal outbox write failed; "
                             "reconciling ownership: %s", label, record.get("delegation_id"),
                             fallback_exc, exc_info=True)
                # The CAS may have lost to a terminal writer after the active
                # read, or this fallback may have committed before raising.
                disposition = reconcile()
            else:
                disposition = "outbox"
        queue_event = disposition in {"lifecycle", "outbox", "active", "missing", "unavailable"}
        evt.pop("_delivery_event_id", None)
        if disposition == "outbox":
            # A commit-then-raise can precede _persist_outbox_event's stamp.
            evt["_delivery_event_id"] = _outbox_event_id(evt, "terminal_fallback")
        elif disposition in {"active", "unavailable"}:
            # Unproven: neither this writer's lifecycle row, its outbox row, nor a competing
            # winner could be verified. Its delegation_id must not claim (and so settle or
            # consume) a row another writer may own; consumers resolve it first.
            evt[_TERMINAL_OWNERSHIP_KEY] = {
                "home": str(_record_context_run(record, get_hermes_home)), "event": dict(evt),
                "persisted_event": persist_evt, "result": result, "expected_state": expected_state,
                "terminal_status": terminal_status, "attempts": 0}
        if not queue_event:
            logger.error("Async delegation %s terminal fallback lost ownership to a competing result",
                         record.get("delegation_id"))
    if queue_event:
        try:
            process_registry.completion_queue.put(evt)
        except Exception as exc:  # pragma: no cover
            logger.error(f"Async delegation{label} %s: failed to enqueue completion event; "
                         "result lost: %s", record.get("delegation_id"), exc)


def push_task_failure_notice(delegation_id: str, entry: Dict[str, Any], *, n_tasks: int) -> None:
    """Surface ONE failed child of a still-running detached batch to the parent now, instead of
    when the slowest sibling finishes. In a 1,393-agent run every wave-1 child died in a 401 storm
    at 08:29 and the parent learned of it at 09:36, when the batch's "unknown outcome" block finally
    arrived: 66 minutes of a dead wave with nothing running. The notice rides the same
    ``type="async_delegation"`` event shape as the batch result (so every drain/route/format path
    treats it identically) with ``task_failure_notice=True`` and a single-entry ``results`` list; the
    batch record is NOT finalized and its consolidated result still arrives as before."""
    with _records_lock:
        record = _records.get(delegation_id)
        if record is None or record.get("status") not in _ACTIVE_STATES:
            return
        snapshot = dict(record)
    try:
        from tools.process_registry import process_registry
    except Exception as exc:  # pragma: no cover
        logger.error("Async delegation batch %s: task failure notice dropped (process_registry import): %s", delegation_id, exc)
        return
    evt = {
        "type": "async_delegation", "task_failure_notice": True, "is_batch": True, "n_tasks": n_tasks,
        "delegation_id": delegation_id, "results": [entry],
        "session_key": snapshot.get("session_key", ""),
        "origin_ui_session_id": snapshot.get("origin_ui_session_id", ""),
        "origin_session_id": snapshot.get("origin_session_id", ""),
        "parent_session_id": snapshot.get("parent_session_id"),
        "goal": snapshot.get("goal", ""), "goals": snapshot.get("goals"), "context": snapshot.get("context"),
        "toolsets": snapshot.get("toolsets"), "role": snapshot.get("role"), "model": snapshot.get("model"),
        "status": "running", "dispatched_at": snapshot.get("dispatched_at") or time.time(), "completed_at": time.time(),
        **{k: snapshot[k] for k in _ROUTING_KEYS if snapshot.get(k)}}
    try:
        _persist_outbox_event(evt, None, event_kind="task_failure")
    except Exception as exc:
        logger.error("Async delegation batch %s: durable task failure notice write failed; notice dropped: %s", delegation_id, exc)
        return
    try:
        process_registry.completion_queue.put(evt)
    except Exception as exc:  # pragma: no cover
        logger.error("Async delegation batch %s: failed to enqueue task failure notice: %s", delegation_id, exc)


# ── Stale monitor ───────────────────────────────────────────────────────────
def _ensure_stale_monitor() -> None:
    """Start (once) the stale-delegation monitor thread. One daemon thread serves
    every dispatch; it exits when no monitorable records remain and is restarted
    by the next dispatch with a ``progress_fn``."""
    global _monitor_thread
    with _monitor_lock:
        if _monitor_thread is not None and _monitor_thread.is_alive():
            return
        _monitor_stop.clear()
        _monitor_thread = threading.Thread(
            target=_stale_monitor_loop, name="async-delegate-stale-monitor", daemon=True)
        _monitor_thread.start()


def _sweep_stale_locked(now: float):
    """One monitor pass over ``_records``; caller holds ``_records_lock``. Returns
    ``(stalled, expired, any_monitorable)``: newly-stalling ``(delegation_id, quiet_for, in_tool)``
    tuples, stalling ids past the grace window, and whether anything is left to monitor."""
    stalled, expired, any_monitorable = [], [], False  # (delegation_id, quiet_for, in_tool) / ids past grace
    for record in _records.values():
        status = record.get("status")
        if status == "queued":
            any_monitorable = True
            continue
        if status == "running" and record.get("_future") is None:
            # Running is only valid after a Future has been attached.  This is
            # a bounded recovery path for an injected/partial submit failure;
            # normal submission transitions to running atomically with Future
            # attachment and never enters this branch.
            any_monitorable = True
            if now - (record.get("_running_at") or record.get("_admitted_at") or now) >= _ADMITTED_RECOVERY_SECONDS:
                delegation_id = record["delegation_id"]
                try:
                    changed = _record_context_run(record, _persist_transition, delegation_id, "running", "queued")
                except Exception:
                    logger.exception("Could not recover running async delegation %s without a Future", delegation_id)
                    changed = 0
                if changed == 1:
                    _transition_memory_locked(record, "queued", expected="running")
                    record["_durable_state"] = "queued"
                    record["_slot_reserved"] = False
                    record["_slot_release_done"] = True
                    record["queue_reason"] = "async pool capacity"
                    if delegation_id not in _PENDING_QUEUE:
                        _PENDING_QUEUE.appendleft(delegation_id)
                else:
                    durable = _record_context_run(record, _durable_state, delegation_id)
                    if durable and durable.get("state") == "queued":
                        _transition_memory_locked(record, "queued", expected="running")
                        record["_durable_state"] = "queued"
                        record["_slot_reserved"] = False
                        record["_slot_release_done"] = True
                        record["queue_reason"] = "async pool capacity"
                        if delegation_id not in _PENDING_QUEUE:
                            _PENDING_QUEUE.appendleft(delegation_id)
            continue
        if status == "admitted":
            any_monitorable = True
            # It is recoverable only while the short pre-submit window is
            # after that, an absent Future means the record was stranded.
            if record.get("_future") is None and now - (record.get("_admitted_at") or now) >= _ADMITTED_RECOVERY_SECONDS:
                delegation_id = record["delegation_id"]
                try:
                    changed = _record_context_run(record, _persist_transition, delegation_id, "admitted", "queued")
                except Exception:
                    logger.exception("Could not recover admitted async delegation %s", delegation_id)
                    changed = 0
                if changed == 1:
                    _transition_memory_locked(record, "queued", expected="admitted")
                    record["_durable_state"] = "queued"
                    record["_slot_reserved"] = False
                    record["_slot_release_done"] = True
                    record["queue_reason"] = "async pool capacity"
                    if delegation_id not in _PENDING_QUEUE:
                        _PENDING_QUEUE.appendleft(delegation_id)
                else:
                    durable = _record_context_run(record, _durable_state, delegation_id)
                    if durable and durable.get("state") == "queued":
                        _transition_memory_locked(record, "queued", expected="admitted")
                        record["_durable_state"] = "queued"
                        record["_slot_reserved"] = False
                        record["_slot_release_done"] = True
                        record["queue_reason"] = "async pool capacity"
                        if delegation_id not in _PENDING_QUEUE:
                            _PENDING_QUEUE.appendleft(delegation_id)
                    elif durable and durable.get("state") in _TERMINAL_STATES:
                        _transition_memory_locked(record, "finalizing", expected="admitted")
                        _transition_memory_locked(record, durable["state"], expected="finalizing")
                        record["_durable_state"] = durable["state"]
                        record["_slot_reserved"] = False
                        record["_slot_release_done"] = True
            continue
        if status == "stalling":
            any_monitorable = True
            if now - (record.get("_interrupted_at") or now) >= _STALL_GRACE_SECONDS:
                expired.append(record["delegation_id"])
            continue
        if status == "running" and not record.get("_started"):
            # An admitted unit can sit between retirement admission and
            # executor.submit(). Keep the monitor alive across that window so
            # a submit failure or fence requeue cannot strand it.
            any_monitorable = True
            continue
        progress_fn = record.get("progress_fn")
        if status != "running" or progress_fn is None:
            continue
        any_monitorable = True
        if not record.get("_started"):
            continue  # queued behind a full pool: not stalled, but keep the monitor alive for when it starts
        try:
            token, in_tool = progress_fn()
        except Exception:
            # An unreadable child must not look permanently healthy —
            # keep the last timestamp running instead of refreshing it.
            token, in_tool = record.get("_progress_token"), False
        if token != record.get("_progress_token"):
            record.update(_progress_token=token, _progress_ts=now)
            continue
        quiet_for = now - (record.get("_progress_ts") or now)
        limit = _STALE_IN_TOOL_SECONDS if in_tool else _STALE_IDLE_SECONDS
        if quiet_for >= limit:
            # Stall context feeds the terminal event and status listings.
            _transition_memory_locked(record, "stalling", expected="running")
            record.update(
                _interrupted_at=now, _stall_quiet_seconds=round(quiet_for, 2),
                _stall_threshold_seconds=limit, _stall_in_tool=bool(in_tool))
            stalled.append((record["delegation_id"], quiet_for, in_tool))
    return stalled, expired, any_monitorable


def _call_interrupt(fn, msg: str, *args, reason: str | None = None) -> bool:
    """Invoke an ``interrupt_fn``; True on success, else debug-log ``msg`` (+ exc)."""
    if not callable(fn):
        return False
    try:
        if reason is None:
            fn()
        else:
            try:
                parameters = inspect.signature(fn).parameters.values()
            except (TypeError, ValueError):
                parameters = ()
            accepts_reason = any(
                p.kind in (inspect.Parameter.POSITIONAL_ONLY,
                           inspect.Parameter.POSITIONAL_OR_KEYWORD,
                           inspect.Parameter.VAR_POSITIONAL)
                for p in parameters
            )
            # Select the legacy ABI before calling; never retry a callback's own TypeError.
            if accepts_reason:
                fn(reason)
            else:
                fn()
        return True
    except Exception as exc:
        logger.debug(msg, *args, exc)
        return False


def _stale_monitor_loop() -> None:
    """Sweep running delegations for stalled progress. A changed progress token refreshes the
    record's timestamp; a frozen token past the idle/in-tool threshold marks the record
    ``stalling`` and calls ``interrupt_fn``; a ``stalling`` record still unreturned after the
    grace window is force-finalized with a terminal ``stalled`` event."""
    while not _monitor_stop.wait(_STALE_CHECK_INTERVAL):
        # Retirement prepare may reopen without a Future callback. Preserve
        # the existing bounded retry wake for durable queued records.
        _admit_pending()
        now = time.time()
        with _records_lock:
            stalled, expired, any_monitorable = _sweep_stale_locked(now)
        for delegation_id, quiet_for, in_tool in stalled:
            logger.warning("Async delegation %s made no progress for %.0fs "
                           "(in_tool=%s) — interrupting; grace window %.0fs",
                           delegation_id, quiet_for, in_tool, _STALL_GRACE_SECONDS)
            with _records_lock:
                fn = (_records.get(delegation_id) or {}).get("interrupt_fn")
            _call_interrupt(
                fn,
                "Async delegation %s stall interrupt failed: %s",
                delegation_id,
                reason=f"stalled: no progress for {quiet_for:.0f}s",
            )
        for delegation_id in expired:
            with _records_lock:
                ctx = (_records.get(delegation_id) or {}).get("_context")
                if ctx is None:
                    ctx = contextvars.copy_context()
            ctx.copy().run(_finalize, delegation_id, lambda rec, d=delegation_id: _stalled_result(d, rec), "stalled")
        if not any_monitorable:
            # Make the exit decision atomic with _ensure_stale_monitor().  A
            # dispatch racing this check either observes a live monitor, or
            # waits for this lock until _monitor_thread is cleared and starts a
            # replacement.  This closes the lost-wakeup window where a queued
            # record could otherwise remain unadmitted forever.
            with _monitor_lock:
                with _records_lock:
                    still_monitorable = any(
                        r.get("status") == "queued"
                        or (r.get("status") == "running" and not r.get("_started"))
                        or (r.get("status") == "running" and r.get("progress_fn") is not None)
                        or r.get("status") == "stalling"
                        for r in _records.values()
                    )
                if not still_monitorable:
                    global _monitor_thread
                    _monitor_thread = None
                    return


def _stalled_error_text(event_record: Dict[str, Any]) -> str:
    """Human wording for a force-finalized stall. This string reaches the user (CLI timeline, Desktop
    async-result card), so it names the task, how long it was silent, and what to do — no issue
    numbers or worker internals (those stay in the log line and the stall_* metadata)."""
    goal = " ".join(str(event_record.get("goal") or "").split())
    label = f'Background task "{goal[:120]}"' if goal else "The background task"
    quiet = float(event_record.get("_stall_quiet_seconds") or 0)
    silence = f" after {round(quiet / 60)} min of no progress" if quiet >= 60 else ""
    return (f"{label} stopped responding{silence} and was cancelled. Nothing else was affected; "
            "ask me to run it again if you still need it.")


def _stalled_result(delegation_id: str, event_record: Dict[str, Any]) -> Dict[str, Any]:
    """Synthetic terminal result for a stalling delegation whose runner never returned."""
    completed_at = event_record.get("completed_at") or time.time()
    duration = round(completed_at - (event_record.get("dispatched_at") or completed_at), 2)
    error = _stalled_error_text(event_record)
    logger.error("Async delegation %s force-finalized as stalled after %.0fs", delegation_id, duration)
    # Structured stall metadata lets parents/UIs distinguish a stall-monitor
    # kill from other failures without parsing the error string.
    stall_in_tool = event_record.get("_stall_in_tool")
    stall_meta = {
        "stalled_after_quiet_seconds": event_record.get("_stall_quiet_seconds"),
        "stall_threshold_seconds": event_record.get("_stall_threshold_seconds"),
        "stall_phase": "in_tool" if stall_in_tool else "idle" if stall_in_tool is not None else None,
        "stall_grace_seconds": _STALL_GRACE_SECONDS}
    if event_record.get("is_batch"):
        return {**_batch_crash(error, duration), **stall_meta}
    return {**_single_crash(error, duration), "status": "stalled", "exit_reason": "stalled", **stall_meta}


# ── Observability + control ─────────────────────────────────────────────────
def _children_activity_from_token(token: Any, now: float) -> Optional[List]:
    """Parse a progress token into per-child activity dicts (best-effort): delegate_tool
    emits one ``(api_call_count, current_tool, last_activity_ts)`` tuple per child;
    foreign token shapes degrade to ``None`` entries."""
    try:
        parts = list(token)
    except TypeError:
        return None
    out: List[Optional[Dict[str, Any]]] = []
    for part in parts:
        if not (isinstance(part, (list, tuple)) and len(part) >= 2):
            out.append(None)
            continue
        entry: Dict[str, Any] = {"api_calls": part[0], "current_tool": part[1]}
        if len(part) >= 3 and isinstance(part[2], (int, float)):
            entry["seconds_since_activity"] = round(max(0.0, now - float(part[2])), 1)
        out.append(entry)
    return out


def list_async_delegations() -> List[Dict[str, Any]]:
    """Snapshot of async delegations (running + recently completed) without callables or private
    monitor bookkeeping; adds computed live fields for UIs (``seconds_since_progress``,
    ``children_activity``/``in_tool`` sampled from ``progress_fn``) and stall context once tripped.

    Safe to call from any thread. See #51690.
    """
    now = time.time()
    samplers: Dict[str, Callable] = {}
    with _records_lock:
        items = []
        for r in _records.values():
            item = {k: v for k, v in r.items() if k not in {"interrupt_fn", "progress_fn", "cancel_fn", "runner", "classify", "crash_result"} and not k.startswith("_")}
            status = r.get("status")
            if status in _ACTIVE_STATES:
                if r.get("_progress_ts"):
                    item["seconds_since_progress"] = round(now - r["_progress_ts"], 1)
                if callable(r.get("progress_fn")):
                    samplers[r["delegation_id"]] = r["progress_fn"]
            if status in ("stalling", "stalled"):
                for src, dst in _STALL_FIELD_MAP:
                    if r.get(src) is not None:
                        item[dst] = r.get(src)
            items.append(item)
    # Sample OUTSIDE the lock — progress_fn reads child-agent attributes and a
    # slow/broken sampler must not block every dispatch/finalize.
    for item in items:
        fn = samplers.get(item.get("delegation_id"))
        if fn is None:
            continue
        try:
            token, in_tool = fn()
        except Exception:
            continue
        activity = _children_activity_from_token(token, now)
        if activity is not None:
            item["children_activity"] = activity
        item["in_tool"] = bool(in_tool)
    return items


def _interrupt_records(targets: List[Dict[str, Any]], caller: str, reason: str, msg: str) -> int:
    """Cancel queued records or signal running records; returns how many changed."""
    count = 0
    for record in targets:
        queued_snapshot = None
        queued_interrupt_fn = None
        persist_wait = None
        delegation_id = record.get("delegation_id")
        if not delegation_id:
            continue
        with _records_lock:
            live = _records.get(delegation_id)
            if live is None or live.get("status") not in _INTERRUPTIBLE_STATES:
                continue
            if live.get("status") == "queued" and live.get("_persisting"):
                # Record the request while the insert owns the lifecycle
                # handoff; dispatch will not publish/admit this record.
                live["_cancel_requested"] = True
                live["_cancel_reason"] = reason
                persist_wait = live.get("_persist_done")
            elif live.get("status") in {"queued", "admitted"} and live.get("_future") is None:
                if live.get("status") == "queued" and not live.get("_persisting"):
                    # A queued record may still be visible in the FIFO; an
                    # admitted no-Future record was already removed by the
                    # admission selector and has no queue entry to remove.
                    try:
                        _PENDING_QUEUE.remove(live["delegation_id"])
                    except ValueError:
                        pass
                prior_status = live.get("status")
                _transition_memory_locked(live, "finalizing")
                live["_durable_state"] = prior_status
                live["completed_at"] = time.time()
                live["_slot_reserved"] = False
                live["_slot_release_done"] = True
                queued_interrupt_fn = live.get("interrupt_fn")
                live["interrupt_fn"] = None
                live["progress_fn"] = None
                queued_snapshot = dict(live)
                interrupt_fn = None
            else:
                interrupt_fn = live.get("interrupt_fn")
        if persist_wait is not None:
            persist_wait.wait(30)
            count += _interrupt_records([record], caller, reason, msg)
            continue
        if queued_snapshot is not None:
            count += 1
            queued_context = queued_snapshot.get("_context")
            if queued_context is None:
                queued_context = contextvars.copy_context()

            def finalize_queued() -> None:
                _call_interrupt(
                    queued_interrupt_fn, "%s: %s interrupt failed: %s", caller, delegation_id, reason=reason,
                )
                queued_snapshot["_terminal_state"] = "cancelled" if caller == "interrupt_delegation" else "interrupted"
                _finalize(
                    queued_snapshot["delegation_id"],
                    {"status": "cancelled", "summary": None, "error": reason, "exit_reason": "cancelled",
                     "results": [] if queued_snapshot.get("is_batch") else None},
                    "interrupted",
                    _claimed_snapshot=queued_snapshot,
                )

            queued_context.copy().run(finalize_queued)
            continue
        if _call_interrupt(
            interrupt_fn, "%s: %s interrupt failed: %s", caller, record.get("delegation_id"), reason=reason,
        ):
            count += 1
    if count:
        logger.info(msg, count, reason)
    return count


def interrupt_all(reason: str = "shutdown") -> int:
    """Signal every interruptible async delegation to stop (``/stop``, shutdown)."""
    with _records_lock:
        targets = [r for r in _records.values() if r.get("status") in _INTERRUPTIBLE_STATES]
    return _interrupt_records(targets, "interrupt_all", reason, "Interrupted %d async delegation(s) (%s)")


def interrupt_delegation(delegation_id: str, reason: str = "stop_command") -> bool:
    """Cancel one queued delegation or signal one running delegation by handle."""
    with _records_lock:
        target = _records.get(delegation_id)
        targets = [target] if target is not None and target.get("status") in _INTERRUPTIBLE_STATES else []
    return bool(_interrupt_records(targets, "interrupt_delegation", reason, "Interrupted %d async delegation(s) (%s)"))


def interrupt_for_session(
    session_key: str = "", origin_ui_session_id: str = "", parent_session_id: str = "", reason: str = "session_end",
) -> int:
    """Signal interruptible async delegations owned by ONE ending session."""
    targets = _session_records(_INTERRUPTIBLE_STATES, session_key, origin_ui_session_id, parent_session_id)
    return _interrupt_records(
        targets, "interrupt_for_session", reason, "Interrupted %d async delegation(s) for ending session (%s)")


def _reset_for_tests() -> None:
    """Test-only: clear all state and tear down the executor + monitor."""
    global _executor, _executor_max_workers, _monitor_thread
    with _executor_lock:
        if _executor is not None:
            _executor.shutdown(wait=False)
        _executor = None
        _executor_max_workers = 0
    _monitor_stop.set()
    with _monitor_lock:
        thread, _monitor_thread = _monitor_thread, None
    if thread is not None and thread.is_alive():
        thread.join(timeout=2)
    with _records_lock:
        _records.clear()
        _PENDING_QUEUE.clear()
        _PENDING_ADMISSION_SLOTS.clear()
    with _orphan_lock:
        _offered.clear()
        _last_orphan_sweep.clear()
        _unresolved.clear()
