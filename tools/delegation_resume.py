#!/usr/bin/env python3
"""Explicit, one-shot recovery of an INTERRUPTED background delegation.

Scope (deliberately narrow — see ``maintenance/delegation-restart.md`` slice 2):

* This module NEVER re-spawns a child and never reconstructs a child at boot. It
  turns one durable ``async_delegations`` row into a *recovery brief* that the
  parent model acts on with a normal, fully-authorized ``delegate_task`` spawn.
  Everything a resumed run does is therefore visible in the parent's own
  transcript and subject to every existing spawn gate (pause, depth, capacity).
* Eligibility is intentionally minimal: a SINGLE-task delegation whose owner
  stopped without a trustworthy terminal result, that carries a routable origin,
  and that has no partial per-child results to reconcile. Batches, units with
  recorded partial results and stateless origins are refused with a truthful
  reason instead of guessed at.
* The claim is DURABLE and ONE-SHOT (``resume_state``/``resume_attempts`` on the
  row). A restart loop, a double-delivered completion, or two consumers racing
  after a restart can produce at most one recovery brief per delegation.
* The brief always instructs the new child to VERIFY workspace/external state
  first: the interrupted child may have completed side effects that its lost
  summary never reported.
* No credentials are read or persisted here. The durable row's ``task_json``
  carries only goal/context/role/model metadata written at dispatch; the resumed
  spawn resolves its own credentials through the normal delegation config path.

Boot auto-trigger is deliberately NOT wired (``AUTO_RESUME_ON_BOOT`` is False and
has no call site): automatic re-spawning of abandoned work at process start is a
separate, riskier slice and stays out until this explicit path has field
evidence.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from typing import Any, Dict, Optional, Tuple

# Boot-time automatic resume is not implemented. Kept as an explicit, greppable
# marker so a future slice has one obvious place to flip, and so nothing today
# silently behaves as if it existed.
AUTO_RESUME_ON_BOOT = False

# Durable states that mean "the owner stopped without a trustworthy terminal
# result". 'unknown' is what recover_abandoned_delegations() writes when the
# owning process disappeared; 'interrupted'/'stalled' are the in-process
# equivalents (shutdown/stop, stall force-finalize).
RESUMABLE_STATES = frozenset({"unknown", "interrupted", "stalled"})

RESUME_STATE_NONE = "none"
RESUME_STATE_CLAIMED = "claimed"

# Refusal reasons (stable tokens; the tool layer turns them into prose).
INELIGIBLE_NO_ROW = "no_such_delegation"
INELIGIBLE_STATE = "not_interrupted"
INELIGIBLE_BATCH = "batch_delegation"
INELIGIBLE_PARTIAL = "partial_results_recorded"
INELIGIBLE_STATELESS = "stateless_origin"
INELIGIBLE_CLAIMED = "already_claimed"


def _ad():
    """Late import: the ledger connection helpers live with the async registry."""
    from tools import async_delegation

    return async_delegation


_SELECT = """SELECT delegation_id, origin_session, origin_ui_session_id, parent_session_id,
       state, dispatched_at, completed_at, task_json, result_json,
       origin_session_id, resume_state, resume_attempts
