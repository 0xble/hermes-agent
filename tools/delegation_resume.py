#!/usr/bin/env python3
"""Explicit, one-shot recovery of an INTERRUPTED background delegation.

Scope (deliberately narrow — see ``maintenance/delegation-restart.md`` slice 2):

* This module NEVER re-spawns a child and never reconstructs a child at boot. It
  turns one durable ``async_delegations`` row into a *recovery brief* that the
  parent model acts on with a normal, fully-authorized ``delegate_task`` spawn.
  Everything a resumed run does is therefore visible in the parent's own
  transcript and subject to every existing spawn gate (pause, depth, capacity).
* A single-goal delegation keeps the existing scalar recovery path, even when
  it has background batch-format metadata. Real fan-outs use durable per-child
  claims: only unfinished, unconsumed children may be replaced, and recorded
  successes are never rerun. Stateless origins are refused rather than guessed at.
* The claim is DURABLE and ONE-SHOT (``resume_state``/``resume_attempts`` on the
  row). A restart loop, a double-delivered completion, or two consumers racing
  after a restart can produce at most one recovery brief per delegation.
* The brief always instructs the new child to VERIFY workspace/external state
  first: the interrupted child may have completed side effects that its lost
  summary never reported.
* No credentials are read or persisted here. The durable row's ``task_json``
  carries only goal/context/role/model metadata written at dispatch; the resumed
  spawn resolves its own credentials through the normal delegation config path.
* Boot auto-resume only emits a parent-facing notice. It never claims
  ``resume_state`` and never starts a child; the parent must call the explicit
  resume action through the normal tool gates.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import time
import uuid
from typing import Any, Dict, List, Optional, Tuple

from persistence.authority.coordination import DelegationRetryChange
from persistence.core.errors import StoreContention

logger = logging.getLogger(__name__)

# Boot-time automatic resume is a parent-facing trigger only. The gateway reads
# ``gateway.auto_resume_on_boot`` from the active profile before queueing notices;
# the explicit one-shot resume action is independent of that setting.
# Persisted producer token for a planned gateway restart. It selects restart wording and the explicit
# resume notice in place of an automatic rerun; recovery authority comes from the absence of user-stop evidence.
GATEWAY_RESTART_CUT_REASON = "gateway shutdown (restart)"
AUTO_RESUME_CLAIM_TTL_SECONDS = 300.0
AUTO_RESUME_STATE_NONE = "none"
AUTO_RESUME_STATE_CLAIMED = "claimed"
AUTO_RESUME_STATE_DELIVERED = "delivered"

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
INELIGIBLE_SEALED = "execution_sealed"


class RetrySweepDeferred(list):
    """An empty sweep result whose busy deferral remains visible to the gateway owner."""

    busy = True


def _ad():
    """Late import: the ledger connection helpers live with the async registry."""
    from tools import async_delegation

    return async_delegation


def _row_to_record(row) -> dict[str, Any]:
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
        "auto_resume_state": row[12] or AUTO_RESUME_STATE_NONE,
        "auto_resume_claim": row[13] or "",
        "auto_resume_claimed_at": row[14],
    }


def _batch_recovery_candidates(record: dict[str, Any]) -> list[dict[str, Any]]:
    """Return only fan-out children without a trustworthy recorded success."""
    task = record.get("task") or {}
    goals = list(task.get("goals") or [])
    indexes = task.get("task_indexes") or list(range(len(goals)))
    # All background dispatches use batch formatting, even for one goal.
    # Only real fan-outs use child claims; preserve scalar retry admission.
    if len(goals) <= 1:
        return []
    result = record.get("result") or {}
    recorded = {entry.get("task_index"): entry for entry in result.get("results") or []
                if isinstance(entry, dict) and isinstance(entry.get("task_index"), int)}
    candidates = []
    for index in indexes:
        entry = recorded.get(index) or {}
        status = str(entry.get("status") or "unknown").lower()
        if status in {"completed", "success", "succeeded"} or _child_already_resuming(entry):
            continue
        candidates.append({
            "task_index": index,
            "goal": goals[index] if 0 <= index < len(goals) else task.get("goal", ""),
            "result": entry,
        })
    return candidates


def _claim_batch_children(conn, record: dict[str, Any], claim: str) -> list[int]:
    """Take one durable claim for each unfinished child in a fan-out."""
    candidates = _batch_recovery_candidates(record)
    claimed = []
    now = time.time()
    for child in candidates:
        changed = conn.execute(
            """INSERT OR IGNORE INTO async_delegation_child_recovery
               (delegation_id, task_index, claim, claimed_at)
               VALUES (?, ?, ?, ?)""",
            (record["delegation_id"], child["task_index"], claim, now),
        ).rowcount
        if changed == 1:
            claimed.append(child["task_index"])
    return claimed


def _batch_recovery_tasks(record: dict[str, Any], indexes: Optional[list[int]] = None) -> list[dict[str, str]]:
    """Build normal delegate_task task payloads for claimed unfinished children."""
    task = record.get("task") or {}
    candidates = _batch_recovery_candidates(record)
    wanted = set(indexes) if indexes is not None else None
    result = record.get("result") or {}
    recorded = {entry.get("task_index"): entry for entry in result.get("results") or []
                if isinstance(entry, dict) and isinstance(entry.get("task_index"), int)}
    transcripts = task.get("task_transcripts") or {}
    tails = result.get("transcript_tails") or {}
    task_contexts = task.get("task_contexts") or {}
    shared = str(task.get("context") or "").strip()
    tasks = []
    for child in candidates:
        index = child["task_index"]
        if wanted is not None and index not in wanted:
            continue
        child_context = task_contexts.get(str(index), task_contexts.get(index)) if isinstance(task_contexts, dict) else None
        child_task = dict(task)
        child_task.update(goal=child["goal"], context=str(child_context or shared).strip())
        child_record = dict(record, task=child_task)
        instruction = build_recovery_instruction(child_record)
        lines = [instruction, "", f"RECOVERY CHILD INDEX: {record['delegation_id']}:{index}"]
        sibling_lines = []
        for sibling_index, entry in sorted(recorded.items()):
            sibling_lines.append(
                f"- child {sibling_index}: status={entry.get('status', 'unknown')} "
                f"summary={entry.get('summary') or entry.get('error') or '(none)'}"
            )
        if sibling_lines:
            lines += ["", "RECORDED SIBLING RESULTS (do not repeat successful work):", *sibling_lines]
        transcript = transcripts.get(str(index), transcripts.get(index)) if isinstance(transcripts, dict) else None
        if transcript:
            lines += ["", f"THIS CHILD'S PRIOR TRANSCRIPT: {transcript}"]
        tail = tails.get(str(index), tails.get(index)) if isinstance(tails, dict) else None
        if tail:
            lines += ["", "THIS CHILD'S PRIOR TRANSCRIPT TAIL:", str(tail)]
        if result.get("git_state_hint"):
            lines += ["", f"OWNER GIT STATE AT CUT: {result['git_state_hint']}"]
        tasks.append({"goal": str(child["goal"] or ""), "context": "\n".join(lines)})
    return tasks


def _recovery_admission_error(record: dict[str, Any]) -> Optional[str]:
    """Shared closure gate for new claims and replacement admission.

    An explicit execution seal closes admission without changing its outcome
    or pretending that the user stopped it.
    """
    if (record.get("task") or {}).get("execution_sealed_at"):
        return INELIGIBLE_SEALED
    if _recovery_stopped(record):
        return INELIGIBLE_STATE
    return None


def _batch_resume_eligibility(record: dict[str, Any]) -> Optional[str]:
    """Eligibility for a fan-out recovery, kept separate from legacy single-task rules."""
    if reason := _recovery_admission_error(record):
        return reason
    if record["state"] not in RESUMABLE_STATES:
        return INELIGIBLE_STATE
    if not (record["parent_session_id"] or record["origin_session_id"]):
        return INELIGIBLE_STATELESS
    if record["resume_state"] != RESUME_STATE_NONE or record["resume_attempts"] > 0:
        return INELIGIBLE_CLAIMED
    if not _batch_recovery_candidates(record):
        return INELIGIBLE_PARTIAL
    return None


def _unfinished_scalar_result(task: dict[str, Any], results: Any) -> bool:
    """Batch-format evidence may describe one unfinished scalar, never a success."""
    return bool(
        task.get("is_batch") and isinstance(task.get("goals"), list) and len(task["goals"]) == 1
        and isinstance(results, list) and len(results) == 1 and isinstance(results[0], dict)
        and results[0].get("task_index", 0) == 0
        and str(results[0].get("status") or "").lower() in RESUMABLE_STATES | {"cut"}
    )


def _eligibility(record: dict[str, Any]) -> Optional[str]:
    """None when the record may be resumed, else a stable refusal token."""
    if reason := _recovery_admission_error(record):
        return reason
    if record["state"] not in RESUMABLE_STATES:
        return INELIGIBLE_STATE
    task = record["task"]
    # Production background units retain batch metadata for completion formatting
    # even when the original call has one goal. That scalar case is resumable
    # when it is the complete call, but split fan-out units remain ineligible.
    if task.get("is_batch") or task.get("goals"):
        goals = task.get("goals")
        if not (task.get("is_batch") and isinstance(goals, list) and len(goals) == 1
                and not task.get("task_indexes")):
            return INELIGIBLE_BATCH
    if task.get("task_indexes"):
        return INELIGIBLE_BATCH
    results = record["result"].get("results")
    # One-goal background units serialize unfinished scalar evidence as a list.
    # Successes and ambiguous/multi-child evidence must never be replayed.
    if results and not _unfinished_scalar_result(task, results):
        return INELIGIBLE_PARTIAL
    if not (record["origin_session_id"] or record["parent_session_id"]):
        return INELIGIBLE_STATELESS
    if record["resume_state"] != RESUME_STATE_NONE or record["resume_attempts"] > 0:
        return INELIGIBLE_CLAIMED
    if not str(task.get("goal") or "").strip():
        return INELIGIBLE_STATE
    return None


def inspect_resumable(delegation_id: str) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """Read the recovery record and check eligibility without claiming it."""
    ad = _ad()
    if not delegation_id or not ad._ledger_exists():
        return None, INELIGIBLE_NO_ROW
    with ad._ledger() as store:
        record = store.coordination.delegation_recovery_record(delegation_id)
    return (record, _eligibility(record)) if record is not None else (None, INELIGIBLE_NO_ROW)


def list_boot_candidates(limit: int = 32) -> list[dict[str, Any]]:
    """Find eligible routed parents. The boot watcher claims separately, immediately before admission."""
    limit = max(0, int(limit))
    ad = _ad()
    if not limit or not ad._ledger_exists():
        return []
    with ad._ledger() as store:
        return list(store.coordination.list_delegation_boot_candidates(
            stale_before=time.time() - AUTO_RESUME_CLAIM_TTL_SECONDS, limit=limit,
            eligible=lambda record: _eligibility(record) is None,
        ))


def _boot_eligibility(record: dict[str, Any]) -> Optional[str]:
    if reason := _eligibility(record):
        return reason
    if not record["parent_session_id"] or not (record["session_key"] or record["origin_session_id"]):
        return INELIGIBLE_STATELESS
    return None


def claim_auto_resume_trigger(record_or_id: dict[str, Any] | str,
                              consumer: str = "gateway-boot") -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """Claim a parent notice without consuming the explicit resume claim."""
    delegation_id = (record_or_id.get("delegation_id") if isinstance(record_or_id, dict)
                     else str(record_or_id or ""))
    ad = _ad()
    if not delegation_id or not ad._ledger_exists():
        return None, INELIGIBLE_NO_ROW
    now = time.time()
    with ad._ledger() as store:
        record, reason, _ = store.coordination.claim_delegation_recovery(
            delegation_id, claim=f"{consumer}:{os.getpid()}:{uuid.uuid4().hex}",
            now=now, eligible=_boot_eligibility, boot_stale_before=now - AUTO_RESUME_CLAIM_TTL_SECONDS,
        )
    return record, reason


def _settle_boot_claim(delegation_id: str, claim_id: str, *, delivered: bool) -> bool:
    ad = _ad()
    if not claim_id or not ad._ledger_exists():
        return False
    with ad._ledger() as store:
        return store.coordination.settle_delegation_boot_claim(
            delegation_id, claim=claim_id, delivered=delivered, now=time.time(),
        )


def release_auto_resume_trigger(delegation_id: str, claim_id: str) -> bool:
    """Refund a boot trigger when parent admission fails."""
    return _settle_boot_claim(delegation_id, claim_id, delivered=False)


def complete_auto_resume_trigger(delegation_id: str, claim_id: str) -> bool:
    """Make an accepted notice terminal so later boots do not repeat it."""
    return _settle_boot_claim(delegation_id, claim_id, delivered=True)


AUTO_RESUME_NOTICE_OPEN = "[IMPORTANT: An interrupted single-task background delegation "


def build_auto_resume_notice(record: dict[str, Any]) -> str:
    """Build a bounded parent instruction; never include task context or credentials."""
    from tools.process_registry_notifications import PROCESS_NOTIFICATION_END
    delegation_id = str(record.get("delegation_id") or "")
    return (
        AUTO_RESUME_NOTICE_OPEN + "is eligible for conservative recovery. "
        f"Delegation ID: {delegation_id}. Do not execute the original task directly and do not assume it is "
        "safe to rerun. First call delegate_task with action='resume' and this exact subagent_id; the normal "
        "one-shot recovery gate will verify eligibility and provide a state-verification brief. If that action "
        "is refused, report the refusal rather than spawning a replacement yourself.]"
        f"\n{PROCESS_NOTIFICATION_END}"
    )


def claim_resume(delegation_id: str, consumer: str = "delegate_task") -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """Take the explicit one-shot claim for an interrupted single-task delegation."""
    ad = _ad()
    if not delegation_id or not ad._ledger_exists():
        return None, INELIGIBLE_NO_ROW
    with ad._ledger() as store:
        record, reason, _ = store.coordination.claim_delegation_recovery(
            delegation_id, claim=f"{consumer}:{os.getpid()}:{uuid.uuid4().hex}",
            now=time.time(), eligible=_eligibility,
        )
    return record, reason


def claim_batch_resume(delegation_id: str, consumer: str = "delegate_task") -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """Claim each unfinished child of a fan-out exactly once."""
    ad = _ad()
    if not delegation_id or not ad._ledger_exists():
        return None, INELIGIBLE_NO_ROW
    with ad._ledger() as store:
        record, reason, indexes = store.coordination.claim_delegation_recovery(
            delegation_id, claim=f"{consumer}:{os.getpid()}:{uuid.uuid4().hex}",
            now=time.time(), eligible=_batch_resume_eligibility,
            children=lambda row: [child["task_index"] for child in _batch_recovery_candidates(row)],
        )
    if reason is None and record is not None:
        record["recovery_tasks"] = _batch_recovery_tasks(record, indexes)
    return record, reason


_STATE_PHRASE = {
    "unknown": "no terminal result was recorded, so its outcome is unknown",
    "interrupted": "it stopped before finishing",
    "stalled": "it stopped making progress and was force-finalized as stalled",
}


def build_recovery_instruction(record: dict[str, Any]) -> str:
    """The context block a parent must pass to the replacement subagent.

    Always leads with state verification: the interrupted child may have already
    performed side effects (files written, commits made, external writes) that
    its lost summary never reported. A resumed child that assumes a clean start
    can duplicate those effects.
    """
    task = record.get("task") or {}
    goal = str(task.get("goal") or "").strip()
    original_context = str(task.get("context") or "").strip()
    state = record.get("state", "")
    phrase = _STATE_PHRASE.get(state, "it failed before finishing"
                               + (f" ({record['retry_reason']})" if record.get("retry_reason") else ""))
    heading = "INTERRUPTED" if state in _STATE_PHRASE else "FAILED"
    stopped_at = record.get("completed_at") or record.get("dispatched_at")
    when = time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime(stopped_at)) if stopped_at else "an unknown time"
    lines = [
        f"RECOVERY OF {heading} DELEGATION {record.get('delegation_id')}.",
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
    transcripts = task.get("task_transcripts") or {}
    if isinstance(transcripts, dict) and transcripts:
        lines += ["", "PRIOR RUN TRANSCRIPT (read it first; it shows what was already done):",
                  *[f"- {path}" for _, path in sorted(transcripts.items())]]
    if original_context:
        lines += ["", "ORIGINAL CONTEXT:", original_context]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Durable automatic retry (guarantee: durable result/retry settlement)
# ---------------------------------------------------------------------------
#
# Messaging-gateway-origin background units are inserted ``retry_state='armed'``. CLI/TUI
# units remain on the main one-shot resume path. When an armed unit reaches a
# terminal state, :func:`schedule_retry` classifies it once:
#
#   success / user stop / superseded / stateless origin  -> 'none' (today's path)
#   transient failure or lost owner, budget left         -> 'scheduled' (due_at)
#   anything else                                         -> 'terminal'
#
# The gateway watcher calls :func:`sweep_retries`; it claims due rows atomically
# and returns actions. A due 'scheduled' row becomes a parent notice telling it to
# call ``delegate_task(action='resume')`` and spawn with the recovery brief. A
# notice the parent ignores (e.g. NO_REPLY) is re-sent once after
# NOTICE_GRACE_S. If it is still undispatched, the row becomes terminal and is
# settled quietly. The sole direct terminal report is a successful result whose
# durable delivery was lost and whose parent session is gone. The spawn of the
# replacement is linked back through the recovery brief's heading
# (``_persist_dispatch`` -> :func:`link_replacement`), which carries the lineage
# budget forward.

RETRY_ARMED = "armed"
RESULT_UNDELIVERABLE = "result_undeliverable"
RETRY_NONE = "none"
RETRY_CANCELLED = "cancelled"
RETRY_SCHEDULED = "scheduled"
RETRY_NOTICING = "noticing"
RETRY_NOTIFIED = "notified"
RETRY_DISPATCHING = "dispatching"
RETRY_DISPATCHED = "dispatched"
RETRY_TERMINAL = "terminal"
RETRY_REPORTING = "reporting"
RETRY_REPORTED = "reported"
# States that still owe the user an outcome: never pruned (async_delegation retention).
RETRY_PENDING_STATES = (RETRY_ARMED, RETRY_SCHEDULED, RETRY_NOTICING, RETRY_NOTIFIED,
                        RETRY_DISPATCHING, RETRY_TERMINAL, RETRY_REPORTING)

MAX_RETRY_ATTEMPTS = 6               # replacements per lineage
MAX_LINEAGE_AGE_S = 24 * 3600.0      # wall-clock ceiling from the root dispatch
BASE_BACKOFF_S = 300.0               # 5m, doubling per attempt
MAX_BACKOFF_S = 3600.0               # 60m cap
NOTICE_GRACE_S = 900.0               # time the parent gets to dispatch after a notice
MAX_RETRY_NOTICES = 2                # first notice + one re-prompt
RETRY_CLAIM_TTL_S = 300.0            # crashed notice/report claim becomes reclaimable

# FailoverReason values (agent/error_classifier.py) that a later attempt can outlive.
TRANSIENT_REASONS = frozenset({
    "rate_limit", "upstream_rate_limit", "overloaded", "server_error", "timeout",
    "incomplete_response", "upstream_blocked",
})
_TRANSIENT_TEXT = ("429", "rate limit", "rate-limit", "ratelimit", "cooling down", "overloaded", "503", "502",
                   "529", "500 internal", "timed out", "timeout", "temporarily unavailable",
                   "all providers failed", "fallback chain exhausted", "connection reset", "try again later")
_LOST_OWNER_STATES = frozenset({"unknown", "interrupted", "stalled"})
REASON_COMPLETED = "completed"
REASON_COMPLETED_BEFORE_OWNER_EXIT = "completed before owner exit"
# Classifications that mean a real result exists and only its delivery is still owed.
SUCCESS_REASONS = (REASON_COMPLETED, REASON_COMPLETED_BEFORE_OWNER_EXIT)
_USER_STOP_MARKERS = ("stop_command", "user_stop", "session_reset", "new_session", "new_command", "session_end",
                      "cancel", "/stop")
_OK_STATUSES = ("completed", "success")
_RECOVERY_HEADING_RE = __import__("re").compile(r"RECOVERY OF (?:INTERRUPTED |FAILED )?DELEGATION (deleg_[\w-]+)\.")
_RESETS_AT_RE = __import__("re").compile(r"resets at (\d{1,2}):(\d{2})", __import__("re").IGNORECASE)

_RETRY_SELECT = """SELECT delegation_id, origin_session, origin_ui_session_id, parent_session_id,
       state, dispatched_at, completed_at, task_json, result_json, origin_session_id,
       resume_state, resume_attempts, auto_resume_state, auto_resume_claim, auto_resume_claimed_at,
       COALESCE(event_json, (SELECT event_json FROM async_delegation_events e
                            WHERE e.delegation_id=async_delegations.delegation_id
                              AND e.event_kind='terminal_fallback')),
       delivery_state, retry_state, retry_reason, retry_due_at, retry_notices,
       retry_claim, retry_claimed_at, retry_root, retry_attempt, retry_root_started_at, retry_replacement, resume_claim
