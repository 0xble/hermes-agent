"""Checkpoint evidence and classification for the existing terminal ownership transaction."""

from __future__ import annotations

import json
import logging
import math
import time
from typing import Any

from agent.runtime_session_store import is_worker_process

logger = logging.getLogger(__name__)

_SUCCESS = {"completed", "success"}
_UNFINISHED = {"interrupted", "cancelled", "unknown", "stalled"}


def _indexes(task: dict[str, Any]) -> list[int]:
    return task.get("task_indexes") or (list(range(len(task.get("goals") or []))) if task.get("is_batch") else [0])


def _children(results: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    return {entry.get("task_index", index): entry for index, entry in enumerate(results)}


def _outcome(task, result, display_status):
    """``(finished, unfinished)`` over every owned task index, never call-wide goal counts."""
    if task.get("is_batch"):
        indexes, children = _indexes(task), _children(result.get("results") or [])
        finished = bool(indexes) and all(i in children and children[i].get("status") in _SUCCESS for i in indexes)
        unfinished = any(i not in children or children[i].get("status") in _UNFINISHED for i in indexes)
        return finished, unfinished
    finished = (result.get("status") or display_status) in _SUCCESS
    return finished, not finished


def durable_status(event, base_status):
    """Durable state implied by a reconciled display event: a surviving cut means interrupted, and
    checkpoint-proven success means completed; otherwise the producer's terminal state stands."""
    if event.get("cut_reason"):
        return "interrupted"
    return "completed" if event.get("status") == "completed" else base_status


def sync_completion_payload(target, source):
    """Copy reconciled producer payload fields without replacing consumer routing or ownership metadata.

    A source event's ``status`` may be the durable state rather than the display status, so status is
    copied only when checkpoint truth upgraded the unit to completed."""
    for key in ("results", "summary", "error", "api_calls", "duration_seconds", "exit_reason"):
        if key in source:
            target[key] = source[key]
    if source.get("status") == "completed":
        target["status"] = "completed"
    if source.get("cut_reason"):
        target["cut_reason"] = source["cut_reason"]
    else:
        target.pop("cut_reason", None)


def _checkpoint_results(task, result, recorded):
    """Successful runner entries keep their postprocessing; checkpoints defeat missing or downgraded entries."""
    transcripts, recovered = task.get("task_transcripts") or {}, []
    indexes = _indexes(task)
    owned = set(indexes)
    children = _children(result.get("results") or []) if task.get("is_batch") else {0: result}
    for entry in recorded:
        index = entry.get("task_index")
        if index not in owned:
            continue
        current = children.get(index)
        if current is not None and current.get("status") in _SUCCESS:
            continue
        child = dict(entry)
        if transcripts.get(str(index)) and not child.get("live_transcript"):
            child["live_transcript"] = transcripts[str(index)]
        children[index] = child
        recovered.append(child)
    if recovered:
        # A crashed aggregator never applied the ordinary result's static summary ceiling.
        from tools.delegate_tool_results import _apply_summary_budget
        _apply_summary_budget(recovered, None)
    if task.get("is_batch"):
        error = result.get("error") or "No terminal result was recorded for this child; outcome unknown."
        result["results"] = [children.get(i) or {"task_index": i, "status": "unknown", "summary": None, "error": error}
                             for i in indexes]
    elif recovered:
        aggregation_error = result.get("error")
        result.update(children[0])
        if aggregation_error:
            result["error"] = aggregation_error
    result.pop("partial", None)


def reconcile_checkpoint(conn, event, result, expected_state, terminal_status=None):
    """Reconcile while holding SQLite write ownership, before serializing either terminal delivery identity.

    Callers acquire BEGIN IMMEDIATE before this read and retain it through their conditional
    lifecycle update and optional outbox insert. A process-local mutex alone cannot protect this
    read/modify/write from a final checkpoint on another connection.

    ``terminal_status`` is the base durable state for an outbox write whose ``event['status']`` is the
    display status. Without it, ``event['status']`` is itself the durable state and is rewritten.
    Returns the durable state to commit.
    """
    lifecycle = terminal_status is None
    base = event.get("status", "completed") if lifecycle else terminal_status
    row = conn.execute("SELECT task_json, result_json FROM async_delegations WHERE delegation_id=? AND state=?",
                       (event["delegation_id"], expected_state)).fetchone()
    if row is None or result is None:
        return base
    task, partial = json.loads(row[0] or "{}"), json.loads(row[1] or "{}")
    task.setdefault("is_batch", bool(event.get("is_batch")))
    recorded = (partial or {}).get("results") or []
    if recorded:
        _checkpoint_results(task, result, recorded)
    for key in ("results", "summary", "error", "api_calls", "exit_reason"):
        if key in result and (key != "results" or task.get("is_batch")):
            event[key] = result[key]
    finished, unfinished = _outcome(task, result, event.get("status"))
    if finished:
        event["status"] = "completed"
        event.pop("cut_reason", None)
        return "completed"
    if event.get("cut_reason") and unfinished:
        durable = "interrupted"
    else:
        event.pop("cut_reason", None)
        durable = base
    if lifecycle:
        event["status"] = durable
    return durable


def record_unit_child(delegation_id: str, entry: dict[str, Any]) -> None:
    """Checkpoint one finished child on the running unit's own row. The terminal owner reconciles
    ``result_json`` before claiming completion; abandoned-unit recovery also replays it.
    Best-effort: a failed write costs recovery fidelity, never the live result."""
    from tools import async_delegation as ad

    entry = ad.redact_child_result(entry)
    if remote := ad.worker_ledger():
        return remote.child(delegation_id, entry)
    try:
        with ad._DB_LOCK, ad._transaction() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT result_json FROM async_delegations WHERE delegation_id=? AND state='running'",
                               (delegation_id,)).fetchone()
            if row is None:
                return
            partial = ad.redact_child_result(json.loads(row[0] or "{}") or {})
            results = [r for r in partial.get("results") or [] if r.get("task_index") != entry.get("task_index")]
            results.append(entry)
            conn.execute("UPDATE async_delegations SET result_json=?, updated_at=? WHERE delegation_id=? AND state='running'",
                         (json.dumps({"results": results, "partial": True}), time.time(), delegation_id))
    except Exception:
        logger.warning("Async delegation %s: could not record finished child %s", delegation_id, entry.get("task_index"), exc_info=True)


