#!/usr/bin/env python3
"""Async (background) delegation registry behind ``delegate_task(background=true)``.

The parent dispatches a subagent on a module-level daemon executor and returns a handle
immediately. On completion a ``type="async_delegation"`` event (self-contained task-source
block) is pushed onto the SHARED ``process_registry.completion_queue`` the CLI/gateway drain
while idle, so results surface as a NEW turn (never mid-turn) and inherit its de-dup and
crash-recovery wiring. Only the async lifecycle lives here; the child run is an injected ``runner``."""

from __future__ import annotations

import inspect
import collections
import contextvars
<<<<<<< HEAD
import hashlib
import json
||||||| dff103e05878
import json
=======
>>>>>>> 32dbc1dcda2255fd35969ec99cfc0170988c3eca
import logging
import os
import threading
import time
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from hermes_constants import get_hermes_home, hermes_home_key
from persistence.authority.coordination import (
    DELEGATION_CLAIM_STALE_SECONDS, DELEGATION_MAX_DELIVERY_ATTEMPTS, DELEGATION_ORPHAN_STALE_SECONDS,
    DELEGATION_REPLAY_MAX_AGE_SECONDS, OwnerStamp,
)
from tools.daemon_pool import DaemonThreadPoolExecutor
from tools.async_delegation_completion import (
    durable_status, reconcile_checkpoint, record_unit_child, recovered_children, replay_outbox_pending, sync_completion_payload,
)
from tools.thread_context import propagate_context_to_thread
from tools.async_delegation_worker import worker_ledger, require_ledger_owner
from tools.delegate_tool_progress import SUBAGENT_FAILURE_STATUSES
from tools.delegation_redaction import redact_child_result, redact_completion_event

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
_records: dict[str, dict[str, Any]] = {}
# A closed session's token -> the (field, value) selectors a new dispatch is refused for (``refuse_for_session``),
# and each selector -> the tokens refusing it, so a dispatch checks its own selectors, not every fence.
_refused: dict[Any, frozenset] = {}
_refused_by: dict[tuple, set] = {}
_FENCED_FIELDS = ("session_key", "parent_session_id")

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


def _transition_memory_locked(record: dict[str, Any], new: str, *, expected: Optional[str] = None) -> str:
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
<<<<<<< HEAD
# Restart-cut/retry rows are protected from the ordinary terminal-history cap, but
# a pathological stream must still have a bounded footprint.
_MAX_RETAINED_RESUMABLE = 500
_PRUNE_ID_LOG_LIMIT = 8
_DURABLE_RETENTION_SECONDS = 7 * 24 * 60 * 60
_MAX_DURABLE_PENDING = 1000
# Rows delegation_resume would still accept for an explicit resume (owner present,
# not yet claimed). Pruning skips them inside the retention window, so a restart
# can't make an interrupted child unrecoverable. Keep in step with
# delegation_resume._eligibility's state and owner checks. One ? is the cutoff.
# Deliberately a superset: batch/partial-result checks live in task/result JSON,
# so a few ineligible rows are kept too, bounded by the retention window.
# A row the retry engine still owes an outcome (pending retry, claimed dispatch,
# unreported terminal line) is exempt regardless of age; budgets bound how long
# that lasts. A legacy explicit-resume claim no longer drops protection either:
# that is how a claimed row was pruned mid-recovery (deleg_a11d4b48). Boot-notice
# claims likewise belong to their owner until completed or released, not retention.
_RETENTION_OWED_SQL = """(
    retry_state IN ('armed','scheduled','noticing','notified','dispatching','terminal','reporting')
    OR auto_resume_state='claimed'
)"""
_RESUMABLE_RETENTION_SQL = """(
    """ + _RETENTION_OWED_SQL + """
    OR (state IN ('unknown','interrupted','stalled')
        AND (COALESCE(parent_session_id, '') != '' OR COALESCE(origin_session_id, '') != '')
        AND updated_at >= ?)
)"""
||||||| dff103e05878
_DURABLE_RETENTION_SECONDS = 7 * 24 * 60 * 60
_MAX_DURABLE_PENDING = 1000
=======
>>>>>>> 32dbc1dcda2255fd35969ec99cfc0170988c3eca
# Cap retried deliveries so an unroutable row converges to terminal 'dropped'.
_MAX_DELIVERY_ATTEMPTS = DELEGATION_MAX_DELIVERY_ATTEMPTS
# Pending completions older than this are dropped on restart replay instead of
# re-run as a full-context turn; 48h keeps weekend results deliverable.
_MAX_COMPLETION_REPLAY_AGE_S = DELEGATION_REPLAY_MAX_AGE_SECONDS
# A delivery claim older than this is abandoned and may be re-claimed.
_CLAIM_LEASE_S = DELEGATION_CLAIM_STALE_SECONDS
_DB_LOCK = threading.Lock()

# ── Orphaned-completion sweep ────────────────────────────────────────────────
# Startup replay runs once per process, so a completion whose owner died while THIS process was
# already running (a desktop reload) would wait for the next restart (#97202). Delivery loops (gateway
# watcher, TUI poller) sweep each home they serve at most once per interval.
ORPHAN_SWEEP_INTERVAL_S = 30.0
# Idle time before a dead owner's pending row is re-offered; keeps the sweep off a row just touched.
_ORPHAN_STALE_S = DELEGATION_ORPHAN_STALE_SECONDS
_orphan_lock = threading.Lock()
# (home key, delegation_id) put on this process's queue by replay or sweep and not re-offered while
# that copy is alive. A consumer that discards its copy with the row still pending hands it back
# (``return_completion_offer``); the delivery claim stays the only thing that settles the row.
_offered: set = set()
# Delegation ids this process first offered from its startup replay. Their copies carry
# ``startup_replay`` on the sweep's later re-offers too, so a refused one stays gated.
_startup_replayed: set = set()
_last_orphan_sweep: dict[str, float] = {}
# (home key, delegation_id) -> when the sweep last re-offered a lost copy. A consumer that keeps
# handing the copy back would otherwise be re-offered every tick: re-offering touches no ledger
# column, so each re-offer gets its own ``_CLAIM_LEASE_S`` here, and only the first one warns.
_reoffered_at: dict[tuple[str, str], float] = {}
# delegation_id -> number of copies a consumer holds off-queue (``mark_copy_held``). The sweep counts
# them as live alongside the queued ones, so it never adds a duplicate beside a held copy.
_held_copies: collections.Counter = collections.Counter()
_held_lock = threading.Lock()  # taken after a queue's mutex, never before

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


<<<<<<< HEAD
# ── Durable ledger (state.db / async_delegations) ───────────────────────────
# In-memory marker on a terminal event whose producer could not prove which durable identity, if
# any, owns it (reconciliation failed). Its delegation_id alone proves nothing: a competing writer
# may own the lifecycle row. Never persisted; only proven deliverable ownership is stripped.
# A rejected marker stays on every retained/copyable event, even after its ledger disappears.
_TERMINAL_OWNERSHIP_KEY = "_terminal_ownership"
_OWNERSHIP_RETRY_BASE_S = 5.0
_OWNERSHIP_RETRY_MAX_S = 300.0
# (home key, delegation_id) -> unresolved event held off the queue until its retry is due.
_unresolved: dict[tuple, dict[str, Any]] = {}
||||||| dff103e05878
# ── Durable ledger (state.db / async_delegations) ───────────────────────────
def _db_path():
    return get_hermes_home() / "state.db"


def _connect() -> sqlite3.Connection:
    from agent.runtime_session_store import WorkerPersistenceError, is_worker_process
    if is_worker_process():
        raise WorkerPersistenceError('worker_delegation_ledger_unavailable')
    return open_state_db(_db_path(), "state.db (async_delegation)")


def open_state_db(path: Path, db_label: str) -> sqlite3.Connection:
    """A short-lived writer connection to a profile's ``state.db`` that never resurrects a removed named profile
    (#123265) and keeps the file and its sidecars owner-only (0600, as ``hermes_state.SessionDB`` does)."""
    from hermes_cli.sqlite_util import open_db
    from hermes_constants import mkdir_under_hermes_home
    from hermes_state import _secure_state_db_files

    mkdir_under_hermes_home(path.parent)
    _secure_state_db_files(path, create_main=True)
    identity = _db_identity(path)
    # wal=False: SessionDB owns state.db's journal mode (_initialize_schema applies the barriers).
    conn = open_db(path, db_label=db_label, busy_timeout_ms=10_000,
                   wal=False, row_factory=None,
                   initialize=lambda conn: _initialize_schema(conn, identity=identity))
    try:
        _secure_state_db_files(path)
    except BaseException:
        conn.close()
        raise
    return conn