FROM async_delegations"""


def _retry_record(row) -> dict[str, Any]:
    record = _row_to_record(row[:15])
    try:
        event = json.loads(row[15] or "{}") or {}
    except (TypeError, ValueError):
        event = {}
    record.update(
        event=event, delivery_state=row[16] or "", retry_state=row[17] or RETRY_NONE, retry_reason=row[18] or "",
        retry_due_at=row[19], retry_notices=int(row[20] or 0), retry_claim=row[21] or "",
        retry_claimed_at=row[22], retry_root=row[23] or row[0], retry_attempt=int(row[24] or 0),
        retry_root_started_at=row[25] or row[5], retry_replacement=row[26] or "", resume_claim=row[27] or "",
    )
    return record


def _load(conn, delegation_id: str) -> Optional[dict[str, Any]]:
    row = conn.execute(_RETRY_SELECT + " WHERE delegation_id=?", (delegation_id,)).fetchone()
    return _retry_record(row) if row else None


def _goal_count(task: dict[str, Any]) -> int:
    goals = task.get("goals")
    return len(goals) if isinstance(goals, list) and goals else 1


def _child_results(record: dict[str, Any]) -> list:
    results = record["result"].get("results")
    return [r for r in results if isinstance(r, dict)] if isinstance(results, list) else []


def _child_already_resuming(entry: dict[str, Any]) -> bool:
    from tools.delegate_tool_auto_resume import waiting_on_resume

    return entry.get("status") == "waiting_to_resume" or waiting_on_resume(entry)


def _texts(record: dict[str, Any]) -> str:
    parts = [record["result"].get("error"), record["result"].get("summary"), record["event"].get("error")]
    for r in _child_results(record):
        parts += [r.get("error"), r.get("summary"), r.get("interrupt_reason"), r.get("failure_reason")]
    parts.append(record["event"].get("interrupt_reason"))
    return "\n".join(str(p) for p in parts if p)


def _reset_delay(text: str, now: float) -> Optional[float]:
    from agent.retry_utils import reset_delay_from_message
    delay = reset_delay_from_message(text)
    if delay is None and (m := _RESETS_AT_RE.search(text)):
        lt = time.localtime(now)
        target = time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, int(m.group(1)), int(m.group(2)), 0, 0, 0, -1))
        delay = target - now if target > now else target + 86400 - now
    return delay if delay is not None and 0 < delay <= MAX_LINEAGE_AGE_S else None


class RetryAdmissionError(RuntimeError):
    """A bounded retry was rejected; it must never execute via an inline fallback."""


def _retry_budget_reason(record: dict[str, Any], now: float, *, check_attempts: bool = True) -> Optional[str]:
    root_start = float(record.get("retry_root_started_at") or record["dispatched_at"])
    if now - root_start >= MAX_LINEAGE_AGE_S:
        return "retry budget exhausted (24h)"
    if check_attempts and int(record.get("retry_attempt") or 0) >= MAX_RETRY_ATTEMPTS:
        return f"retry budget exhausted ({MAX_RETRY_ATTEMPTS} attempts)"
    return None


def _expire_retry(conn, record: dict[str, Any], now: float, *, check_attempts: bool = True,
                  forced_reason: Optional[str] = None) -> Optional[str]:
    reason = forced_reason or _retry_budget_reason(record, now, check_attempts=check_attempts)
    if reason:
        conn.execute("""UPDATE async_delegations SET retry_state='terminal', retry_reason=?,
                        retry_due_at=NULL, retry_claim=NULL, retry_claimed_at=NULL, updated_at=?
                        WHERE delegation_id=?""", (reason, now, record["delegation_id"]))
        record.update(retry_state=RETRY_TERMINAL, retry_reason=reason, retry_due_at=None,
                      retry_claim="", retry_claimed_at=None)
    return reason


def is_user_cancel_reason(reason: str) -> bool:
    """Only trusted stop-producer categories belong here, never free-form child output."""
    return any(marker in str(reason).lower() for marker in _USER_STOP_MARKERS)


def is_trusted_restart_cut(reason: Any) -> bool:
    """The canonical gateway-restart cut token.

    Recovery authority does not depend on it: any cut without positive user-stop evidence is
    recoverable (``_recovery_stopped``). It selects only restart wording and the single resume
    notice that replaces the automatic retry for a restart cut."""
    return str(reason).strip().casefold() == GATEWAY_RESTART_CUT_REASON


def _cut_reason(record: dict[str, Any]) -> Any:
    return (record.get("event") or {}).get("cut_reason") or (record.get("result") or {}).get("cut_reason")


def _recovery_stopped(record: dict[str, Any]) -> bool:
    """Only positive user-stop evidence revokes recovery authority.

    Gateway lifecycle cuts are recoverable even when their producer did not emit
    the canonical restart token: older builds persisted phase-specific reasons,
    and an unplanned shutdown may still be followed by a service-manager boot.
    """
    metadata = [record.get("event") or {}, record.get("result") or {}, *_child_results(record)]
    return record.get("retry_state") == RETRY_CANCELLED or any(
        is_user_cancel_reason(item.get("cut_reason") or "")
        or is_user_cancel_reason(item.get("interrupt_reason") or "") for item in metadata
    )


def gateway_retry_origin(record: dict[str, Any]) -> bool:
    """Only messaging gateway routes have the driver; CLI/TUI retain main's resume behavior."""
    # ``origin_session_id`` is the gateway's durable wake target, not a CLI/TUI
    # marker; gateway chats commonly set it so a restart can route the result.
    if record.get("origin_ui_session_id"):
        return False
    parts = str(record.get("session_key") or "").split(":")
    # build_session_key may end at the chat-type slot (bare per-platform DM sink), so require
    # only namespace, platform and chat type.
    if len(parts) < 4 or parts[0] != "agent" or not parts[3]:
        return False
    if parts[1] not in {"main", "main~"} and not __import__("re").fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", parts[1]):
        return False
    from gateway.config import Platform
    try:
        platform = Platform(parts[2])
    except ValueError:
        return False
    return platform not in {Platform.LOCAL, Platform.API_SERVER}


