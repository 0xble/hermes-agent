"""Cross-session goal and loop controls with durable approval records."""
from __future__ import annotations

import json
import logging
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Dict, Optional, Tuple

from hermes_cli.goals import (
    _REVISION_QUOTE_MIN_CHARS,
    _REVISION_SOURCE_MAX_CHARS,
    _get_session_db,
    _is_user_typed,
)

logger = logging.getLogger(__name__)

_REQUEST_TTL_SECONDS = 24 * 60 * 60
_CONTROL_PREFIX = "session_control:"
_CURRENT_AUTHORITY: ContextVar[Optional[Dict[str, Any]]] = ContextVar(
    "session_control_authority", default=None
)


def _current_profile() -> str:
    try:
        from hermes_cli.profiles import current_profile_name
        return str(current_profile_name("default") or "default")
    except Exception:
        return "default"


def _db():
    return _get_session_db()


def _record_key(request_id: str) -> str:
    return f"{_CONTROL_PREFIX}{request_id}"


def _now() -> float:
    return time.time()


def _normalize(text: str) -> str:
    return " ".join(str(text or "").split())


def resolve_target(target: str) -> str:
    """Resolve a bare session id or ``hermes:<profile>/<session_id>``."""
    raw = str(target or "").strip()
    if raw.startswith("hermes:"):
        value = raw[len("hermes:"):]
        if "/" not in value:
            raise ValueError("unknown_target")
        profile, sid = value.split("/", 1)
        if profile != _current_profile():
            raise ValueError("cross_profile")
        raw = sid
    if not raw:
        raise ValueError("unknown_target")
    db = _db()
    if db is None or not db.get_session(raw):
        raise ValueError("unknown_target")
    return raw


def check_user_quote(requester_session_id: str, quote: str) -> Tuple[str, str] | str:
    """Validate a quote against only the requester's latest typed user message."""
    normalized = _normalize(quote)
    if len(normalized) < _REVISION_QUOTE_MIN_CHARS:
        return "user_quote_too_short"
    db = _db()
    if db is None:
        return "user_quote_not_found"
    try:
        rows = db.messages_by_role(requester_session_id, "user", since=0.0, limit=500)
    except Exception:
        return "user_quote_not_found"
    latest = next((row for row in rows if _is_user_typed(row)), None)
    if latest is None:
        return "user_quote_not_found"
    source_raw = str(latest.get("content") or "")
    try:
        from gateway.response_filters import is_agent_origin_text
        if is_agent_origin_text(source_raw):
            return "user_quote_not_found"
    except ValueError:
        raise
    except Exception:
        return "user_quote_not_found"
    source = _normalize(source_raw)
    if len(source) > _REVISION_SOURCE_MAX_CHARS:
        return "user_message_too_long"
    if normalized not in source:
        return "user_quote_not_found"
    return normalized, source


def _load_record(request_id: str) -> Optional[Dict[str, Any]]:
    db = _db()
    if db is None:
        return None
    raw = db.get_meta(_record_key(request_id))
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (TypeError, ValueError):
        return None


def _save_record(record: Dict[str, Any], *, cursor=None) -> None:
    db = _db()
    if db is None:
        raise RuntimeError("session database unavailable")
    db.set_meta(_record_key(record["id"]), json.dumps(record, ensure_ascii=False), cursor=cursor)