def _db_identity(path) -> tuple[int, int] | None:
    """Stat only: opening/closing another fd would release SQLite's POSIX locks."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    return (st.st_dev, st.st_ino) if st.st_ino else None


def _initialize_schema(conn: sqlite3.Connection, *, identity: tuple[int, int] | None = None) -> None:
    from hermes_state_repair import apply_durability_barriers
    from hermes_state_schema import reconcile_state_schema
    # Preserve the journal mode SessionDB configured on state.db: forcing WAL from
    # every short-lived connection collides with live transcript/FTS writers.
    apply_durability_barriers(conn)
    # schema_version is a per-file DDL counter, not an identity: different files can share it.
    # Pair it with the inode to detect replacements, retaining the version for recycled empty
    # files and same-file migrations. In-place restores must be done offline with a process
    # restart: neither an inode nor a DDL counter identifies arbitrary overwritten bytes.
    row = conn.execute("PRAGMA database_list").fetchone()
    key = os.path.normcase(os.path.abspath(row[2])) if row and row[2] else None
    # Match a stat taken BEFORE open to one after it; never associate an old connection's
    # schema with a replacement path. Unnamed, unstatable or unverified connections don't cache.
    if key is None or identity is None or _db_identity(key) != identity:
        reconcile_state_schema(conn)
        return
    version = conn.execute("PRAGMA schema_version").fetchone()[0]
    with _SCHEMA_READY_LOCK:
        if _SCHEMA_READY.get(key) == (identity, version):
            return
    # Single durable-shape authority: the canonical SCHEMA_SQL drives both
    # table creation and column backfill (reconcile_state_schema replays the
    # canonical DDL and reuses SessionDB's declarative reconciliation). This
    # module previously carried its own CREATE TABLE + ALTER column list,
    # which drifted from SCHEMA_SQL — same-name columns with different
    # nullability/defaults depending on which authority touched the database
    # first (#94691).
    reconcile_state_schema(conn)
    if _db_identity(key) == identity:
        with _SCHEMA_READY_LOCK:  # version AFTER replay; don't mark a replacement as reconciled
            _SCHEMA_READY[key] = (identity, conn.execute("PRAGMA schema_version").fetchone()[0])


def _transaction():
    from hermes_cli.sqlite_util import transaction

    return transaction(_connect())


def _capture_routing_origin() -> dict[str, Any]:
    """Snapshot scope_id/user_id/user_name on the PARENT thread (the daemon worker
    has no contextvars) so a restart-replayed completion can rebuild a SessionSource.
    Best-effort: empty values are omitted."""
    try:
        from gateway.session_context import get_session_env
        return {k: v for k in _ROUTING_KEYS if (v := get_session_env(f"HERMES_SESSION_{k.upper()}", ""))}
    except Exception:
        return {}


def _persist_dispatch(record: dict[str, Any]) -> None:
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
        for key in ("goal", "goals", "context", "toolsets", "role", "model", "is_batch", "task_indexes", "task_transcripts", *_ROUTING_KEYS)
        if key in record}
    try:  # where the children's terminals started; lets recovery add a git-state hint
        task_payload["owner_cwd"] = os.getcwd()
    except OSError:
        pass
    with _DB_LOCK, _transaction() as conn:
        conn.execute("""INSERT OR REPLACE INTO async_delegations
               (delegation_id, origin_session, origin_ui_session_id,
                parent_session_id, state, dispatched_at, updated_at,
                delivery_state, delivery_attempts, owner_pid,
                owner_started_at, task_json, origin_session_id)
               VALUES (?, ?, ?, ?, 'running', ?, ?, 'pending', 0, ?, ?, ?, ?)""",
            (record["delegation_id"], record.get("session_key", ""), record.get("origin_ui_session_id", ""),
             record.get("parent_session_id"), record["dispatched_at"], now, os.getpid(), owner_started_at,
             json.dumps(task_payload), record.get("origin_session_id", "")))
    _prune_durable_records()


def _prune_durable_records() -> None:
    """Bound terminal history, preferring delivered records for deletion."""
    require_ledger_owner()
    from tools.delegate_tool_auto_resume import KEEP_WAITING_UNITS_SQL as keep
    cutoff = time.time() - _DURABLE_RETENTION_SECONDS
    with _DB_LOCK, _transaction() as conn:
        conn.execute(
            "DELETE FROM async_delegations WHERE delivery_state='delivered' AND updated_at < ?" + keep, (cutoff,))
        terminal_count = conn.execute(
            "SELECT COUNT(*) FROM async_delegations WHERE state!='running'" + keep).fetchone()[0]
        if terminal_count > _MAX_RETAINED_COMPLETED:
            conn.execute(f"""DELETE FROM async_delegations WHERE delegation_id IN (
                     SELECT delegation_id FROM async_delegations
                     WHERE state!='running'{keep}
                     ORDER BY CASE delivery_state WHEN 'delivered' THEN 0 ELSE 1 END,
                              updated_at ASC LIMIT ?
                   )""", (terminal_count - _MAX_RETAINED_COMPLETED,))
        pending_count = conn.execute("""SELECT COUNT(*) FROM async_delegations
               WHERE state!='running' AND delivery_state='pending'""").fetchone()[0]
        if pending_count > _MAX_DURABLE_PENDING:
            conn.execute("""DELETE FROM async_delegations WHERE delegation_id IN (
                     SELECT delegation_id FROM async_delegations
                     WHERE state!='running' AND delivery_state='pending'
                     ORDER BY updated_at ASC LIMIT ?
                   )""", (pending_count - _MAX_DURABLE_PENDING,))


def _persist_completion(event: dict[str, Any], result: dict[str, Any], *, delivery: str = "pending") -> None:
    """*delivery* ``delivered``: a unit whose every child waits on a promised automatic resume is recorded but never
    offered (restart recovery must not resurrect it)."""
    event, result = redact_completion_event(event), redact_child_result(result)
    if remote := worker_ledger():
        return remote.complete(event, result, delivery)
    now = time.time()
    with _DB_LOCK, _transaction() as conn:
        conn.execute("""UPDATE async_delegations SET state=?, completed_at=?, updated_at=?,
               event_json=?, result_json=?, delivery_state=?
               WHERE delegation_id=?""",
            (event.get("status", "completed"), event.get("completed_at", now), now,
             json.dumps(event), json.dumps(result), delivery, event["delegation_id"]))


def record_unit_child(delegation_id: str, entry: dict[str, Any]) -> None:
    """Durably record ONE finished child of a still-running multi-child unit on the unit's own row, so a crash before
    the unit joins loses only the children that had not finished. Stored in ``result_json`` (overwritten by the real
    result at finalize); ``recover_abandoned_delegations`` replays it. Best-effort: a failed write costs recovery
    fidelity, never the live result."""
    entry = redact_child_result(entry)
    if isinstance(entry.get("task_index"), int):  # a stall or runner crash keeps it (``_keep_finished_children``)
        with _records_lock:
            if (record := _records.get(delegation_id)) is not None and record.get("status") in _ACTIVE_STATES:
                record.setdefault("_finished_children", {})[entry["task_index"]] = entry
    if remote := worker_ledger():
        return remote.child(delegation_id, entry)
    try:
        with _DB_LOCK, _transaction() as conn:
            row = conn.execute("SELECT result_json FROM async_delegations WHERE delegation_id=? AND state='running'",
                               (delegation_id,)).fetchone()
            if row is None:
                return
            partial = redact_child_result(json.loads(row[0] or "{}") or {})
            results = [r for r in partial.get("results") or [] if r.get("task_index") != entry.get("task_index")]
            results.append(entry)
            conn.execute("UPDATE async_delegations SET result_json=?, updated_at=? WHERE delegation_id=? AND state='running'",
                         (json.dumps({"results": results, "partial": True}), time.time(), delegation_id))
    except Exception:
        logger.warning("Async delegation %s: could not record finished child %s", delegation_id, entry.get("task_index"), exc_info=True)


def _recovered_results(task: dict[str, Any], result_json: Optional[str], error: str) -> Optional[list[dict[str, Any]]]:
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
    require_ledger_owner()
    alive = _owner_liveness()
    if alive is None:
        return 0
    now, recovered = time.time(), 0
    from tools.delegate_tool_resumable import continuable_of, plan_recovery, recover_abandoned
    plan = plan_recovery(_db_path(), alive)  # its git probes run before the write lock below
    recovered_records: dict[str, dict[str, Any]] = {}
    with _DB_LOCK, _transaction() as conn:
        recover_abandoned(conn, plan, recovered_records)
        rows = conn.execute("""SELECT delegation_id, origin_session, origin_ui_session_id,
                      parent_session_id, dispatched_at, owner_pid,
                      owner_started_at, task_json, origin_session_id, result_json, state
               FROM async_delegations WHERE state='running'""").fetchall()
        for row in rows:
            delegation_id, session_key, origin_ui, parent_id, dispatched_at, pid, started, task_json, origin_sid, result_json, last_state = row
            if alive(pid, started):
                continue
            task = json.loads(task_json or "{}")
            error = "Delegation owner exited before recording a terminal result; outcome unknown."
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
            silent = False
            if continuable := continuable_of(conn, delegation_id):
                diagnostics["resumable_subagent_ids"] = continuable
                from tools.delegate_tool_auto_resume import boot_notice
                notice, promised = boot_notice(conn, continuable, recovered_records)
                error += notice or (
                    " Its subagents can be continued on their own conversation and worktree: "
                    "delegate_task(action='continue', subagent_id=..., message=...); check git state and the "
                    "last tool before re-running side effects.")
                n_tasks = len(task.get("task_indexes") or task.get("goals") or [0])
                finished = any(r.get("status") != "unknown" for r in recovered_results or ())
                silent = len(promised) >= n_tasks and not finished  # the scheduler reports each child: no wake
                diagnostics.update({"waiting_to_resume": promised} if silent else {})
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
            event, result = redact_completion_event(event), redact_child_result(result)
            conn.execute("""UPDATE async_delegations SET state='unknown', completed_at=?,
                   updated_at=?, event_json=?, result_json=?, delivery_state=?
                   WHERE delegation_id=?""", (now, now, json.dumps(event), json.dumps(result),
                                              "delivered" if silent else "pending", delegation_id))
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
    from agent.runtime_session_store import is_worker_process
    if is_worker_process():
        # The ordinary owner restores delivery-only results; a compute import
        # must neither reap that owner's ledger nor steal its delivery queue.
        return 0
    if not _db_path().exists():
        return 0  # nothing to replay; a replay must not create (or migrate) the ledger (#123265)
    recover_abandoned_delegations()
    now = time.time()
    with _DB_LOCK, _transaction() as conn:
        rows = conn.execute("""SELECT delegation_id, event_json, completed_at, dispatched_at
               FROM async_delegations
               WHERE state != 'running' AND delivery_state='pending' AND event_json IS NOT NULL
               ORDER BY completed_at, delegation_id""").fetchall()
        return _replay_pending(conn, rows, target_queue, now, startup=True)


def _replay_pending(conn, rows, target_queue, now: float, *, startup: bool = False) -> int:
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
        with _orphan_lock:
            if (home, delegation_id) in _offered:
                # The owner's explicit startup replay and the first-consume replay
                # (process_registry.restore_completions) may both run: offer once.
                continue
        evt = json.loads(payload)
        if isinstance(evt, dict):
            evt = redact_completion_event(evt)
        with _orphan_lock:
            if startup:
                _startup_replayed.add(delegation_id)
            if isinstance(evt, dict):
                evt["restored"] = True
                if delegation_id in _startup_replayed:
                    evt["startup_replay"] = True
        target_queue.put(evt)
        with _orphan_lock:
            _offered.add((home, delegation_id))
        restored += 1
    return restored


def completion_copy_id(evt: Any) -> str:
    """The delegation id ``evt`` is the final-result copy of, or "" (other events, interim notices)."""
    if not isinstance(evt, dict) or evt.get("type") != "async_delegation" or is_interim_delegation_event(evt):
        return ""
    return str(evt.get("delegation_id") or "")


def mark_copy_held(delegation_id: str) -> None:
    """Record that a consumer holds a copy of ``delegation_id`` off every queue (a poller between dequeue and
    requeue, a parked copy). The orphan sweep treats a held copy like a queued one. A holder that moves a copy
    onto a queue marks before it dequeues, or under the queue's mutex, and unmarks only after any requeue, so
    the sweep never sees the copy as neither queued nor held. Pair every call with ``unmark_copy_held``."""
    if delegation_id:
        with _held_lock:
            _held_copies[delegation_id] += 1


def unmark_copy_held(delegation_id: str) -> None:
    """End one ``mark_copy_held`` hold on ``delegation_id``."""
    if delegation_id:
        with _held_lock:
            _held_copies[delegation_id] -= 1
            if _held_copies[delegation_id] <= 0:
                del _held_copies[delegation_id]


def _queued_completion_ids(*queues) -> Optional[set]:
    """Delegation ids with a final-result copy sitting in any of ``queues`` or held off-queue by a consumer
    (``mark_copy_held``), or None when a queue cannot be inspected. Each queue is read under its own mutex
    (the lock ``put``/``get`` and ``take_ready`` take) with the held ids read inside it, so a copy moving
    between a queue and a hold is always seen in one of them. Interim per-task notices share their batch's
    id but are not the result, so they never count as its copy."""
    ids: set = set()
    for q in {id(q): q for q in queues if q is not None}.values():
        mutex, items = getattr(q, "mutex", None), getattr(q, "queue", None)
        if mutex is None or items is None:
            return None
        with mutex:
            snapshot = list(items)
            with _held_lock:
                ids.update(_held_copies)
        ids.update(completion_copy_id(evt) for evt in snapshot)
    ids.discard("")
    return ids


def _copy_may_be_live(delegation_id: str, pid, updated_at: float, live_ids: set, home: str,
                      offered: set, now: float) -> bool:
    """For a row whose copy this process made (it owns the row or offered it): whether that copy may still be
    delivered, so the sweep must not add another. Live = queued or held (``live_ids``), or the row or this
    process's last re-offer was touched within a claim lease, the span after which a silent claim counts as
    abandoned. A row owned by ANOTHER live process is left to it: its queue is out of sight."""
    if not (delegation_id in offered or int(pid) == os.getpid()):
        return True
    if delegation_id in live_ids or updated_at >= now - _CLAIM_LEASE_S:
        return True
    with _orphan_lock:
        return _reoffered_at.get((home, delegation_id), float("-inf")) >= now - _CLAIM_LEASE_S


def _note_reoffers(home: str, lost: list, now: float) -> None:
    """Start each lost copy's re-offer lease and log it: WARNING the first time a row is re-offered, DEBUG
    after that (a consumer that keeps handing the copy back would otherwise warn once per lease)."""
    with _orphan_lock:
        first = [d for d in lost if (home, d) not in _reoffered_at]
        for key in [k for k, at in _reoffered_at.items() if now - at > _MAX_COMPLETION_REPLAY_AGE_S]:
            del _reoffered_at[key]  # its row is past the replay cap, so it was dropped
        _reoffered_at.update({(home, d): now for d in lost})
        _offered.difference_update({(home, d) for d in lost})
    log = logger.warning if first else logger.debug
    log("Re-offering %d pending async completion(s) whose in-memory copy was lost while this process ran: %s",
        len(lost), ", ".join(lost))


def sweep_orphaned_completions(target_queue, *, now: Optional[float] = None) -> int:
    """Re-offer this home's pending completions that no live in-memory copy will deliver (#97202, #140718).

    Startup replay (``restore_undelivered_completions``) covers owners that died before the process
    started; this covers the rest while it runs. Abandoned in-flight rows are first classified by
    ``recover_abandoned_delegations``. A terminal row qualifies when it is pending with an event, idle
    past ``_ORPHAN_STALE_S`` and not under a live delivery claim, and then:

    - its owner is dead and this process has not offered it yet; or
    - its owner is THIS process, or this process already offered it, the row has been idle for a whole
      ``_CLAIM_LEASE_S`` and no copy of it sits in ``target_queue`` or the producers'
      ``completion_queue``: the one in-memory copy was lost (a consumer discarded it, or it went onto
      a queue no delivery loop drains), and without this the row would stay pending with zero
      attempts until a restart (#144563).

    The sweep never adds a copy beside a live one: queued, held off-queue by a consumer
    (``mark_copy_held``), under a claim lease (filtered above), or touched within a lease (a claim, defer
    or release refreshes ``updated_at``). Each re-offer starts its own lease, so a consumer that keeps
    handing the copy back sees it at most once per lease, and only the first re-offer warns. A row owned
    by ANOTHER live process is left to that process, whose queue this one cannot see. The consumer's
    ``claim_completion_delivery`` stays the atomic cross-process gate, so a duplicate copy can never
    deliver twice. Rows past the delivery budget or the replay age converge to ``dropped``. Reads the
    current profile's ledger: callers bind the owning profile first."""
    alive = _owner_liveness()
    if alive is None or not _db_path().exists():
        return 0  # never create a ledger just to sweep it
    recover_abandoned_delegations()
    now = time.time() if now is None else now
    home = hermes_home_key(get_hermes_home())
    from tools.process_registry import process_registry
    live = _queued_completion_ids(target_queue, process_registry.completion_queue)
    with _orphan_lock:
        offered = {delegation_id for key, delegation_id in _offered if key == home}
    with _DB_LOCK, _transaction() as conn:
        rows = conn.execute("""SELECT delegation_id, event_json, completed_at, dispatched_at,
                      owner_pid, owner_started_at, delivery_attempts, updated_at
               FROM async_delegations
               WHERE state!='running' AND delivery_state='pending'
                 AND event_json IS NOT NULL AND updated_at < ?
                 AND (delivery_claim IS NULL OR delivery_claimed_at < ?)
               ORDER BY completed_at, delegation_id""", (now - _ORPHAN_STALE_S, now - _CLAIM_LEASE_S)).fetchall()
        orphans, lost = [], []
        for delegation_id, payload, completed_at, dispatched_at, pid, started, attempts, updated_at in rows:
            copy_lost = alive(pid, started) or delegation_id in offered
            if copy_lost and (live is None or _copy_may_be_live(delegation_id, pid, updated_at, live, home,
                                                                  offered, now)):
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
            if copy_lost:
                lost.append(delegation_id)
        if lost:
            _note_reoffers(home, lost, now)
        return _replay_pending(conn, orphans, target_queue, now)


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


def claim_completion_delivery(delegation_id: str, claim_id: str) -> bool:
    """Claim one pending completion across competing consumers/processes."""
    if remote := worker_ledger():
        return remote.claim(delegation_id, claim_id)
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


def is_interim_delegation_event(evt: dict[str, Any]) -> bool:
    """An early per-task notice for a batch that is still running. It shares the batch's
    ``delegation_id`` but is NOT the durable completion: it must never claim, acknowledge or
    dedup against the final result's row (independent review reproduced exactly that loss)."""
    return evt.get("type") == "async_delegation" and bool(evt.get("task_failure_notice"))


def claim_event_delivery(evt: dict[str, Any], consumer: str) -> Optional[str]:
    """Claim a durable delegation event; non-durable events (and interim notices) need no token."""
    if is_interim_delegation_event(evt):
        return ""
    delegation_id = str(evt.get("delegation_id") or "") if evt.get("type") == "async_delegation" else ""
    if not delegation_id:
        return ""
    claim_id = f"{consumer}:{os.getpid()}:{uuid.uuid4().hex}"
    return claim_id if claim_completion_delivery(delegation_id, claim_id) else None


def release_completion_delivery(delegation_id: str, claim_id: str) -> bool:
    """Release a failed delivery claim so another consumer may retry. Attempts are
    counted at claim time; once the budget is exhausted the row converges to
    terminal ``dropped`` (only pending rows replay on restart)."""
    if remote := worker_ledger():
        return remote.release(delegation_id, claim_id)
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
    if remote := worker_ledger():
        return remote.defer(delegation_id, claim_id)
    return _update_delivery("""UPDATE async_delegations SET delivery_claim=NULL,
                  delivery_claimed_at=NULL, delivery_attempts=MAX(0, delivery_attempts-1),
                  updated_at=?
           WHERE delegation_id=? AND delivery_state='pending' AND delivery_claim=?""",
        (time.time(), delegation_id, claim_id))


def drop_completion_delivery(delegation_id: str, claim_id: str) -> bool:
    """Terminally drop a claimed completion whose target is permanently gone (the
    spawning session ended at an explicit user boundary such as /new or reset).
    ``dropped`` — not ``delivered`` — keeps the ack honest; not ``pending`` keeps
    restart recovery from replaying it into a fail-closed drop forever."""
    if remote := worker_ledger():
        return remote.drop(delegation_id, claim_id)
    return _update_delivery("""UPDATE async_delegations SET delivery_state='dropped',
                  updated_at=?, delivery_claim=NULL,
                  delivery_claimed_at=NULL
           WHERE delegation_id=? AND delivery_state='pending'
             AND delivery_claim=?""", (time.time(), delegation_id, claim_id))


def complete_completion_delivery(delegation_id: str, claim_id: str) -> bool:
    """Acknowledge acceptance for the consumer holding this claim."""
    if remote := worker_ledger():
        return remote.ack(delegation_id, claim_id)
    now = time.time()
    return _update_delivery("""UPDATE async_delegations SET delivery_state='delivered',
                  delivered_at=?, updated_at=?, delivery_claim=NULL,
                  delivery_claimed_at=NULL
           WHERE delegation_id=? AND delivery_state='pending'
             AND delivery_claim=?""", (now, now, delegation_id, claim_id))


def requeue_admitted_completion(delegation_id: str, target_queue) -> bool:
    """Offer an acknowledged completion again when its receiving turn could not confirm the owner.

    The row returns to pending and its event goes back on ``target_queue``, so the next delivery
    re-runs the pre-flight. A row past the delivery budget converges to ``dropped`` instead, which
    bounds the loop; a row without a stored event is left as it is."""
    now = time.time()
    with _DB_LOCK, _transaction() as conn:
        exhausted = conn.execute("""UPDATE async_delegations SET delivery_state='dropped', updated_at=?
               WHERE delegation_id=? AND delivery_state='delivered' AND delivery_attempts>=?""",
            (now, delegation_id, _MAX_DELIVERY_ATTEMPTS))
        if exhausted.rowcount == 1:
            logger.warning("Async delegation %s exhausted its %d delivery attempts; "
                           "marking terminally dropped (result remains queryable).",
                           delegation_id, _MAX_DELIVERY_ATTEMPTS)
            return False
        reopened = conn.execute("""UPDATE async_delegations SET delivery_state='pending',
                      delivered_at=NULL, updated_at=?
               WHERE delegation_id=? AND delivery_state='delivered' AND event_json IS NOT NULL""",
            (now, delegation_id))
        if reopened.rowcount != 1:
            return False
        rows = conn.execute("""SELECT delegation_id, event_json, completed_at, dispatched_at
               FROM async_delegations WHERE delegation_id=?""", (delegation_id,)).fetchall()
        with _orphan_lock:  # an admitted copy already had its consumer: requeue keeps live routing
            _startup_replayed.discard(delegation_id)
        return _replay_pending(conn, rows, target_queue, now) == 1


def complete_event_delivery(evt: dict[str, Any], claim_id: str) -> None:
    _event_delivery(complete_completion_delivery, evt, claim_id)


def release_event_delivery(evt: dict[str, Any], claim_id: str) -> None:
    """Release a failed claim for a consumer that discards its copy (the TUI poller): the row is pending
    again, so it must stay eligible for the orphan sweep."""
    _event_delivery(release_completion_delivery, evt, claim_id)
    return_completion_offer(evt)


def return_completion_offer(evt: dict[str, Any]) -> None:
    """Hand an offered completion back to the orphan sweep after its in-memory copy was discarded while
    the durable row stays pending, e.g. a TUI session that cannot prove it owns the event drops it (every
    session poller drains one process-wide queue). The next sweep may offer the row again. Delegation ids
    are unique across profiles, so this clears the offer in every home."""
    delegation_id = str(evt.get("delegation_id") or "") if evt.get("type") == "async_delegation" else ""
    if not delegation_id or is_interim_delegation_event(evt):
        return
    with _orphan_lock:
        _offered.difference_update({key for key in _offered if key[1] == delegation_id})


def _event_delivery(fn, evt: dict[str, Any], claim_id: str) -> None:
    if claim_id and evt.get("type") == "async_delegation":
        fn(str(evt.get("delegation_id") or ""), claim_id)


def get_durable_delegation(delegation_id: str) -> Optional[dict[str, Any]]:
    if remote := worker_ledger():
        saved = remote.read(delegation_id)
        if saved is not None and saved.get("result") is not None:
            return {**saved, "result": redact_child_result(saved["result"])}
        return saved
    with _DB_LOCK, _transaction() as conn:
        row = conn.execute("""SELECT origin_session, state, dispatched_at, completed_at,
                      result_json, delivery_state, delivery_attempts,
                      origin_session_id
               FROM async_delegations WHERE delegation_id=?""", (delegation_id,)).fetchone()
    return None if row is None else {
        "delegation_id": delegation_id, "origin_session": row[0], "state": row[1], "dispatched_at": row[2],
        "completed_at": row[3], "result": redact_child_result(json.loads(row[4])) if row[4] else None, "delivery_state": row[5],
        "delivery_attempts": row[6], "origin_session_id": row[7] or ""}
=======
# ── Durable ledger (the profile store's async_delegations) ──────────────────
def _scope():
    from persistence_assembly import profile_scope
    return profile_scope()


def _ledger_exists():
    """Whether the ledger's store exists yet; a read or a sweep never creates it."""
    from persistence_assembly import store_exists
    return store_exists(_scope())


def _ledger():
    """A handle on the ledger's store through the profile's registered writer; the caller closes it."""
    from agent.runtime_session_store import WorkerPersistenceError, is_worker_process
    if is_worker_process():
        raise WorkerPersistenceError('worker_delegation_ledger_unavailable')
    from persistence_assembly import open_persistence
    return open_persistence(_scope())


def _ledger_reader():
    """A read-only handle on the ledger's store, which opens no store writer; the caller closes it."""
    from persistence_assembly import open_persistence_reader
    return open_persistence_reader(_scope().profile_dir)


def _capture_routing_origin() -> dict[str, Any]:
    """Snapshot scope_id/user_id/user_name on the PARENT thread (the daemon worker
    has no contextvars) so a restart-replayed completion can rebuild a SessionSource.
    Best-effort: empty values are omitted."""
    try:
        from gateway.session_context import get_session_env
        return {k: v for k in _ROUTING_KEYS if (v := get_session_env(f"HERMES_SESSION_{k.upper()}", ""))}
    except Exception:
        return {}


def _persist_dispatch(record: dict[str, Any]) -> None:
    if remote := worker_ledger():
        return remote.dispatch(record)
    try:
        from gateway.status import get_process_start_time
        owner_started_at = get_process_start_time(os.getpid())
    except Exception:
        owner_started_at = None
    task_payload = {
        key: record.get(key)
        for key in ("goal", "goals", "context", "toolsets", "role", "model", "is_batch", "task_indexes", "task_transcripts", *_ROUTING_KEYS)
        if key in record}
    try:  # where the children's terminals started; lets recovery add a git-state hint
        task_payload["owner_cwd"] = os.getcwd()
    except OSError:
        pass
    with _DB_LOCK, _ledger() as store:
        store.coordination.record_delegation_dispatch(
            delegation_id=record["delegation_id"], origin_session=record.get("session_key", ""),
            origin_ui_session_id=record.get("origin_ui_session_id", ""),
            parent_session_id=record.get("parent_session_id"), dispatched_at=record["dispatched_at"],
            owner=OwnerStamp(pid=os.getpid(), started_at=owner_started_at), task=task_payload,
            origin_session_id=record.get("origin_session_id", ""))
    _prune_durable_records()


def _prune_durable_records() -> None:
    """Bound terminal history, preferring delivered records for deletion."""
    require_ledger_owner()
    with _DB_LOCK, _ledger() as store:
        store.coordination.prune_delegation_ledger()


def _persist_completion(event: dict[str, Any], result: dict[str, Any], *, delivery: str = "pending") -> None:
    """*delivery* ``delivered``: a unit whose every child waits on a promised automatic resume is recorded but never
    offered (restart recovery must not resurrect it)."""
    event, result = redact_completion_event(event), redact_child_result(result)
    if remote := worker_ledger():
        return remote.complete(event, result, delivery)
    with _DB_LOCK, _ledger() as store:
        store.coordination.record_delegation_completion(
            delegation_id=event["delegation_id"], state=event.get("status", "completed"),
            completed_at=event.get("completed_at", time.time()), event=event, result=result, delivery=delivery)


def record_unit_child(delegation_id: str, entry: dict[str, Any]) -> None:
    """Durably record ONE finished child of a still-running multi-child unit on the unit's own row, so a crash before
    the unit joins loses only the children that had not finished. Stored in ``result_json`` (overwritten by the real
    result at finalize); ``recover_abandoned_delegations`` replays it. Best-effort: a failed write costs recovery
    fidelity, never the live result."""
    entry = redact_child_result(entry)
    if isinstance(entry.get("task_index"), int):  # a stall or runner crash keeps it (``_keep_finished_children``)
        with _records_lock:
            if (record := _records.get(delegation_id)) is not None and record.get("status") in _ACTIVE_STATES:
                record.setdefault("_finished_children", {})[entry["task_index"]] = entry
    if remote := worker_ledger():
        return remote.child(delegation_id, entry)
    try:
        with _DB_LOCK, _ledger() as store:
            store.coordination.record_unit_child(delegation_id=delegation_id, entry=entry)
    except Exception:
        logger.warning("Async delegation %s: could not record finished child %s", delegation_id, entry.get("task_index"), exc_info=True)


def _recovered_results(task: dict[str, Any], stored: Any, error: str) -> Optional[list[dict[str, Any]]]:
    """Per-task results for an abandoned unit: recorded children as they finished, the rest ``unknown``."""
    partial = stored if isinstance(stored, dict) else {}
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
    require_ledger_owner()
    alive = _owner_liveness()
    if alive is None:
        return 0
    if not _ledger_exists():
        return 0  # nothing to recover; a recovery must not create the ledger
    from tools.delegate_tool_resumable import _limits, recovered_records, recovery_plan, subagent_recoveries
    # Everything is read and built before the one recovery write (the plan's git probes first, holding no lock); a
    # ledger with nothing abandoned is never opened for writing. The write lands each record and row only while it
    # is still the one read here.
    with _ledger_reader() as reader:
        recovered = recovered_records(recovery_plan(reader.coordination.list_inflight_subagents(), alive))
        recoveries, promised = _recoveries(reader.coordination, alive, recovered)
    if not (recoveries or recovered):
        return 0
    with _DB_LOCK, _ledger() as store:
        return store.coordination.recover_abandoned_delegations(
            recoveries=recoveries, subagent_recoveries=subagent_recoveries(recovered, promised), limits=_limits())


def _recoveries(ledger, alive: Callable[[Any, Any], bool], recovered: dict[str, Any]) -> tuple[list[Any], set[str]]:
    """``(recoveries, promised ids)``: the ``unknown`` outcome of every ``running`` unit whose owner
    is gone, and the subagents among *recovered* the scheduler will report on (``boot_notice``)."""
    from persistence import DelegationId
    from persistence.authority.coordination import DelegationRecovery
    from tools.delegate_tool_resumable import continuable_of
    now = time.time()
    records = {sid: record for sid, (record, _stopped) in recovered.items()}
    recoveries, promised_ids = [], set()
    for row in ledger.list_inflight_delegations():
        if alive(row.owner_pid, row.owner_started_at):
            continue
        delegation_id, last_state = row.delegation_id, row.state
        task = row.task if isinstance(row.task, dict) else {}
        error = "Delegation owner exited before recording a terminal result; outcome unknown."
        recovered_results = _recovered_results(task, row.result, error)
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
        silent = False
        if continuable := continuable_of(ledger, delegation_id, recovered):
            diagnostics["resumable_subagent_ids"] = continuable
            from tools.delegate_tool_auto_resume import boot_notice
            notice, promised = boot_notice(continuable, records)
            promised_ids.update(promised)
            error += notice or (
                " Its subagents can be continued on their own conversation and worktree: "
                "delegate_task(action='continue', subagent_id=..., message=...); check git state and the "
                "last tool before re-running side effects.")
            n_tasks = len(task.get("task_indexes") or task.get("goals") or [0])
            finished = any(r.get("status") != "unknown" for r in recovered_results or ())
            silent = len(promised) >= n_tasks and not finished  # the scheduler reports each child: no wake
            diagnostics.update({"waiting_to_resume": promised} if silent else {})
        event = {
            "type": "async_delegation", "delegation_id": delegation_id, "session_key": row.origin_session,
            "origin_ui_session_id": row.origin_ui_session_id, "origin_session_id": row.origin_session_id or "",
            "parent_session_id": row.parent_session_id, "goal": task.get("goal", ""), "goals": task.get("goals"),
            "context": task.get("context"), "toolsets": task.get("toolsets"), "role": task.get("role"),
            "model": task.get("model"), "is_batch": bool(task.get("is_batch")),
            "status": "unknown", "summary": None, "error": error, **diagnostics,
            **({"results": recovered_results} if recovered_results else {}),
            "dispatched_at": row.dispatched_at, "completed_at": now,
            **{k: task[k] for k in _ROUTING_KEYS if task.get(k)}}
        result = {"status": "unknown", "summary": None, "error": event["error"], **diagnostics,
                  **({"results": recovered_results} if recovered_results else {})}
        event, result = redact_completion_event(event), redact_child_result(result)
        recoveries.append(DelegationRecovery(
            delegation_id=DelegationId(delegation_id), expected_state=last_state, expected_updated_at=row.updated_at,
            completed_at=now, event=event, result=result, delivery="delivered" if silent else "pending"))
    return recoveries, promised_ids


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
    from agent.runtime_session_store import is_worker_process
    if is_worker_process():
        # The ordinary owner restores delivery-only results; a compute import
        # must neither reap that owner's ledger nor steal its delivery queue.
        return 0
    if not _ledger_exists():
        return 0  # nothing to replay; a replay must not create (or migrate) the ledger (#123265)
    recover_abandoned_delegations()
    with _ledger_reader() as reader:
        if not reader.coordination.has_replayable_completions():
            return 0
    with _DB_LOCK, _ledger() as store:
        return _offer_pending(store.coordination.take_replayable_completions(), target_queue, startup=True)


def _offer_pending(completions, target_queue, *, startup: bool = False) -> int:
    """Put each pending completion's event on ``target_queue`` stamped ``restored`` (the store already dropped
    rows past ``_MAX_COMPLETION_REPLAY_AGE_S``). Records the offer so the orphan sweep skips the row until the
    copy is handed back (``return_completion_offer``)."""
    home, restored = hermes_home_key(get_hermes_home()), 0
    for completion in completions:
        delegation_id = completion.delegation_id
        with _orphan_lock:
            if (home, delegation_id) in _offered:
                # The owner's explicit startup replay and the first-consume replay
                # (process_registry.restore_completions) may both run: offer once.
                continue
        evt = completion.event
        if isinstance(evt, dict):
            evt = redact_completion_event(evt)
        with _orphan_lock:
            if startup:
                _startup_replayed.add(delegation_id)
            if isinstance(evt, dict):
                evt["restored"] = True
                if delegation_id in _startup_replayed:
                    evt["startup_replay"] = True
        target_queue.put(evt)
        with _orphan_lock:
            _offered.add((home, delegation_id))
        restored += 1
    return restored


def completion_copy_id(evt: Any) -> str:
    """The delegation id ``evt`` is the final-result copy of, or "" (other events, interim notices)."""
    if not isinstance(evt, dict) or evt.get("type") != "async_delegation" or is_interim_delegation_event(evt):
        return ""
    return str(evt.get("delegation_id") or "")


def mark_copy_held(delegation_id: str) -> None:
    """Record that a consumer holds a copy of ``delegation_id`` off every queue (a poller between dequeue and
    requeue, a parked copy). The orphan sweep treats a held copy like a queued one. A holder that moves a copy
    onto a queue marks before it dequeues, or under the queue's mutex, and unmarks only after any requeue, so
    the sweep never sees the copy as neither queued nor held. Pair every call with ``unmark_copy_held``."""
    if delegation_id:
        with _held_lock:
            _held_copies[delegation_id] += 1


def unmark_copy_held(delegation_id: str) -> None:
    """End one ``mark_copy_held`` hold on ``delegation_id``."""
    if delegation_id:
        with _held_lock:
            _held_copies[delegation_id] -= 1
            if _held_copies[delegation_id] <= 0:
                del _held_copies[delegation_id]


def _queued_completion_ids(*queues) -> Optional[set]:
    """Delegation ids with a final-result copy sitting in any of ``queues`` or held off-queue by a consumer
    (``mark_copy_held``), or None when a queue cannot be inspected. Each queue is read under its own mutex
    (the lock ``put``/``get`` and ``take_ready`` take) with the held ids read inside it, so a copy moving
    between a queue and a hold is always seen in one of them. Interim per-task notices share their batch's
    id but are not the result, so they never count as its copy."""
    ids: set = set()
    for q in {id(q): q for q in queues if q is not None}.values():
        mutex, items = getattr(q, "mutex", None), getattr(q, "queue", None)
        if mutex is None or items is None:
            return None
        with mutex:
            snapshot = list(items)
            with _held_lock:
                ids.update(_held_copies)
        ids.update(completion_copy_id(evt) for evt in snapshot)
    ids.discard("")
    return ids


def _copy_may_be_live(delegation_id: str, pid, updated_at: float, live_ids: set, home: str,
                      offered: set, now: float) -> bool:
    """For a row whose copy this process made (it owns the row or offered it): whether that copy may still be
    delivered, so the sweep must not add another. Live = queued or held (``live_ids``), or the row or this
    process's last re-offer was touched within a claim lease, the span after which a silent claim counts as
    abandoned. A row owned by ANOTHER live process is left to it: its queue is out of sight."""
    if not (delegation_id in offered or int(pid) == os.getpid()):
        return True
    if delegation_id in live_ids or updated_at >= now - _CLAIM_LEASE_S:
        return True
    with _orphan_lock:
        return _reoffered_at.get((home, delegation_id), float("-inf")) >= now - _CLAIM_LEASE_S


def _note_reoffers(home: str, lost: list, now: float) -> None:
    """Start each lost copy's re-offer lease and log it: WARNING the first time a row is re-offered, DEBUG
    after that (a consumer that keeps handing the copy back would otherwise warn once per lease)."""
    with _orphan_lock:
        first = [d for d in lost if (home, d) not in _reoffered_at]
        for key in [k for k, at in _reoffered_at.items() if now - at > _MAX_COMPLETION_REPLAY_AGE_S]:
            del _reoffered_at[key]  # its row is past the replay cap, so it was dropped
        _reoffered_at.update({(home, d): now for d in lost})
        _offered.difference_update({(home, d) for d in lost})
    log = logger.warning if first else logger.debug
    log("Re-offering %d pending async completion(s) whose in-memory copy was lost while this process ran: %s",
        len(lost), ", ".join(lost))


def sweep_orphaned_completions(target_queue, *, now: Optional[float] = None) -> int:
    """Re-offer this home's pending completions that no live in-memory copy will deliver (#97202, #140718).

    Startup replay (``restore_undelivered_completions``) covers owners that died before the process
    started; this covers the rest while it runs. Abandoned in-flight rows are first classified by
    ``recover_abandoned_delegations``. A terminal row qualifies when it is pending with an event, idle
    past ``_ORPHAN_STALE_S`` and not under a live delivery claim, and then:

    - its owner is dead and this process has not offered it yet; or
    - its owner is THIS process, or this process already offered it, the row has been idle for a whole
      ``_CLAIM_LEASE_S`` and no copy of it sits in ``target_queue`` or the producers'
      ``completion_queue``: the one in-memory copy was lost (a consumer discarded it, or it went onto
      a queue no delivery loop drains), and without this the row would stay pending with zero
      attempts until a restart (#144563).

    The sweep never adds a copy beside a live one: queued, held off-queue by a consumer
    (``mark_copy_held``), under a claim lease (filtered above), or touched within a lease (a claim, defer
    or release refreshes ``updated_at``). Each re-offer starts its own lease, so a consumer that keeps
    handing the copy back sees it at most once per lease, and only the first re-offer warns. A row owned
    by ANOTHER live process is left to that process, whose queue this one cannot see. The consumer's
    ``claim_completion_delivery`` stays the atomic cross-process gate, so a duplicate copy can never
    deliver twice. Rows past the delivery budget or the replay age converge to ``dropped``. Reads the
    current profile's ledger: callers bind the owning profile first."""
    alive = _owner_liveness()
    if alive is None or not _ledger_exists():
        return 0  # never create a ledger just to sweep it
    recover_abandoned_delegations()
    now = time.time() if now is None else now
    home = hermes_home_key(get_hermes_home())
    from tools.process_registry import process_registry
    live = _queued_completion_ids(target_queue, process_registry.completion_queue)
    with _orphan_lock:
        offered = frozenset(delegation_id for key, delegation_id in _offered if key == home)
    with _DB_LOCK, _ledger() as store:
        # Owners are probed and lost copies judged between the candidate read and the sweep's write; the store
        # drops rows past the delivery budget or the replay age. The offers go out before that write commits,
        # so a crash between them leaves every candidate pending.
        candidates = store.coordination.list_orphan_completions(now=now)
        dead = frozenset(item.owner for item in candidates if not alive(item.owner.pid, item.owner.started_at))
        lost = frozenset(item.delegation_id for item in candidates
                         if (item.owner not in dead or item.delegation_id in offered) and live is not None
                         and not _copy_may_be_live(item.delegation_id, item.owner.pid, item.updated_at, live, home,
                                                   offered, now))
        sweep = dict(now=now, exclude_ids=offered, dead_owners=dead, lost_ids=lost)
        orphans = store.coordination.list_orphan_completion_offers(**sweep)
        reoffered = [item.delegation_id for item in orphans if item.delegation_id in lost]
        if reoffered:
            _note_reoffers(home, reoffered, now)
        restored = _offer_pending(orphans, target_queue)
        store.coordination.sweep_orphaned_completions(**sweep)
        return restored


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


def claim_completion_delivery(delegation_id: str, claim_id: str) -> bool:
    """Claim one pending completion across competing consumers/processes."""
    if remote := worker_ledger():
        return remote.claim(delegation_id, claim_id)
    with _DB_LOCK, _ledger() as store:
        return store.coordination.claim_delivery(delegation_id=delegation_id, claim_id=claim_id)


def is_interim_delegation_event(evt: dict[str, Any]) -> bool:
    """An early per-task notice for a batch that is still running. It shares the batch's
    ``delegation_id`` but is NOT the durable completion: it must never claim, acknowledge or
    dedup against the final result's row (independent review reproduced exactly that loss)."""
    return evt.get("type") == "async_delegation" and bool(evt.get("task_failure_notice"))


def claim_event_delivery(evt: dict[str, Any], consumer: str) -> Optional[str]:
    """Claim a durable delegation event; non-durable events (and interim notices) need no token."""
    if is_interim_delegation_event(evt):
        return ""
    delegation_id = str(evt.get("delegation_id") or "") if evt.get("type") == "async_delegation" else ""
    if not delegation_id:
        return ""
    claim_id = f"{consumer}:{os.getpid()}:{uuid.uuid4().hex}"
    return claim_id if claim_completion_delivery(delegation_id, claim_id) else None


def release_completion_delivery(delegation_id: str, claim_id: str) -> bool:
    """Release a failed delivery claim so another consumer may retry. Attempts are
    counted at claim time; once the budget is exhausted the row converges to
    terminal ``dropped`` (only pending rows replay on restart)."""
    if remote := worker_ledger():
        return remote.release(delegation_id, claim_id)
    with _DB_LOCK, _ledger() as store:
        return store.coordination.release_delivery(delegation_id=delegation_id, claim_id=claim_id)


def defer_completion_delivery(delegation_id: str, claim_id: str) -> bool:
    """Return an unadmitted completion to pending without spending a delivery attempt."""
    if remote := worker_ledger():
        return remote.defer(delegation_id, claim_id)
    with _DB_LOCK, _ledger() as store:
        return store.coordination.defer_delivery(delegation_id=delegation_id, claim_id=claim_id)


def drop_completion_delivery(delegation_id: str, claim_id: str) -> bool:
    """Terminally drop a claimed completion whose target is permanently gone (the
    spawning session ended at an explicit user boundary such as /new or reset).
    ``dropped`` — not ``delivered`` — keeps the ack honest; not ``pending`` keeps
    restart recovery from replaying it into a fail-closed drop forever."""
    if remote := worker_ledger():
        return remote.drop(delegation_id, claim_id)
    with _DB_LOCK, _ledger() as store:
        return store.coordination.drop_delivery(delegation_id=delegation_id, claim_id=claim_id)


def complete_completion_delivery(delegation_id: str, claim_id: str) -> bool:
    """Acknowledge acceptance for the consumer holding this claim."""
    if remote := worker_ledger():
        return remote.ack(delegation_id, claim_id)
    with _DB_LOCK, _ledger() as store:
        return store.coordination.complete_delivery(delegation_id=delegation_id, claim_id=claim_id)


def requeue_admitted_completion(delegation_id: str, target_queue) -> bool:
    """Offer an acknowledged completion again when its receiving turn could not confirm the owner.

    The row returns to pending and its event goes back on ``target_queue``, so the next delivery
    re-runs the pre-flight. A row past the delivery budget converges to ``dropped`` instead, which
    bounds the loop; a row without a stored event is left as it is."""
    with _DB_LOCK, _ledger() as store:
        # The copy is offered before the reopen commits, so a crash between them leaves the row delivered; a
        # consumer's claim waits on ``_DB_LOCK`` until the row is pending.
        admitted = store.coordination.get_admitted_completion(delegation_id=delegation_id)
        offered = 0
        if admitted is not None:
            with _orphan_lock:  # an admitted copy already had its consumer: requeue keeps live routing
                _startup_replayed.discard(delegation_id)
            offered = _offer_pending((admitted,), target_queue)
        reopened = store.coordination.reopen_admitted_completion(delegation_id=delegation_id)
        return reopened is not None and offered == 1


def complete_event_delivery(evt: dict[str, Any], claim_id: str) -> None:
    _event_delivery(complete_completion_delivery, evt, claim_id)


def release_event_delivery(evt: dict[str, Any], claim_id: str) -> None:
    """Release a failed claim for a consumer that discards its copy (the TUI poller): the row is pending
    again, so it must stay eligible for the orphan sweep."""
    _event_delivery(release_completion_delivery, evt, claim_id)
    return_completion_offer(evt)


def return_completion_offer(evt: dict[str, Any]) -> None:
    """Hand an offered completion back to the orphan sweep after its in-memory copy was discarded while
    the durable row stays pending, e.g. a TUI session that cannot prove it owns the event drops it (every
    session poller drains one process-wide queue). The next sweep may offer the row again. Delegation ids
    are unique across profiles, so this clears the offer in every home."""
    delegation_id = str(evt.get("delegation_id") or "") if evt.get("type") == "async_delegation" else ""
    if not delegation_id or is_interim_delegation_event(evt):
        return
    with _orphan_lock:
        _offered.difference_update({key for key in _offered if key[1] == delegation_id})


def _event_delivery(fn, evt: dict[str, Any], claim_id: str) -> None:
    if claim_id and evt.get("type") == "async_delegation":
        fn(str(evt.get("delegation_id") or ""), claim_id)


def get_durable_delegation(delegation_id: str) -> Optional[dict[str, Any]]:
    if remote := worker_ledger():
        saved = remote.read(delegation_id)
        if saved is not None and saved.get("result") is not None:
            return {**saved, "result": redact_child_result(saved["result"])}
        return saved
    with _DB_LOCK, _ledger() as store:
        record = store.coordination.get_delegation(delegation_id=delegation_id)
    return None if record is None else {
        "delegation_id": delegation_id, "origin_session": record.origin_session, "state": record.state,
        "dispatched_at": record.dispatched_at, "completed_at": record.completed_at,
        "result": redact_child_result(record.result) if record.result is not None else None,
        "delivery_state": record.delivery_state.value, "delivery_attempts": record.delivery_attempts,
        "origin_session_id": record.origin_session_id}
>>>>>>> 32dbc1dcda2255fd35969ec99cfc0170988c3eca


_FAILED_TASK_STATES = SUBAGENT_FAILURE_STATUSES | {"stalled", "unknown", "interrupted"}
<<<<<<< HEAD
_FAILURE_SURFACE_WINDOW_S = 24 * 3600.0
||||||| dff103e05878
_FAILURE_SURFACE_WINDOW_S = 24 * 3600.0


def _json_object(raw: Optional[str]) -> dict[str, Any]:
    try:
        value = json.loads(raw) if raw else {}
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


def failed_delegations_for_session(
    origin_ui_session_id: str = "", parent_session_id: str = "", *, limit: int = 20, now: Optional[float] = None,
) -> list[dict[str, Any]]:
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
                WHERE ({owner_sql}) AND state!='running' AND completed_at >= ?
                ORDER BY completed_at DESC LIMIT ?""",
            (*(val for _, val in selectors), cutoff, limit)).fetchall()
    failed: list[dict[str, Any]] = []
    for delegation_id, state, dispatched_at, completed_at, task_json, result_json in rows:
        task, result = _json_object(task_json), redact_child_result(_json_object(result_json))
        goals = task.get("goals") if isinstance(task.get("goals"), list) and task["goals"] else [task.get("goal") or ""]
        tasks = result["results"] if isinstance(result.get("results"), list) else [] if task.get("is_batch") else [result]
        from tools.delegate_tool_auto_resume import waiting_on_resume
        if waiting_on_resume(result):  # every child waits on its automatic resume (or its one give-up failure)
            continue
        if not tasks and str(state).lower() in _FAILED_TASK_STATES:  # crash, stall or owner exit: no per-task rows
            tasks = [{"task_index": i, "error": result.get("error")} for i in task.get("task_indexes") or [0]]
        for entry in tasks:
            status = str(entry.get("status") or state or "").lower()
            if status not in _FAILED_TASK_STATES or waiting_on_resume(entry):
                continue
            index = entry.get("task_index") if isinstance(entry.get("task_index"), int) else 0
            error = entry.get("error") or result.get("error")
            failed.append({
                "delegation_id": delegation_id, "task_index": index, "status": status,
                # ``goals`` is the whole call's list (split units store it too); task_index indexes it.
                "goal": str((goals[index] if 0 <= index < len(goals) else goals[0]) or ""),
                "error": str(error) if error else None,
                "dispatched_at": dispatched_at, "completed_at": completed_at})
    return failed[:limit]
=======


def failed_delegations_for_session(
    origin_ui_session_id: str = "", parent_session_id: str = "", *, limit: int = 20, now: Optional[float] = None,
) -> list[dict[str, Any]]:
    """Recently failed async delegation tasks owned by a session, newest first.

    The live roster forgets a child once it ends and does not survive a renderer reload, so a failed
    delegation had nowhere to show (#97202). This reads the durable row instead: one entry per failed
    task (a batch unit that "completed" can still carry failed tasks) with ``delegation_id``,
    ``task_index``, ``goal``, ``status``, ``error``, ``dispatched_at`` and ``completed_at``. Either selector claims a row:
    the UI session id at dispatch, or the spawner's durable session id (survives a reload re-mint)."""
    if not (origin_ui_session_id or parent_session_id):
        return []
    with _DB_LOCK, _ledger() as store:
        rows = store.coordination.list_finished_delegations(
            origin_ui_session_id=origin_ui_session_id, parent_session_id=parent_session_id, limit=limit, now=now)
    failed: list[dict[str, Any]] = []
    for row in rows:
        delegation_id, state, dispatched_at, completed_at = row.delegation_id, row.state, row.dispatched_at, row.completed_at
        task, result = row.task, redact_child_result(row.result)
        goals = task.get("goals") if isinstance(task.get("goals"), list) and task["goals"] else [task.get("goal") or ""]
        tasks = result["results"] if isinstance(result.get("results"), list) else [] if task.get("is_batch") else [result]
        from tools.delegate_tool_auto_resume import waiting_on_resume
        if waiting_on_resume(result):  # every child waits on its automatic resume (or its one give-up failure)
            continue
        if not tasks and str(state).lower() in _FAILED_TASK_STATES:  # crash, stall or owner exit: no per-task rows
            tasks = [{"task_index": i, "error": result.get("error")} for i in task.get("task_indexes") or [0]]
        for entry in tasks:
            status = str(entry.get("status") or state or "").lower()
            if status not in _FAILED_TASK_STATES or waiting_on_resume(entry):
                continue
            index = entry.get("task_index") if isinstance(entry.get("task_index"), int) else 0
            error = entry.get("error") or result.get("error")
            failed.append({
                "delegation_id": delegation_id, "task_index": index, "status": status,
                # ``goals`` is the whole call's list (split units store it too); task_index indexes it.
                "goal": str((goals[index] if 0 <= index < len(goals) else goals[0]) or ""),
                "error": str(error) if error else None,
                "dispatched_at": dispatched_at, "completed_at": completed_at})
    return failed[:limit]
>>>>>>> 32dbc1dcda2255fd35969ec99cfc0170988c3eca


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


def open_state_db(path: Path, db_label: str) -> sqlite3.Connection:
    """A short-lived writer connection to a profile's ``state.db`` that never resurrects a removed named profile
    (#123265) and keeps the file and its sidecars owner-only (0600, as ``hermes_state.SessionDB`` does)."""
    from hermes_cli.sqlite_util import open_db
    from hermes_constants import mkdir_under_hermes_home
    from hermes_state import _secure_state_db_files
    mkdir_under_hermes_home(path.parent)
    _secure_state_db_files(path, create_main=True)
    identity = _db_identity(path)
    # wal=False: SessionDB owns state.db's journal mode (_initialize_schema applies the barriers).
    conn = open_db(path, db_label=db_label, busy_timeout_ms=10_000,
                   wal=False, row_factory=None,
                   initialize=lambda conn: _initialize_schema(conn, identity=identity))
    try:
        _secure_state_db_files(path)
    except BaseException:
        conn.close()
        raise
    return conn


def active_count(*, home: Optional[Path] = None) -> int:
    """Number of live async delegation UNITS (one per completion message: a task group or an ungrouped task);
    *home*: only the ones that profile dispatched."""
    # Writers hold the registry lock across state.db commits. The gateway loop only needs a
    # snapshot, copied in one C call under the GIL, so it must never wait for that writer.
    key = hermes_home_key(home) if home is not None else None
    return sum(1 for r in list(_records.values()) if r.get("status") in _LIVE_STATES
               and (key is None or r.get("_home") == key))


def active_records() -> list[dict[str, Any]]:
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


def _session_selectors(session_key: str, origin_ui_session_id: str, parent_session_id: Optional[str]) -> frozenset:
    return frozenset((field, wanted) for field, wanted in (
        ("origin_ui_session_id", origin_ui_session_id), ("session_key", session_key),
        ("parent_session_id", parent_session_id)) if wanted)


def _session_records(statuses, session_key: str, origin_ui_session_id: str, parent_session_id: str) -> list:
    """Records in ``statuses`` owned by a session: any non-empty selector claims the
    record — ``origin_ui_session_id`` (TUI tab), ``session_key`` (routing key at
    dispatch), or ``parent_session_id`` (spawner's durable id — the right one for
    gateway chats, whose session_key survives ``/new`` while the session id rotates)."""
    selectors = _session_selectors(session_key, origin_ui_session_id, parent_session_id)
    if not selectors:
        return []
    with _records_lock:
        return [r for r in _records.values() if r.get("status") in statuses
                and any(str(r.get(field) or "") == wanted for field, wanted in selectors)]


def refuse_for_session(token: Any, *, session_key: str = "", parent_session_ids=(), origin_ui_session_id: str = "") -> None:
    """Refuse new dispatches for ONE closing session (any selector matches) until ``allow_for_session``.
    Set before ``interrupt_for_session``: a dispatch registered first is interrupted, a later one refused.
    ``origin_ui_session_id`` additionally fences a TUI tab's dispatches."""
    selectors = set(_session_selectors(session_key, origin_ui_session_id, None))
    selectors.update(("parent_session_id", parent) for parent in parent_session_ids if parent)
    with _records_lock:
        _unfence(token)
        _refused[token] = frozenset(selectors)
        for selector in selectors:
            _refused_by.setdefault(selector, set()).add(token)


def allow_for_session(token: Any) -> None:
    """Lift ONE session's refusal: a viewer attached again, or the owner retired its live state."""
    with _records_lock:
        _unfence(token)


def _unfence(token: Any) -> None:
    """Drop *token*'s refusal; caller holds ``_records_lock``."""
    for selector in _refused.pop(token, ()):
        tokens = _refused_by[selector]
        tokens.discard(token)
        if not tokens:
            del _refused_by[selector]


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
def _single_crash(error: str, duration: float) -> dict[str, Any]:
    return {"status": "error", "summary": None, "error": error, "api_calls": 0, "duration_seconds": duration}


def _batch_crash(error: str, duration: float) -> dict[str, Any]:
    return {"results": [], "error": error, "total_duration_seconds": duration}


def _batch_status(combined: dict[str, Any]) -> str:
    """Batch status: completed unless every child errored/was interrupted."""
    child_results = combined.get("results") or []
    return "error" if child_results and all(r.get("status") != "completed" for r in child_results) else "completed"


def _dispatch(**kwargs) -> dict[str, Any]:
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


def _record_context_run(record: dict[str, Any], fn: Callable, *args):
    ctx = record.get("_context") or contextvars.copy_context()
    return ctx.copy().run(fn, *args)


def _submit_record(record: dict[str, Any], max_async_children: int) -> Optional[str]:
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
    from tools.delegation_resume import retry_submission_error
    # Global queue admission may run from another profile's context: read this record's own ledger.
    if reason := _record_context_run(record, retry_submission_error, delegation_id):
        record["_retry_expired"] = True
        _finalize(delegation_id, record["crash_result"](reason, 0), "error")
        return reason
    try:
        live_units = sum(1 for r in _records.values() if r.get("status") in _LIVE_STATES)
        executor = _get_executor(max(max_async_children, live_units))
    except Exception as exc:
        logger.warning("Async delegation %s could not create an executor: %s", delegation_id, exc)
        return f"Failed to schedule async delegation{label}: {exc}"

    driver_finished = Future()

    from tools.approval_notify_lease import run as run_with_notify_lease
    lease = record["_approval_lease"]

    def _worker() -> None:
        result: dict[str, Any] = {}
        status = "error"
        with _records_lock:
            rec = _records.get(delegation_id)
            if rec is None or rec is not record or rec.get("status") != "running":
                # Terminalization may precede executor entry. The queued callable
                # must not run a child that the outcome path has already closed.
                return
            # A failed submit may leave its callable queued while this record
            # is admitted again. Only this submission's placeholder or returned
            # Future owns entry, not a later submission of the same record.
            owned_future = rec.get("_future")
            if owned_future is not placeholder and (future is None or owned_future is not future):
                return
            rec.update(_started=True, _progress_ts=time.time())
        try:
            result = run_with_notify_lease(lease, record["runner"]) or {}
            status = record["classify"](result)
        except Exception as exc:
            logger.exception(f"Async delegation{label} %s crashed", delegation_id)
            result = record["crash_result"](
                f"{type(exc).__name__}: {exc}", round(time.time() - record["dispatched_at"], 2)
            )
        finally:
            try:
                _finalize(delegation_id, result, status)
            finally:
                # Only this producer closes the driver, after all aggregate
                # hooks and finalization. A submit exception is not start proof.
                driver_finished.set_result(None)

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
            from tools.async_delegation_completion import record_unit_finished
            _record_context_run(record, record_unit_finished, record, done_future)
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

        def worker_done(completed: Any) -> None:
            try:
                lease.release()
                if completed.cancelled():
                    _finalize(delegation_id, record["crash_result"]("Worker cancelled before start", 0), "interrupted")
            finally:
                retirement.release()

        future.add_done_callback(worker_done)
        record["_submitting"] = False
    except Exception as exc:  # pragma: no cover — pool submit/transition failure is rare
        record["_submitting"] = False
        if future is None and record.get("_started"):
            # A callable can start before submit raises. Keep its real producer
            # ownership instead of replaying it in the caller's inline fallback.
            future = driver_finished
        if future is not None:
            # The record owns a real submitted or positively started driver even
            # if submit/callback setup failed. It must not be rolled back with
            # its siblings or have its completion Future hidden.
            record["_future"] = future
            record["_slot_release_done"] = False
            record["_slot_reserved"] = True
            logger.warning("Async delegation %s completed submit setup with a live Future: %s", delegation_id, exc)
            try:
                future.add_done_callback(release_slot)
                future.add_done_callback(lambda _: (lease.release(), retirement.release()))
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


def _queue_selected_locked(selected: list[dict[str, Any]]) -> None:
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
    selected: list[dict[str, Any]], submitted: list[dict[str, Any]], error: str,
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

    def settle_group(items: list[dict[str, Any]], target: str) -> list[dict[str, Any]]:
        """Persist and publish one target for groups split by current state."""
        settled: list[dict[str, Any]] = []
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
            selected: list[dict[str, Any]] = []
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

            submitted: list[dict[str, Any]] = []
            submission_error: Optional[str] = None
            try:
                for item in selected:
                    try:
                        error = _record_context_run(item, _submit_record, item, item["max_async_children"])
                    except Exception as exc:
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
    *, delegation_id: str, goal: str, goals: Optional[list[str]], context: Optional[str],
    toolsets: Optional[list[str]], role: str, model: Optional[str], session_key: str,
    parent_session_id: Optional[str], runner: Callable[[], dict[str, Any]], origin_ui_session_id: str,
    origin_session_id: str, interrupt_fn: Optional[Callable[[], None]], max_async_children: int,
    progress_fn: Optional[Callable[[], tuple]], capacity_error: str, slot_key: Optional[str] = None,
    task_indexes: Optional[list[int]] = None,
    task_contexts: Optional[dict[str, str]] = None,
    task_transcripts: Optional[dict[str, str]] = None,
    child_sessions: Optional[dict[str, str]] = None,
    cron_execution: Optional[dict[str, str]] = None,
    max_queued_delegations: int = _DEFAULT_MAX_QUEUED_DELEGATIONS,
    cancel_fn: Optional[Callable[[str], None]] = None,
) -> dict[str, Any]:
    """Register and submit one async unit without ever running its runner inline.

    A full pool places the unit in a bounded FIFO when possible. Only the queue
    overflow is rejected; callers that can receive detached completions must not
    turn capacity pressure into synchronous work.
    """
    worker_ledger()  # refuse unbound workers before publishing a live record
    is_batch = goals is not None
    label = " batch" if is_batch else ""
    classify = _batch_status if is_batch else (lambda r: r.get("status") or "completed")
    crash_result = _batch_crash if is_batch else _single_crash
    dispatched_at = time.time()
    record: dict[str, Any] = {
        "delegation_id": delegation_id, "goal": goal, **({"goals": list(goals)} if is_batch else {}),
        "context": context, "toolsets": list(toolsets) if toolsets else None, "role": role, "model": model,
        "session_key": session_key, "origin_ui_session_id": origin_ui_session_id,
        "origin_session_id": origin_session_id, "parent_session_id": parent_session_id,
        **_capture_routing_origin(), "status": "queued", "_lifecycle_state": "new", "_durable_state": None,
        "dispatched_at": dispatched_at, "completed_at": None,
        "interrupt_fn": interrupt_fn, "cancel_fn": cancel_fn, "runner": runner, "classify": classify,
        "crash_result": crash_result, **({"is_batch": True} if is_batch else {}), "progress_fn": progress_fn,
        "slot_key": slot_key or delegation_id, "max_async_children": max_async_children,
        **(cron_execution or {}), **({"task_contexts": dict(task_contexts)} if task_contexts else {}),
        **({"task_transcripts": dict(task_transcripts)} if task_transcripts else {}),
        **({"child_sessions": dict(child_sessions)} if child_sessions else {}),
        **({"task_indexes": list(task_indexes)} if task_indexes is not None else {}),
        "_context": contextvars.copy_context(),
        "_worker_context_runner": propagate_context_to_thread(lambda worker: worker()), "_progress_token": None,
        "_home": hermes_home_key(get_hermes_home()),  # the profile that dispatched it (``active_count(home=...)``)
        "_progress_ts": dispatched_at, "_interrupted_at": None, "_started": False,
        "_slot_reserved": False, "_slot_release_done": True,
        "_persisting": True, "_persist_done": threading.Event(),
    }
    session_selectors = _session_selectors(session_key, origin_ui_session_id, parent_session_id)
    with _records_lock:
        if any(selector in _refused_by for selector in session_selectors):
            return {"status": "rejected", "accepted": False,
                    "error": "This session was closed (exit or /new): no new background work."}
        existing = _records.get(delegation_id)
        if existing is not None:
            return {"status": "rejected", "accepted": False,
                    "error": f"Async delegation {delegation_id} already exists in memory; refusing duplicate id."}
        try:
<<<<<<< HEAD
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
||||||| dff103e05878
            result = run_with_notify_lease(lease, runner) or {}
            status = classify(result)
        except Exception as exc:
            logger.exception(f"Async delegation{label} %s crashed", delegation_id)
            result = crash_result(f"{type(exc).__name__}: {exc}", round(time.time() - dispatched_at, 2))
        finally:
            _finalize(delegation_id, result, status)

    from hermes_cli.backend_retirement import retirement

    # The outer dispatch reservation prevents a freeze during this handoff. Retain a worker
    # reservation too: the stall monitor may finalize its registry record before it really exits.
    retirement.acquire()
    try:
        future = executor.submit(propagate_context_to_thread(_worker))
        def worker_done(completed):
            try:
                lease.release()
                if completed.cancelled():
                    _finalize(delegation_id, crash_result("Worker cancelled before start", 0), "interrupted")
            finally:
                retirement.release()

        future.add_done_callback(worker_done)
    except Exception as exc:  # pragma: no cover — pool submit failure is rare
        retirement.release()
        lease.release()
        with _records_lock:
            _records.pop(delegation_id, None)
        if remote := worker_ledger():
            remote.unscheduled(delegation_id)
=======
            result = run_with_notify_lease(lease, runner) or {}
            status = classify(result)
        except Exception as exc:
            from storage_user_copy import error_type_name
            logger.exception(f"Async delegation{label} %s crashed", delegation_id)
            result = crash_result(f"{error_type_name(exc)}: {exc}", round(time.time() - dispatched_at, 2))
        finally:
            _finalize(delegation_id, result, status)

    from hermes_cli.backend_retirement import retirement

    # The outer dispatch reservation prevents a freeze during this handoff. Retain a worker
    # reservation too: the stall monitor may finalize its registry record before it really exits.
    retirement.acquire()
    try:
        future = executor.submit(propagate_context_to_thread(_worker))
        def worker_done(completed):
            try:
                lease.release()
                if completed.cancelled():
                    _finalize(delegation_id, crash_result("Worker cancelled before start", 0), "interrupted")
            finally:
                retirement.release()

        future.add_done_callback(worker_done)
    except Exception as exc:  # pragma: no cover — pool submit failure is rare
        retirement.release()
        lease.release()
        with _records_lock:
            _records.pop(delegation_id, None)
        if remote := worker_ledger():
            remote.unscheduled(delegation_id)
>>>>>>> 32dbc1dcda2255fd35969ec99cfc0170988c3eca
        else:
<<<<<<< HEAD
            _PENDING_ADMISSION_SLOTS.add(slot)
        from tools.approval_notify_lease import acquire
        # Capture the dispatcher's background transport even when capacity queues this unit.
        record["_approval_lease"] = acquire(session_key)
        _records[delegation_id] = record
    try:
        # Publish no admission-eligible queue entry until this insert commits.
        # Cancellation observes ``_persisting`` and waits for this same record
        # to become durable before taking the terminal transition.
        _persist_dispatch(record)
    except Exception as exc:
        from tools.delegation_resume import RetryAdmissionError, claimed_retry_source, is_recovery_spawn, release_retry_dispatch
        # Policy rejection must not reopen a consumed or explicitly closed
        # claim, or release sibling claims. Only storage/insert failures unwind
        # a claim taken by this dispatch.
        retry_admission_rejected = isinstance(exc, RetryAdmissionError)
        managed_recovery = is_recovery_spawn(record)
        retry_rearmed = False
        try:
            if not retry_admission_rejected:
                prior = claimed_retry_source(record)
                if prior is not None:
                    # Explicit CLI/TUI resume claims are re-armed for a later manual
                    # resume, not scheduled for the automatic retry notice path.
                    had_retry_claim = bool(prior.get("retry_claim"))
                    released = release_retry_dispatch(prior)
                    retry_rearmed = had_retry_claim and released
        except Exception:
            logger.warning("Failed to release managed retry claim after persistence failure", exc_info=True)
        with _records_lock:
            if _records.get(delegation_id) is record:
                _records.pop(delegation_id, None)
                _PENDING_ADMISSION_SLOTS.discard(record["slot_key"])
        record["_approval_lease"].release()
        record["_persist_done"].set()
        from tools.delegation_resume import RetryAdmissionError
        logger.error("Failed to persist new async delegation %s", delegation_id, exc_info=True)
        no_inline_fallback = managed_recovery or isinstance(exc, RetryAdmissionError)
        error = f"Failed to persist async delegation{label}: {exc}"
        if managed_recovery:
            error += "; recovery was not started"
        if retry_rearmed:
            error += " and will be retried later"
        return {"status": "rejected", "accepted": False, "no_inline_fallback": no_inline_fallback,
                "error": error}
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
        _interrupt_records([record], record.get("_cancel_caller", "interrupt_delegation"),
                           record.get("_cancel_reason", "cancelled"),
                           "Interrupted %d async delegation(s) (%s)")
        return {"status": "cancelled", "delegation_id": delegation_id, "accepted": True}
    with _records_lock:
        if record.get("_schedule_error"):
            from tools.delegation_resume import is_recovery_spawn
            # A managed retry's replacement row is already durable and linked; running it inline
            # would duplicate it. The never-started row is retried by the sweep instead.
            return {"status": "rejected", "accepted": False, "error": record["_schedule_error"],
                    "no_inline_fallback": bool(record.get("_retry_expired")) or is_recovery_spawn(record)}
        if status == "queued" or record.get("_initially_queued"):
            _ensure_stale_monitor()
            return {"status": "queued", "accepted": True, "delegation_id": delegation_id,
                    "queue_reason": record.get("queue_reason", "async pool capacity")}
        if status in _TERMINAL_STATES:
            return {"status": status, "accepted": True, "delegation_id": delegation_id}
        return {"status": "dispatched", "accepted": True, "delegation_id": delegation_id}
||||||| dff103e05878
            with _DB_LOCK, _transaction() as conn:
                conn.execute("DELETE FROM async_delegations WHERE delegation_id=?", (delegation_id,))
        return {"status": "rejected", "error": f"Failed to schedule async delegation{label}: {exc}"}
    if progress_fn is not None:
        _ensure_stale_monitor()
    return {"status": "dispatched", "delegation_id": delegation_id}
=======
            with _DB_LOCK, _ledger() as store:
                store.coordination.delete_unscheduled_delegation(delegation_id=delegation_id)
        return {"status": "rejected", "error": f"Failed to schedule async delegation{label}: {exc}"}
    if progress_fn is not None:
        _ensure_stale_monitor()
    return {"status": "dispatched", "delegation_id": delegation_id}
>>>>>>> 32dbc1dcda2255fd35969ec99cfc0170988c3eca


def dispatch_async_delegation(
    *, goal: str, context: Optional[str], toolsets: Optional[list[str]], role: str, model: Optional[str],
    session_key: str, parent_session_id: Optional[str] = None, runner: Callable[[], dict[str, Any]],
    origin_ui_session_id: str = "", origin_session_id: str = "", interrupt_fn: Optional[Callable[[], None]] = None,
    max_async_children: int = _DEFAULT_MAX_ASYNC_CHILDREN, max_queued_delegations: int = _DEFAULT_MAX_QUEUED_DELEGATIONS,
    progress_fn: Optional[Callable[[], tuple]] = None, delegation_id: Optional[str] = None,
    cron_execution: Optional[dict[str, str]] = None, cancel_fn: Optional[Callable[[str], None]] = None,
) -> dict[str, Any]:
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
    *, goals: list[str], context: Optional[str], toolsets: Optional[list[str]], role: str, model: Optional[str],
    session_key: str, parent_session_id: Optional[str] = None, runner: Callable[[], dict[str, Any]],
    origin_ui_session_id: str = "", origin_session_id: str = "", interrupt_fn: Optional[Callable[[], None]] = None,
    max_async_children: int = _DEFAULT_MAX_ASYNC_CHILDREN, max_queued_delegations: int = _DEFAULT_MAX_QUEUED_DELEGATIONS,
    delegation_id: Optional[str] = None, progress_fn: Optional[Callable[[], tuple]] = None, slot_key: Optional[str] = None,
    task_indexes: Optional[list[int]] = None,
    task_contexts: Optional[dict[str, str]] = None,
    task_transcripts: Optional[dict[str, str]] = None, cancel_fn: Optional[Callable[[str], None]] = None,
    child_sessions: Optional[dict[str, str]] = None,
) -> dict[str, Any]:
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
        task_indexes=task_indexes, task_contexts=task_contexts, task_transcripts=task_transcripts,
        child_sessions=child_sessions,
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