def cancel_pending_retries(*, delegation_id: str = "", session_key: str = "", origin_ui_session_id: str = "",
                           parent_session_id: str = "", all_sessions: bool = False,
                           reason: str = "stop_command") -> set[str]:
    """Cancel owed retries and revoke unadmitted legacy recovery claims in one stop transaction.

    ``all_sessions`` cancels owed retries everywhere, but a claimed legacy recovery
    (``retry_state='none'``) belongs to the session that claimed it: it is revoked only by
    ``delegation_id`` or by the stopping session's selectors, never by an unscoped stop."""
    ad = _ad()
    if not (delegation_id or session_key or origin_ui_session_id or parent_session_id or all_sessions):
        return set()
    if not ad._ledger_exists():
        return set()
    with ad._ledger() as store:
        return store.coordination.cancel_delegation_retries(
            delegation_id=delegation_id, origin_session=session_key,
            origin_ui_session_id=origin_ui_session_id, parent_session_id=parent_session_id,
            all_sessions=all_sessions, reason=str(reason or "stop_command"), now=time.time(),
            pending_states=RETRY_PENDING_STATES,
        )

def retry_action_is_current(action: dict[str, Any]) -> bool:
    """Collected actions lose authority when /stop clears their durable claim."""
    record = retry_status(action["delegation_id"])
    expected = RETRY_NOTICING if action["kind"] == "notice" else RETRY_REPORTING
    return bool(record and record["retry_state"] == expected and record["retry_claim"] == action["claim"])


