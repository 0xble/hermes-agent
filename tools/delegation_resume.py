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
* Boot auto-resume only emits a parent-facing notice. It never claims
  ``resume_state`` and never starts a child; the parent must call the explicit
  resume action through the normal tool gates.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from typing import Any, Dict, Optional, Tuple

# Boot-time automatic resume is a parent-facing trigger only. The gateway reads
# ``gateway.auto_resume_on_boot`` from the active profile before queueing notices;
# the explicit one-shot resume action is independent of that setting.
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


def _ad():
    """Late import: the ledger connection helpers live with the async registry."""
    from tools import async_delegation

    return async_delegation


_SELECT = """SELECT delegation_id, origin_session, origin_ui_session_id, parent_session_id,
       state, dispatched_at, completed_at, task_json, result_json,
       origin_session_id, resume_state, resume_attempts,
       auto_resume_state, auto_resume_claim, auto_resume_claimed_at
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
        "auto_resume_state": row[12] or AUTO_RESUME_STATE_NONE,
        "auto_resume_claim": row[13] or "",
        "auto_resume_claimed_at": row[14],
    }


def _eligibility(record: Dict[str, Any]) -> Optional[str]:
    """None when the record may be resumed, else a stable refusal token."""
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


def list_boot_candidates(limit: int = 32) -> list[Dict[str, Any]]:
    """Return bounded, single-task rows eligible for a parent-facing boot notice.

    This is read-only. The durable trigger claim is taken later, immediately before
    injection, after the gateway has proved that the original parent is live and
    routable. Repeated gateways therefore cannot manufacture duplicate notices.
    """
    limit = max(0, int(limit))
    if limit == 0:
        return []
    ad = _ad()
    now = time.time()
    candidates = []
    with ad._DB_LOCK, ad._transaction() as conn:
        rows = conn.execute(
            """SELECT delegation_id, origin_session, origin_ui_session_id, parent_session_id,
                      state, dispatched_at, completed_at, task_json, result_json,
                      origin_session_id, resume_state, resume_attempts,
                      auto_resume_state, auto_resume_claim, auto_resume_claimed_at
               FROM async_delegations
              WHERE state IN ('unknown', 'interrupted', 'stalled')
                AND retry_state='none'
                AND parent_session_id IS NOT NULL AND parent_session_id != ''
                AND (auto_resume_state='none' OR
                     (auto_resume_state='claimed' AND auto_resume_claimed_at < ?))
              ORDER BY updated_at ASC, delegation_id ASC""",
            (now - AUTO_RESUME_CLAIM_TTL_SECONDS,),
        )
        # Stream until the eligible result limit, not the raw-row limit. Otherwise
        # old unresumable rows permanently starve later recoverable work on every boot.
        for row in rows:
            record = _row_to_record(row)
            if _eligibility(record) is not None:
                continue
            if not (record["session_key"] or record["origin_session_id"]):
                continue
            candidates.append(record)
            if len(candidates) >= limit:
                break
    return candidates


def claim_auto_resume_trigger(record_or_id: Dict[str, Any] | str,
                              consumer: str = "gateway-boot") -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Take the one durable claim for a boot notice, never the explicit resume claim."""
    ad = _ad()
    delegation_id = (record_or_id.get("delegation_id") if isinstance(record_or_id, dict)
                     else str(record_or_id or ""))
    if not delegation_id:
        return None, INELIGIBLE_NO_ROW
    claim_id = f"{consumer}:{os.getpid()}:{uuid.uuid4().hex}"
    now = time.time()
    stale_before = now - AUTO_RESUME_CLAIM_TTL_SECONDS
    with ad._DB_LOCK, ad._transaction() as conn:
        row = conn.execute(_SELECT, (delegation_id,)).fetchone()
        if row is None:
            return None, INELIGIBLE_NO_ROW
        record = _row_to_record(row)
        reason = _eligibility(record)
        if reason is not None:
            return record, reason
        if not record["parent_session_id"] or not (record["session_key"] or record["origin_session_id"]):
            return record, INELIGIBLE_STATELESS
        changed = conn.execute(
            """UPDATE async_delegations
                  SET auto_resume_state=?, auto_resume_claim=?, auto_resume_claimed_at=?, updated_at=?
                WHERE delegation_id=?
                  AND (auto_resume_state=? OR
                       (auto_resume_state=? AND auto_resume_claimed_at < ?))""",
            (AUTO_RESUME_STATE_CLAIMED, claim_id, now, now, delegation_id,
             AUTO_RESUME_STATE_NONE, AUTO_RESUME_STATE_CLAIMED, stale_before),
        ).rowcount
        if changed != 1:
            if record["auto_resume_state"] == AUTO_RESUME_STATE_DELIVERED:
                return record, "already_triggered"
            return record, "already_triggered"
    record["auto_resume_state"] = AUTO_RESUME_STATE_CLAIMED
    record["auto_resume_claim"] = claim_id
    record["auto_resume_claimed_at"] = now
    return record, None