def _finalize(delegation_id: str, result: Any, status: str, *, _claimed_snapshot: Optional[dict[str, Any]] = None) -> None:
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
            record["_stall_stopped"] = prior_status == "stalling"  # the stall monitor's stop, read by completion_event
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
    if (lease := snapshot.get("_approval_lease")) is not None:
        lease.release()  # a finalized unit can no longer answer an approval prompt
    if (was_queued or was_admitted_unstarted) and snapshot.get("cancel_fn") is not None:
        try:
            snapshot["cancel_fn"]("queued delegation cancelled")
        except Exception:
            logger.debug("Queued delegation %s cleanup failed", delegation_id, exc_info=True)
    _push_completion_event(snapshot, result(snapshot) if callable(result) else result, status)
    with _records_lock:
        live = _records.get(delegation_id)
        if live is not None:
            completion = live if live.get("_completion_status") else snapshot
            _transition_memory_locked(live, completion.get("_completion_status") or status, expected="finalizing")
            live["_durable_state"] = completion.get("_terminal_state") or status
            # Only an active record is ever force-finalized under its dispatcher's Context; a
            # retained completed one would otherwise pin that turn's agent and history.
            live.pop("_context", None)
            live.pop("_worker_context_runner", None)
        _prune_completed_locked()
    # A terminal event does not free a submitted slot; only release_slot does.