def retry_notice_is_current(delegation_id: str, claim: str) -> bool:
    """An admitted notice may still run while its row holds the notice's claim.

    Admission moves ``noticing`` to ``notified`` under the same claim. A cancel clears the
    claim, and a dispatch or a later notice takes a new one, so either retires this wake."""
    record = retry_status(delegation_id)
    return bool(record and record["retry_state"] in (RETRY_NOTICING, RETRY_NOTIFIED)
                and record["retry_claim"] == claim)


def classify_outcome(record: dict[str, Any]) -> tuple[str, str]:
    """``(verdict, reason)`` with verdict ``none`` | ``retry`` | ``terminal``. Pure; no I/O."""
    state = record["state"]
    task = record["task"]
    if record.get("delivery_state") == "superseded" or record["event"].get("superseded_by"):
        return RETRY_NONE, "superseded"
    if not (record["parent_session_id"] or record["origin_session_id"]):
        return RETRY_NONE, "stateless origin"  # cron/one-shot: no chat owns a recovery
    results = _child_results(record)
    if record["result"].get("waiting_to_resume") or record["event"].get("waiting_to_resume"):
        return RETRY_NONE, "native child resume already scheduled"
    if results and all(_child_already_resuming(entry) for entry in results):
        return RETRY_NONE, "native child resume already scheduled"
    if not results and _child_already_resuming(record["result"]):
        return RETRY_NONE, "native child resume already scheduled"
    failed = [r for r in results if r.get("status") not in _OK_STATUSES and not _child_already_resuming(r)]
    if state == "completed" and not failed:
        return RETRY_NONE, REASON_COMPLETED
    if (state in _LOST_OWNER_STATES and results and len(results) >= _goal_count(task)
            and all(r.get("status") in _OK_STATUSES for r in results)):
        # Every child durably finished before the owner died: deliver that result, never rerun the work.
        return RETRY_NONE, REASON_COMPLETED_BEFORE_OWNER_EXIT
    text = _texts(record)
    # Only trusted stop-producer metadata counts here, never free-form error or child output.
    stop_text = " ".join(str(x) for x in (record["event"].get("interrupt_reason"),
                                          record["result"].get("interrupt_reason"),
                                          *(r.get("interrupt_reason") for r in results)) if x).lower()
    if is_user_cancel_reason(record["event"].get("cut_reason")) or is_user_cancel_reason(record["result"].get("cut_reason")):
        return RETRY_NONE, "stopped by user"
    if (state in {"interrupted", "cancelled"} or any(r.get("status") == "interrupted" for r in results)) and is_user_cancel_reason(stop_text):
        return RETRY_NONE, "stopped by user"
    if _recovery_stopped(record):
        return RETRY_NONE, "stopped without restart continuation"
    if state == "cancelled":
        return RETRY_NONE, "cancelled"
    if _goal_count(task) > 1 or task.get("task_indexes"):
        # Lost-owner/cut batches go through the same retry claim as scalar work;
        # the claim returns state-verifying briefs only for unfinished indexes.
        if state in RESUMABLE_STATES and _batch_recovery_candidates(record):
            return "retry", state
        done = len(results) - len(failed)
        return RETRY_TERMINAL, f"{len(failed) or 'some'} of {_goal_count(task)} parallel tasks did not finish ({done} done); needs review"
    if state in _LOST_OWNER_STATES or (failed and all(r.get("status") == "interrupted" for r in failed)):
        return "retry", state if state in _LOST_OWNER_STATES else "interrupted"
    if state == "failed" and not results and not record["event"] and not _texts(record):
        return "retry", "not started"  # the scheduler rejected it before any child ran
    reasons = {str(r.get("failure_reason") or "") for r in failed} - {""}
    if reasons and reasons <= TRANSIENT_REASONS:
        return "retry", min(reasons)
    if reasons - {"unknown"}:
        return RETRY_TERMINAL, ", ".join(sorted(reasons - {"unknown"}))
    if any(p in text.lower() for p in _TRANSIENT_TEXT):
        return "retry", "transient provider error"
    first_line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "error")
    return RETRY_TERMINAL, first_line[:160]