def record_child_finished(delegation_id: str, task_index: int, child_session_id: str) -> bool:
    """Record the native driver's finished-child checkpoint, not physical worker quiescence.

    The producer calls only after closing its driver and collecting every admitted conversation.
    A terminal aggregate may precede this acknowledgment. Never change its outcome or delivery,
    and never synthesize prospective identity for legacy rows or a different native child.
    """
    from tools import async_delegation as ad

    if not delegation_id or not isinstance(child_session_id, str) or not child_session_id:
        return False
    if is_worker_process():  # worker-routed rows carry no admitted checkpoint; stay unknown
        return False
    if not isinstance(task_index, int) or isinstance(task_index, bool):
        return False
    try:
        with ad._DB_LOCK, ad._transaction() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT task_json FROM async_delegations WHERE delegation_id=?",
                               (delegation_id,)).fetchone()
            if row is None:
                return False
            task = ad._json_object(row[0])
            children = task.get("child_execution")
            checkpoint = children.get(str(task_index)) if isinstance(children, dict) else None
            if not isinstance(checkpoint, dict) or checkpoint.get("child_session_id") != child_session_id:
                return False
            if checkpoint.get("checkpoint") == "finished_child":
                return True
            if checkpoint.get("checkpoint") != "admitted":
                return False
            checkpoint.update(checkpoint="finished_child", finished_at=time.time())
            conn.execute("UPDATE async_delegations SET task_json=? WHERE delegation_id=?",
                         (json.dumps(task), delegation_id))
        return True
    except Exception:
        logger.warning("Async delegation %s: could not checkpoint finished child %s", delegation_id,
                       child_session_id, exc_info=True)
        return False


def record_unit_finished(record: dict[str, Any], done_future: Any) -> bool:
    """Checkpoint the real native unit driver, not its outcome or nested workers.

    The callback follows aggregate hooks and finalization. A producer Future
    is valid only after a witnessed driver start and that driver's closure.
    A placeholder, pre-start cancellation or legacy identity remains unknown.
    """
    from tools import async_delegation as ad

    from concurrent.futures import Future

    if (not isinstance(done_future, Future) or not done_future.done() or done_future.cancelled()
            or record.get("_future") is not done_future or not record.get("_started")):
        return False
    expected = {str(index): sid for index, sid in (record.get("child_sessions") or {}).items()
                if isinstance(sid, str) and sid}
    if not expected or is_worker_process():
        return False
    delegation_id = record["delegation_id"]
    try:
        with ad._DB_LOCK, ad._transaction() as conn:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute("SELECT dispatched_at, task_json FROM async_delegations WHERE delegation_id=?",
                               (delegation_id,)).fetchone()
            if row is None or row[0] != record["dispatched_at"]:
                return False
            task = ad._json_object(row[1])
            children = task.get("child_execution")
            if not isinstance(children, dict) or set(children) != set(expected):
                return False
            if any(not isinstance(children[index], dict) or children[index].get("child_session_id") != sid
                   for index, sid in expected.items()):
                return False
            unit_execution = task.get("unit_execution")
            if isinstance(unit_execution, dict) and unit_execution.get("checkpoint") == "finished_unit":
                return True
            task["unit_execution"] = {"checkpoint": "finished_unit", "finished_at": time.time()}
            conn.execute("UPDATE async_delegations SET task_json=? WHERE delegation_id=?",
                         (json.dumps(task), delegation_id))
        return True
    except Exception:
        logger.warning("Async delegation %s: could not checkpoint finished completion unit", delegation_id,
                       exc_info=True)
        return False