def completion_event(record: dict[str, Any], result: dict[str, Any], status: str) -> tuple[dict, dict, bool]:
    """``(event, result, silent)`` for a finished unit: the type='async_delegation' event, the result it carries,
    and whether every child waits on a promised automatic resume (the parent then hears nothing)."""
    result = redact_child_result(result, force=False)
    if record.get("_stall_stopped") and "stall_grace_seconds" not in result:
        # Its runner unwound on the stall monitor's stop inside the grace window: read as interrupted, like a user stop.
        result = {**result, **_stall_meta(record)}
    is_batch = bool(record.get("is_batch"))
    children = result.get("results") or ([] if is_batch else [result])
    from tools.delegate_tool_auto_resume import waiting_on_resume
    waiting = [r for r in children if waiting_on_resume(r)]
    silent = bool(children) and len(waiting) == len(children)  # every child will resume: the parent hears nothing
    if waiting and not silent:  # a mixed unit: the promised child reads as waiting, not failed
        result = {**result, "results": [{**r, "status": "waiting_to_resume"} if r in waiting else r for r in children]}
    cut_reason = record.get("_cut_reason")
    dispatched_at = record.get("dispatched_at") or time.time()
    completed_at = record.get("completed_at") or time.time()
    if is_batch:
        payload = {
            "is_batch": True, "results": result.get("results") or [],
            **({"task_indexes": list(record["task_indexes"])} if record.get("task_indexes") is not None else {}),
            "live_transcripts": result.get("live_transcripts"), "error": result.get("error"),
            "total_duration_seconds": result.get("total_duration_seconds"),
            **({"group": result["group"]} if result.get("group") is not None else {})}
    else:
        # A runner with no measured count (a cron run without a session) omits api_calls: never a fake 0.
        payload = {
            "summary": result.get("summary"), "error": result.get("error"),
            **({"api_calls": result["api_calls"]} if result.get("api_calls") is not None else {}),
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
        **({"cut_reason": cut_reason} if cut_reason else {}),
        **({} if is_batch else {"exit_reason": result.get("exit_reason")}),
        **{k: record[k] for k in _ROUTING_KEYS if record.get(k)},
        **{k: result[k] for k in _STALL_META_KEYS if k in result}}
    return evt, result, silent


