"""``CoordinationRepository`` on SQLite: the compression lock, the turn lease, the hygiene streak, session handoffs and
the delegation ledger of the bound profile's ``state.db`` (the delivery-obligation verbs come from
``DeliveryObligationVerbs``), each verb one transaction of the store or of a ledger through the store holder.

The lease verbs resolve the conversation key and judge a holder's liveness with the conversation store's bodies. The
housekeeping repositories import the store's write-guard checks (``_guarded_ids``, ``_write_guards_reject``) from
here."""

from __future__ import annotations

import json
import logging
import threading
import time
import weakref
from collections.abc import Callable, Sequence
from typing import Any, Literal, Optional

from persistence import (
    AuthorityRefusal, DelegationId, JsonValue, PersistenceError, SessionId, StoreContention, StoreReplaced,
)
from persistence.core.errors import HandoffUnsettled
from persistence.sqlite._state.write import write_site, writer_mutex_until
from persistence.authority.coordination import (
    COMPLETION_DELIVERIES, DELEGATION_CLAIM_STALE_SECONDS, DELEGATION_FAILURE_WINDOW_SECONDS,
    DELEGATION_MAX_DELIVERY_ATTEMPTS, DELEGATION_MAX_DURABLE_PENDING, DELEGATION_MAX_RETAINED_TERMINAL,
    DELEGATION_ORPHAN_STALE_SECONDS, DELEGATION_REPLAY_MAX_AGE_SECONDS, DELEGATION_RETENTION_SECONDS,
    TURN_LEASE_SWEEP_GRACE_SECONDS, DelegationRecord, DelegationRecovery, DelegationReplacement, DelegationRetryChange, DeliveryState,
    FinishedDelegationRow,
    HandoffState, HandoffStatus, InflightDelegation, OrphanCompletion, OwnerStamp, PendingCompletion, PendingHandoff,
    RecordedOwner,
)
from persistence.authority.subagents import SubagentLimits, SubagentRecovery
from process_identity import holder_process_is_dead

from .._backend import SQLiteStore, register_repository, translated
from ..conversation._store.compression import _session_turn_lease_key_on_conn
from ..conversation._store.write_guards import _guarded_ids, _write_guards_reject
from ..gateway._delivery_ledger import LEDGER_PATIENCE_S, DeliveryObligationVerbs
from .subagents import KEEP_WAITING_UNITS_SQL, SubagentLedgerVerbs, recover_subagents

__all__ = ["SQLiteCoordinationRepository"]

# The log records keep main's logger names: the store's and the delegation tool's.
logger = logging.getLogger("hermes_state")
_delegation_logger = logging.getLogger("tools.async_delegation")

# What the compression-lock writes log and answer as their empty value: a store failure (translated) or a torn-down
# backend. A replaced store is neither: ``StoreReplaced`` propagates so the caller re-acquires the store.
_LOCK_FAILURES = (PersistenceError, RuntimeError)

# Per store holder: the writer connection ``_ledger_setup`` last ran on and the setup steps it ran there.
_setup_done: weakref.WeakKeyDictionary[Any, tuple[Any, set[Any]]] = weakref.WeakKeyDictionary()
_setup_lock = threading.Lock()

_HANDOFF_FAIL_SQL = "UPDATE sessions SET handoff_state = 'failed', handoff_error = ? WHERE "


def _require_settled(holder, conn, session_id: str) -> None:
    """Refuse a handoff while durable work owns ``session_id``'s runtime lineage: a queued, started or unknown
    admission, a live worker, or a live turn lease or compression lock, checked on the caller's write."""
    from persistence.sqlite._state.errors import SessionCompressionInProgressError, SessionTurnLeaseLostError

    from ..workers._runtime_workers import runtime_lineage
    from ._runtime_guards import require_idle
    try:
        require_idle(holder, conn, sorted(runtime_lineage(conn, session_id)))
    except (SessionTurnLeaseLostError, SessionCompressionInProgressError):
        raise HandoffUnsettled("session_busy") from None
    except AuthorityRefusal as exc:
        if exc.reason not in ("session_busy", "unknown_execution"):
            raise
        raise HandoffUnsettled(exc.reason) from None


def _claim_lease_row(conn, table: str, key_col: str, key: str, holder: str, now: float, expires_at: float,
                     stale) -> tuple[bool, Optional[str]]:
    """Single-transaction lease claim: DELETE a stale holder's row (``stale(holder,
    expires_at)``), INSERT OR IGNORE ours, then SELECT to confirm ownership (INSERT OR
    IGNORE gives no rowcount signal). Returns ``(acquired, reclaimed_holder)``."""
    reclaimed_holder = None
    row = conn.execute(f"SELECT holder, expires_at FROM {table} WHERE {key_col} = ?", (key,)).fetchone()
    if row is not None and stale(row["holder"], row["expires_at"]):
        conn.execute(f"DELETE FROM {table} WHERE {key_col} = ? AND holder = ?", (key, row["holder"]))
        reclaimed_holder = row["holder"]
    conn.execute(
        f"INSERT OR IGNORE INTO {table} ({key_col}, holder, acquired_at, expires_at) VALUES (?, ?, ?, ?)",
        (key, holder, now, expires_at))
    owner = conn.execute(f"SELECT holder FROM {table} WHERE {key_col} = ?", (key,)).fetchone()
    return owner is not None and owner["holder"] == holder, reclaimed_holder


# The in-memory offer of each returned completion stays with the caller.
def _replay_pending(conn, rows, now: float) -> tuple[PendingCompletion, ...]:
    """Each pending ``(delegation_id, event_json, completed_at, dispatched_at)`` row as a completion to offer, or
    terminally dropped past ``DELEGATION_REPLAY_MAX_AGE_SECONDS``."""
    offered = []
    for delegation_id, payload, completed_at, dispatched_at in rows:
        age_basis = completed_at or dispatched_at
        if age_basis and (now - age_basis) > DELEGATION_REPLAY_MAX_AGE_SECONDS:
            conn.execute("""UPDATE async_delegations SET delivery_state='dropped',
                          delivery_claim=NULL, delivery_claimed_at=NULL,
                          updated_at=?
                   WHERE delegation_id=? AND delivery_state='pending'""", (now, delegation_id))
            _delegation_logger.warning("Async delegation %s: pending completion is %.1fh old "
                                       "(cap %.1fh); terminally dropping the replay (result remains queryable).",
                                       delegation_id, (now - age_basis) / 3600.0,
                                       DELEGATION_REPLAY_MAX_AGE_SECONDS / 3600.0)
            continue
        offered.append(PendingCompletion(delegation_id=DelegationId(delegation_id), event=json.loads(payload),
                                         completed_at=completed_at, dispatched_at=dispatched_at))
    return tuple(offered)


def _past_replay_age(now: float, completed_at: Optional[float], dispatched_at: Optional[float]) -> bool:
    age_basis = completed_at or dispatched_at
    return bool(age_basis) and (now - age_basis) > DELEGATION_REPLAY_MAX_AGE_SECONDS


# A JSON object column as a dict: ``{}`` when empty, undecodable or not an object.
def _json_object(raw: Optional[str]) -> dict[str, Any]:
    try:
        value = json.loads(raw) if raw else {}
    except ValueError:
        return {}
    return value if isinstance(value, dict) else {}


_RECOVERY_SELECT = """
    SELECT d.*, COALESCE(d.event_json, (
        SELECT e.event_json FROM async_delegation_events e
        WHERE e.delegation_id=d.delegation_id AND e.event_kind='terminal_fallback'
        ORDER BY e.updated_at DESC, e.event_id LIMIT 1
    )) AS recovery_event
    FROM async_delegations d
"""