def release_auto_resume_trigger(delegation_id: str, claim_id: str) -> bool:
    """Refund a boot trigger when parent admission fails."""
    ad = _ad()
    with ad._DB_LOCK, ad._transaction() as conn:
        return conn.execute(
            """UPDATE async_delegations SET auto_resume_state=?, auto_resume_claim=NULL,
                      auto_resume_claimed_at=NULL, updated_at=?
                WHERE delegation_id=? AND auto_resume_state=? AND auto_resume_claim=?""",
            (AUTO_RESUME_STATE_NONE, time.time(), delegation_id,
             AUTO_RESUME_STATE_CLAIMED, claim_id),
        ).rowcount == 1


def complete_auto_resume_trigger(delegation_id: str, claim_id: str) -> bool:
    """Make an accepted boot notice terminal so later boots do not repeat it."""
    ad = _ad()
    with ad._DB_LOCK, ad._transaction() as conn:
        return conn.execute(
            """UPDATE async_delegations SET auto_resume_state=?, updated_at=?
                WHERE delegation_id=? AND auto_resume_state=? AND auto_resume_claim=?""",
            (AUTO_RESUME_STATE_DELIVERED, time.time(), delegation_id,
             AUTO_RESUME_STATE_CLAIMED, claim_id),
        ).rowcount == 1


AUTO_RESUME_NOTICE_OPEN = "[IMPORTANT: An interrupted single-task background delegation "


def build_auto_resume_notice(record: Dict[str, Any]) -> str:
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
    "unknown": "no terminal result was recorded, so its outcome is unknown",
    "interrupted": "it stopped before finishing",
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
# Durable automatic retry (guarantee: delivered completion OR one visible line)
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
# NOTICE_GRACE_S. If it is still undispatched, the row turns 'terminal' and the
# gateway posts one plain line to the user itself, so the guarantee never rests
# on model compliance. The spawn of the replacement is linked back through the
# recovery brief's heading (``_persist_dispatch`` -> :func:`link_replacement`),
# which carries the lineage budget forward.

RETRY_ARMED = "armed"
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
       retry_claim, retry_claimed_at, retry_root, retry_attempt, retry_root_started_at, retry_replacement