def _push_completion_event(record: dict[str, Any], result: dict[str, Any], status: str) -> None:
    """Push a type='async_delegation' event onto the shared completion queue. Batch records
    (``is_batch``) carry the per-task ``results`` list (plus live transcript paths, the
    full-fidelity record of each child's run) instead of a single summary. Best-effort: failure
    must not crash the worker, but it WOULD mean a silently-lost result, so we log loudly.

    The queued event is the parent's in-process copy and honours ``security.redact_secrets: false``;
    ``_persist_completion`` re-scrubs with force for the durable row (and the worker IPC)."""
    label = " batch" if record.get("is_batch") else ""
    try:
        from tools.process_registry import process_registry
    except Exception as exc:  # pragma: no cover
        logger.error(f"Async delegation{label} %s finished but process_registry import failed; "
                     "result lost: %s", record.get("delegation_id"), exc, exc_info=True)
        return
    evt, result, silent = completion_event(record, result, status)
    if silent and not record.get("superseded_by"):
        # Every child waits on its promised automatic resume: record the unit delivered; the parent hears nothing.
        try:
            _persist_completion(dict(evt, status=record.get("_terminal_state") or status), result, delivery_state="delivered",
                                expected_state=record.get("_durable_state") or "running")
        except Exception as exc:
            logger.error("Async delegation%s %s: silent completion write failed: %s", label, record.get("delegation_id"), exc, exc_info=True)
        record.update(_terminal_state=durable_status(evt, record.get("_terminal_state") or status), _completion_status=evt["status"])
        logger.info("Async delegation %s: every child waits on an automatic resume; no completion delivered",
                    record.get("delegation_id"))
        return
    if record.get("superseded_by"):
        # The owner already replaced this unit with a running delegation whose own completion will report the
        # outcome. Record the result durably, but never wake the parent for an attempt it no longer waits on.
        evt.update(superseded_by=record["superseded_by"], superseded_reason=record.get("superseded_reason") or "")
        try:
            _persist_completion(evt, result, delivery_state="superseded",
                                expected_state=record.get("_durable_state") or "running")
        except Exception as exc:
            logger.error("Async delegation %s: superseded completion write failed: %s", record.get("delegation_id"), exc)
        record.update(_terminal_state=durable_status(evt, record.get("_terminal_state") or status), _completion_status=evt["status"])
        logger.info("Async delegation%s %s superseded by %s; completion recorded, not delivered",
                    label, record.get("delegation_id"), record["superseded_by"])
        return
    expected_state = record.get("_durable_state") or "running"
    terminal_status = record.get("_terminal_state") or status
    try:
        persist_evt = dict(evt)
        persist_evt["status"] = terminal_status
        ok = _persist_completion(persist_evt, result, expected_state=expected_state)
    except Exception as exc:
        logger.error("Async delegation%s %s: durable terminal row write failed; reconciling fallback ownership: %s",
                     label, record.get("delegation_id"), exc)
        ok = False
    sync_completion_payload(evt, persist_evt)
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
    record.update(_terminal_state=durable_status(evt, terminal_status), _completion_status=evt["status"])
    if queue_event:
        if not evt.get(_TERMINAL_OWNERSHIP_KEY):
            _record_context_run(record, _attach_retry_note, evt)
        try:
            process_registry.completion_queue.put(evt)
        except Exception as exc:  # pragma: no cover
            logger.error(f"Async delegation{label} %s: failed to enqueue completion event; "
                         "result lost: %s", record.get("delegation_id"), exc, exc_info=True)


