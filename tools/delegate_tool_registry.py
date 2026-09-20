"""Live-subagent registry + model-facing control plane (list/steer/stop) for delegate_task."""

from __future__ import annotations

import logging
import json
import threading
import time
from typing import Any, Dict, List, Optional
from agent.interrupt_compat import request_hard_interrupt
from tools.registry import tool_error

logger = logging.getLogger("tools.delegate_tool")  # log-record parity with the origin module

_spawn_pause_lock = threading.Lock()
_spawn_paused: bool = False
_active_subagents_lock = threading.Lock()
# subagent_id -> mutable record tracking the live child agent.  Stays only
# for the lifetime of the run; _run_single_child is the owner.
_active_subagents: Dict[str, Dict[str, Any]] = {}
# subagent_id -> {goal, delegation_id, owner_agent_session_id} retained AFTER the child finishes (bounded FIFO).
# Child-started background processes routinely outlive the child (its npm ci with notify_on_complete=true finishes
# after the summary was delivered); their completion notifications reach the parent via the shared completion_queue
# and need delegation attribution even though the live registry entry is gone.
_RECENT_SUBAGENTS_CAP = 200
_recent_subagents: Dict[str, Dict[str, Any]] = {}

def get_subagent_attribution(task_id: Optional[str]) -> Optional[Dict[str, Any]]:
    """``{subagent_id, goal, delegation_id}`` for a process task_id that belongs to a live or recently-finished child
    (children run their terminal sessions under ``task_id == subagent_id``), else None."""
    if not task_id or not isinstance(task_id, str):
        return None
    with _active_subagents_lock:
        record = _active_subagents.get(task_id) or _recent_subagents.get(task_id)
    if record is None:
        return None
    return {"subagent_id": task_id, "goal": record.get("goal"), "delegation_id": record.get("delegation_id")}

def set_spawn_paused(paused: bool) -> bool:
    """Globally block/unblock NEW delegate_task spawns (active children keep running). Returns the new state."""
    global _spawn_paused
    with _spawn_pause_lock:
        _spawn_paused = bool(paused)
        return _spawn_paused

def is_spawn_paused() -> bool:
    with _spawn_pause_lock:
        return _spawn_paused

def _register_subagent(record: Dict[str, Any]) -> None:
    sid = record.get("subagent_id")
    if not sid:
        return
    record.setdefault("accepting_steer", True)
    with _active_subagents_lock:
        _active_subagents[sid] = record

def _unregister_subagent(subagent_id: str, *, agent: Any = None) -> None:
    """Drop the live record (exact agent identity when given) and keep a bounded attribution stub."""
    with _active_subagents_lock:
        record = _active_subagents.get(subagent_id)
        if record is None or not (agent is None or record.get("agent") is agent):
            return
        _active_subagents.pop(subagent_id, None)
        sid = record.get("subagent_id")
        if not sid:
            return
        _recent_subagents[sid] = {k: record.get(k) for k in ("goal", "delegation_id", "owner_agent_session_id")}
        while len(_recent_subagents) > _RECENT_SUBAGENTS_CAP:
            _recent_subagents.pop(next(iter(_recent_subagents)), None)

def _close_subagent_steering(subagent_id: str, agent: Any) -> Optional[str]:
    """Atomically close steer acceptance and drain its final durable artifact. ``steer_subagent`` holds the same
    registry lock through ``agent.steer``, so either acceptance wins and this drain sees its exact text, or closure
    wins and the caller is rejected. Exact agent identity prevents a finishing child with a recycled public id from
    closing its replacement."""
    with _active_subagents_lock:
        record = _active_subagents.get(subagent_id)
        if record is None or record.get("agent") is not agent:
            return None
        record["accepting_steer"] = False
        drain = getattr(agent, "_drain_pending_steer", None)
        if not callable(drain):
            return None
        try:
            pending = drain()
        except Exception as exc:
            logger.debug("final steer drain for %s failed: %s", subagent_id, exc)
            return None
        return pending if isinstance(pending, str) and pending.strip() else None

def interrupt_subagent(subagent_id: str) -> bool:
    """Request that one running subagent stop at its next iteration boundary
    (cooperative: the flag propagates to in-flight tools and recurses into
    grandchildren via AIAgent.interrupt()). True iff a matching subagent was found."""
    with _active_subagents_lock:
        record = _active_subagents.get(subagent_id)
    agent = record.get("agent") if record else None
    if agent is None:
        return False
    try:
        return bool(request_hard_interrupt(agent, f"Interrupted via TUI ({subagent_id})"))
    except Exception as exc:
        logger.debug("interrupt_subagent(%s) failed: %s", subagent_id, exc)
        return False

