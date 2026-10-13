"""Checkpoint evidence and classification for the existing terminal ownership transaction."""

from __future__ import annotations

import json
from typing import Any

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


def recovered_children(task, result_json, error):
    """Per-task results for an abandoned unit: recorded children as they finished, the rest unknown."""
    partial = json.loads(result_json or "{}") or {}
    # One-child units record too (their whole pre-finalize window), not just batches.
    if not (partial.get("partial") and partial.get("results")):
        return None
    recorded = {r["task_index"]: r for r in partial["results"] if isinstance(r.get("task_index"), int)}
    indexes = task.get("task_indexes") or list(range(max(1, len(task.get("goals") or []))))
    return [recorded.get(i) or {"task_index": i, "status": "unknown", "summary": None, "error": error} for i in indexes]