def _manager_apply(kind: str, action: str, target_sid: str, *, reason: str,
                   payload: Optional[Dict[str, Any]] = None,
                   authority: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    payload = dict(payload or {})
    handler = CONTROLS.get((kind, action))
    if action == "replace" and kind == "goal":
        from hermes_cli.goals import GoalManager
        manager = GoalManager(target_sid)
        with _approved_authority(authority):
            result = manager.replace(
                reason=reason or "session-control",
                goal=str(payload.get("goal") or ""),
                max_turns=payload.get("max_turns"),
                contract=payload.get("contract"),
                user_quote=str((authority or {}).get("quote") or ""),
                user_messages=[str((authority or {}).get("message") or "")],
            )
        return {"result": result, "state": getattr(manager, "state", None)}
    if not callable(handler):
        raise ValueError("unsupported_control")
    result = handler(target_sid, reason, payload, authority)
    if kind == "goal":
        from hermes_cli.goals import load_goal
        state = load_goal(target_sid)
    elif kind == "loop":
        from hermes_cli.loops import load_loop
        state = load_loop(target_sid)
    else:
        raise ValueError("unsupported_control")
    return {"result": result, "state": state}


def _append_control_revision(kind: str, action: str, target_sid: str, *, reason: str,
                             authority: Optional[Dict[str, Any]]) -> None:
    """Keep the target's existing JSON revision history useful to status/judge consumers."""
    if action == "replace":
        return
    revision = {"kind": "session-control", "action": action, "reason": reason or "session-control",
                "authority": dict(authority or {}), "at": _now(), "actor": "session-control"}
    try:
        if kind == "goal":
            from hermes_cli.goals import load_goal, save_goal
            state = load_goal(target_sid)
            if state is not None:
                state.revisions.append(revision)
                save_goal(target_sid, state)
        elif kind == "loop":
            from hermes_cli.loops import load_loop, save_loop
            state = load_loop(target_sid)
            if state is not None:
                state.revisions.append(revision)
                save_loop(target_sid, state)
    except Exception:
        logger.debug("could not append session-control revision", exc_info=True)


@contextmanager
def _approved_authority(authority: Optional[Dict[str, Any]]):
    token = _CURRENT_AUTHORITY.set(authority if authority else None)
    try:
        yield
    finally:
        _CURRENT_AUTHORITY.reset(token)


def request_control(kind: str, action: str, target_sid: str, *, requester_sid: str,
                    reason: str = "", payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Create a durable, 24-hour pending approval request."""
    if (kind, action) not in CONTROLS:
        raise ValueError("unsupported_control")
    target_sid = resolve_target(target_sid)
    request_id = uuid.uuid4().hex[:12]
    now = _now()
    record = {
        "id": request_id, "kind": kind, "action": action,
        "target_session_id": target_sid, "requester_session_id": requester_sid,
        "reason": reason or "session-control", "payload": dict(payload or {}),
        "authority": {"via": "button", "user_id": None}, "status": "pending",
        "created_at": now, "expires_at": now + _REQUEST_TTL_SECONDS,
        "resolved_at": None, "error": None,
        "request_posted": False, "target_notice_sent": False,
        "requester_notified": False, "continuations_cleared": False,
    }
    _save_record(record)
    return record


def apply_control(kind: str, action: str, target_sid: str, *, requester_sid: str,
                  reason: str = "", user_quote: str = "",
                  payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Apply an authorized control immediately, or create an approval request."""
    if (kind, action) not in CONTROLS:
        return {"ok": False, "error_code": "unsupported_control"}
    try:
        target_sid = resolve_target(target_sid)
    except ValueError as exc:
        return {"ok": False, "error_code": str(exc), "error": str(exc)}
    quote_check = check_user_quote(requester_sid, user_quote)
    if isinstance(quote_check, str):
        code = quote_check
        if user_quote:
            return {"ok": False, "error_code": code, "error": code}
        record = request_control(kind, action, target_sid, requester_sid=requester_sid,
                                 reason=reason, payload=payload)
        return {"ok": True, "status": "pending", "request_id": record["id"], "record": record}
    quote, message = quote_check
    authority = {"via": "quote", "quote": quote, "message": message}
    try:
        result = _manager_apply(kind, action, target_sid, reason=reason, payload=payload, authority=authority)
        if isinstance(result.get("result"), dict) and not result["result"].get("ok", True):
            raise ValueError(result["result"].get("error_code") or result["result"].get("error") or "apply_failed")
        _append_control_revision(kind, action, target_sid, reason=reason, authority=authority)
        record = {
            "id": uuid.uuid4().hex[:12], "kind": kind, "action": action,
            "target_session_id": target_sid, "requester_session_id": requester_sid,
            "reason": reason or "session-control", "payload": dict(payload or {}),
            "authority": authority, "status": "applied", "created_at": _now(),
            "expires_at": None, "resolved_at": _now(), "error": None,
            "request_posted": False, "target_notice_sent": False,
            "requester_notified": False, "continuations_cleared": False,
        }
        _save_record(record)
        return {"ok": True, "status": "applied", "record": record, **result}
    except Exception as exc:
        return {"ok": False, "error_code": "apply_failed", "error": str(exc)}


def resolve_request(request_id: str, decision: str, user_id: str) -> Optional[Dict[str, Any]]:
    """Atomically claim a pending request, then apply it when approved."""
    if decision not in {"approve", "deny"}:
        return None
    db = _db()
    if db is None:
        return None
    key = _record_key(request_id)
    now = _now()
    def _claim(conn):
        row = conn.execute("SELECT value FROM state_meta WHERE key = ?", (key,)).fetchone()
        if row is None:
            return None
        try:
            record = json.loads(row[0])
        except (TypeError, ValueError):
            return None
        if record.get("status") != "pending":
            return None
        if float(record.get("expires_at") or 0) <= now:
            record["status"] = "expired"
            record["resolved_at"] = now
            db.set_meta(key, json.dumps(record, ensure_ascii=False), cursor=conn)
            return None
        if decision == "deny":
            record["status"] = "denied"
            record["authority"] = {"via": "button", "user_id": str(user_id)}
            record["resolved_at"] = now
            db.set_meta(key, json.dumps(record, ensure_ascii=False), cursor=conn)
            return record
        record["status"] = "applying"
        record["authority"] = {"via": "button", "user_id": str(user_id)}
        record["resolved_at"] = now
        db.set_meta(key, json.dumps(record, ensure_ascii=False), cursor=conn)
        return record
    claimed = db._execute_write(_claim)
    if claimed is None or claimed.get("status") == "expired":
        return None
    if claimed.get("status") == "denied":
        return claimed
    try:
        authority = claimed["authority"]
        result = _manager_apply(claimed["kind"], claimed["action"], claimed["target_session_id"],
                                reason=claimed.get("reason") or "session-control",
                                payload=claimed.get("payload"), authority=authority)
        if isinstance(result.get("result"), dict) and not result["result"].get("ok", True):
            raise ValueError(result["result"].get("error_code") or result["result"].get("error") or "apply_failed")
        _append_control_revision(claimed["kind"], claimed["action"], claimed["target_session_id"],
                                 reason=claimed.get("reason") or "session-control", authority=authority)
        claimed["status"] = "applied"
        claimed["error"] = None
    except Exception as exc:
        claimed["status"] = "failed"
        claimed["error"] = str(exc)
    _save_record(claimed)
    return claimed


def pending_outbox() -> list[Dict[str, Any]]:
    db = _db()
    if db is None:
        return []
    out = []
    for _key, raw in db.list_meta_prefix(_CONTROL_PREFIX):
        try:
            record = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if record.get("status") == "pending" or not record.get("target_notice_sent") or not record.get("requester_notified"):
            out.append(record)
    return out


def mark_outbox(request_id: str, flag: str) -> Optional[Dict[str, Any]]:
    record = _load_record(request_id)
    if record is None:
        return None
    record[flag] = True
    _save_record(record)
    return record


CONTROLS = {
    ("goal", "pause"): lambda sid, reason, payload, authority: __import__("hermes_cli.goals", fromlist=["GoalManager"]).GoalManager(sid).pause(reason=reason or "session-control"),
    ("goal", "resume"): lambda sid, reason, payload, authority: __import__("hermes_cli.goals", fromlist=["GoalManager"]).GoalManager(sid).resume(),
    ("goal", "clear"): lambda sid, reason, payload, authority: __import__("hermes_cli.goals", fromlist=["GoalManager"]).GoalManager(sid).clear(),
    ("loop", "pause"): lambda sid, reason, payload, authority: __import__("hermes_cli.loops", fromlist=["LoopManager"]).LoopManager(sid).pause(reason=reason or "session-control"),
    ("loop", "resume"): lambda sid, reason, payload, authority: __import__("hermes_cli.loops", fromlist=["LoopManager"]).LoopManager(sid).resume(),
    ("loop", "stop"): lambda sid, reason, payload, authority: __import__("hermes_cli.loops", fromlist=["LoopManager"]).LoopManager(sid).clear(),
    ("goal", "replace"): None,
}

__all__ = [
    "CONTROLS", "resolve_target", "check_user_quote", "apply_control", "request_control",
    "resolve_request", "pending_outbox", "mark_outbox",
]