def _route_chain(record: dict[str, Any]) -> list[tuple[str, str, str]]:
    task = record.get("task") or {}
    return [tuple(route) for route in task.get("route_chain") or []
            if isinstance(route, (list, tuple)) and len(route) == 3]


def _backoff(attempt: int) -> float:
    return min(MAX_BACKOFF_S, BASE_BACKOFF_S * (2 ** max(0, attempt)))


_ACTIVE_RETRY_ORIGIN_STATES = frozenset({"new", "queued", "admitted", "running", "stalling", "finalizing"})


def _classify_retry(record, now, routes, available_at):
    if (record["retry_state"] != RETRY_ARMED or record["state"] in _ACTIVE_RETRY_ORIGIN_STATES
            or _recovery_admission_error(record) == INELIGIBLE_SEALED):
        return None
    verdict, reason = classify_outcome(record)
    due = None
    attempt = record["retry_attempt"]
    root_start = float(record["retry_root_started_at"] or now)
    if verdict == "retry":
        if attempt >= MAX_RETRY_ATTEMPTS:
            verdict, reason = RETRY_TERMINAL, f"retry budget exhausted after {attempt} retries ({reason})"
        elif now - root_start >= MAX_LINEAGE_AGE_S:
            verdict, reason = RETRY_TERMINAL, f"retry budget exhausted: still failing after 24h ({reason})"
        else:
            candidates = []
            if (available_at is not None and _route_chain(record) == routes
                    and 0 < available_at - now <= MAX_LINEAGE_AGE_S):
                candidates.append(available_at - now)
            if reset_delay := _reset_delay(_texts(record), now):
                candidates.append(reset_delay)
            reset_wait = min(candidates) + 30.0 if candidates else 0.0
            due = now + max(_backoff(attempt), reset_wait)
            if due >= root_start + MAX_LINEAGE_AGE_S:
                verdict, reason, due = RETRY_TERMINAL, "retry budget exhausted (24h)", None
            else:
                verdict = RETRY_SCHEDULED
    return DelegationRetryChange(verdict, reason, due, None, None, record["retry_notices"])


def schedule_retry(
    delegation_id: str, *, now: Optional[float] = None, best_effort: bool = False,
) -> Optional[dict[str, Any]]:
    """Classify one terminal armed row. Provider cooldown reads take place outside the writer."""
    now = time.time() if now is None else now
    preview = retry_status(delegation_id)
    if not preview or preview["retry_state"] != RETRY_ARMED or preview["state"] in _ACTIVE_RETRY_ORIGIN_STATES:
        return None
    routes = _route_chain(preview)
    available_at = None
    if routes:
        from agent.shared_primary_cooldown import earliest_available
        _, available_at = earliest_available(routes, now=now)
    with _ad()._ledger() as store:
        rows = store.coordination.change_delegation_retries(
            [delegation_id], now=now, best_effort=best_effort,
            decide=lambda record: _classify_retry(record, now, routes, available_at),
        )
    if not rows or rows[0]["retry_state"] == RETRY_NONE:
        return None
    record = rows[0]
    verdict = record["retry_state"]
    return {"delegation_id": delegation_id, "retry_state": verdict, "reason": record["retry_reason"],
            "due_at": record["retry_due_at"], "cut_reason": _cut_reason(record),
            "attempt": record["retry_attempt"] + (1 if verdict == RETRY_SCHEDULED else 0)}

def _action(record: dict[str, Any], kind: str, claim: str) -> dict[str, Any]:
    task = record["task"]
    return {
        "kind": kind, "claim": claim, "delegation_id": record["delegation_id"],
        "session_key": record["session_key"], "origin_ui_session_id": record["origin_ui_session_id"],
        "origin_session_id": record["origin_session_id"], "parent_session_id": record["parent_session_id"],
        **{k: task.get(k, "") for k in ("scope_id", "user_id", "user_name")},
        "text": build_retry_notice(record) if kind == "notice" else build_terminal_line(record),
    }


def classify_armed(*, now: Optional[float] = None, limit: int = 64) -> int:
    """Classify orphaned terminal rows and recover successful results whose delivery was dropped."""
    ad = _ad()
    if not ad._ledger_exists():
        return 0
    now = time.time() if now is None else now
    with ad._ledger_reader() as store:
        armed = store.coordination.list_delegation_retries(
            states=(RETRY_ARMED,), limit=limit, terminal_armed=True,
        )
        dropped = store.coordination.list_delegation_retries(
            states=(RETRY_NONE,), limit=limit, dropped_success_reasons=SUCCESS_REASONS,
        )
    if dropped:
        with ad._ledger() as store:
            store.coordination.change_delegation_retries(
                [row["delegation_id"] for row in dropped], now=now, best_effort=True,
                decide=lambda row: DelegationRetryChange(
                    RETRY_TERMINAL, RESULT_UNDELIVERABLE, None, None, None, row["retry_notices"],
                ) if row["retry_state"] == RETRY_NONE and row["retry_reason"] in SUCCESS_REASONS else None,
            )
    for row in armed:
        schedule_retry(row["delegation_id"], now=now, best_effort=True)
    return len(armed)


def _retry_ready(record, now):
    state = record["retry_state"]
    due = record["retry_due_at"]
    claimed_at = record["retry_claimed_at"]
    if state == RETRY_SCHEDULED:
        return due is not None and due <= now
    if state == RETRY_TERMINAL:
        return due is None or due <= now
    if state in (RETRY_NOTIFIED, RETRY_DISPATCHING):
        return claimed_at is not None and claimed_at <= now - NOTICE_GRACE_S
    if state in (RETRY_NOTICING, RETRY_REPORTING):
        return claimed_at is not None and claimed_at <= now - RETRY_CLAIM_TTL_S
    return False


def _restart_wake_already_delivered(record):
    return is_trusted_restart_cut(_cut_reason(record)) and (
        record["delivery_state"] == "delivered" or record["retry_notices"] > 0
    )


def _retry_candidate(record, now):
    state = record["retry_state"]
    if state not in (RETRY_TERMINAL, RETRY_REPORTING) and _recovery_admission_error(record) == INELIGIBLE_SEALED:
        return False
    return not (
        state in (RETRY_SCHEDULED, RETRY_NOTIFIED)
        and not _recovery_stopped(record)
        and _restart_wake_already_delivered(record)
        and record["retry_root_started_at"] + MAX_LINEAGE_AGE_S > now
    )


def _sweep_change(record, now):
    if not _retry_ready(record, now) or not _retry_candidate(record, now):
        return None
    state = record["retry_state"]
    reason = record["retry_reason"]
    due = record["retry_due_at"]
    notices = record["retry_notices"]
    release_children = state == RETRY_DISPATCHING
    stopped = _recovery_stopped(record)
    if stopped and state not in (RETRY_TERMINAL, RETRY_REPORTING):
        return DelegationRetryChange(RETRY_CANCELLED, "stopped by user", None, None, None,
                                     notices, release_children)
    if stopped and state == RETRY_REPORTING:
        due = None
    if state in (RETRY_SCHEDULED, RETRY_NOTICING, RETRY_NOTIFIED, RETRY_DISPATCHING):
        if budget_reason := _retry_budget_reason(record, now):
            state, reason, due = RETRY_TERMINAL, budget_reason, None
    if not stopped and _restart_wake_already_delivered(record):
        # The cut completion owns the one parent wake. Recovery is explicit.
        if state == RETRY_TERMINAL:
            return DelegationRetryChange(RETRY_REPORTED, reason, None, None, None, notices, release_children)
        if state == RETRY_DISPATCHING:
            return DelegationRetryChange(
                RETRY_SCHEDULED, reason, record["retry_root_started_at"] + MAX_LINEAGE_AGE_S,
                None, None, notices, release_children=True,
            )
        if state in (RETRY_SCHEDULED, RETRY_NOTIFIED):
            return None
    if state in (RETRY_NOTIFIED, RETRY_DISPATCHING) and notices >= MAX_RETRY_NOTICES:
        state, reason = RETRY_TERMINAL, f"the parent did not dispatch the retry ({reason})"
    claimed_state = RETRY_REPORTING if state in (RETRY_TERMINAL, RETRY_REPORTING) else RETRY_NOTICING
    claim = f"retry:{os.getpid()}:{uuid.uuid4().hex}"
    return DelegationRetryChange(claimed_state, reason, due, claim, now, notices, release_children)