def _delegation_recovery_record(row) -> dict[str, Any]:
    return {
        "delegation_id": row["delegation_id"],
        "session_key": row["origin_session"] or "",
        "origin_ui_session_id": row["origin_ui_session_id"] or "",
        "parent_session_id": row["parent_session_id"] or "",
        "state": row["state"] or "",
        "dispatched_at": row["dispatched_at"],
        "completed_at": row["completed_at"],
        "task": _json_object(row["task_json"]),
        "result": _json_object(row["result_json"]),
        "origin_session_id": row["origin_session_id"] or "",
        "resume_state": row["resume_state"] or "none",
        "resume_attempts": int(row["resume_attempts"] or 0),
        "resume_claim": row["resume_claim"] or "",
        "auto_resume_state": row["auto_resume_state"] or "none",
        "auto_resume_claim": row["auto_resume_claim"] or "",
        "auto_resume_claimed_at": row["auto_resume_claimed_at"],
        "event": _json_object(row["recovery_event"]),
        "delivery_state": row["delivery_state"] or "",
        "retry_state": row["retry_state"] or "none",
        "retry_reason": row["retry_reason"] or "",
        "retry_due_at": row["retry_due_at"],
        "retry_notices": int(row["retry_notices"] or 0),
        "retry_claim": row["retry_claim"] or "",
        "retry_claimed_at": row["retry_claimed_at"],
        "retry_root": row["retry_root"] or row["delegation_id"],
        "retry_attempt": int(row["retry_attempt"] or 0),
        "retry_root_started_at": row["retry_root_started_at"] or row["dispatched_at"],
        "retry_replacement": row["retry_replacement"] or "",
    }


def _read_delegation_recovery(conn, delegation_id):
    row = conn.execute(_RECOVERY_SELECT + " WHERE d.delegation_id=?", (delegation_id,)).fetchone()
    return _delegation_recovery_record(row) if row is not None else None


def _validate_delegation_replacement(conn, replacement, origin_session, parent_session_id):
    prior = _read_delegation_recovery(conn, replacement.source_id)
    if prior is None or not (
        (origin_session and origin_session == prior["session_key"])
        or (parent_session_id and parent_session_id in (prior["parent_session_id"], prior["origin_session_id"]))
    ):
        return prior, "recovery claim is missing or belongs to another conversation"
    if prior["retry_state"] == "none":
        if prior["resume_state"] != "claimed" or not prior["resume_claim"] or prior["retry_replacement"]:
            return prior, "recovery claim is missing or already consumed"
        claim = prior["resume_claim"]
    elif prior["retry_state"] == "dispatching" and prior["retry_claim"]:
        claim = prior["retry_claim"]
    else:
        return prior, "recovery claim cancelled, superseded, or not taken"

    child_claims = conn.execute(
        """SELECT task_index, claim, replacement_delegation_id
           FROM async_delegation_child_recovery WHERE delegation_id=?""", (replacement.source_id,),
    ).fetchall()
    goals = prior["task"].get("goals")
    if (child_claims or (isinstance(goals, list) and len(goals) > 1)) and not replacement.child_indexes:
        return prior, "batch recovery requires a claimed child index"
    by_index = {row["task_index"]: row for row in child_claims}
    for index in replacement.child_indexes:
        child = by_index.get(index)
        if child is None or child["claim"] != claim or child["replacement_delegation_id"] is not None:
            return prior, "child recovery claim is missing, superseded, or already consumed"
    return prior, None


def _link_delegation_replacement(conn, replacement, prior, delegation_id, now):
    if replacement.child_indexes:
        conn.executemany(
            """UPDATE async_delegation_child_recovery SET replacement_delegation_id=?
               WHERE delegation_id=? AND task_index=?""",
            ((delegation_id, replacement.source_id, index) for index in replacement.child_indexes),
        )
        if prior["retry_state"] == "none":
            return
    elif prior["retry_state"] == "none":
        conn.execute("UPDATE async_delegations SET retry_replacement=?, updated_at=? WHERE delegation_id=?",
                     (delegation_id, now, replacement.source_id))
        return

    conn.execute("""UPDATE async_delegations SET retry_root=?, retry_attempt=?, retry_root_started_at=?
                    WHERE delegation_id=?""",
                 (prior["retry_root"], prior["retry_attempt"] + 1, prior["retry_root_started_at"], delegation_id))
    conn.execute("""UPDATE async_delegations SET retry_state='dispatched', retry_replacement=?, updated_at=?
                    WHERE delegation_id=? AND NOT EXISTS (
                        SELECT 1 FROM async_delegation_child_recovery
                        WHERE delegation_id=? AND replacement_delegation_id IS NULL)""",
                 (delegation_id, now, replacement.source_id, replacement.source_id))


def _rearm_delegation_recovery(conn, prior, source_id, delegation_id, now):
    if prior["retry_state"] == "none":
        conn.execute(
            """UPDATE async_delegations SET resume_state='none', resume_attempts=0,
               resume_claim=NULL, resume_claimed_at=NULL, retry_replacement=NULL, updated_at=?
               WHERE delegation_id=? AND (retry_replacement=? OR retry_replacement IS NULL)""",
            (now, source_id, delegation_id),
        )
        claim = prior["resume_claim"]
    else:
        conn.execute(
            """UPDATE async_delegations SET retry_state='scheduled', retry_due_at=?, retry_claim=NULL,
               retry_claimed_at=NULL, retry_replacement=NULL, updated_at=?
               WHERE delegation_id=? AND retry_state IN ('dispatching','dispatched')""",
            (now, now, source_id),
        )
        claim = prior["retry_claim"]
    conn.execute(
        """DELETE FROM async_delegation_child_recovery WHERE delegation_id=? AND claim=?
           AND (replacement_delegation_id=? OR replacement_delegation_id IS NULL)""",
        (source_id, claim, delegation_id),
    )


_RETRY_AFTER_DELIVERY_SQL = """ AND (d.event_json IS NULL OR d.delivery_state!='pending')
    AND NOT EXISTS (SELECT 1 FROM async_delegation_events e
                    WHERE e.delegation_id=d.delegation_id AND e.delivery_state='pending')"""


def _write_delegation_retry_change(conn, record, change: DelegationRetryChange, now):
    delegation_id = record["delegation_id"]
    conn.execute(
        """UPDATE async_delegations SET retry_state=?, retry_reason=?, retry_due_at=?, retry_claim=?,
           retry_claimed_at=?, retry_notices=?, updated_at=? WHERE delegation_id=?""",
        (change.state, change.reason, change.due_at, change.claim, change.claimed_at,
         change.notices, now, delegation_id),
    )
    if change.release_children and record["retry_claim"]:
        conn.execute(
            """DELETE FROM async_delegation_child_recovery WHERE delegation_id=? AND claim=?
               AND replacement_delegation_id IS NULL""",
            (delegation_id, record["retry_claim"]),
        )
    record.update(retry_state=change.state, retry_reason=change.reason or "", retry_due_at=change.due_at,
                  retry_claim=change.claim or "", retry_claimed_at=change.claimed_at, retry_notices=change.notices)


def _exhausted_warning(delegation_id: str) -> None:
    _delegation_logger.warning("Async delegation %s exhausted its %d delivery attempts; "
                               "marking terminally dropped (result remains queryable).",
                               delegation_id, DELEGATION_MAX_DELIVERY_ATTEMPTS)