def replay_outbox_pending(conn, rows, target_queue, now: float) -> int:
    """Replay pending outbox events, preserving their event-specific delivery identity."""
    from tools import async_delegation as ad

    home, restored = ad.hermes_home_key(ad.get_hermes_home()), 0
    for event_id, payload, created_at, updated_at in rows:
        age_basis = updated_at or created_at
        if age_basis and (now - age_basis) > ad._MAX_COMPLETION_REPLAY_AGE_S:
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
            ad._stamp_resume_availability(evt, conn)
        target_queue.put(evt)
        with ad._orphan_lock:
            ad._offered.add((home, event_id))
        restored += 1
    return restored


def _finished_execution_children(task: dict[str, Any]) -> set[str]:
    """Native checkpoint identities, or no proof when any owned driver is unknown."""
    indexes = _indexes(task)
    children, unit = task.get("child_execution"), task.get("unit_execution")
    if (not indexes or any(type(i) is not int or i < 0 for i in indexes)
            or len(set(indexes)) != len(indexes) or not isinstance(children, dict)
            or set(children) != {str(i) for i in indexes}
            or not isinstance(unit, dict) or unit.get("checkpoint") != "finished_unit"):
        return set()
    sessions = []
    finished = [unit.get("finished_at")]
    for child in children.values():
        if (not isinstance(child, dict) or child.get("checkpoint") != "finished_child"
                or not isinstance(child.get("child_session_id"), str) or not child["child_session_id"]):
            return set()
        sessions.append(child["child_session_id"])
        finished.append(child.get("finished_at"))
    if (len(set(sessions)) != len(sessions)
            or any(type(t) not in (int, float) or not math.isfinite(t) or t <= 0 for t in finished)):
        return set()
    return set(sessions)


def seal_execution(delegation_id: str, *, child_session_id: str) -> bool:
    """Close the entire recovery lineage after positive native execution proof.

    The caller must match a child of the requested unit. An empty delegation id
    resolves that exact native child from the durable ledger, without owner state.
    Every connected unit must have finished child and enclosing-driver checkpoints.
    Outcome, delivery and claim states are neither proof nor changed here.
    """
    from tools import async_delegation as ad

    if not isinstance(child_session_id, str) or not child_session_id:
        return False
    ad.require_ledger_owner()
    with ad._DB_LOCK, ad._transaction(immediate=True) as conn:
        if not delegation_id:
            matches = [did for did, raw in conn.execute(
                "SELECT delegation_id, task_json FROM async_delegations"
            ) if isinstance(children := ad._json_object(raw).get("child_execution"), dict)
                and any(isinstance(child, dict) and child.get("child_session_id") == child_session_id
                        for child in children.values())]
            if len(matches) != 1:
                return False
            delegation_id = matches[0]
        # Follow both directions so a replacement cannot hide a running parent
        # or sibling. UNION visits each identity once, including cyclic edges.
        # A missing referenced row survives the LEFT JOIN and fails proof below.
        rows = conn.execute("""WITH RECURSIVE edges(source, target) AS (
                SELECT delegation_id, retry_root FROM async_delegations
                  WHERE COALESCE(retry_root, '') != '' AND retry_root != delegation_id
                UNION SELECT delegation_id, retry_replacement FROM async_delegations
                  WHERE COALESCE(retry_replacement, '') != ''
                UNION SELECT delegation_id, replacement_delegation_id FROM async_delegation_child_recovery
                  WHERE COALESCE(replacement_delegation_id, '') != ''
            ), lineage(delegation_id) AS (
                SELECT ?
                UNION SELECT CASE WHEN e.source=l.delegation_id THEN e.target ELSE e.source END
                  FROM edges e JOIN lineage l ON e.source=l.delegation_id OR e.target=l.delegation_id
            ) SELECT l.delegation_id, d.task_json FROM lineage l
              LEFT JOIN async_delegations d ON d.delegation_id=l.delegation_id""", (delegation_id,)).fetchall()
        try:
            tasks = {did: json.loads(raw or "{}") for did, raw in rows}
        except (json.JSONDecodeError, TypeError):
            return False
        if any(not isinstance(task, dict) for task in tasks.values()):
            return False
        finished = {did: _finished_execution_children(task) for did, task in tasks.items()}
        if not all(finished.values()) or child_session_id not in finished[delegation_id]:
            return False
        sealed_at = time.time()
        for did, task in tasks.items():
            if not task.get("execution_sealed_at"):
                task["execution_sealed_at"] = sealed_at
                conn.execute("UPDATE async_delegations SET task_json=? WHERE delegation_id=?",
                             (json.dumps(task), did))
    return True


def recovered_children(task, result_json, error):
    """Per-task results for an abandoned unit: recorded children as they finished, the rest unknown."""
    partial = json.loads(result_json or "{}") or {}
    # One-child units record too (their whole pre-finalize window), not just batches.
    if not (partial.get("partial") and partial.get("results")):
        return None
    recorded = {r["task_index"]: r for r in partial["results"] if isinstance(r.get("task_index"), int)}
    indexes = task.get("task_indexes") or list(range(max(1, len(task.get("goals") or []))))
    return [recorded.get(i) or {"task_index": i, "status": "unknown", "summary": None, "error": error} for i in indexes]