FROM async_delegations"""


def _retry_record(row) -> Dict[str, Any]:
    record = _row_to_record(row[:15])
    try:
        event = json.loads(row[15] or "{}") or {}
    except (TypeError, ValueError):
        event = {}
    record.update(
        event=event, delivery_state=row[16] or "", retry_state=row[17] or RETRY_NONE, retry_reason=row[18] or "",
        retry_due_at=row[19], retry_notices=int(row[20] or 0), retry_claim=row[21] or "",
        retry_claimed_at=row[22], retry_root=row[23] or row[0], retry_attempt=int(row[24] or 0),
        retry_root_started_at=row[25] or row[5], retry_replacement=row[26] or "",
    )
    return record


def _load(conn, delegation_id: str) -> Optional[Dict[str, Any]]:
    row = conn.execute(_RETRY_SELECT + " WHERE delegation_id=?", (delegation_id,)).fetchone()
    return _retry_record(row) if row else None


def _goal_count(task: Dict[str, Any]) -> int:
    goals = task.get("goals")
    return len(goals) if isinstance(goals, list) and goals else 1


def _child_results(record: Dict[str, Any]) -> list:
    results = record["result"].get("results")
    return [r for r in results if isinstance(r, dict)] if isinstance(results, list) else []


def _texts(record: Dict[str, Any]) -> str:
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


def _retry_budget_reason(record: Dict[str, Any], now: float, *, check_attempts: bool = True) -> Optional[str]:
    root_start = float(record.get("retry_root_started_at") or record["dispatched_at"])
    if now - root_start >= MAX_LINEAGE_AGE_S:
        return "retry budget exhausted (24h)"
    if check_attempts and int(record.get("retry_attempt") or 0) >= MAX_RETRY_ATTEMPTS:
        return f"retry budget exhausted ({MAX_RETRY_ATTEMPTS} attempts)"
    return None


def _expire_retry(conn, record: Dict[str, Any], now: float, *, check_attempts: bool = True) -> Optional[str]:
    reason = _retry_budget_reason(record, now, check_attempts=check_attempts)
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


def gateway_retry_origin(record: Dict[str, Any]) -> bool:
    """Only messaging gateway routes have the driver; CLI/TUI retain main's resume behavior."""
    # ``origin_session_id`` is the gateway's durable wake target, not a CLI/TUI
    # marker; gateway chats commonly set it so a restart can route the result.
    if record.get("origin_ui_session_id"):
        return False
    parts = str(record.get("session_key") or "").split(":")
    if len(parts) < 5 or parts[0] != "agent" or not parts[4]:
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
                           parent_session_id: str = "", all_sessions: bool = False) -> set[str]:
    """Cancel only still-owed retry states, atomically invalidating notice/report/dispatch claims."""
    selectors = [(field, value) for field, value in (
        ("origin_session", session_key), ("origin_ui_session_id", origin_ui_session_id),
        ("parent_session_id", parent_session_id)) if value]
    if delegation_id:
        scope, values = "delegation_id=?", [delegation_id]
    elif all_sessions:
        scope, values = "1", []
    elif selectors:
        scope = "(" + " OR ".join(field + "=?" for field, _ in selectors) + ")"
        values = [value for _, value in selectors]
    else:
        return set()  # an empty session selector never means everyone
    ad = _ad()
    placeholders = ",".join("?" for _ in RETRY_PENDING_STATES)
    with ad._DB_LOCK, ad._transaction() as conn:
        rows = conn.execute(f"""UPDATE async_delegations SET retry_state='cancelled', retry_reason='stop_command',
                       retry_due_at=NULL, retry_claim=NULL, retry_claimed_at=NULL, updated_at=?
                       WHERE retry_state IN ({placeholders}) AND {scope} RETURNING delegation_id""",
                            (time.time(), *RETRY_PENDING_STATES, *values)).fetchall()
    return {row[0] for row in rows}


def retry_action_is_current(action: Dict[str, Any]) -> bool:
    """Collected actions lose authority when /stop clears their durable claim."""
    record = retry_status(action["delegation_id"])
    expected = RETRY_NOTICING if action["kind"] == "notice" else RETRY_REPORTING
    return bool(record and record["retry_state"] == expected and record["retry_claim"] == action["claim"])