def _subagent_transport_matches(record, transport) -> bool:
    """Authority follows the owning session's LIVE transport slot, read at check time.

    ``owner_transport`` on the record is only the capture-time marker that a gateway session
    commissioned the child (``None`` = no RPC authority ever). The slot is authoritative because
    every reattach path (prompt.submit, queued drain, resume, activate, viewer failover) already
    mutates it; a per-record copy needed a matching registry sync at each of those sites and two
    were missed (#106663). Records whose owner is not a session dict keep the exact-object rule."""
    from tui_gateway.transport import FanoutTransport

    if record.get("owner_transport") is None:
        return False
    owner = record.get("owner_session_record")
    bound = owner.get("transport") if isinstance(owner, dict) else record.get("owner_transport")
    return bound is transport or (isinstance(bound, FanoutTransport) and bound.contains(transport))


def steer_subagent(
    subagent_id: str, text: str, *, owner_session_id: Optional[str] = None, owner_transport: Any = None,
    owner_session_record: Any = None,
) -> bool:
    """Queue steering text into a running subagent without stopping it.

    AIAgent.steer() appends the text to the child's last tool result at its next iteration boundary — the current tool
    call is never cut. True iff the text was QUEUED while the child still accepted work; False for unknown/closed id,
    ownership mismatch, no live agent, or empty text. ``owner_session_id=None`` keeps the in-process helper contract;
    gateway callers must pass exact authority. Acceptance and completion are linearized by the registry lock: if
    acceptance wins but no delivery boundary remains, the text lands in the entry as ``missed_steer``.
    """
    if not text or not text.strip():
        return False
    with _active_subagents_lock:
        record = _active_subagents.get(subagent_id)
        if not record or not record.get("accepting_steer", False):
            return False
        if owner_session_id is not None and (
            record.get("owner_session_id") != owner_session_id
            or owner_transport is None
            or not _subagent_transport_matches(record, owner_transport)
            or owner_session_record is None
            or record.get("owner_session_record") is not owner_session_record
        ):
            return False
        agent = record.get("agent")
        if agent is None:
            return False
        try:
            return bool(agent.steer(text))
        except Exception as exc:
            logger.debug("steer_subagent(%s) failed: %s", subagent_id, exc)
            return False

def _capture_gateway_steer_authority(owner_session_id: Optional[str]) -> tuple[Any, Any]:
    """Exact request transport + live session generation, if any — an in-process
    bridge, not a serializable capability. Non-gateway hosts get ``(None, None)``."""
    if not owner_session_id:
        return None, None
    try:
        from tui_gateway.server import _current_session_steer_authority
        return _current_session_steer_authority(owner_session_id)
    except Exception:
        return None, None

# Registry record fields never exposed to the TUI/RPC snapshot.
_PRIVATE_RECORD_KEYS = frozenset({"agent", "owner_session_id", "owner_transport", "owner_session_record", "accepting_steer"})

def list_active_subagents() -> List[Dict[str, Any]]:
    """Copy of the running subagent tree ({subagent_id, parent_id, depth, goal, model,
    started_at, tool_count, status, ...}); safe from any thread."""
    with _active_subagents_lock:
        return [{k: v for k, v in r.items() if k not in _PRIVATE_RECORD_KEYS} for r in _active_subagents.values()]

def _is_descendant_of(child_agent: Any, parent_agent: Any, max_hops: int = 8) -> bool:
    """True when *child_agent* sits below *parent_agent* in the spawn tree (walks the ``_delegate_parent_ref`` weakref
    chain stamped at build time). Identity only — a parent may steer/stop its own children and grandchildren, never
    a sibling tree owned by another conversation."""
    if child_agent is None or parent_agent is None:
        return False
    cur = child_agent
    for _ in range(max_hops):
        ref = getattr(cur, "_delegate_parent_ref", None)
        ancestor = ref() if callable(ref) else None
        if ancestor is None:
            return False
        if ancestor is parent_agent:
            return True
        cur = ancestor
    return False

# Model-facing control actions accepted by delegate_task(action=...).
# "spawn" (or omitted) keeps the historical spawn semantics.
_CONTROL_ACTIONS = frozenset({"list", "steer", "stop", "resume"})

def _resolve_session_lineage(session_id: Optional[str], parent_agent: Any) -> str:
    """Tip of a session id's compression lineage via the parent's live SessionDB (best-effort; input unchanged when
    unavailable) so a delegation dispatched before a compression rotation still matches the rotated parent."""
    sid = str(session_id or "")
    db = getattr(parent_agent, "_session_db", None)
    if not sid or db is None:
        return sid
    try:
        resolved = db.resolve_resume_session_id(sid)
        return str(resolved) if resolved else sid
    except Exception:
        return sid