def push_task_failure_notice(delegation_id: str, entry: dict[str, Any], *, n_tasks: int) -> None:
    """Surface ONE failed child of a still-running detached batch to the parent now, instead of
    when the slowest sibling finishes. In a 1,393-agent run every wave-1 child died in a 401 storm
    at 08:29 and the parent learned of it at 09:36, when the batch's "unknown outcome" block finally
    arrived: 66 minutes of a dead wave with nothing running. The notice rides the same
    ``type="async_delegation"`` event shape as the batch result (so every drain/route/format path
    treats it identically) with ``task_failure_notice=True`` and a single-entry ``results`` list; the
    batch record is NOT finalized and its consolidated result still arrives as before. In-process
    queue only (never persisted), so the parent's copy honours ``security.redact_secrets: false``."""
    entry = redact_child_result(entry, force=False)
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


def _stalled_error_text(event_record: dict[str, Any]) -> str:
    """Human wording for a force-finalized stall. This string reaches the user (CLI timeline, Desktop
    async-result card), so it names the task, how long it was silent, and what to do — no issue
    numbers or worker internals (those stay in the log line and the stall_* metadata)."""
    goal = " ".join(str(event_record.get("goal") or "").split())
    label = f'Background task "{goal[:120]}"' if goal else "The background task"
    quiet = float(event_record.get("_stall_quiet_seconds") or 0)
    silence = f" after {round(quiet / 60)} min of no progress" if quiet >= 60 else ""
    return (f"{label} stopped responding{silence} and was cancelled. Nothing else was affected; "
            "ask me to run it again if you still need it.")