def sweep_retries(*, now: Optional[float] = None, limit: int = 32) -> list:
    """Claim due notices and reports after the original completion/outbox settles."""
    ad = _ad()
    if limit <= 0 or not ad._ledger_exists():
        return []
    now = time.time() if now is None else now
    try:
        classify_armed(now=now, limit=limit)
        with ad._ledger_reader() as store:
            candidates = store.coordination.list_delegation_retries(
                states=RETRY_PENDING_STATES[1:], limit=limit, after_delivery=True,
                ready_at=(now, now - NOTICE_GRACE_S, now - RETRY_CLAIM_TTL_S),
                eligible=lambda record: _retry_candidate(record, now),
            )
        if not candidates:
            return []
        with ad._ledger() as store:
            claimed = store.coordination.change_delegation_retries(
                [row["delegation_id"] for row in candidates], now=now, after_delivery=True,
                best_effort=True, decide=lambda row: _sweep_change(row, now),
            )
    except StoreContention:
        return RetrySweepDeferred()
    kinds = {RETRY_NOTICING: "notice", RETRY_REPORTING: "terminal"}
    return [_action(row, kinds[row["retry_state"]], row["retry_claim"])
            for row in claimed if row["retry_state"] in kinds]


def _settle(
    delegation_id: str, claim: str, expected: str, state: str, *,
    now: Optional[float] = None, reason: Optional[str] = None,
    accepted: bool = False, retry_report: bool = False,
) -> bool:
    ad = _ad()
    if not claim or not ad._ledger_exists():
        return False
    now = time.time() if now is None else now

    def decide(row):
        if row["retry_state"] != expected or row["retry_claim"] != claim:
            return None
        notices = row["retry_notices"]
        due = row["retry_due_at"]
        preserve_claim = state in (RETRY_NOTIFIED, RETRY_REPORTED)
        if retry_report:
            due = now + _backoff(min(notices, MAX_RETRY_ATTEMPTS))
        return DelegationRetryChange(
            state, reason if reason is not None else row["retry_reason"], due,
            claim if preserve_claim else None,
            now if accepted else row["retry_claimed_at"] if preserve_claim else None,
            notices + int(accepted or retry_report),
        )

    with ad._ledger() as store:
        return bool(store.coordination.change_delegation_retries([delegation_id], now=now, decide=decide))


def mark_notice_accepted(delegation_id: str, claim: str, *, now: Optional[float] = None) -> bool:
    """The parent turn was admitted; start its dispatch grace window."""
    return _settle(delegation_id, claim, RETRY_NOTICING, RETRY_NOTIFIED, now=now, accepted=True)


def release_notice(delegation_id: str, claim: str) -> bool:
    """Injection failed or parent route not ready: offer the notice on a later sweep."""
    return _settle(delegation_id, claim, RETRY_NOTICING, RETRY_SCHEDULED)


def mark_retry_terminal(delegation_id: str, claim: str, reason: str) -> bool:
    """The parent conversation disappeared. Settle the retry without waking it again."""
    return _settle(delegation_id, claim, RETRY_NOTICING, RETRY_TERMINAL, reason=reason)


def mark_terminal_reported(delegation_id: str, claim: str) -> bool:
    return _settle(delegation_id, claim, RETRY_REPORTING, RETRY_REPORTED)


def suppress_terminal_report(delegation_id: str, claim: str) -> bool:
    return _settle(delegation_id, claim, RETRY_REPORTING, RETRY_CANCELLED, reason="authorization_revoked")


def suppress_retry_notice(delegation_id: str, claim: str) -> bool:
    return _settle(delegation_id, claim, RETRY_NOTICING, RETRY_CANCELLED, reason="authorization_revoked")


def release_terminal_report(delegation_id: str, claim: str) -> bool:
    """Unsent reports remain owed, with bounded backoff."""
    return _settle(delegation_id, claim, RETRY_REPORTING, RETRY_TERMINAL, retry_report=True)

def _retry_child_claim_eligibility(record: dict[str, Any], existing: dict) -> Optional[str]:
    """Check whether a fan-out still has unclaimed unfinished work."""
    if _goal_count(record.get("task") or {}) <= 1:
        return None
    candidates = _batch_recovery_candidates(record)
    if not candidates:
        return INELIGIBLE_PARTIAL
    # Consumed claims allow recovery of remaining siblings; an unspent claim
    # belongs to its current consumer and must not be advertised again.
    if any(child["task_index"] in existing and not existing[child["task_index"]] for child in candidates):
        return INELIGIBLE_CLAIMED
    if all(existing.get(child["task_index"]) for child in candidates):
        return INELIGIBLE_CLAIMED
    return None


def _resume_available_record(record: Optional[dict], children: dict) -> bool:
    if record is None or _recovery_admission_error(record):
        return False
    if record["retry_state"] in RETRY_PENDING_STATES + (RETRY_DISPATCHED, RETRY_REPORTED):
        return (record["retry_state"] in (RETRY_SCHEDULED, RETRY_NOTICING, RETRY_NOTIFIED)
                and not _recovery_stopped(record)
                and _retry_budget_reason(record, time.time()) is None
                and _retry_child_claim_eligibility(record, children) is None)
    reason = _eligibility(record)
    if reason == INELIGIBLE_BATCH:
        reason = _batch_resume_eligibility(record)
    return reason is None


def resume_available(delegation_id: str) -> bool:
    """Read-only cut-notice admission; the control action rechecks before claiming."""
    ad = _ad()
    if not delegation_id or not ad._ledger_exists():
        return False
    with ad._ledger_reader() as store:
        record = store.coordination.delegation_recovery_record(delegation_id, include_children=True)
    return _resume_available_record(record, (record or {}).get("child_replacements", {}))


def stamp_resume_availability(evt: dict[str, Any], conn=None) -> None:
    """Record cut-notice resume admission on the event where it is built, off the event loop.

    Notification formatters read only ``evt["resume_available"]`` and never touch state.db, so a
    busy ledger cannot make a drain raise or block an event loop. Any failure stamps False (no
    resume line); the resume control action rechecks durable admission before claiming anyway.
    """
    if evt.get("type") != "async_delegation" or not evt.get("cut_reason") or evt.get("task_failure_notice"):
        return
    delegation_id = str(evt.get("delegation_id") or "")
    try:
        if not delegation_id:
            available = False
        elif conn is not None:
            children = dict(conn.execute(
                """SELECT task_index, replacement_delegation_id FROM async_delegation_child_recovery
                   WHERE delegation_id=?""", (delegation_id,),
            ))
            available = _resume_available_record(_load(conn, delegation_id), children)
        else:
            available = resume_available(delegation_id)
    except Exception:
        logger.warning("Async delegation %s: resume availability unreadable; omitting resume offer",
                       delegation_id, exc_info=True)
        available = False
    evt["resume_available"] = bool(available)