def classify_outcome(record: Dict[str, Any]) -> Tuple[str, str]:
    """``(verdict, reason)`` with verdict ``none`` | ``retry`` | ``terminal``. Pure; no I/O."""
    state = record["state"]
    task = record["task"]
    if record.get("delivery_state") == "superseded" or record["event"].get("superseded_by"):
        return RETRY_NONE, "superseded"
    if not (record["parent_session_id"] or record["origin_session_id"]):
        return RETRY_NONE, "stateless origin"  # cron/one-shot: no chat owns a recovery
    results = _child_results(record)
    failed = [r for r in results if r.get("status") not in _OK_STATUSES]
    if state == "completed" and not failed:
        return RETRY_NONE, "completed"
    text = _texts(record)
    # Only the recorded stop reason counts here, never free-form child output.
    stop_text = " ".join(str(x) for x in (record["event"].get("interrupt_reason"), record["result"].get("error"),
                                          *(r.get("interrupt_reason") for r in results)) if x).lower()
    if (state in {"interrupted", "cancelled"} or any(r.get("status") == "interrupted" for r in results)) and is_user_cancel_reason(stop_text):
        return RETRY_NONE, "stopped by user"
    if state == "cancelled":
        return RETRY_NONE, "cancelled"
    if _goal_count(task) > 1 or task.get("task_indexes"):
        # Fan-out units: per-index retry would need to reconcile siblings' side effects; surface instead.
        done = len(results) - len(failed)
        return RETRY_TERMINAL, f"{len(failed) or 'some'} of {_goal_count(task)} parallel tasks did not finish ({done} done); needs review"
    if state in _LOST_OWNER_STATES or (failed and all(r.get("status") == "interrupted" for r in failed)):
        return "retry", state if state in _LOST_OWNER_STATES else "interrupted"
    reasons = {str(r.get("failure_reason") or "") for r in failed} - {""}
    if reasons and reasons <= TRANSIENT_REASONS:
        return "retry", sorted(reasons)[0]
    if reasons - {"unknown"}:
        return RETRY_TERMINAL, ", ".join(sorted(reasons - {"unknown"}))
    if any(p in text.lower() for p in _TRANSIENT_TEXT):
        return "retry", "transient provider error"
    first_line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "error")
    return RETRY_TERMINAL, first_line[:160]


def _backoff(attempt: int) -> float:
    return min(MAX_BACKOFF_S, BASE_BACKOFF_S * (2 ** max(0, attempt)))


