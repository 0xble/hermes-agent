"""Profile-local durable handoff for cron delivery through live gateway adapters.

A restart-safe cron worker executes outside the gateway cgroup.  It cannot own
relay/E2EE adapter objects, so it queues the final send here.  A gateway claims
each row at most once.  Reviewed rows use ``reviewing`` until the final send
starts, so a restart can replay the side-effect-free review instead of losing
the report.  If the gateway dies after the final send begins, the outcome is
marked unknown and never retried.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from contextlib import closing, contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Iterator, Optional

from agent.redact import redact_sensitive_text
from cron.executions import _owner_is_live, _process_start_time
from hermes_constants import get_hermes_home
from hermes_time import now as _hermes_now

logger = logging.getLogger(__name__)

DELIVERY_DB: Optional[Path] = None
_PROCESS_ID = uuid.uuid4().hex
_lock = threading.RLock()
_ACTIVE_DELIVERIES: set[str] = set()
_TERMINAL = ("delivered", "failed", "unknown", "suppressed")
MAX_TERMINAL_DELIVERIES = 1000
DEFAULT_DELIVERY_WAIT_TIMEOUT_SECONDS = 300.0
MISSING_EXECUTION_GRACE = timedelta(hours=24)


def _prune_terminal_unlocked(conn: sqlite3.Connection) -> None:
    """Redact terminal payloads and retain only bounded outcome metadata."""
    conn.execute(
        """UPDATE deliveries SET job_json='{}', content=''
           WHERE status IN ('delivered','failed','unknown','suppressed')
             AND (job_json != '{}' OR content != '')"""
    )
    keep = max(0, int(MAX_TERMINAL_DELIVERIES))
    terminal_count = int(
        conn.execute(
            "SELECT COUNT(*) FROM deliveries "
            "WHERE status IN ('delivered','failed','unknown','suppressed')"
        ).fetchone()[0]
    )
    excess = terminal_count - keep
    if excess > 0:
        conn.execute(
            """INSERT OR IGNORE INTO delivery_tombstones
               (execution_id, terminal_status, finished_at, projected)
               SELECT execution_id, status, finished_at, projected FROM deliveries
               WHERE status IN ('delivered','failed','unknown','suppressed')
               ORDER BY julianday(finished_at), finished_at,
                        julianday(created_at), created_at, execution_id
               LIMIT ?""",
            (excess,),
        )
        conn.execute(
            """DELETE FROM deliveries WHERE execution_id IN (
                 SELECT execution_id FROM deliveries
                 WHERE status IN ('delivered','failed','unknown','suppressed')
                 ORDER BY julianday(finished_at), finished_at,
                          julianday(created_at), created_at, execution_id
                 LIMIT ?
               )""",
            (excess,),
        )


def queue_path(home: Optional[Path] = None) -> Path:
    """The queue file of ``home`` (the active home when None); a test override wins."""
    if DELIVERY_DB is not None:
        return DELIVERY_DB
    root = Path(home) if home is not None else get_hermes_home()
    return root.resolve() / "cron" / "deliveries.db"


def _path() -> Path:
    return queue_path()


def _initialize_schema(conn: sqlite3.Connection) -> None:
    from hermes_cli.sqlite_util import add_column_if_missing

    # SQLite cannot widen a CHECK in place. Preserve all old rows atomically,
    # including claimed sends, while admitting the replayable review disposition
    # and the distinct never-sent disposition.
    for table in ("deliveries", "delivery_tombstones"):
        row = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()
        required = "'reviewing'" if table == "deliveries" else "'suppressed'"
        if row and required not in row[0]:
            conn.execute("SAVEPOINT notification_disposition")
            try:
                conn.execute(f"ALTER TABLE {table} RENAME TO {table}_old")
                sql = row[0]
                missing = [state for state in ("suppressed", "reviewing")
                           if f"'{state}'" not in sql
                           and (state != "reviewing" or table == "deliveries")]
                sql = sql.replace("'unknown'", "'unknown'" + "".join(f",'{state}'" for state in missing))
                conn.execute(sql)
                conn.execute(f"INSERT INTO {table} SELECT * FROM {table}_old")
                conn.execute(f"DROP TABLE {table}_old")
                conn.execute("RELEASE notification_disposition")
            except BaseException:
                conn.execute("ROLLBACK TO notification_disposition")
                conn.execute("RELEASE notification_disposition")
                raise

    conn.execute(
        """CREATE TABLE IF NOT EXISTS deliveries (
             execution_id TEXT PRIMARY KEY,
             job_json TEXT NOT NULL,
             content TEXT NOT NULL,
             for_failure INTEGER NOT NULL DEFAULT 0,
             status TEXT NOT NULL CHECK(status IN
               ('pending','reviewing','delivering','delivered','failed','unknown','suppressed')),
             owner_process_id TEXT,
             owner_pid INTEGER,
             owner_started_at INTEGER,
             created_at TEXT NOT NULL,
             finished_at TEXT,
             error TEXT
           )"""
    )
    conn.execute(
        """CREATE TABLE IF NOT EXISTS delivery_tombstones (
             execution_id TEXT PRIMARY KEY,
             terminal_status TEXT NOT NULL CHECK(terminal_status IN
               ('delivered','failed','unknown','suppressed')),
             finished_at TEXT
           )"""
    )
    add_column_if_missing(
        conn, "deliveries", "terminal_gate", "terminal_gate INTEGER NOT NULL DEFAULT 0",
    )
    add_column_if_missing(
        conn, "deliveries", "for_failure",
        "for_failure INTEGER NOT NULL DEFAULT 0",
    )
    add_column_if_missing(
        conn, "deliveries", "projected",
        "projected INTEGER NOT NULL DEFAULT 0",
    )
    add_column_if_missing(
        conn, "delivery_tombstones", "projected",
        "projected INTEGER NOT NULL DEFAULT 0",
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_deliveries_terminal_projection "
        "ON deliveries(status, projected, finished_at, execution_id)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_delivery_tombstones_projection "
        "ON delivery_tombstones(projected, finished_at, execution_id)"
    )


def _connect() -> sqlite3.Connection:
    # Late imports: a scheduler daemon that outlives an on-disk upgrade already has the OLD
    # ``hermes_cli.sqlite_util`` / ``cron.jobs`` cached, so new names must be resolved at call time,
    # not at import time (the guarantee cron/ledger.py used to carry, see e24c8499).
    from hermes_cli.sqlite_util import open_db

    path = _path()
    conn = open_db(path, db_label="cron/deliveries.db", synchronous_full=True, initialize=_initialize_schema)
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return conn


@contextmanager
def _transaction() -> Iterator[sqlite3.Connection]:
    # Pruning is done explicitly by the paths that create terminal
    # rows (_finish / recover_abandoned / _terminalize_wait_timeout);
    # read-only polls must not pay for a full-table UPDATE + COUNT.
    from hermes_cli.sqlite_util import transaction

    with _lock, transaction(_connect()) as conn:
        yield conn


def _reflect_execution_delivery(execution_id: str, status: str) -> None:
    """Project the independent queue receipt onto an existing execution, never create a run."""
    from cron import executions
    path = executions.EXECUTIONS_FILE or get_hermes_home().resolve() / "cron" / "executions.db"
    if Path(path).exists():
        executions.record_delivery_status(str(execution_id), status)


def _reflect_terminal_deliveries(execution_ids: list[str], status: str) -> None:
    """Project committed terminal transitions, including rows pruned to tombstones."""
    for execution_id in execution_ids:
        _reflect_execution_delivery(execution_id, status)


def _mark_terminal_projected(execution_ids: list[str]) -> None:
    """Mark receipts only after their execution-ledger projection committed."""
    clean = [(str(execution_id),) for execution_id in execution_ids]
    if not clean:
        return
    with _transaction() as conn:
        conn.executemany(
            "UPDATE deliveries SET projected=1 WHERE execution_id=? AND projected=0",
            clean,
        )
        conn.executemany(
            "UPDATE delivery_tombstones SET projected=1 "
            "WHERE execution_id=? AND projected=0",
            clean,
        )


def reconcile_terminal_deliveries() -> int:
    """Repair execution projections for unprojected terminal queue receipts.

    The queue and execution ledger are separate SQLite stores.  A process crash
    between their commits can leave the queue terminal while the ledger still
    says ``pending``.  The queue's indexed projection marker makes successful
    receipts cheap to skip while retaining retries when either database write
    fails.  The execution ledger is opened exactly once for this batch.
    """
    with _transaction() as conn:
        rows = conn.execute(
            "SELECT execution_id, status, 0 AS is_tombstone FROM deliveries "
            "WHERE status IN ('delivered','failed','unknown','suppressed') "
            "AND projected=0 "
            "UNION ALL SELECT execution_id, terminal_status AS status, 1 AS is_tombstone "
            "FROM delivery_tombstones "
            "WHERE projected=0"
        ).fetchall()
    if not rows:
        return 0

    from cron import executions
    from hermes_cli.sqlite_util import transaction

    # Commit the ledger projection first.  If marking the queue receipt fails,
    # the next drain repeats an idempotent ledger update rather than losing the
    # repair.  Marking projected before this commit would lose it permanently.
    ledger = executions._connect()
    with transaction(ledger) as ledger_conn:
        executions._project_delivery_statuses(
            ledger_conn,
            ((str(row["status"]), str(row["execution_id"])) for row in rows),
        )

    with _transaction() as conn:
        deliveries = [
            (str(row["execution_id"]),)
            for row in rows
            if not row["is_tombstone"]
        ]
        tombstones = [
            (str(row["execution_id"]),)
            for row in rows
            if row["is_tombstone"]
        ]
        if deliveries:
            conn.executemany(
                "UPDATE deliveries SET projected=1 "
                "WHERE execution_id=? AND projected=0",
                deliveries,
            )
        if tombstones:
            conn.executemany(
                "UPDATE delivery_tombstones SET projected=1 "
                "WHERE execution_id=? AND projected=0",
                tombstones,
            )
    return len(rows)


def enqueue(
    execution_id: str,
    job: dict,
    content: str,
    *,
    for_failure: bool = False,
    terminal_gate: bool = False,
) -> dict:
    """Persist one idempotent delivery request before the worker waits."""
    with _transaction() as conn:
        # Serialize the tombstone check and insert with retention in other
        # processes, which can move a terminal delivery into the tombstone table.
        conn.execute("BEGIN IMMEDIATE")
        tombstone = conn.execute(
            "SELECT terminal_status, finished_at FROM delivery_tombstones "
            "WHERE execution_id=?",
            (str(execution_id),),
        ).fetchone()
        if tombstone is not None:
            result = {
                "execution_id": str(execution_id),
                "status": tombstone["terminal_status"],
                "finished_at": tombstone["finished_at"],
            }
        else:
            conn.execute(
                """INSERT OR IGNORE INTO deliveries
                   (execution_id, job_json, content, for_failure, terminal_gate, status, created_at)
                   VALUES (?, ?, ?, ?, ?, 'pending', ?)""",
                (
                    str(execution_id),
                    json.dumps(job, ensure_ascii=False, sort_keys=True),
                    str(content),
                    int(bool(for_failure)),
                    int(bool(terminal_gate)),
                    _hermes_now().isoformat(),
                ),
            )
            row = conn.execute(
                "SELECT * FROM deliveries WHERE execution_id=?", (str(execution_id),)
            ).fetchone()
            result = dict(row)
    _reflect_execution_delivery(str(execution_id), "pending" if result["status"] == "reviewing" else result["status"])
    return result


def get_status(execution_id: str) -> Optional[dict]:
    with _transaction() as conn:
        row = conn.execute(
            "SELECT * FROM deliveries WHERE execution_id=?", (str(execution_id),)
        ).fetchone()
        if row is not None:
            return dict(row)
        tombstone = conn.execute(
            "SELECT execution_id, terminal_status, finished_at "
            "FROM delivery_tombstones WHERE execution_id=?",
            (str(execution_id),),
        ).fetchone()
    if tombstone is None:
        return None
    return {
        "execution_id": tombstone["execution_id"],
        "status": tombstone["terminal_status"],
        "finished_at": tombstone["finished_at"],
        "error": None,
    }


def claim_next() -> Optional[dict]:
    """Atomically claim one pending send before touching the transport."""
    pid = os.getpid()
    started = _process_start_time(pid)
    with _transaction() as conn:
        rows = conn.execute(
            "SELECT execution_id, job_json, for_failure, terminal_gate, created_at FROM deliveries WHERE status='pending' "
            "ORDER BY julianday(created_at), created_at, execution_id"
        ).fetchall()
        from cron.scheduler_review import reviews_stopped
        if reviews_stopped():
            # A stopping gateway leaves held reports pending for the next gateway.
            rows = [candidate for candidate in rows if not _is_review_row(candidate)]
        row = None
        suppressed_missing = False
        # Read all gated executions from one ledger connection rather than opening
        # one for each pending notice under the queue lock. On ledger failure,
        # defer all gated rows; absence is only meaningful after a successful read.
        gated_ids = [str(candidate["execution_id"]) for candidate in rows if candidate["terminal_gate"]]
        executions_by_id = None
        if gated_ids:
            from cron import executions
            ledger_path = executions.EXECUTIONS_FILE or get_hermes_home().resolve() / "cron" / "executions.db"
            if Path(ledger_path).exists():
                try:
                    with closing(executions._connect()) as ledger:
                        executions_by_id = {}
                        for start in range(0, len(gated_ids), 500):
                            batch = gated_ids[start:start + 500]
                            found = ledger.execute(
                                "SELECT id, status, error FROM executions WHERE id IN ("
                                + ",".join("?" for _ in batch) + ")", batch,
                            ).fetchall()
                            executions_by_id.update((str(execution["id"]), execution) for execution in found)
                except (OSError, sqlite3.Error):
                    logger.warning("Cron delivery ledger unavailable; deferring gated notices", exc_info=True)
                    executions_by_id = None
        # A queue admission can precede the worker's terminal commit. Never
        # send while the result is unsettled or if a timeout won that race.
        for candidate in rows:
            if not candidate["terminal_gate"]:
                row = candidate
                break
            if executions_by_id is None:
                continue
            execution = executions_by_id.get(str(candidate["execution_id"]))
            if execution is None:
                if datetime.fromisoformat(candidate["created_at"]) <= _hermes_now() - MISSING_EXECUTION_GRACE:
                    conn.execute("""UPDATE deliveries SET status='suppressed', finished_at=?,
                        error='Execution record missing after retention grace', projected=1
                        WHERE execution_id=? AND status='pending'""",
                        (_hermes_now().isoformat(), candidate["execution_id"]),
                    )
                    suppressed_missing = True
                continue
            if execution["status"] in ("claimed", "running"):
                continue
            # A pre-admitted failure notice contains the worker's original failure
            # summary. If a watchdog later overwrites the terminal reason, it remains
            # a true failure notice but may cite that earlier reason. Suppressing every
            # mismatch would silently lose a legitimate failure alert; the ledger is
            # authoritative for the final error in status/history.
            if (execution["status"] == "unknown" or
                    (execution["status"] == "failed") != bool(candidate["for_failure"])):
                conn.execute("""UPDATE deliveries SET status='suppressed', finished_at=?,
                    error='Execution outcome changed before delivery', projected=0
                    WHERE execution_id=? AND status='pending'""",
                    (_hermes_now().isoformat(), candidate["execution_id"]),
                )
                continue
            row = candidate
            break
        if suppressed_missing:
            _prune_terminal_unlocked(conn)
        if row is None:
            return None
        status = "reviewing" if _is_review_row(row) else "delivering"
        cur = conn.execute(
            """UPDATE deliveries SET status=?, owner_process_id=?,
               owner_pid=?, owner_started_at=?
               WHERE execution_id=? AND status='pending'""",
            (status, _PROCESS_ID, pid, started, row["execution_id"]),
        )
        if cur.rowcount != 1:
            return None
        claimed = conn.execute(
            "SELECT * FROM deliveries WHERE execution_id=?", (row["execution_id"],)
        ).fetchone()
        _ACTIVE_DELIVERIES.add(row["execution_id"])
    result = dict(claimed)
    result["job"] = json.loads(result.pop("job_json"))
    return result


def _is_review_row(row) -> bool:
    return not row["for_failure"] and bool(json.loads(row["job_json"]).get("review"))


def record_review_progress(execution_id: str, owner_process_id: str, **fields) -> bool:
    """Persist review progress so a replay never repeats a decided review or a rerun."""
    with _transaction() as conn:
        row = conn.execute(
            """SELECT job_json FROM deliveries WHERE execution_id=? AND status='reviewing'
                 AND owner_process_id=? AND owner_pid=?""",
            (str(execution_id), owner_process_id, os.getpid()),
        ).fetchone()
        if row is None:
            return False
        job = json.loads(row["job_json"])
        job.update(fields)
        conn.execute(
            "UPDATE deliveries SET job_json=? WHERE execution_id=?",
            (json.dumps(job, ensure_ascii=False, sort_keys=True), str(execution_id)),
        )
    return True


def begin_final_delivery(execution_id: str, owner_process_id: str) -> bool:
    """Claim a reviewed row for the only phase whose outcome may be unknown."""
    with _transaction() as conn:
        cur = conn.execute(
            """UPDATE deliveries SET status='delivering'
               WHERE execution_id=? AND status='reviewing'
                 AND owner_process_id=? AND owner_pid=?""",
            (str(execution_id), owner_process_id, os.getpid()),
        )
    return cur.rowcount == 1


def _finish(execution_id: str, *, error: Optional[str], suppressed: bool = False) -> bool:
    status = "failed" if error else "suppressed" if suppressed else "delivered"
    safe_error = (
        redact_sensitive_text(str(error), force=True, redact_url_credentials=True)
        if error
        else None
    )
    with _transaction() as conn:
        cur = conn.execute(
            """UPDATE deliveries SET status=?, finished_at=?, error=?
               WHERE execution_id=? AND status IN ('reviewing','delivering')
                 AND owner_process_id=? AND owner_pid=?""",
            (
                status,
                _hermes_now().isoformat(),
                safe_error,
                execution_id,
                _PROCESS_ID,
                os.getpid(),
            ),
        )
        _prune_terminal_unlocked(conn)
    if cur.rowcount == 1:
        _reflect_terminal_deliveries([execution_id], status)
        _mark_terminal_projected([execution_id])
    return cur.rowcount == 1


def recover_abandoned() -> int:
    """Replay abandoned reviews, but never replay uncertain final sends."""
    changed = 0
    terminal_ids: list[str] = []
    unknown_receipts: list[dict] = []
    with _transaction() as conn:
        rows = conn.execute(
            "SELECT execution_id, job_json, status, owner_process_id, owner_pid, owner_started_at "
            "FROM deliveries WHERE status IN ('reviewing','delivering')"
        ).fetchall()
        for row in rows:
            same_process = row["owner_process_id"] == _PROCESS_ID
            if same_process:
                with _lock:
                    if row["execution_id"] in _ACTIVE_DELIVERIES:
                        continue
            elif _owner_is_live(int(row["owner_pid"]), row["owner_started_at"]):
                continue
            if row["status"] == "reviewing":
                cur = conn.execute(
                    """UPDATE deliveries SET status='pending', owner_process_id=NULL,
                           owner_pid=NULL, owner_started_at=NULL
                       WHERE execution_id=? AND status='reviewing'""",
                    (row["execution_id"],),
                )
                changed += cur.rowcount
                continue
            error = (
                "Gateway finished delivery but could not persist its outcome; "
                "send was not retried."
                if same_process
                else "Gateway exited during delivery; send outcome is unknown and was not retried."
            )
            cur = conn.execute(
                """UPDATE deliveries SET status='unknown', finished_at=?, error=?
                   WHERE execution_id=? AND status='delivering'""",
                (
                    _hermes_now().isoformat(),
                    error,
                    row["execution_id"],
                ),
            )
            changed += cur.rowcount
            if cur.rowcount == 1:
                terminal_ids.append(row["execution_id"])
                job = json.loads(row["job_json"])
                if job.get("review"):
                    unknown_receipts.append({
                        "job_id": job.get("id"), "run_id": row["execution_id"],
                        "subject": job["review"].get("subject"), "decision": "DELIVERY_UNKNOWN",
                        "candidates": [], "text_sent": None, "message_id": None, "error": error})
        _prune_terminal_unlocked(conn)
    _reflect_terminal_deliveries(terminal_ids, "unknown")
    _mark_terminal_projected(terminal_ids)
    # The receipt file is an audit trail; a failed write must never stall the queue drain.
    for receipt in unknown_receipts:
        try:
            from cron.review import append_receipt
            append_receipt(receipt)
        except Exception:
            logger.exception("Cron review receipt for %s could not be written", receipt["run_id"])
    return changed


def _send_claimed(row, send):
    try:
        try:
            error = send(row["job"], row["content"], bool(row["for_failure"]))
        except BaseException as exc:
            from cron.scheduler_review import ReviewAbandoned
            if isinstance(exc, ReviewAbandoned):
                return  # Keep the original and admission deadline for the next gateway.
            logger.exception("Cron delivery failed for %s", row["execution_id"])
            error = f"{type(exc).__name__}: {exc}"
        _finish(row["execution_id"], error=error,
                suppressed=bool(row["job"].get("_notification_all_targets_suppressed")))
    finally:
        with _lock:
            _ACTIVE_DELIVERIES.discard(row["execution_id"])


def drain(
    send: Callable[[dict, str, bool], Optional[str]], *, limit: int = 20
) -> int:
    """Deliver pending rows through *send*, terminalizing every claimed row."""
    reconcile_terminal_deliveries()
    recover_abandoned()
    processed = 0
    for _ in range(max(0, limit)):
        row = claim_next()
        if row is None:
            break
        with _lock:
            _ACTIVE_DELIVERIES.add(row["execution_id"])
        if row["job"].get("review") and not row["for_failure"]:
            row["job"]["_review_queue_claim"] = (row["execution_id"], row["owner_process_id"])
            # A held report uses delivery capacity, never one of the four cron run slots,
            # and never blocks the drain of unrelated notices. Profile context follows it.
            import contextvars
            context = contextvars.copy_context()
            threading.Thread(target=context.run, args=(_send_claimed, row, send),
                             name="cron-review-delivery", daemon=True).start()
        else:
            _send_claimed(row, send)
        processed += 1
    return processed


def _terminalize_wait_timeout(execution_id: str) -> str:
    """Fence a delivery whose worker can no longer wait for confirmation.

    A row still ``pending`` or ``reviewing`` has no outbound effects, so it is left queued
    for whichever gateway comes up next (a restart that includes an update can
    easily exceed the worker's wait budget).  That is a deferral, not a
    failure: report success so the job is not recorded ``delivery_failed`` for
    a message the drain will still send.  Only a row caught mid-send is
    uncertain and gets fenced ``unknown``.
    """
    now = _hermes_now().isoformat()
    uncertain_error = (
        "timed out while gateway delivery was in progress; outcome is unknown and "
        "was not retried"
    )
    with _transaction() as conn:
        row = conn.execute(
            "SELECT status FROM deliveries WHERE execution_id=?",
            (str(execution_id),),
        ).fetchone()
        if row is not None and row["status"] in ("pending", "reviewing"):
            logger.warning(
                "Cron delivery %s: no live gateway within the wait budget; "
                "left queued for the next gateway",
                execution_id,
            )
            return ""
        cur = conn.execute(
            """UPDATE deliveries SET status='unknown', finished_at=?, error=?
               WHERE execution_id=? AND status='delivering'""",
            (now, uncertain_error, str(execution_id)),
        )
        row = conn.execute(
            "SELECT status, error FROM deliveries WHERE execution_id=?",
            (str(execution_id),),
        ).fetchone()
        _prune_terminal_unlocked(conn)
    if cur.rowcount:
        _reflect_terminal_deliveries([str(execution_id)], "unknown")
        _mark_terminal_projected([str(execution_id)])
    if row is None:
        return "timed out waiting for live gateway delivery"
    if row["status"] in {"delivered", "suppressed"}:
        return ""
    return str(row["error"] or f"delivery {row['status']}")


def enqueue_and_wait(
    execution_id: str,
    job: dict,
    content: str,
    *,
    for_failure: bool = False,
    timeout: Optional[float] = None,
) -> Optional[str]:
    """Queue delivery and wait for a gateway's terminal at-most-once outcome."""
    queued = enqueue(execution_id, job, content, for_failure=for_failure)
    if queued["status"] in _TERMINAL:
        return None if queued["status"] in {"delivered", "suppressed"} else str(
            queued.get("error") or f"delivery {queued['status']}"
        )
    wait_timeout = (
        DEFAULT_DELIVERY_WAIT_TIMEOUT_SECONDS if timeout is None else max(0.0, timeout)
    )
    deadline = time.monotonic() + wait_timeout
    while time.monotonic() < deadline:
        row = get_status(execution_id)
        if row and row["status"] in _TERMINAL:
            return None if row["status"] in {"delivered", "suppressed"} else str(
                row.get("error") or f"delivery {row['status']}"
            )
        time.sleep(1.0)
    return _terminalize_wait_timeout(execution_id) or None