def claim_retry_dispatch(delegation_id: str) -> tuple[Optional[dict[str, Any]], Optional[str]]:
    """Atomically take the one dispatch claim for a retry-pending row."""
    ad = _ad()
    if not delegation_id or not ad._ledger_exists():
        return None, INELIGIBLE_NO_ROW
    now = time.time()

    def eligible(record):
        if record["retry_state"] in (RETRY_DISPATCHING, RETRY_DISPATCHED):
            return INELIGIBLE_CLAIMED, False
        if record["retry_state"] not in (RETRY_SCHEDULED, RETRY_NOTICING, RETRY_NOTIFIED):
            return INELIGIBLE_STATE, False
        if reason := _recovery_admission_error(record):
            return reason, False
        if reason := _retry_budget_reason(record, now):
            return reason, True
        if _goal_count(record["task"]) > 1 and not _batch_recovery_candidates(record):
            return INELIGIBLE_PARTIAL, False
        return None, False

    with ad._ledger() as store:
        record, reason, indexes = store.coordination.claim_delegation_retry_dispatch(
            delegation_id, claim=f"retry:{os.getpid()}:{uuid.uuid4().hex}", now=now, eligible=eligible,
            children=lambda row: ([child["task_index"] for child in _batch_recovery_candidates(row)]
                                  if _goal_count(row["task"]) > 1 else []),
        )
    if indexes:
        record["recovery_tasks"] = _batch_recovery_tasks(record, indexes)
    return record, reason

def _has_recovery_claim(record: dict[str, Any]) -> bool:
    return (record["retry_state"] == RETRY_DISPATCHING
            or (record["retry_state"] == RETRY_NONE
                and record["resume_state"] == RESUME_STATE_CLAIMED
                and bool(record.get("resume_claim"))))