def _stall_meta(event_record: dict[str, Any]) -> dict[str, Any]:
    """Structured stall context: lets parents/UIs tell a stall-monitor stop from other outcomes
    without parsing the error string."""
    stall_in_tool = event_record.get("_stall_in_tool")
    return {
        "stalled_after_quiet_seconds": event_record.get("_stall_quiet_seconds"),
        "stall_threshold_seconds": event_record.get("_stall_threshold_seconds"),
        "stall_phase": "in_tool" if stall_in_tool else "idle" if stall_in_tool is not None else None,
        "stall_grace_seconds": _STALL_GRACE_SECONDS}


def _stalled_result(delegation_id: str, event_record: dict[str, Any]) -> dict[str, Any]:
    """Synthetic terminal result for a stalling delegation whose runner never returned."""
    completed_at = event_record.get("completed_at") or time.time()
    duration = round(completed_at - (event_record.get("dispatched_at") or completed_at), 2)
    error = _stalled_error_text(event_record)
    logger.error("Async delegation %s force-finalized as stalled after %.0fs", delegation_id, duration)
    stall_meta = _stall_meta(event_record)
    if event_record.get("is_batch"):
        return {**_batch_crash(error, duration), **stall_meta}
    return {**_single_crash(error, duration), "status": "stalled", "exit_reason": "stalled", **stall_meta}