FROM async_delegations WHERE delegation_id=?"""


def _row_to_record(row) -> Dict[str, Any]:
    task = {}
    result = {}
    try:
        task = json.loads(row[7] or "{}") or {}
    except (TypeError, ValueError):
        task = {}
    try:
        result = json.loads(row[8] or "{}") or {}
    except (TypeError, ValueError):
        result = {}
    return {
        "delegation_id": row[0],
        "session_key": row[1] or "",
        "origin_ui_session_id": row[2] or "",
        "parent_session_id": row[3] or "",
        "state": row[4] or "",
        "dispatched_at": row[5],
        "completed_at": row[6],
        "task": task,
        "result": result,
        "origin_session_id": row[9] or "",
        "resume_state": row[10] or RESUME_STATE_NONE,
        "resume_attempts": int(row[11] or 0),
    }


def _eligibility(record: Dict[str, Any]) -> Optional[str]:
    """None when the record may be resumed, else a stable refusal token."""
    if record["state"] not in RESUMABLE_STATES:
        return INELIGIBLE_STATE
    task = record["task"]
    if task.get("is_batch") or task.get("goals") or task.get("task_indexes"):
        return INELIGIBLE_BATCH
    if record["result"].get("results"):
        # A unit that recorded per-child results needs reconciliation, not a
        # blind re-run: refuse rather than duplicate finished children's work.
        return INELIGIBLE_PARTIAL
    if not (record["origin_session_id"] or record["parent_session_id"]):
        # Stateless origins (cron/one-shot CLI) have nowhere to deliver a
        # resumed result and no conversation that can own the recovery.
        return INELIGIBLE_STATELESS
    if record["resume_state"] != RESUME_STATE_NONE or record["resume_attempts"] > 0:
        return INELIGIBLE_CLAIMED
    if not str(task.get("goal") or "").strip():
        # Without the original goal there is nothing truthful to resume.
        return INELIGIBLE_STATE
    return None


def inspect_resumable(delegation_id: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """``(record, None)`` when *delegation_id* is resumable, else ``(record_or_None, reason)``.

    Read-only: takes no claim. The claim is taken separately by
    :func:`claim_resume`, which re-checks every condition inside one transaction.
    """
    ad = _ad()
    if not delegation_id:
        return None, INELIGIBLE_NO_ROW
    with ad._DB_LOCK, ad._transaction() as conn:
        row = conn.execute(_SELECT, (delegation_id,)).fetchone()
    if row is None:
        return None, INELIGIBLE_NO_ROW
    record = _row_to_record(row)
    return record, _eligibility(record)


def claim_resume(delegation_id: str, consumer: str = "delegate_task") -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Durably take the ONE recovery claim for *delegation_id*.

    Returns ``(record, None)`` on success — the record snapshot as it was when
    claimed — or ``(record_or_None, reason)`` when the row is missing, ineligible,
    or already claimed. Re-reads and re-checks eligibility inside the same
    transaction as the UPDATE, so two racing callers cannot both win, and the
    guarded UPDATE (``resume_state='none' AND resume_attempts=0``) is the
    authority even if the read raced.
    """
    ad = _ad()
    if not delegation_id:
        return None, INELIGIBLE_NO_ROW
    claim_id = f"{consumer}:{os.getpid()}:{uuid.uuid4().hex}"
    now = time.time()
    with ad._DB_LOCK, ad._transaction() as conn:
        row = conn.execute(_SELECT, (delegation_id,)).fetchone()
        if row is None:
            return None, INELIGIBLE_NO_ROW
        record = _row_to_record(row)
        reason = _eligibility(record)
        if reason is not None:
            return record, reason
        changed = conn.execute(
            """UPDATE async_delegations
               SET resume_state=?, resume_attempts=resume_attempts+1,
                   resume_claim=?, resume_claimed_at=?, updated_at=?
               WHERE delegation_id=? AND resume_state=? AND resume_attempts=0""",
            (RESUME_STATE_CLAIMED, claim_id, now, now, delegation_id, RESUME_STATE_NONE),
        ).rowcount
        if changed != 1:
            return record, INELIGIBLE_CLAIMED
    record["resume_claim"] = claim_id
    record["resume_state"] = RESUME_STATE_CLAIMED
    record["resume_attempts"] = 1
    return record, None


_STATE_PHRASE = {
    "unknown": "its owning process exited before recording a result, so its outcome is unknown",
    "interrupted": "it was interrupted before finishing (shutdown, restart, or an explicit stop)",
    "stalled": "it stopped making progress and was force-finalized as stalled",
}


def build_recovery_instruction(record: Dict[str, Any]) -> str:
    """The context block a parent must pass to the replacement subagent.

    Always leads with state verification: the interrupted child may have already
    performed side effects (files written, commits made, external writes) that
    its lost summary never reported. A resumed child that assumes a clean start
    can duplicate those effects.
    """
    task = record.get("task") or {}
    goal = str(task.get("goal") or "").strip()
    original_context = str(task.get("context") or "").strip()
    phrase = _STATE_PHRASE.get(record.get("state", ""), "it did not report a terminal result")
    stopped_at = record.get("completed_at") or record.get("dispatched_at")
    when = time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime(stopped_at)) if stopped_at else "an unknown time"
    lines = [
        f"RECOVERY OF INTERRUPTED DELEGATION {record.get('delegation_id')}.",
        f"A previous subagent was given this exact goal and {phrase}. It stopped around {when}.",
        "",
        "BEFORE doing any new work you MUST verify current state, because the interrupted run "
        "may have already completed part of the task without reporting it:",
        "- Inspect the actual workspace/repository state (files, git status and log, build artifacts).",
        "- Read back any external target the goal names before writing to it again; never repeat an "
        "external write without confirming it did not already land.",
        "- Report what you found already done versus what you actually did in this run.",
        "",
        "Then finish only the remaining work. Do not assume a clean starting point, and do not "
        "assume the previous run accomplished nothing.",
        "",
        f"ORIGINAL GOAL: {goal}",
    ]
    if original_context:
        lines += ["", "ORIGINAL CONTEXT:", original_context]
    return "\n".join(lines)