def _owns_subagent_record(record: Dict[str, Any], parent_agent: Any) -> bool:
    """True when *parent_agent*'s conversation owns this live-child record.

    Tier 1: identity — the ``_delegate_parent_ref`` weakref chain reaches
    *parent_agent* (fast path while the parent AIAgent survives the run). Tier 2:
    durable lineage — the record's ``owner_agent_session_id`` matches the caller's
    ``session_id`` after resolving compression-rotation lineage on both sides.
    Tier 2 exists because the identity chain is BRITTLE across parent rebuilds:
    the CLI sets ``self.agent = None`` mid-session (route change, credential
    refresh, /model, MoA one-shots) and builds a NEW AIAgent while the child keeps
    a weakref to the old one. Delivery routes by durable session id; control must
    use the same spine or running children go invisible/unsteerable.
    """
    if _is_descendant_of(record.get("agent"), parent_agent):
        return True
    owner_sid = str(record.get("owner_agent_session_id") or "")
    parent_sid = str(getattr(parent_agent, "session_id", "") or "")
    if not owner_sid or not parent_sid:
        return False
    if owner_sid == parent_sid:
        return True
    # Compression rotation on either side: compare lineage tips.
    return _resolve_session_lineage(owner_sid, parent_agent) in {parent_sid, _resolve_session_lineage(parent_sid, parent_agent)}

def _list_payload(parent_agent: Any) -> Dict[str, Any]:
    with _active_subagents_lock:
        records = list(_active_subagents.values())
    entries = []
    for r in records:
        if not _owns_subagent_record(r, parent_agent):
            continue
        started = r.get("started_at")
        entries.append({
            "subagent_id": r.get("subagent_id"),
            "parent_id": r.get("parent_id"),
            "goal": r.get("goal"),
            "model": r.get("model"),
            "status": r.get("status"),
            "running_seconds": round(time.time() - started, 1) if isinstance(started, (int, float)) else None,
            "accepting_steer": bool(r.get("accepting_steer", False)),
            "live_transcript": getattr(r.get("agent"), "_live_transcript_path", None),
        })
    payload: Dict[str, Any] = {"action": "list", "count": len(entries), "subagents": entries}
    if not entries:
        payload["note"] = (
            "No live subagents right now. Children that already finished "
            "have delivered (or will deliver) their results as normal "
            "completion messages — there is nothing to steer or stop."
        )
    return payload

def _owns_durable_delegation(record: Dict[str, Any], parent_agent: Any) -> bool:
    """True when *parent_agent*'s conversation owns a DURABLE delegation row.

    Same durable spine as :func:`_owns_subagent_record` tier 2 (the live weakref
    chain is gone by definition — the child is dead), comparing the row's
    ``parent_session_id`` / ``origin_session_id`` against the caller's session id
    after resolving compression-rotation lineage on both sides. Fails closed when
    either side has no durable id, so a session that cannot prove ownership can
    never claim another conversation's interrupted work.
    """
    parent_sid = str(getattr(parent_agent, "session_id", "") or "")
    if not parent_sid:
        return False
    parent_tip = _resolve_session_lineage(parent_sid, parent_agent)
    for key in ("parent_session_id", "origin_session_id"):
        owner_sid = str(record.get(key) or "")
        if not owner_sid:
            continue
        if owner_sid == parent_sid:
            return True
        if _resolve_session_lineage(owner_sid, parent_agent) in {parent_sid, parent_tip}:
            return True
    return False


# Refusal token -> prose shown to the model. Every refusal is truthful about WHY
# the interrupted work is not safe to resume automatically.
_RESUME_REFUSALS = {
    "no_such_delegation": (
        "No durable record for delegation '{sid}'. Only background delegations dispatched by this "
        "installation are recoverable, and terminal records are pruned after a retention window."
    ),
    "not_interrupted": (
        "Delegation '{sid}' is not an interrupted single-task delegation awaiting recovery (it is "
        "still running, already reported a terminal result, or has no recorded goal). Nothing to resume."
    ),
    "batch_delegation": (
        "Delegation '{sid}' is a multi-task batch. Batch recovery is out of scope: re-delegate the "
        "specific tasks you can show are unfinished, after checking their results."
    ),
    "partial_results_recorded": (
        "Delegation '{sid}' recorded partial per-child results. Those need reconciliation, not a "
        "blind re-run — read the recorded results and delegate only the proven gap."
    ),
    "stateless_origin": (
        "Delegation '{sid}' was dispatched from a stateless origin (cron or a one-shot run) with no "
        "conversation to own a recovery. Re-run it from a session that can receive the result."
    ),
    "already_claimed": (
        "Delegation '{sid}' has already been claimed for recovery once. Recovery is one-shot per "
        "delegation by design; if the work is still unfinished, spawn a fresh task describing the "
        "verified current state yourself."
    ),
}