def schedule_retry(delegation_id: str, *, now: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """Classify one terminal ``armed`` row exactly once. Returns the plan, or None when not applicable."""
    ad = _ad()
    now = time.time() if now is None else now
    with ad._DB_LOCK, ad._transaction() as conn:
        record = _load(conn, delegation_id)
        if record is None or record["retry_state"] != RETRY_ARMED:
            return None
        if record["state"] in {"new", "queued", "admitted", "running", "stalling", "finalizing"}:
            return None
        verdict, reason = classify_outcome(record)
        due = None
        if verdict == "retry":
            if record["retry_attempt"] >= MAX_RETRY_ATTEMPTS:
                verdict, reason = RETRY_TERMINAL, f"retry budget exhausted after {record['retry_attempt']} retries ({reason})"
            elif now - float(record["retry_root_started_at"] or now) >= MAX_LINEAGE_AGE_S:
                verdict, reason = RETRY_TERMINAL, f"retry budget exhausted: still failing after 24h ({reason})"
            else:
                verdict = RETRY_SCHEDULED
                due = now + max(_backoff(record["retry_attempt"]), (_reset_delay(_texts(record), now) or 0) + 30.0)
        conn.execute(
            """UPDATE async_delegations SET retry_state=?, retry_reason=?, retry_due_at=?, updated_at=?
               WHERE delegation_id=? AND retry_state=?""",
            (verdict, reason, due, now, delegation_id, RETRY_ARMED))
    if verdict == RETRY_NONE:
        return None
    return {"delegation_id": delegation_id, "retry_state": verdict, "reason": reason, "due_at": due,
            "attempt": record["retry_attempt"] + (1 if verdict == RETRY_SCHEDULED else 0)}


def _action(record: Dict[str, Any], kind: str, claim: str) -> Dict[str, Any]:
    task = record["task"]
    return {
        "kind": kind, "claim": claim, "delegation_id": record["delegation_id"],
        "session_key": record["session_key"], "origin_ui_session_id": record["origin_ui_session_id"],
        "origin_session_id": record["origin_session_id"], "parent_session_id": record["parent_session_id"],
        **{k: task.get(k, "") for k in ("scope_id", "user_id", "user_name")},
        "text": build_retry_notice(record) if kind == "notice" else build_terminal_line(record),
    }


def _claim(conn, record: Dict[str, Any], expected: str, new: str, now: float, **extra) -> Optional[str]:
    claim = f"retry:{os.getpid()}:{uuid.uuid4().hex}"
    sets = "".join(f", {k}=?" for k in extra)
    changed = conn.execute(
        f"""UPDATE async_delegations SET retry_state=?, retry_claim=?, retry_claimed_at=?, updated_at=?{sets}
            WHERE delegation_id=? AND retry_state=? AND COALESCE(retry_claim,'')=?""",
        (new, claim, now, now, *extra.values(), record["delegation_id"], expected, record["retry_claim"])).rowcount
    return claim if changed == 1 else None


def classify_armed(*, now: Optional[float] = None, limit: int = 64) -> int:
    """Classify terminal rows still ``armed`` (owner died, classification crashed, or a restart)."""
    ad = _ad()
    with ad._DB_LOCK, ad._transaction() as conn:
        armed = [r[0] for r in conn.execute(
            """SELECT delegation_id FROM async_delegations WHERE retry_state='armed'
               AND state NOT IN ('new','queued','admitted','running','stalling','finalizing') LIMIT ?""", (limit,))]
    for delegation_id in armed:
        schedule_retry(delegation_id, now=now)
    return len(armed)


def sweep_retries(*, now: Optional[float] = None, limit: int = 32) -> list:
    """Claim due retry work only after the original completion/outbox delivery settles.

    The failure-delivery outbox owns its original payload and exactly-once replay;
    a retry notice or terminal report must never race that still-pending delivery.
    """
    ad = _ad()
    now = time.time() if now is None else now
    classify_armed(now=now, limit=limit)
    actions = []
    stale = now - RETRY_CLAIM_TTL_S
    with ad._DB_LOCK, ad._transaction() as conn:
        rows = conn.execute(
            _RETRY_SELECT + """ WHERE ((retry_state='scheduled' AND retry_due_at <= ?)
                 OR (retry_state IN ('notified','dispatching') AND retry_claimed_at <= ?)
                 OR (retry_state='terminal' AND (retry_due_at IS NULL OR retry_due_at <= ?))
                 OR (retry_state IN ('noticing','reporting') AND retry_claimed_at <= ?))
               AND (event_json IS NULL OR delivery_state != 'pending')
               AND NOT EXISTS (SELECT 1 FROM async_delegation_events e
                               WHERE e.delegation_id=async_delegations.delegation_id
                                 AND e.delivery_state='pending')
               ORDER BY updated_at ASC LIMIT ?""", (now, now - NOTICE_GRACE_S, now, stale, limit)).fetchall()
        for row in rows:
            record = _retry_record(row)
            if record["retry_state"] in (RETRY_SCHEDULED, RETRY_NOTICING, RETRY_NOTIFIED, RETRY_DISPATCHING):
                _expire_retry(conn, record, now)
            state = record["retry_state"]
            if state == RETRY_NOTICING:  # crashed mid-injection: same notice again
                state, record["retry_state"] = RETRY_SCHEDULED, RETRY_NOTICING
            if state == RETRY_REPORTING:
                state = RETRY_TERMINAL
            if state in (RETRY_NOTIFIED, RETRY_DISPATCHING) and record["retry_notices"] >= MAX_RETRY_NOTICES:
                reason = f"the parent did not dispatch the retry ({record['retry_reason']})"
                conn.execute("UPDATE async_delegations SET retry_state='terminal', retry_reason=?, updated_at=? "
                             "WHERE delegation_id=? AND retry_state=?",
                             (reason, now, record["delegation_id"], record["retry_state"]))
                record.update(retry_state=RETRY_TERMINAL, retry_reason=reason)
                state = RETRY_TERMINAL
            if state == RETRY_TERMINAL:
                claim = _claim(conn, record, record["retry_state"], RETRY_REPORTING, now)
                if claim:
                    actions.append(_action(record, "terminal", claim))
            else:
                claim = _claim(conn, record, record["retry_state"], RETRY_NOTICING, now)
                if claim:
                    actions.append(_action(record, "notice", claim))
    return actions


def _settle(delegation_id: str, claim: str, expected: str, sql_set: str, params: tuple) -> bool:
    ad = _ad()
    with ad._DB_LOCK, ad._transaction() as conn:
        return conn.execute(
            f"UPDATE async_delegations SET {sql_set}, updated_at=? WHERE delegation_id=? AND retry_state=? AND retry_claim=?",
            (*params, time.time(), delegation_id, expected, claim)).rowcount == 1


def mark_notice_accepted(delegation_id: str, claim: str, *, now: Optional[float] = None) -> bool:
    """The parent turn was admitted; start its dispatch grace window."""
    now = time.time() if now is None else now
    return _settle(delegation_id, claim, RETRY_NOTICING,
                   "retry_state='notified', retry_notices=retry_notices+1, retry_claimed_at=?", (now,))


def release_notice(delegation_id: str, claim: str) -> bool:
    """Injection failed or parent route not ready: retry the notice on a later sweep."""
    return _settle(delegation_id, claim, RETRY_NOTICING, "retry_state='scheduled', retry_claim=NULL", ())


def mark_retry_terminal(delegation_id: str, claim: str, reason: str) -> bool:
    """Parent conversation is gone: the user still gets the terminal line."""
    return _settle(delegation_id, claim, RETRY_NOTICING,
                   "retry_state='terminal', retry_claim=NULL, retry_reason=?", (reason,))


def mark_terminal_reported(delegation_id: str, claim: str) -> bool:
    return _settle(delegation_id, claim, RETRY_REPORTING, "retry_state='reported'", ())


def suppress_terminal_report(delegation_id: str, claim: str) -> bool:
    """A revoked target must never receive this report, even after a later reconnect."""
    return _settle(delegation_id, claim, RETRY_REPORTING,
                   "retry_state='cancelled', retry_reason='authorization_revoked', retry_claim=NULL", ())


def suppress_retry_notice(delegation_id: str, claim: str) -> bool:
    """A revoked target must never trigger autonomous recovery work or receive a parent reply."""
    return _settle(delegation_id, claim, RETRY_NOTICING,
                   "retry_state='cancelled', retry_reason='authorization_revoked', retry_claim=NULL", ())


def release_terminal_report(delegation_id: str, claim: str) -> bool:
    """Unsent reports remain owed, with bounded backoff; only a receipt may mark them reported."""
    ad = _ad()
    now = time.time()
    with ad._DB_LOCK, ad._transaction() as conn:
        row = conn.execute("SELECT retry_notices FROM async_delegations "
                           "WHERE delegation_id=? AND retry_state='reporting' AND retry_claim=?",
                           (delegation_id, claim)).fetchone()
        if row is None:
            return False
        due = now + _backoff(min(int(row[0] or 0), MAX_RETRY_ATTEMPTS))
        return conn.execute(
            """UPDATE async_delegations SET retry_notices=retry_notices+1, retry_claim=NULL, updated_at=?,
                      retry_state='terminal', retry_due_at=?
               WHERE delegation_id=? AND retry_state='reporting' AND retry_claim=?""",
            (now, due, delegation_id, claim)).rowcount == 1


def claim_retry_dispatch(delegation_id: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    """Atomically take the one dispatch claim for a retry-pending row (concurrent callers: one winner)."""
    ad = _ad()
    if not delegation_id:
        return None, INELIGIBLE_NO_ROW
    now = time.time()
    with ad._DB_LOCK, ad._transaction() as conn:
        record = _load(conn, delegation_id)
        if record is None:
            return None, INELIGIBLE_NO_ROW
        if record["retry_state"] not in (RETRY_SCHEDULED, RETRY_NOTICING, RETRY_NOTIFIED):
            return record, INELIGIBLE_CLAIMED if record["retry_state"] in (RETRY_DISPATCHING, RETRY_DISPATCHED) \
                else INELIGIBLE_STATE
        if reason := _expire_retry(conn, record, now):
            return record, reason
        claim = _claim(conn, record, record["retry_state"], RETRY_DISPATCHING, now)
        if claim is None:
            return record, INELIGIBLE_CLAIMED
    record.update(retry_state=RETRY_DISPATCHING, retry_claim=claim, retry_claimed_at=now)
    return record, None


def claimed_retry_source(record: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Return the retry row whose dispatch claim authorizes this recovery spawn."""
    ad = _ad()
    with ad._DB_LOCK, ad._transaction() as conn:
        prior = _replacement_source(conn, record, str(record.get("context") or ""))
    if prior and prior["retry_state"] == RETRY_DISPATCHING:
        return prior
    return None


def is_recovery_spawn(record: Dict[str, Any]) -> bool:
    """True when the spawn carries another delegation's recovery brief (no ledger access)."""
    m = _RECOVERY_HEADING_RE.search(str(record.get("context") or ""))
    return bool(m and m.group(1) != record.get("delegation_id"))


def release_retry_dispatch(record: Dict[str, Any]) -> bool:
    """Return a claimed retry to the scheduler after replacement persistence fails."""
    now = time.time()
    due = now + _backoff(int(record.get("retry_attempt") or 0))
    return _settle(
        record["delegation_id"], record.get("retry_claim", ""), RETRY_DISPATCHING,
        "retry_state='scheduled', retry_claim=NULL, retry_claimed_at=NULL, retry_due_at=?", (due,)
    )


def _replacement_source(conn, new: Dict[str, Any], task_text: str) -> Optional[Dict[str, Any]]:
    m = _RECOVERY_HEADING_RE.search(str(new.get("context") or "")) or _RECOVERY_HEADING_RE.search(task_text or "")
    if not m or m.group(1) == new["delegation_id"]:
        return None
    prior = _load(conn, m.group(1))
    if prior is None:
        return None
    same_owner = ((prior["session_key"] and prior["session_key"] == new.get("session_key"))
                  or (prior["parent_session_id"] and prior["parent_session_id"] == new.get("parent_session_id")))
    return prior if same_owner else None


def replacement_admission_error(conn, new: Dict[str, Any], task_text: str) -> Optional[str]:
    """Recheck in the INSERT transaction; commit expiry without inserting a child."""
    prior = _replacement_source(conn, new, task_text)
    if prior is None:
        return None
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
    with ad._DB_LOCK, ad._transaction() as conn:
        record = _load(conn, delegation_id)
        if record is None or not record["retry_attempt"]:
            return None
        # Attempt six may execute; only the next claim is barred by the attempt ceiling.
        return _expire_retry(conn, record, time.time(), check_attempts=False)


def link_replacement(conn, new: Dict[str, Any], task_text: str) -> None:
    """Carry lineage from the claimed recovery brief to the new row, inside the INSERT transaction."""
    prior = _replacement_source(conn, new, task_text)
    if prior is None:
        return
    new_id, dispatched_at = new["delegation_id"], new["dispatched_at"]
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


def retry_status(delegation_id: str) -> Optional[Dict[str, Any]]:
    ad = _ad()
    with ad._DB_LOCK, ad._transaction() as conn:
        record = _load(conn, delegation_id)
    return record


def list_retry_pending(limit: int = 50) -> list:
    """Rows Hermes is still retrying or reporting; for delegate_task action=list."""
    ad = _ad()
    with ad._DB_LOCK, ad._transaction() as conn:
        rows = conn.execute(_RETRY_SELECT + """ WHERE retry_state IN
            ('scheduled','noticing','notified','dispatching','terminal','reporting') ORDER BY updated_at DESC LIMIT ?""",
                            (limit,)).fetchall()
    return [_retry_record(r) for r in rows]


def _when(ts: Optional[float]) -> str:
    return time.strftime("%H:%M", time.localtime(ts)) if ts else "soon"


def retry_note(plan: Optional[Dict[str, Any]]) -> str:
    """One line for the completion notice so the parent does not dispatch a duplicate (R5)."""
    if not plan:
        return ""
    if plan["retry_state"] == RETRY_SCHEDULED:
        return (f"Hermes will retry this automatically at {_when(plan['due_at'])} (retry {plan['attempt']}/"
                f"{MAX_RETRY_ATTEMPTS}, {plan['reason']}); you will get a notice then. Do not re-dispatch it yourself.")
    return f"Hermes will not retry this ({plan['reason']}); the user is being told it stopped."


def build_retry_notice(record: Dict[str, Any]) -> str:
    from tools.process_registry_notifications import PROCESS_NOTIFICATION_END
    did = record["delegation_id"]
    attempt = record["retry_attempt"] + 1
    reprompt = record["retry_notices"] >= 1
    return (
        f"[IMPORTANT: AUTOMATIC RETRY DUE — {did}] "
        + ("REMINDER: the previous retry notice was not acted on. " if reprompt else "")
        + f"A background delegation stopped ({record['retry_reason']}) and Hermes scheduled retry "
        f"{attempt}/{MAX_RETRY_ATTEMPTS}. Do it now: call delegate_task(action='resume', subagent_id='{did}'), "
        "then call delegate_task with the returned goal and pass 'recovery_context' verbatim as context. "
        "If it is no longer wanted, say so to the user. "
        + ("If you do not dispatch it, Hermes will tell the user the task stopped." if reprompt else
           "Replying NO_REPLY without dispatching does not cancel the retry; you will be asked once more.")
        + f"]\n{PROCESS_NOTIFICATION_END}"
    )


_TERMINAL_REASON_LABELS = {
    "billing": "provider rejected the request",
    "authentication": "provider rejected the request",
    "auth_permanent": "provider rejected the request",
    "auth": "provider rejected the request",
    "content_policy_blocked": "provider rejected the request",
    "format_error": "provider rejected the request",
    "context_length_exceeded": "provider rejected the request",
    "rate_limit": "rate-limited too many times",
    "upstream_rate_limit": "rate-limited too many times",
    "timeout": "timed out too many times",
}


def _terminal_reason_label(reason: str) -> str:
    # Never interpolate exception text: only fixed categories can cross the egress boundary.
    if reason.startswith("retry budget exhausted"):
        return "still failing after 24h" if "24h" in reason else "retry limit reached"
    if reason.startswith("the parent did not dispatch the retry"):
        return "the retry was not dispatched"
    if reason == "its conversation ended before the retry could run":
        return "its conversation ended"
    return _TERMINAL_REASON_LABELS.get(reason, "the task failed")


def build_terminal_line(record: Dict[str, Any]) -> str:
    from agent.redact import redact_for_egress

    goal = redact_for_egress(str((record.get("task") or {}).get("goal") or "background task"))
    goal = " ".join(goal.split())
    if len(goal) > 120:
        goal = goal[:117] + "…"
    why = _terminal_reason_label(str(record.get("retry_reason") or ""))
    return f"Background task stopped: {goal} — {why}."