# ── Observability + control ─────────────────────────────────────────────────
def _children_activity_from_token(token: Any, now: float) -> Optional[list]:
    """Parse a progress token into per-child activity dicts (best-effort): delegate_tool
    emits one ``(api_call_count, current_tool, last_activity_ts)`` tuple per child;
    foreign token shapes degrade to ``None`` entries."""
    try:
        parts = list(token)
    except TypeError:
        return None
    out: list[Optional[dict[str, Any]]] = []
    for part in parts:
        if not (isinstance(part, (list, tuple)) and len(part) >= 2):
            out.append(None)
            continue
        entry: dict[str, Any] = {"api_calls": part[0], "current_tool": part[1]}
        if len(part) >= 3 and isinstance(part[2], (int, float)):
            entry["seconds_since_activity"] = round(max(0.0, now - float(part[2])), 1)
        out.append(entry)
    return out


def list_async_delegations() -> list[dict[str, Any]]:
    """Snapshot of async delegations (running + recently completed) without callables or private
    monitor bookkeeping; adds computed live fields for UIs (``seconds_since_progress``,
    ``children_activity``/``in_tool`` sampled from ``progress_fn``) and stall context once tripped.

    Safe to call from any thread. See #51690.
    """
    now = time.time()
    samplers: dict[str, Callable] = {}
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


def _interrupt_records(targets: list[dict[str, Any]], caller: str, reason: str, msg: str,
                       affected_ids: Optional[set] = None) -> int:
    """Cancel queued records or signal running records; optionally union affected durable ids."""
    count = 0
    affected_ids = set() if affected_ids is None else affected_ids
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
                live["_cancel_caller"] = caller
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
                if caller == "interrupt_all":
                    # A signal is only a cut request. A runner that finishes
                    # successfully before returning must keep its completed result.
                    live["_cut_reason"] = reason
        if persist_wait is not None:
            persist_wait.wait(30)
            count += _interrupt_records([record], caller, reason, msg, affected_ids)
            continue
        if queued_snapshot is not None:
            count += 1
            affected_ids.add(delegation_id)
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
                     "interrupt_reason": reason,  # trusted stop category for retry classification
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
            affected_ids.add(delegation_id)
    if count:
        logger.info(msg, count, reason)
    return count



def _cancel_pending_retries_safely(caller: str, **selectors: Any) -> set:
    """Best-effort durable cancellation; a locked ledger must not block live stops."""
    from tools.delegation_resume import cancel_pending_retries, is_user_cancel_reason
    reason = str(selectors.pop("reason", ""))
    if not is_user_cancel_reason(reason):
        return set()
    try:
        return set(cancel_pending_retries(**selectors, reason=reason))
    except Exception as exc:
        logger.warning("%s: durable retry cancellation failed: %s", caller, exc, exc_info=True)
        return set()


def interrupt_all(reason: str = "shutdown", *, session_key: str = "", origin_ui_session_id: str = "",
                  parent_session_id: str = "", resumable: bool = False) -> int:
    """Signal every interruptible async delegation to stop (``/stop``, shutdown).

    Owed retries are cancelled everywhere. A claimed legacy recovery is revoked only for the
    stopping session named by the selectors; another session's claim is never touched. ``resumable``
    (gateway shutdown): a restart, not a person, stops them, so each child keeps a resumable record
    (``delegate_tool_resumable``)."""
    if resumable:
        from tools.delegate_tool_resumable import note_gateway_shutdown
        note_gateway_shutdown()
    with _records_lock:
        targets = [r for r in _records.values() if r.get("status") in _INTERRUPTIBLE_STATES]
    live_count = _interrupt_records(targets, "interrupt_all", reason, "Interrupted %d async delegation(s) (%s)")
    affected = _cancel_pending_retries_safely(
        "interrupt_all", all_sessions=True, session_key=session_key,
        origin_ui_session_id=origin_ui_session_id, parent_session_id=parent_session_id, reason=reason)
    return len(affected) if affected else live_count


def interrupt_delegation(delegation_id: str, reason: str = "stop_command") -> bool:
    """Cancel one queued delegation or signal one running delegation by handle."""
    with _records_lock:
        target = _records.get(delegation_id)
        targets = [target] if target is not None and target.get("status") in _INTERRUPTIBLE_STATES else []
    live_count = _interrupt_records(targets, "interrupt_delegation", reason, "Interrupted %d async delegation(s) (%s)")
    affected = _cancel_pending_retries_safely("interrupt_delegation", delegation_id=delegation_id, reason=reason)
    return bool(live_count or affected)


def interrupt_for_session(
    session_key: str = "", origin_ui_session_id: str = "", parent_session_id: str = "", reason: str = "session_end",
) -> int:
    """Signal interruptible async delegations owned by ONE ending session."""
    from tools.delegate_tool_auto_resume import supersede_for_session
    try:
        supersede_for_session(session_key=session_key, origin_ui_session_id=origin_ui_session_id,
                              parent_session_id=parent_session_id, reason=reason)
    except Exception:  # the interrupt itself must still run
        logger.warning("could not cancel automatic resumes for an ending session", exc_info=True)
    targets = _session_records(_INTERRUPTIBLE_STATES, session_key, origin_ui_session_id, parent_session_id)
    live_count = _interrupt_records(targets, "interrupt_for_session", reason,
                                    "Interrupted %d async delegation(s) for ending session (%s)")
    affected = _cancel_pending_retries_safely("interrupt_for_session", session_key=session_key,
                                              origin_ui_session_id=origin_ui_session_id,
                                              parent_session_id=parent_session_id, reason=reason)
    return len(affected) if affected else live_count


def refused_for_session(token: Any) -> bool:
    """Return whether a session token has been fenced against new dispatches."""
    with _records_lock:
        return token in _refused


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
        _refused.clear()
        _refused_by.clear()
    with _orphan_lock:
        _offered.clear()
        _startup_replayed.clear()
        _last_orphan_sweep.clear()
        _unresolved.clear()
        _reoffered_at.clear()
    with _held_lock:
        _held_copies.clear()
<<<<<<< HEAD
    # Defined below (imported from the ledger submodule); resolved at call time.
    with _SCHEMA_READY_LOCK:
        _SCHEMA_READY.clear()


from tools.async_delegation_ledger import (
    _SCHEMA_READY, _SCHEMA_READY_LOCK, _db_identity,
    _db_path, _connect, _initialize_schema, _transaction, _capture_routing_origin, _persist_dispatch,
    _persist_transition, _persist_transition_group, _durable_state, _log_pruned_ids,
    _prune_durable_records, _persist_completion, _attach_retry_note, _stamp_resume_availability,
    _outbox_event_id, _persist_outbox_event, _terminal_payload_matches, _reconcile_terminal_write,
    _outbox_event_id_from_claim, _claim_outbox_delivery, _update_outbox_delivery,
    _outbox_delivery_rows, _recovered_results, _owner_liveness,
)


from tools.async_delegation_recovery import (
    recover_abandoned_delegations, restore_undelivered_completions, _replay_pending,
    completion_copy_id, mark_copy_held, unmark_copy_held, _queued_completion_ids, _copy_may_be_live,
    _note_reoffers, sweep_orphaned_completions, maybe_sweep_orphaned_completions, _update_delivery,
    mark_completion_delivered, claim_completion_delivery, _hold_unresolved,
    reoffer_unresolved_completions, _resolve_terminal_ownership, resolve_event_ownership,
    is_interim_delegation_event, claim_event_delivery, release_completion_delivery,
    defer_completion_delivery, drop_completion_delivery, complete_completion_delivery,
    requeue_admitted_completion, complete_event_delivery, release_event_delivery,
    return_completion_offer, _event_delivery, supervisor_inventory, retirement_inventory,
    transfer_retirement_owner, refresh_retirement_route, get_durable_delegation, _json_object,
    failed_delegations_for_session,
)
||||||| dff103e05878
    with _SCHEMA_READY_LOCK:
        _SCHEMA_READY.clear()
=======
>>>>>>> 32dbc1dcda2255fd35969ec99cfc0170988c3eca