def _handle_resume_action(subagent_id: Optional[str], parent_agent: Any) -> str:
    """action='resume': hand back a ONE-SHOT recovery brief for an interrupted delegation.

    This never re-spawns anything. It durably claims the row (at most one claim per
    delegation, ever) and returns the exact ``context`` the parent must pass to a
    normal ``delegate_task`` spawn — so the replacement child goes through every
    existing spawn gate and shows up in the parent's own transcript.
    """
    from tools.delegation_resume import build_recovery_instruction, claim_resume, inspect_resumable

    sid = (subagent_id or "").strip()
    if not sid:
        return tool_error(
            "action='resume' requires subagent_id set to the delegation_id of the interrupted "
            "background delegation (it appears in its completion message)."
        )
    record, reason = inspect_resumable(sid)
    if reason is None and record is not None and not _owns_durable_delegation(record, parent_agent):
        # Ownership is checked BEFORE the claim so a foreign caller cannot burn
        # the one recovery claim belonging to another conversation.
        return tool_error(
            f"Delegation '{sid}' does not belong to this conversation. Only the session that "
            "dispatched a delegation can recover it."
        )
    if reason is None:
        record, reason = claim_resume(sid)
    if reason is not None or record is None:
        return tool_error(_RESUME_REFUSALS.get(reason or "", _RESUME_REFUSALS["not_interrupted"]).format(sid=sid))
    return json.dumps({
        "action": "resume",
        "delegation_id": sid,
        "status": "recovery_claimed",
        "goal": (record.get("task") or {}).get("goal"),
        "recovery_context": build_recovery_instruction(record),
        "note": (
            "This claim is one-shot and already spent — no second recovery brief will be issued for "
            "this delegation. Nothing has been spawned. To act on it, call delegate_task normally "
            "with the original goal and pass 'recovery_context' verbatim as the task's context. The "
            "replacement subagent must verify current workspace and external state before doing any "
            "new work: the interrupted run may have completed side effects it never reported."
        ),
    }, ensure_ascii=False)


def _handle_control_action(action: str, subagent_id: Optional[str], message: Optional[str], parent_agent: Any) -> str:
    """Synchronous control plane for delegate_task: list/steer/stop/resume. Runs in-turn (never backgrounded) over the
    same registry the TUI overlay drives, scoped so a conversation can only control its own spawn tree."""
    if action == "list":
        return json.dumps(_list_payload(parent_agent), ensure_ascii=False)
    if action == "resume":
        return _handle_resume_action(subagent_id, parent_agent)

    # steer / stop need a resolvable, owned target.
    sid = (subagent_id or "").strip()
    if not sid:
        return tool_error(f"action='{action}' requires subagent_id (from the spawn dispatch response or action='list').")
    with _active_subagents_lock:
        record = _active_subagents.get(sid)
    if record is None or not _owns_subagent_record(record, parent_agent):
        return tool_error(
            f"No live subagent '{sid}' in this conversation's spawn tree. It "
            "may have already finished (its result arrives as a normal "
            "completion message). Use action='list' to see live children."
        )
    if action == "steer" and not (message or "").strip():
        return tool_error("action='steer' requires a non-empty 'message' describing the course correction.")
    outcome = _CONTROL_OUTCOMES.get(action)
    if outcome is None:
        return tool_error(f"Unknown action '{action}'. Use spawn, list, steer, or stop.")
    status, note, failure = outcome
    ok = interrupt_subagent(sid) if action == "stop" else steer_subagent(sid, message.strip())
    if ok:
        return json.dumps({"action": action, "subagent_id": sid, "status": status, "note": note}, ensure_ascii=False)
    return tool_error(failure.format(sid=sid))

# action -> (success status, success note, failure error template)
_CONTROL_OUTCOMES = {
    "stop": (
        "interrupt_requested",
        "The subagent stops at its next iteration boundary (in-flight tool calls are asked to cancel). Its "
        "partial result still re-enters the conversation as a completion message — do not wait or poll.",
        "Could not interrupt '{sid}' — it likely finished in the last "
        "moment. Its result arrives as a normal completion message.",
    ),
    "steer": (
        "queued",
        "Steering text queued. The subagent sees it appended to its next tool result — the current tool call is "
        "never cut. If the child finishes before a delivery boundary remains, the text is reported back as "
        "missed_steer in its completion entry.", "Subagent '{sid}' is no longer accepting steering (finishing or "
        "already finished). Its result arrives as a normal completion "
        "message; re-delegate a follow-up task if more work is needed.",
    ),
}