def claimed_retry_source(record: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Return the recovery row whose claim authorizes this recovery spawn."""
    prior = retry_status(recovery_source_id(record.get("context")) or "")
    if prior is None or not _has_recovery_claim(prior):
        return None
    if not ((record.get("session_key") and record["session_key"] == prior["session_key"])
            or (record.get("parent_session_id") and record["parent_session_id"] in
                (prior["parent_session_id"], prior["origin_session_id"]))):
        return None
    return prior


def recovery_source_id(context: Any) -> Optional[str]:
    """Current brief's source; ancestor headings in ORIGINAL CONTEXT are evidence only."""
    match = _RECOVERY_HEADING_RE.match(str(context or "").lstrip())
    return match.group(1) if match else None


def is_recovery_spawn(record: dict[str, Any]) -> bool:
    """True when the spawn carries another delegation's recovery brief (no ledger access)."""
    source = recovery_source_id(record.get("context"))
    return bool(source and source != record.get("delegation_id"))


def recovery_replacement(contexts):
    """Decode the current recovery brief(s) before starting a detached unit."""
    from persistence.authority.coordination import DelegationReplacement
    import re

    contexts = tuple(str(context or "").lstrip() for context in contexts)
    sources = [recovery_source_id(context) for context in contexts]
    if not any(sources):
        return None
    if len(set(sources)) != 1:
        raise RetryAdmissionError("a recovery unit must contain only briefs from the same delegation")
    source_id = sources[0]
    marker = re.compile(rf"^RECOVERY CHILD INDEX: {re.escape(source_id)}:(\d+)\s*$", re.MULTILINE)
    indexes = []
    for context in contexts:
        matches = marker.findall(context)
        if len(matches) == 1:
            indexes.append(int(matches[0]))
        elif len(contexts) > 1 or matches:
            raise RetryAdmissionError("each recovered batch task must name exactly one claimed child index")
    if len(indexes) != len(set(indexes)):
        raise RetryAdmissionError("a claimed child cannot be dispatched twice in one unit")
    return DelegationReplacement(source_id, tuple(indexes))


def recovery_dispatch_refusal(record: dict[str, Any], now: float) -> tuple[Optional[str], bool]:
    """Recheck the durable cut and retry budget at the replacement's insertion boundary."""
    if reason := _recovery_admission_error(record):
        return reason, False
    if record["retry_state"] == RETRY_DISPATCHING:
        reason = _retry_budget_reason(record, now)
        return reason, reason is not None
    return None, False


def release_retry_dispatch(record: dict[str, Any]) -> bool:
    """Return a failed persist and its unspent recovery claims to the scheduler."""
    ad = _ad()
    if not ad._ledger_exists():
        return False
    did, now = record["delegation_id"], time.time()
    with ad._ledger() as store:
        if record.get("retry_state") == RETRY_NONE and record.get("resume_state") == RESUME_STATE_CLAIMED:
            return store.coordination.release_delegation_resume_claim(
                did, claim=record.get("resume_claim", ""), now=now,
            )
        claim = record.get("retry_claim")
        if not claim:
            return False

        def decide(row):
            if row["retry_state"] != RETRY_DISPATCHING or row["retry_claim"] != claim:
                return None
            return DelegationRetryChange(
                RETRY_SCHEDULED, row["retry_reason"], now + _backoff(row["retry_attempt"]),
                None, None, row["retry_notices"], release_children=True,
            )

        return bool(store.coordination.change_delegation_retries([did], now=now, decide=decide))


def _replacement_source(conn, new: dict[str, Any], task_text: str) -> Optional[dict[str, Any]]:
    m = _RECOVERY_HEADING_RE.search(str(new.get("context") or "")) or _RECOVERY_HEADING_RE.search(task_text or "")
    if not m or m.group(1) == new["delegation_id"]:
        return None
    prior = _load(conn, m.group(1))
    if prior is None:
        return None
    same_owner = ((prior["session_key"] and prior["session_key"] == new.get("session_key"))
                  or (prior["parent_session_id"] and prior["parent_session_id"] == new.get("parent_session_id")))
    return prior if same_owner else None


def _replacement_children(conn, prior: dict[str, Any], new: dict[str, Any]) -> Optional[list[int]]:
    """Indexes named by child recovery briefs, or None for the scalar contract.

    A parent cannot turn a batch claim into a scalar whole-batch replay by
    dropping the child marker. Every named index must be claimed and unspent.
    """
    task = prior.get("task") or {}
    if not (_goal_count(task) > 1 or conn.execute(
            "SELECT 1 FROM async_delegation_child_recovery WHERE delegation_id=? LIMIT 1",
            (prior["delegation_id"],)).fetchone()):
        return None
    import re
    # Older-generation briefs remain verbatim inside ORIGINAL CONTEXT. Only the
    # current delegation can authorize or consume its own child claims.
    pattern = rf"RECOVERY CHILD INDEX: {re.escape(prior['delegation_id'])}:(\d+)\b"
    return sorted({int(index) for index in re.findall(pattern, str(new.get("context") or ""))})


def replacement_admission_error(conn, new: dict[str, Any], task_text: str) -> Optional[str]:
    """Recheck in the INSERT transaction; commit expiry without inserting a child."""
    prior = _replacement_source(conn, new, task_text)
    if prior is None:
        return None
    if _has_recovery_claim(prior) and _recovery_admission_error(prior) == INELIGIBLE_SEALED:
        return "recovery execution is sealed; no replacement was started"
    if _recovery_stopped(prior):
        return "recovery source was stopped without restart continuation; no replacement was started"
    indexes = _replacement_children(conn, prior, new)
    if indexes is not None:
        if not indexes:
            return "batch recovery requires a claimed child index; no replacement was started"
        for index in indexes:
            child = conn.execute("SELECT claim, replacement_delegation_id FROM async_delegation_child_recovery "
                                 "WHERE delegation_id=? AND task_index=?", (prior["delegation_id"], index)).fetchone()
            expected_claim = prior["retry_claim"] if prior["retry_state"] != RETRY_NONE else prior["resume_claim"]
            if child is None or child[0] != expected_claim:
                return "child recovery claim missing or superseded; no replacement was started"
            if child[1]:
                return "child recovery claim already consumed; no replacement was started"
        if prior["retry_state"] == RETRY_NONE:
            return None
        if prior["retry_state"] != RETRY_DISPATCHING:
            return "child recovery claim cancelled or not taken; no replacement was started"
        return _expire_retry(conn, prior, time.time())
    # Legacy/CLI/TUI rows have no durable retry state, but an explicit recovery
    # claim authorizes exactly the first replacement spawn. Admission is paired
    # with link_replacement() below, which records that replacement atomically.
    if prior["retry_state"] == RETRY_NONE:
        if prior["retry_replacement"]:
            return "recovery claim already consumed; no replacement was started"
        return None  # retain the legacy one-shot resume contract before a claim
    now = time.time()
    if prior["retry_state"] in (RETRY_SCHEDULED, RETRY_NOTICING, RETRY_NOTIFIED):
        # Seeing a recovery brief is not a claim: a model may have copied it
        # from an unrelated turn. Admit this as a fresh root, but still enforce
        # the original lineage deadline before doing so.
        if reason := _retry_budget_reason(prior, now):
            _expire_retry(conn, prior, now)
            return reason
        return None
    if prior["retry_state"] == RETRY_DISPATCHING:
        if reason := _retry_budget_reason(prior, now):
            _expire_retry(conn, prior, now)
            return reason
        return None
    return "recovery claim already consumed, cancelled, or not taken; no replacement was started"


def retry_submission_error(delegation_id: str) -> Optional[str]:
    """Queued retries must not start after the lineage deadline when a pool slot frees."""
    ad = _ad()
    now = time.time()
    preview = retry_status(delegation_id)
    if not preview or not preview["retry_attempt"] or not _retry_budget_reason(preview, now, check_attempts=False):
        return None

    def expire(record):
        # Attempt six may execute; only the next claim is barred by the attempt ceiling.
        if record["retry_attempt"] and (reason := _retry_budget_reason(record, now, check_attempts=False)):
            return DelegationRetryChange(RETRY_TERMINAL, reason, None, None, None, record["retry_notices"])
        return None

    with ad._ledger() as store:
        rows = store.coordination.change_delegation_retries([delegation_id], now=now, decide=expire)
    return rows[0]["retry_reason"] if rows else None


def link_replacement(conn, new: dict[str, Any], task_text: str) -> None:
    """Carry lineage from the claimed recovery brief to the new row, inside the INSERT transaction."""
    prior = _replacement_source(conn, new, task_text)
    if prior is None:
        return
    new_id, dispatched_at = new["delegation_id"], new["dispatched_at"]
    indexes = _replacement_children(conn, prior, new)
    if indexes is not None:
        for index in indexes:
            conn.execute("UPDATE async_delegation_child_recovery SET replacement_delegation_id=? "
                         "WHERE delegation_id=? AND task_index=? AND replacement_delegation_id IS NULL",
                         (new_id, prior["delegation_id"], index))
        if prior["retry_state"] == RETRY_DISPATCHING:
            conn.execute("UPDATE async_delegations SET retry_root=?, retry_attempt=?, retry_root_started_at=? "
                         "WHERE delegation_id=?",
                         (prior["retry_root"], prior["retry_attempt"] + 1,
                          prior["retry_root_started_at"] or dispatched_at, new_id))
            conn.execute("UPDATE async_delegations SET retry_state='dispatched', retry_replacement=?, updated_at=? "
                         "WHERE delegation_id=? AND retry_state='dispatching' AND NOT EXISTS "
                         "(SELECT 1 FROM async_delegation_child_recovery WHERE delegation_id=? "
                         "AND replacement_delegation_id IS NULL)",
                         (new_id, time.time(), prior["delegation_id"], prior["delegation_id"]))
        return
    if prior["retry_state"] == RETRY_NONE:
        # Legacy explicit-resume rows have no retry claim state to advance. The
        # guarded write is the durable admission receipt for the one allowed
        # replacement; a racing second INSERT sees it and is rejected above.
        conn.execute("""UPDATE async_delegations
                       SET retry_replacement=?, updated_at=?
                       WHERE delegation_id=? AND retry_state=?
                         AND resume_state=? AND COALESCE(retry_replacement, '')=''""",
                     (new_id, time.time(), prior["delegation_id"], RETRY_NONE, RESUME_STATE_CLAIMED))
        return
    if prior["retry_state"] != RETRY_DISPATCHING:
        return
    conn.execute("UPDATE async_delegations SET retry_root=?, retry_attempt=?, retry_root_started_at=? WHERE delegation_id=?",
                 (prior["retry_root"], prior["retry_attempt"] + 1, prior["retry_root_started_at"] or dispatched_at,
                  new_id))
    conn.execute("""UPDATE async_delegations SET retry_state='dispatched', retry_replacement=?, updated_at=?
                   WHERE delegation_id=? AND retry_state='dispatching'""",
                 (new_id, time.time(), prior["delegation_id"]))


def retry_status(delegation_id: str) -> Optional[dict[str, Any]]:
    ad = _ad()
    if not delegation_id or not ad._ledger_exists():
        return None
    with ad._ledger_reader() as store:
        return store.coordination.delegation_recovery_record(delegation_id)


def list_retry_pending(limit: int = 50) -> list:
    """Rows Hermes is still retrying or reporting; for delegate_task action=list."""
    ad = _ad()
    if not ad._ledger_exists():
        return []
    with ad._ledger_reader() as store:
        return list(store.coordination.list_delegation_retries(
            states=RETRY_PENDING_STATES[1:], limit=limit, newest_first=True,
        ))

def _when(ts: Optional[float]) -> str:
    return time.strftime("%H:%M", time.localtime(ts)) if ts else "soon"


def retry_note(plan: Optional[dict[str, Any]]) -> str:
    """One line for the completion notice so the parent does not dispatch a duplicate (R5)."""
    if not plan:
        return ""
    cut_reason = plan.get("cut_reason")
    if cut_reason and is_user_cancel_reason(cut_reason):
        return ""
    if plan["retry_state"] == RETRY_SCHEDULED and is_trusted_restart_cut(cut_reason):
        return (f"Work was cut before finishing. To continue call delegate_task(action='resume', "
                f"subagent_id='{plan['delegation_id']}') and use its state-verification brief. "
                "Do not rerun completed children. This recovery window expires after the 24h lineage budget; "
                "Hermes will not automatically rerun this cut work or send another continuation notice.")
    if plan["retry_state"] == RETRY_SCHEDULED:
        return (f"Hermes will retry this automatically at {_when(plan['due_at'])} (retry {plan['attempt']}/"
                f"{MAX_RETRY_ATTEMPTS}, {plan['reason']}); you will get a notice then. Do not re-dispatch it yourself.")
    return f"Hermes will not retry this ({plan['reason']})."


def build_retry_notice(record: dict[str, Any]) -> str:
    from tools.process_registry_notifications import PROCESS_NOTIFICATION_END
    did = record["delegation_id"]
    attempt = record["retry_attempt"] + 1
    reprompt = record["retry_notices"] >= 1
    spawn = ("then call delegate_task(tasks=<returned tasks list>), preserving each child's context verbatim. "
             "Do not rerun completed children. " if _goal_count(record.get("task") or {}) > 1 else
             "then call delegate_task with the returned goal and pass 'recovery_context' verbatim as context. ")
    return (
        f"[IMPORTANT: AUTOMATIC RETRY DUE — {did}] "
        + ("REMINDER: the previous retry notice was not acted on. " if reprompt else "")
        + f"A background delegation stopped ({record['retry_reason']}) and Hermes scheduled retry "
        f"{attempt}/{MAX_RETRY_ATTEMPTS}. Do it now: call delegate_task(action='resume', subagent_id='{did}'), "
        + spawn + "If it is no longer wanted, say so to the user. "
        + ("If you do not dispatch it, Hermes will stop retrying it." if reprompt else
           "Replying NO_REPLY without dispatching does not cancel the retry; you will be asked once more.")
        + f"]\n{PROCESS_NOTIFICATION_END}"
    )


def build_terminal_line(record: dict[str, Any]) -> str:
    """Return user-facing terminal text only for a completed result that lost its delivery."""
    if str(record.get("retry_reason") or "") != RESULT_UNDELIVERABLE:
        return ""
    from agent.redact import redact_for_egress

    goal = redact_for_egress(str((record.get("task") or {}).get("goal") or "background task"))
    goal = " ".join(goal.split())
    if len(goal) > 120:
        goal = goal[:117] + "…"
    # Never interpolate exception text: only fixed wording crosses the egress boundary.
    return f"Background task finished: {goal} — its result could not be delivered."