class SQLiteCoordinationRepository(DeliveryObligationVerbs, SubagentLedgerVerbs):
    """Compression lock, turn lease, hygiene streak, handoffs and the two ledgers of the bound profile's
    ``state.db``, the delegation ledger with its subagent records. The ledger verbs run through the registered writer
    with the ledgers' 10 s lock wait."""

    def __init__(self, store: SQLiteStore) -> None:
        self._store = store

    def _ledger_write(self, fn):
        """One ledger transaction on the store's writer (BEGIN IMMEDIATE) with the ledgers' lock wait."""
        return self._store.holder()._execute_write(fn, patience_s=LEDGER_PATIENCE_S)

    def _ledger_read(self, read):
        """``read(conn)`` on a read connection with the ledgers' lock wait."""
        with self._store.holder()._read_ctx_with_timeout(LEDGER_PATIENCE_S) as conn:
            return read(conn)

    def _ledger_setup(self, step) -> None:
        """``step(conn)`` (a ledger's table step) once per writer connection of the store, outside any transaction,
        waiting out lock contention for the ledgers' lock wait. A writer reopened on a file restored in place runs it
        again."""
        holder = self._store.holder()
        conn, done = _setup_done.get(holder, (None, set()))
        if conn is holder._conn and step in done:
            return
        deadline = time.monotonic() + LEDGER_PATIENCE_S
        # Both locks are taken against the ledgers' lock wait, as every write takes the writer lock: a long write
        # elsewhere refuses the setup with ``StoreContention`` instead of holding every ledger verb behind it.
        if not _setup_lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
            raise StoreContention("database is locked (ledger setup waited past the ledgers' lock wait)")
        try:
            while True:
                try:
                    with translated(), writer_mutex_until(holder._lock, deadline, db=holder, site=write_site(step)):
                        holder._raise_if_db_replaced()
                        if holder._conn is None:
                            holder._reopen_after_close_locked(context="write")
                        conn, done = _setup_done.get(holder, (None, set()))
                        if conn is not holder._conn:
                            conn, done = holder._conn, set()
                            _setup_done[holder] = (conn, done)
                        if step not in done:
                            step(conn)
                            done.add(step)
                    return
                except StoreContention:
                    if not holder._sleep_before_write_retry(deadline, LEDGER_PATIENCE_S):
                        raise
        finally:
            _setup_lock.release()

    def _write_sql_logged(self, op: str, session_id: str, sql: str, params) -> None:
        """``_write_sql`` that logs a store failure instead of raising."""
        try:
            with translated():
                self._store.holder()._write_sql(sql, params)
        except StoreReplaced:
            raise
        except _LOCK_FAILURES as exc:
            logger.warning("%s(%s) failed: %s", op, session_id, exc)

    # ── compression lock ────────────────────────────────────────────────────

    def try_acquire_compression_lock(self, *, session_id: SessionId, holder: str, ttl_seconds: float = 300.0) -> bool:
        if not session_id:
            return False
        now = time.time()
        expires_at = now + ttl_seconds
        def _do(conn):
            return _claim_lease_row(
                conn, "compression_locks", "session_id", session_id, holder, now, expires_at,
                lambda h, e: e < now or holder_process_is_dead(h))

        try:
            with translated():
                acquired, reclaimed_holder = self._store.holder()._execute_write(_do)
        except StoreReplaced:
            raise
        except _LOCK_FAILURES as exc:
            # False makes the caller skip compression: safe when the lock subsystem is broken.
            logger.warning("try_acquire_compression_lock(%s) failed: %s", session_id, exc)
            return False
        if reclaimed_holder:
            logger.warning("Reclaimed stale compression lock for session=%s (holder=%s)",
                           session_id, reclaimed_holder)
        return bool(acquired)

    def refresh_compression_lock(self, *, session_id: SessionId, holder: str, ttl_seconds: float = 300.0) -> bool:
        """Ownership is decided by ``holder`` alone, not ``expires_at``: a live owner whose refresher stalled past
        its TTL revives its still-unclaimed row; a reclaim cannot interleave because SQLite serialises writes."""
        if not session_id or not holder:
            return False
        expires_at = time.time() + ttl_seconds
        try:
            with translated():
                return self._store.holder()._write_rowcount(
                    "UPDATE compression_locks SET expires_at = ? WHERE session_id = ? AND holder = ?",
                    (expires_at, session_id, holder)) > 0
        except StoreReplaced:
            raise
        except _LOCK_FAILURES as exc:
            logger.warning("refresh_compression_lock(%s) failed: %s", session_id, exc)
            return False

    def release_compression_lock(self, *, session_id: SessionId, holder: str) -> None:
        if not session_id:
            return
        self._write_sql_logged(
            "release_compression_lock", session_id,
            "DELETE FROM compression_locks WHERE session_id = ? AND holder = ?",
            (session_id, holder))

    def get_compression_lock_holder(self, *, session_id: SessionId) -> Optional[str]:
        if not session_id:
            return None
        with translated():
            row = self._store.holder()._read_one(
                "SELECT holder FROM compression_locks WHERE session_id = ? AND expires_at >= ?",
                (session_id, time.time()))
        return None if row is None else row[0]

    # ── turn lease ──────────────────────────────────────────────────────────

    def try_acquire_turn_lease(self, *, session_id: SessionId, holder: str, ttl_seconds: float = 300.0,
                               patience_s: Optional[float] = None) -> bool:
        if not session_id or not holder:
            return False
        now = time.time()
        expires_at = now + max(0.1, float(ttl_seconds))
        store = self._store.holder()  # resolved before the write: a re-resolve inside it can wait on a sweep behind this lock
        def _do(conn):
            # Sweep rows that expired long ago: a holder that died without releasing leaves its row behind, and
            # nothing else revisits a conversation nobody resumes. The grace keeps the recent expiries a live
            # owner can renew from a starved refresher.
            conn.execute("DELETE FROM session_turn_leases WHERE expires_at < ?",
                         (now - TURN_LEASE_SWEEP_GRACE_SECONDS,))
            conversation_id = _session_turn_lease_key_on_conn(store, conn, session_id)
            return _claim_lease_row(
                conn, "session_turn_leases", "conversation_id", conversation_id, holder, now, expires_at,
                lambda h, e: float(e) <= now or holder_process_is_dead(h),
            )[0]
        with translated():
            return bool(store._execute_write(_do, patience_s=patience_s))

    def refresh_turn_lease(self, *, session_id: SessionId, holder: str, ttl_seconds: float = 300.0,
                           patience_s: Optional[float] = None) -> bool:
        """``expires_at`` is stamped before the write-lock wait, so a caller timestamp taken before this call never
        exceeds it."""
        if not session_id or not holder:
            return False
        expires_at = time.time() + max(0.1, float(ttl_seconds))
        store = self._store.holder()
        def _do(conn):
            conversation_id = _session_turn_lease_key_on_conn(store, conn, session_id)
            return conn.execute(
                "UPDATE session_turn_leases SET expires_at = ? "
                "WHERE conversation_id = ? AND holder = ?", (expires_at, conversation_id, holder),
            ).rowcount > 0
        with translated():
            return bool(store._execute_write(_do, patience_s=patience_s))

    def release_turn_lease(self, *, session_id: SessionId, holder: str) -> None:
        if not session_id or not holder:
            return
        store = self._store.holder()
        def _do(conn):
            conversation_id = _session_turn_lease_key_on_conn(store, conn, session_id)
            conn.execute(
                "DELETE FROM session_turn_leases WHERE conversation_id = ? AND holder = ?",
                (conversation_id, holder))
        with translated():
            store._execute_write(_do)

    def turn_lease_expires_at(self, *, session_id: SessionId, holder: str) -> Optional[float]:
        if not session_id or not holder:
            return None
        store = self._store.holder()
        with translated(), store._read_ctx() as conn:
            row = conn.execute(
                "SELECT expires_at FROM session_turn_leases WHERE conversation_id = ? AND holder = ?",
                (_session_turn_lease_key_on_conn(store, conn, session_id), holder),
            ).fetchone()
        return float(row[0]) if row else None

    # ── hygiene streak ──────────────────────────────────────────────────────

    def increment_hygiene_failure_streak(self, *, session_key: str) -> int:
        if not session_key:
            return 1
        def _do(conn):
            conn.execute(
                """INSERT INTO gateway_hygiene_state (session_key, failure_streak)
                   VALUES (?, 1)
                   ON CONFLICT(session_key) DO UPDATE SET
                       failure_streak = gateway_hygiene_state.failure_streak + 1""",
                (session_key,),
            )
            row = conn.execute(
                "SELECT failure_streak FROM gateway_hygiene_state WHERE session_key = ?", (session_key,),
            ).fetchone()
            return int(row[0])
        with translated():
            return self._store.holder()._execute_write(_do)

    def reset_hygiene_failure_streak(self, *, session_key: str) -> None:
        if not session_key:
            return
        with translated():
            self._store.holder()._write_sql("DELETE FROM gateway_hygiene_state WHERE session_key = ?", (session_key,))

    # ── handoffs ────────────────────────────────────────────────────────────

    def request_handoff(self, *, session_id: SessionId, platform: str, require_settled: bool = False) -> bool:
        holder = self._store.holder()
        def _do(conn):
            if require_settled:
                _require_settled(holder, conn, session_id)
            return conn.execute(
                "UPDATE sessions SET handoff_state = 'pending', handoff_platform = ?, handoff_error = NULL "
                "WHERE id = ? AND (handoff_state IS NULL OR handoff_state IN ('completed', 'failed'))",
                (platform, session_id)).rowcount
        with translated():
            return holder._execute_write(_do) > 0

    def check_handoff_settled(self, *, session_id: SessionId) -> None:
        holder = self._store.holder()
        with translated():
            holder._execute_write(lambda conn: _require_settled(holder, conn, session_id))

    def claim_handoff(self, *, session_id: SessionId) -> bool:
        with translated():
            return self._store.holder()._write_rowcount(
                "UPDATE sessions SET handoff_state = 'running' WHERE id = ? AND handoff_state = 'pending'",
                (session_id,),
            ) > 0

    def complete_handoff(self, *, session_id: SessionId) -> None:
        with translated():
            self._store.holder()._write_sql(
                "UPDATE sessions SET handoff_state = 'completed', handoff_error = NULL WHERE id = ?",
                (session_id,))

    def fail_handoff(self, *, session_id: SessionId, error: str,
                     only_states: Optional[tuple[HandoffStatus, ...]] = None) -> bool:
        """``only_states`` makes the write a compare-and-swap on ``handoff_state``: a waiter that gives up passes
        ``(pending,)``, since the watcher owns the terminal state of a row it claimed."""
        states = tuple(only_states) if only_states else ()
        sql = _HANDOFF_FAIL_SQL + "id = ?" + (
            f" AND handoff_state IN ({', '.join('?' for _ in states)})" if states else "")
        with translated():
            return self._store.holder()._write_rowcount(sql, (error[:500], session_id, *states)) > 0

    def reclaim_stale_running_handoffs(self, *, error: str) -> tuple[SessionId, ...]:
        """A ``running`` row at watcher startup belongs to a previous gateway that died mid-dispatch; it is failed,
        not re-queued, because the dead gateway may already have dispatched the turn."""
        def _do(conn):
            cur = conn.execute("SELECT id FROM sessions WHERE handoff_state = 'running'")
            ids = [r[0] for r in cur.fetchall()]
            if ids:
                conn.execute(_HANDOFF_FAIL_SQL + "handoff_state = 'running'", (error[:500],))
            return ids
        try:
            with translated():
                ids = self._store.holder()._execute_write(_do) or []
        except Exception:
            # Swallow but never silently: a persistently failing reclaim leaves poisonous 'running' rows.
            logger.warning(
                "reclaim_stale_running_handoffs failed; stranded 'running' "
                "handoff rows (if any) were left in place", exc_info=True)
            return ()
        return tuple(SessionId(sid) for sid in ids)

    def get_handoff(self, *, session_id: SessionId) -> Optional[HandoffState]:
        try:
            row = self._store.holder()._read_one(
                "SELECT handoff_state, handoff_platform, handoff_error FROM sessions WHERE id = ?",
                (session_id,))
            if not row or row["handoff_state"] is None:
                return None
            return HandoffState(state=HandoffStatus(row["handoff_state"]), platform=row["handoff_platform"],
                                error=row["handoff_error"])
        except Exception:  # health: allow BLE001 -- handoff state is advisory; a failed read answers no handoff
            return None

    def list_pending_handoffs(self) -> tuple[PendingHandoff, ...]:
        try:
            rows = self._store.holder()._read_all(
                "SELECT s.*, COALESCE(sp.prompt, s.system_prompt) AS _system_prompt_resolved FROM sessions s "
                "LEFT JOIN system_prompts sp ON sp.hash = s.system_prompt_hash "
                "WHERE s.handoff_state = 'pending' "
                "ORDER BY s.started_at ASC")
            return tuple(PendingHandoff(session_id=SessionId(r["id"]), platform=r["handoff_platform"],
                                        title=r["title"], started_at=float(r["started_at"])) for r in rows)
        except Exception:  # health: allow BLE001 -- the watcher polls again; a failed read answers none pending
            return ()

    def has_pending_handoffs(self) -> bool:
        with translated():
            return self._store.holder()._read_one(
                "SELECT 1 FROM sessions WHERE handoff_state = 'pending' LIMIT 1") is not None

    # ── delegation ledger ───────────────────────────────────────────────────

    # A dispatch replaces any earlier row of its id: running, its delivery pending, no attempts spent.
    def record_delegation_dispatch(self, *, delegation_id: DelegationId, origin_session: str,
                                   origin_ui_session_id: str, parent_session_id: Optional[SessionId],
                                   dispatched_at: float, owner: OwnerStamp, task: JsonValue,
                                   origin_session_id: str = '', retry_armed: bool = False,
                                   replacement: Optional[DelegationReplacement] = None,
                                   recovery_refusal: Optional[Callable[[dict[str, Any], float], tuple[Optional[str], bool]]] = None
                                   ) -> Optional[str]:
        now = time.time()

        def _do(conn):
            prior = None
            if replacement is not None:
                prior, refusal = _validate_delegation_replacement(
                    conn, replacement, origin_session, parent_session_id,
                )
                if refusal:
                    return refusal
                refusal, expired = recovery_refusal(prior, now) if recovery_refusal else (None, False)
                if refusal:
                    if expired:
                        _write_delegation_retry_change(conn, prior, DelegationRetryChange(
                            "terminal", refusal, None, None, None, prior["retry_notices"], release_children=True,
                        ), now)
                    return refusal
            statement = "INSERT" if replacement is not None else "INSERT OR REPLACE"
            conn.execute(f"""{statement} INTO async_delegations
                   (delegation_id, origin_session, origin_ui_session_id,
                    parent_session_id, state, dispatched_at, updated_at,
                    delivery_state, delivery_attempts, owner_pid,
                    owner_started_at, task_json, origin_session_id)
                   VALUES (?, ?, ?, ?, 'running', ?, ?, 'pending', 0, ?, ?, ?, ?)""",
                (delegation_id, origin_session, origin_ui_session_id,
                 parent_session_id, dispatched_at, now, owner.pid, owner.started_at,
                 json.dumps(task), origin_session_id))
            if retry_armed:
                conn.execute(
                    """UPDATE async_delegations SET retry_state='armed',
                       retry_root=delegation_id, retry_root_started_at=dispatched_at WHERE delegation_id=?""",
                    (delegation_id,),
                )
            if replacement is not None:
                _link_delegation_replacement(conn, replacement, prior, delegation_id, now)
            return None

        with translated():
            return self._ledger_write(_do)

    def release_failed_replacement(
        self, delegation_id: DelegationId, replacement: DelegationReplacement, *,
        origin_session: str, parent_session_id: Optional[SessionId],
    ) -> bool:
        """Re-arm an owned claim only while the failed dispatch is absent and the claim remains unspent."""
        now = time.time()

        def write(conn):
            if _read_delegation_recovery(conn, delegation_id) is not None:
                return False
            prior, refusal = _validate_delegation_replacement(conn, replacement, origin_session, parent_session_id)
            if refusal:
                return False
            _rearm_delegation_recovery(conn, prior, replacement.source_id, delegation_id, now)
            return True

        with translated():
            return self._ledger_write(write)

    def rollback_unscheduled_replacement(self, delegation_id: DelegationId, replacement: DelegationReplacement) -> None:
        now = time.time()

        def write(conn):
            prior = _read_delegation_recovery(conn, replacement.source_id)
            linked = prior is not None and (prior["retry_replacement"] == delegation_id or conn.execute(
                """SELECT 1 FROM async_delegation_child_recovery
                   WHERE delegation_id=? AND replacement_delegation_id=? LIMIT 1""",
                (replacement.source_id, delegation_id),
            ).fetchone())
            if not linked or conn.execute(
                "DELETE FROM async_delegations WHERE delegation_id=? AND state='running' AND result_json IS NULL",
                (delegation_id,),
            ).rowcount != 1:
                return
            _rearm_delegation_recovery(conn, prior, replacement.source_id, delegation_id, now)

        with translated():
            self._ledger_write(write)

    def prune_delegation_ledger(self) -> None:
        """Bound terminal history, preferring delivered records for deletion. A unit a waiting child reports on is
        kept out of the retention and the terminal cap (``KEEP_WAITING_UNITS_SQL``), not the pending cap."""
        cutoff = time.time() - DELEGATION_RETENTION_SECONDS
        retry_settled = " AND retry_state IN ('none','cancelled','dispatched','reported')"
        keep = KEEP_WAITING_UNITS_SQL + retry_settled
        def _do(conn):
            conn.execute(
                "DELETE FROM async_delegations WHERE delivery_state='delivered' AND updated_at < ?" + keep, (cutoff,))
            terminal_count = conn.execute(
                "SELECT COUNT(*) FROM async_delegations WHERE state!='running'" + keep
            ).fetchone()[0]
            if terminal_count > DELEGATION_MAX_RETAINED_TERMINAL:
                conn.execute(f"""DELETE FROM async_delegations WHERE delegation_id IN (
                         SELECT delegation_id FROM async_delegations
                         WHERE state!='running'{keep}
                         ORDER BY CASE delivery_state WHEN 'delivered' THEN 0 ELSE 1 END,
                                  updated_at ASC LIMIT ?
                       )""", (terminal_count - DELEGATION_MAX_RETAINED_TERMINAL,))
            pending_count = conn.execute("""SELECT COUNT(*) FROM async_delegations
                   WHERE state!='running' AND delivery_state='pending'""" + retry_settled).fetchone()[0]
            if pending_count > DELEGATION_MAX_DURABLE_PENDING:
                conn.execute(f"""DELETE FROM async_delegations WHERE delegation_id IN (
                         SELECT delegation_id FROM async_delegations
                         WHERE state!='running' AND delivery_state='pending'{retry_settled}
                         ORDER BY updated_at ASC LIMIT ?
                       )""", (pending_count - DELEGATION_MAX_DURABLE_PENDING,))
        with translated():
            self._ledger_write(_do)

    # The finished state, event and result; a ``pending`` completion then waits for delivery, a ``delivered`` one
    # (every child of the unit waits on a promised automatic resume) is recorded but never offered.
    def record_delegation_completion(self, *, delegation_id: DelegationId, state: str, completed_at: float,
                                     event: JsonValue, result: JsonValue,
                                     delivery: Literal["pending", "delivered"] = "pending") -> None:
        if delivery not in COMPLETION_DELIVERIES:
            raise AuthorityRefusal("invalid_params", field="delivery")
        now = time.time()
        def _do(conn):
            conn.execute("""UPDATE async_delegations SET state=?, completed_at=?, updated_at=?,
                   event_json=?, result_json=?, delivery_state=?
                   WHERE delegation_id=?""",
                (state, completed_at, now,
                 json.dumps(event), json.dumps(result), delivery, delegation_id))
        with translated():
            self._ledger_write(_do)

    def record_unit_child(self, *, delegation_id: DelegationId, entry: JsonValue) -> None:
        """Merge one finished child of a still-running unit into the stored partial by ``task_index``; the caller
        redacts ``entry``, and the stored partial holds only entries redacted that way."""
        def _do(conn):
            row = conn.execute("SELECT result_json FROM async_delegations WHERE delegation_id=? AND state='running'",
                               (delegation_id,)).fetchone()
            if row is None:
                return
            partial = json.loads(row[0] or "{}") or {}
            results = [r for r in partial.get("results") or [] if r.get("task_index") != entry.get("task_index")]
            results.append(entry)
            conn.execute("UPDATE async_delegations SET result_json=?, updated_at=? WHERE delegation_id=? AND state='running'",
                         (json.dumps({"results": results, "partial": True}), time.time(), delegation_id))
        with translated():
            self._ledger_write(_do)

    # Each recovery lands only while its row keeps the state and ``updated_at`` the caller read, after the subagent
    # records still held by their dead owners; the caller probes owners and builds each record and event outside the
    # transaction.
    def recover_abandoned_delegations(self, *, recoveries: Sequence[DelegationRecovery],
                                      subagent_recoveries: Sequence[SubagentRecovery] = (),
                                      limits: Optional[SubagentLimits] = None) -> int:
        now = time.time()
        def _do(conn):
            recover_subagents(conn, subagent_recoveries, limits, now)
            recovered = 0
            for recovery in recoveries:
                cursor = conn.execute("""UPDATE async_delegations SET state='unknown', completed_at=?,
                       updated_at=?, event_json=?, result_json=?, delivery_state=?
                       WHERE delegation_id=? AND state=? AND updated_at=?""",
                    (recovery.completed_at, now, json.dumps(recovery.event), json.dumps(recovery.result),
                     recovery.delivery, recovery.delegation_id, recovery.expected_state,
                     recovery.expected_updated_at))
                recovered += cursor.rowcount
            return recovered
        with translated():
            return self._ledger_write(_do)

    # Every finished, undelivered completion to replay, oldest first; one past the replay age cap is dropped.
    def take_replayable_completions(self) -> tuple[PendingCompletion, ...]:
        now = time.time()
        def _do(conn):
            rows = conn.execute("""SELECT delegation_id, event_json, completed_at, dispatched_at
                   FROM async_delegations
                   WHERE state != 'running' AND delivery_state='pending' AND event_json IS NOT NULL
                   ORDER BY completed_at, delegation_id""").fetchall()
            return _replay_pending(conn, rows, now)
        with translated():
            return self._ledger_write(_do)

    # The owner probe and the caller's lost copies arrive as ``dead_owners`` and ``lost_ids``; the offer runs in
    # the caller after the commit.
    def sweep_orphaned_completions(self, *, now: float, exclude_ids: frozenset[DelegationId] = frozenset(),
                                   dead_owners: frozenset[RecordedOwner],
                                   lost_ids: frozenset[DelegationId] = frozenset()) -> tuple[PendingCompletion, ...]:
        def _do(conn):
            rows = conn.execute("""SELECT delegation_id, event_json, completed_at, dispatched_at,
                          owner_pid, owner_started_at, delivery_attempts
                   FROM async_delegations
                   WHERE state!='running' AND delivery_state='pending'
                     AND event_json IS NOT NULL AND updated_at < ?
                     AND (delivery_claim IS NULL OR delivery_claimed_at < ?)
                   ORDER BY completed_at, delegation_id""",
                (now - DELEGATION_ORPHAN_STALE_SECONDS, now - DELEGATION_CLAIM_STALE_SECONDS)).fetchall()
            orphans = []
            for delegation_id, payload, completed_at, dispatched_at, pid, started, attempts in rows:
                if delegation_id not in lost_ids and (
                        delegation_id in exclude_ids or RecordedOwner(pid=pid, started_at=started) not in dead_owners):
                    continue
                if (attempts or 0) >= DELEGATION_MAX_DELIVERY_ATTEMPTS:
                    # Its last claimant died holding the final attempt; converge like release_delivery.
                    conn.execute("""UPDATE async_delegations SET delivery_state='dropped',
                                  delivery_claim=NULL, delivery_claimed_at=NULL, updated_at=?
                           WHERE delegation_id=? AND delivery_state='pending'""", (now, delegation_id))
                    _exhausted_warning(delegation_id)
                    continue
                orphans.append((delegation_id, payload, completed_at, dispatched_at))
            return _replay_pending(conn, orphans, now)
        with translated():
            return self._ledger_write(_do)

    def _update_delivery(self, sql: str, params: tuple) -> bool:
        """Run one UPDATE on the ledger; True iff exactly one row changed."""
        def _do(conn):
            return conn.execute(sql, params).rowcount == 1
        with translated():
            return self._ledger_write(_do)

    def mark_delegation_delivered(self, *, delegation_id: DelegationId) -> bool:
        now = time.time()
        return self._update_delivery(
            """UPDATE async_delegations SET delivery_state='delivered', delivered_at=?, updated_at=?
               WHERE delegation_id=? AND delivery_state!='delivered'""", (now, now, delegation_id))

    # Claim a pending completion for delivery (a stale claim is taken over); each claim spends one attempt.
    def claim_delivery(self, *, delegation_id: DelegationId, claim_id: str) -> bool:
        now = time.time()
        def _do(conn):
            row = conn.execute(
                "SELECT delivery_state FROM async_delegations WHERE delegation_id=?", (delegation_id,)).fetchone()
            if row is None:
                return True  # legacy event created before durable dispatch
            cur = conn.execute("""UPDATE async_delegations SET delivery_claim=?, delivery_claimed_at=?,
                          delivery_attempts=delivery_attempts+1, updated_at=?
                   WHERE delegation_id=? AND delivery_state='pending'
                     AND (delivery_claim IS NULL OR delivery_claimed_at < ?)""",
                (claim_id, now, now, delegation_id, now - DELEGATION_CLAIM_STALE_SECONDS))
            return cur.rowcount == 1
        with translated():
            return self._ledger_write(_do)

    def release_delivery(self, *, delegation_id: DelegationId, claim_id: str) -> bool:
        """Attempts are counted at claim time; once the budget is spent the row converges to terminal ``dropped``."""
        now = time.time()
        def _do(conn):
            capped = conn.execute("""UPDATE async_delegations SET delivery_state='dropped',
                          delivery_claim=NULL, delivery_claimed_at=NULL, updated_at=?
                   WHERE delegation_id=? AND delivery_state='pending'
                     AND delivery_claim=? AND delivery_attempts>=?""",
                (now, delegation_id, claim_id, DELEGATION_MAX_DELIVERY_ATTEMPTS))
            if capped.rowcount == 1:
                _exhausted_warning(delegation_id)
                return True
            cur = conn.execute("""UPDATE async_delegations SET delivery_claim=NULL,
                          delivery_claimed_at=NULL, updated_at=?
                   WHERE delegation_id=? AND delivery_state='pending'
                     AND delivery_claim=?""", (now, delegation_id, claim_id))
            return cur.rowcount == 1
        with translated():
            return self._ledger_write(_do)

    # Give the claim back and refund the attempt it spent.
    def defer_delivery(self, *, delegation_id: DelegationId, claim_id: str) -> bool:
        return self._update_delivery("""UPDATE async_delegations SET delivery_claim=NULL,
                      delivery_claimed_at=NULL, delivery_attempts=MAX(0, delivery_attempts-1),
                      updated_at=?
               WHERE delegation_id=? AND delivery_state='pending' AND delivery_claim=?""",
            (time.time(), delegation_id, claim_id))

    def drop_delivery(self, *, delegation_id: DelegationId, claim_id: str) -> bool:
        return self._update_delivery("""UPDATE async_delegations SET delivery_state='dropped',
                      updated_at=?, delivery_claim=NULL,
                      delivery_claimed_at=NULL
               WHERE delegation_id=? AND delivery_state='pending'
                 AND delivery_claim=?""", (time.time(), delegation_id, claim_id))

    def complete_delivery(self, *, delegation_id: DelegationId, claim_id: str) -> bool:
        now = time.time()
        return self._update_delivery("""UPDATE async_delegations SET delivery_state='delivered',
                      delivered_at=?, updated_at=?, delivery_claim=NULL,
                      delivery_claimed_at=NULL
               WHERE delegation_id=? AND delivery_state='pending'
                 AND delivery_claim=?""", (now, now, delegation_id, claim_id))

    # A delivered completion goes back to pending (dropped once its attempts are spent); the caller offers the
    # returned completion after the commit.
    def reopen_admitted_completion(self, *, delegation_id: DelegationId) -> Optional[PendingCompletion]:
        now = time.time()
        def _do(conn):
            exhausted = conn.execute("""UPDATE async_delegations SET delivery_state='dropped', updated_at=?
                   WHERE delegation_id=? AND delivery_state='delivered' AND delivery_attempts>=?""",
                (now, delegation_id, DELEGATION_MAX_DELIVERY_ATTEMPTS))
            if exhausted.rowcount == 1:
                _exhausted_warning(delegation_id)
                return None
            reopened = conn.execute("""UPDATE async_delegations SET delivery_state='pending',
                          delivered_at=NULL, updated_at=?
                   WHERE delegation_id=? AND delivery_state='delivered' AND event_json IS NOT NULL""",
                (now, delegation_id))
            if reopened.rowcount != 1:
                return None
            rows = conn.execute("""SELECT delegation_id, event_json, completed_at, dispatched_at
                   FROM async_delegations WHERE delegation_id=?""", (delegation_id,)).fetchall()
            offered = _replay_pending(conn, rows, now)
            return offered[0] if offered else None
        with translated():
            return self._ledger_write(_do)

    # The cleanup of a dispatch whose submit was never scheduled.
    def delete_unscheduled_delegation(self, *, delegation_id: DelegationId) -> None:
        def _do(conn):
            conn.execute("DELETE FROM async_delegations WHERE delegation_id=?", (delegation_id,))
        with translated():
            self._ledger_write(_do)

    # The caller redacts ``result``.
    def delegation_recovery_record(
        self, delegation_id: DelegationId, *, include_children: bool = False,
    ) -> Optional[dict[str, Any]]:
        def read(conn):
            record = _read_delegation_recovery(conn, delegation_id)
            if record is not None and include_children:
                record["child_replacements"] = dict(conn.execute(
                    """SELECT task_index, replacement_delegation_id FROM async_delegation_child_recovery
                       WHERE delegation_id=?""", (delegation_id,),
                ))
            return record

        with translated():
            return self._ledger_read(read)

    def list_delegation_boot_candidates(
        self, *, stale_before: float, limit: int, eligible: Callable[[dict[str, Any]], bool],
    ) -> tuple[dict[str, Any], ...]:
        if limit <= 0:
            return ()

        def read(conn):
            rows = conn.execute(
                _RECOVERY_SELECT + """ WHERE d.state IN ('unknown', 'interrupted', 'stalled')
                    AND d.retry_state='none' AND d.parent_session_id != ''
                    AND (d.origin_session != '' OR d.origin_session_id != '')
                    AND (d.auto_resume_state='none' OR
                         (d.auto_resume_state='claimed' AND d.auto_resume_claimed_at < ?))
                  ORDER BY d.updated_at, d.delegation_id""", (stale_before,))
            matches = []
            for row in rows:
                record = _delegation_recovery_record(row)
                if eligible(record):
                    matches.append(record)
                    if len(matches) >= limit:
                        break
            return tuple(matches)

        with translated():
            return self._ledger_read(read)

    def claim_delegation_recovery(
        self, delegation_id: DelegationId, *, claim: str, now: float,
        eligible: Callable[[dict[str, Any]], Optional[str]],
        boot_stale_before: Optional[float] = None,
        children: Optional[Callable[[dict[str, Any]], Sequence[int]]] = None,
    ) -> tuple[Optional[dict[str, Any]], Optional[str], list[int]]:
        def write(conn):
            record = _read_delegation_recovery(conn, delegation_id)
            if record is None:
                return None, "no_such_delegation", []
            reason = eligible(record)
            if reason is not None:
                return record, reason, []
            if boot_stale_before is not None:
                claimed = conn.execute(
                    """UPDATE async_delegations SET auto_resume_state='claimed', auto_resume_claim=?,
                       auto_resume_claimed_at=?, updated_at=?
                       WHERE delegation_id=? AND retry_state='none'
                         AND (auto_resume_state='none' OR
                              (auto_resume_state='claimed' AND auto_resume_claimed_at < ?))""",
                    (claim, now, now, delegation_id, boot_stale_before),
                ).rowcount
                if claimed != 1:
                    return record, "already_triggered", []
                record.update(auto_resume_state="claimed", auto_resume_claim=claim, auto_resume_claimed_at=now)
                return record, None, []

            claimed = conn.execute(
                """UPDATE async_delegations SET resume_state='claimed', resume_attempts=resume_attempts+1,
                   resume_claim=?, resume_claimed_at=?, updated_at=?
                   WHERE delegation_id=? AND resume_state='none' AND resume_attempts=0""",
                (claim, now, now, delegation_id),
            ).rowcount
            if claimed != 1:
                return record, "already_claimed", []
            record.update(resume_state="claimed", resume_attempts=1, resume_claim=claim)
            indexes = []
            if children is not None:
                for index in children(record):
                    if conn.execute(
                        """INSERT OR IGNORE INTO async_delegation_child_recovery
                           (delegation_id, task_index, claim, claimed_at) VALUES (?, ?, ?, ?)""",
                        (delegation_id, index, claim, now),
                    ).rowcount == 1:
                        indexes.append(index)
            return record, None, indexes

        with translated():
            return self._ledger_write(write)

    def settle_delegation_boot_claim(
        self, delegation_id: DelegationId, *, claim: str, delivered: bool, now: float,
    ) -> bool:
        if delivered:
            return self._update_delivery(
                """UPDATE async_delegations SET auto_resume_state='delivered', updated_at=?
                   WHERE delegation_id=? AND auto_resume_state='claimed' AND auto_resume_claim=?""",
                (now, delegation_id, claim),
            )
        return self._update_delivery(
            """UPDATE async_delegations SET auto_resume_state='none', auto_resume_claim=NULL,
               auto_resume_claimed_at=NULL, updated_at=?
               WHERE delegation_id=? AND auto_resume_state='claimed' AND auto_resume_claim=?""",
            (now, delegation_id, claim),
        )

    def list_delegation_retries(
        self, *, states: Sequence[str], limit: int, newest_first: bool = False,
        eligible: Optional[Callable[[dict[str, Any]], bool]] = None,
        after_delivery: bool = False,
        ready_at: Optional[tuple[float, float, float]] = None,
        terminal_armed: bool = False,
        dropped_success_reasons: Sequence[str] = (),
    ) -> tuple[dict[str, Any], ...]:
        if not states or limit <= 0:
            return ()
        marks = ",".join("?" for _ in states)
        order = "DESC" if newest_first else "ASC"
        sql = _RECOVERY_SELECT + f" WHERE d.retry_state IN ({marks})"
        params = list(states)
        if after_delivery:
            sql += _RETRY_AFTER_DELIVERY_SQL
        if ready_at is not None:
            now, notice_stale, claim_stale = ready_at
            sql += """ AND ((d.retry_state='scheduled' AND d.retry_due_at <= ?)
                OR (d.retry_state IN ('notified','dispatching') AND d.retry_claimed_at <= ?)
                OR (d.retry_state='terminal' AND (d.retry_due_at IS NULL OR d.retry_due_at <= ?))
                OR (d.retry_state IN ('noticing','reporting') AND d.retry_claimed_at <= ?))"""
            params.extend((now, notice_stale, now, claim_stale))
        if terminal_armed:
            sql += " AND d.state NOT IN ('new','queued','admitted','running','stalling','finalizing')"
        if dropped_success_reasons:
            reasons = ",".join("?" for _ in dropped_success_reasons)
            sql += f""" AND d.retry_reason IN ({reasons}) AND (d.delivery_state='dropped' OR EXISTS (
                SELECT 1 FROM async_delegation_events e WHERE e.delegation_id=d.delegation_id
                AND e.event_kind='terminal_fallback' AND e.delivery_state='dropped'))"""
            params.extend(dropped_success_reasons)
        sql += f" ORDER BY d.updated_at {order}, d.delegation_id {order}"
        if eligible is None:
            sql += " LIMIT ?"
            params.append(limit)

        def read(conn):
            found = []
            for row in conn.execute(sql, params):
                record = _delegation_recovery_record(row)
                if eligible is None or eligible(record):
                    found.append(record)
                    if len(found) == limit:
                        break
            return tuple(found)

        with translated():
            return self._ledger_read(read)

    def release_delegation_resume_claim(self, delegation_id: DelegationId, *, claim: str, now: float) -> bool:
        if not claim:
            return False

        def write(conn):
            changed = conn.execute(
                """UPDATE async_delegations SET resume_state='none', resume_attempts=0, resume_claim=NULL,
                   resume_claimed_at=NULL, updated_at=? WHERE delegation_id=? AND retry_state='none'
                   AND resume_state='claimed' AND resume_claim=? AND COALESCE(retry_replacement,'')=''""",
                (now, delegation_id, claim),
            ).rowcount
            if changed:
                conn.execute(
                    """DELETE FROM async_delegation_child_recovery WHERE delegation_id=? AND claim=?
                       AND replacement_delegation_id IS NULL""", (delegation_id, claim),
                )
            return changed == 1

        with translated():
            return self._ledger_write(write)

    def cancel_delegation_retries(
        self, *, delegation_id: str, origin_session: str, origin_ui_session_id: str,
        parent_session_id: str, all_sessions: bool, reason: str, now: float,
        pending_states: Sequence[str],
    ) -> set[str]:
        selectors = [(name, value) for name, value in (
            ("origin_session", origin_session), ("origin_ui_session_id", origin_ui_session_id),
            ("parent_session_id", parent_session_id),
        ) if value]
        if not (delegation_id or all_sessions or selectors):
            return set()
        session_scope = " OR ".join(name + "=?" for name, _ in selectors)
        session_values = [value for _, value in selectors]
        pending = f"retry_state IN ({','.join('?' for _ in pending_states)})"
        legacy = "(retry_state='none' AND resume_state='claimed' AND COALESCE(retry_replacement,'')='')"
        if delegation_id:
            where, params = f"({pending} OR {legacy}) AND delegation_id=?", [delegation_id]
        elif all_sessions and selectors:
            where, params = f"({pending} OR ({legacy} AND ({session_scope})))", session_values
        elif all_sessions:
            where, params = pending, []
        else:
            where, params = f"({pending} OR {legacy}) AND ({session_scope})", session_values

        def write(conn):
            rows = conn.execute(
                f"SELECT delegation_id, retry_claim, resume_claim FROM async_delegations WHERE {where}",
                (*pending_states, *params),
            ).fetchall()
            conn.executemany(
                """UPDATE async_delegations SET retry_state='cancelled', retry_reason=?, retry_due_at=NULL,
                   retry_claim=NULL, retry_claimed_at=NULL, updated_at=? WHERE delegation_id=?""",
                ((reason, now, row[0]) for row in rows),
            )
            conn.executemany(
                """DELETE FROM async_delegation_child_recovery WHERE delegation_id=? AND claim IN (?, ?)
                   AND replacement_delegation_id IS NULL""", rows,
            )
            return {row[0] for row in rows}

        with translated():
            return self._ledger_write(write)

    def change_delegation_retries(
        self, delegation_ids: Sequence[DelegationId], *, now: float,
        decide: Callable[[dict[str, Any]], Optional[DelegationRetryChange]],
        after_delivery: bool = False, best_effort: bool = False,
    ) -> tuple[dict[str, Any], ...]:
        if not delegation_ids:
            return ()
        sql = _RECOVERY_SELECT + " WHERE d.delegation_id=?"
        if after_delivery:
            sql += _RETRY_AFTER_DELIVERY_SQL

        def write(conn):
            changed = []
            for delegation_id in delegation_ids:
                row = conn.execute(sql, (delegation_id,)).fetchone()
                if row is None:
                    continue
                record = _delegation_recovery_record(row)
                if (change := decide(record)) is not None:
                    _write_delegation_retry_change(conn, record, change, now)
                    changed.append(record)
            return tuple(changed)

        with translated():
            if best_effort:
                return self._store.holder()._execute_write(write, patience_s=0.1)
            return self._ledger_write(write)

    def claim_delegation_retry_dispatch(
        self, delegation_id: DelegationId, *, claim: str, now: float,
        eligible: Callable[[dict[str, Any]], tuple[Optional[str], bool]],
        children: Callable[[dict[str, Any]], Sequence[int]],
    ) -> tuple[Optional[dict[str, Any]], Optional[str], list[int]]:
        def write(conn):
            record = _read_delegation_recovery(conn, delegation_id)
            if record is None:
                return None, "no_such_delegation", []
            reason, expired = eligible(record)
            if reason:
                if expired:
                    _write_delegation_retry_change(conn, record, DelegationRetryChange(
                        "terminal", reason, None, None, None, record["retry_notices"], release_children=True,
                    ), now)
                return record, reason, []
            indexes = children(record)
            if not indexes and isinstance(record["task"].get("goals"), list) and len(record["task"]["goals"]) > 1:
                return record, "no_unfinished_children", []
            existing = dict(conn.execute(
                """SELECT task_index, replacement_delegation_id FROM async_delegation_child_recovery
                   WHERE delegation_id=?""", (delegation_id,),
            ))
            if any(index in existing and existing[index] is None for index in indexes):
                return record, "already_claimed", []
            remaining = [index for index in indexes if index not in existing]
            if indexes and not remaining:
                return record, "already_claimed", []
            conn.executemany(
                """INSERT INTO async_delegation_child_recovery
                   (delegation_id, task_index, claim, claimed_at) VALUES (?, ?, ?, ?)""",
                ((delegation_id, index, claim, now) for index in remaining),
            )
            _write_delegation_retry_change(conn, record, DelegationRetryChange(
                "dispatching", record["retry_reason"], record["retry_due_at"], claim,
                now, record["retry_notices"],
            ), now)
            return record, None, remaining

        with translated():
            return self._ledger_write(write)

    def get_delegation(self, *, delegation_id: DelegationId) -> Optional[DelegationRecord]:
        def _read(conn):
            return conn.execute("""SELECT origin_session, state, dispatched_at, completed_at,
                          result_json, delivery_state, delivery_attempts,
                          origin_session_id
                   FROM async_delegations WHERE delegation_id=?""", (delegation_id,)).fetchone()
        with translated():
            row = self._ledger_read(_read)
        return None if row is None else DelegationRecord(
            delegation_id=delegation_id, origin_session=row[0], state=row[1], dispatched_at=row[2],
            completed_at=row[3], result=json.loads(row[4]) if row[4] else None,
            delivery_state=DeliveryState(row[5]), delivery_attempts=row[6], origin_session_id=row[7] or "")

    # A session's delegations finished inside the failure window, newest first; the caller expands failed
    # tasks.
    def list_finished_delegations(self, *, origin_ui_session_id: str = '', parent_session_id: str = '',
                                  limit: int = 20, now: Optional[float] = None) -> tuple[FinishedDelegationRow, ...]:
        selectors = [(col, val) for col, val in (
            ("origin_ui_session_id", origin_ui_session_id), ("parent_session_id", parent_session_id)) if val]
        if not selectors:
            return ()
        cutoff = (now if now is not None else time.time()) - DELEGATION_FAILURE_WINDOW_SECONDS
        owner_sql = " OR ".join(f"{col}=?" for col, _ in selectors)
        def _read(conn):
            return conn.execute(
                f"""SELECT delegation_id, state, dispatched_at, completed_at, task_json, result_json FROM async_delegations
                    WHERE ({owner_sql}) AND state!='running' AND completed_at >= ?
                    ORDER BY completed_at DESC LIMIT ?""",
                (*(val for _, val in selectors), cutoff, limit)).fetchall()
        with translated():
            rows = self._ledger_read(_read)
        return tuple(FinishedDelegationRow(
            delegation_id=DelegationId(delegation_id), state=state, dispatched_at=dispatched_at,
            completed_at=completed_at, task=_json_object(task_json), result=_json_object(result_json))
            for delegation_id, state, dispatched_at, completed_at, task_json, result_json in rows)

    # The candidates of ``recover_abandoned_delegations``, with the ``updated_at`` its conditional update
    # compares.
    def list_inflight_delegations(self) -> tuple[InflightDelegation, ...]:
        def _read(conn):
            return conn.execute("""SELECT delegation_id, origin_session, origin_ui_session_id,
                          parent_session_id, dispatched_at, owner_pid,
                          owner_started_at, task_json, origin_session_id, result_json, state, updated_at
                   FROM async_delegations WHERE state='running'""").fetchall()
        with translated():
            rows = self._ledger_read(_read)
        return tuple(InflightDelegation(
            delegation_id=DelegationId(delegation_id), origin_session=session_key, origin_ui_session_id=origin_ui,
            parent_session_id=parent_id, dispatched_at=dispatched_at, owner_pid=pid, owner_started_at=started,
            task=json.loads(task_json or "{}"), origin_session_id=origin_sid or "",
            result=json.loads(result_json) if result_json else None, state=last_state, updated_at=updated_at)
            for (delegation_id, session_key, origin_ui, parent_id, dispatched_at, pid, started, task_json, origin_sid,
                 result_json, last_state, updated_at) in rows)

    def has_replayable_completions(self) -> bool:
        def _read(conn):
            return conn.execute("""SELECT 1 FROM async_delegations
                   WHERE state != 'running' AND delivery_state='pending' AND event_json IS NOT NULL
                   LIMIT 1""").fetchone()
        with translated():
            return self._ledger_read(_read) is not None

    # The owner stamps of the completions ``sweep_orphaned_completions`` would consider.
    def list_orphan_completion_owners(self, *, now: float) -> frozenset[RecordedOwner]:
        def _read(conn):
            return conn.execute("""SELECT DISTINCT owner_pid, owner_started_at
                   FROM async_delegations
                   WHERE state!='running' AND delivery_state='pending'
                     AND event_json IS NOT NULL AND updated_at < ?
                     AND (delivery_claim IS NULL OR delivery_claimed_at < ?)""",
                (now - DELEGATION_ORPHAN_STALE_SECONDS, now - DELEGATION_CLAIM_STALE_SECONDS)).fetchall()
        with translated():
            rows = self._ledger_read(_read)
        return frozenset(RecordedOwner(pid=pid, started_at=started) for pid, started in rows)

    def list_orphan_completions(self, *, now: float) -> tuple[OrphanCompletion, ...]:
        def _read(conn):
            return conn.execute("""SELECT delegation_id, owner_pid, owner_started_at, updated_at
                   FROM async_delegations
                   WHERE state!='running' AND delivery_state='pending'
                     AND event_json IS NOT NULL AND updated_at < ?
                     AND (delivery_claim IS NULL OR delivery_claimed_at < ?)
                   ORDER BY completed_at, delegation_id""",
                (now - DELEGATION_ORPHAN_STALE_SECONDS, now - DELEGATION_CLAIM_STALE_SECONDS)).fetchall()
        with translated():
            rows = self._ledger_read(_read)
        return tuple(OrphanCompletion(delegation_id=DelegationId(delegation_id),
                                      owner=RecordedOwner(pid=pid, started_at=started), updated_at=updated_at)
                     for delegation_id, pid, started, updated_at in rows)


    # The completions ``sweep_orphaned_completions`` returns for the same arguments, read before its drops commit.
    def list_orphan_completion_offers(self, *, now: float, exclude_ids: frozenset[DelegationId] = frozenset(),
                                      dead_owners: frozenset[RecordedOwner],
                                      lost_ids: frozenset[DelegationId] = frozenset()) -> tuple[PendingCompletion, ...]:
        def _read(conn):
            return conn.execute("""SELECT delegation_id, event_json, completed_at, dispatched_at,
                          owner_pid, owner_started_at, delivery_attempts
                   FROM async_delegations
                   WHERE state!='running' AND delivery_state='pending'
                     AND event_json IS NOT NULL AND updated_at < ?
                     AND (delivery_claim IS NULL OR delivery_claimed_at < ?)
                   ORDER BY completed_at, delegation_id""",
                (now - DELEGATION_ORPHAN_STALE_SECONDS, now - DELEGATION_CLAIM_STALE_SECONDS)).fetchall()
        with translated():
            rows = self._ledger_read(_read)
        return tuple(
            PendingCompletion(delegation_id=DelegationId(delegation_id), event=json.loads(payload),
                              completed_at=completed_at, dispatched_at=dispatched_at)
            for delegation_id, payload, completed_at, dispatched_at, pid, started, attempts in rows
            if (delegation_id in lost_ids or (delegation_id not in exclude_ids
                                              and RecordedOwner(pid=pid, started_at=started) in dead_owners))
            and (attempts or 0) < DELEGATION_MAX_DELIVERY_ATTEMPTS
            and not _past_replay_age(now, completed_at, dispatched_at))

    # The completion ``reopen_admitted_completion`` returns, read before the reopen commits.
    def get_admitted_completion(self, *, delegation_id: DelegationId) -> Optional[PendingCompletion]:
        def _read(conn):
            return conn.execute("""SELECT event_json, completed_at, dispatched_at, delivery_attempts
                   FROM async_delegations
                   WHERE delegation_id=? AND delivery_state='delivered' AND event_json IS NOT NULL""",
                (delegation_id,)).fetchone()
        with translated():
            row = self._ledger_read(_read)
        if (row is None or row[3] >= DELEGATION_MAX_DELIVERY_ATTEMPTS
                or _past_replay_age(time.time(), row[1], row[2])):
            return None
        return PendingCompletion(delegation_id=delegation_id, event=json.loads(row[0]), completed_at=row[1],
                                 dispatched_at=row[2])


register_repository("coordination", SQLiteCoordinationRepository, {"transactional_claims": True})
