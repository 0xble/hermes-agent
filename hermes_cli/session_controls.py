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
        continuation_prompt = manager.next_continuation_prompt() if result.get("ok") else None
        return {"result": result, "state": getattr(manager, "state", None),
                "continuation_prompt": continuation_prompt}
    if kind == "goal" and action == "resume":
        from hermes_cli.goals import GoalManager
        manager = GoalManager(target_sid)
        if manager.state is None or manager.state.status == "cleared":
            code = f"nothing_to_{action}"
            return {"result": {"ok": False, "error_code": code, "error": code},
                    "state": manager.state}
        result = manager.resume()
        if result is None:
            code = f"nothing_to_{action}"
            return {"result": {"ok": False, "error_code": code, "error": code},
                    "state": getattr(manager, "state", None)}
        return {"result": result, "state": getattr(manager, "state", None),
                "continuation_prompt": manager.next_continuation_prompt()}
    if not callable(handler):
        raise ValueError("unsupported_control")
    before_state = None
    if kind == "goal":
        from hermes_cli.goals import load_goal
        before_state = load_goal(target_sid)
        # A cleared goal stays stored; pausing or clearing it again must not revive or re-announce it.
        if before_state is None or getattr(before_state, "status", None) not in {"active", "paused"}:
            code = f"nothing_to_{action}"
            return {"result": {"ok": False, "error_code": code, "error": code}, "state": before_state}
    result = handler(target_sid, reason, payload, authority)
    if kind == "goal":
        from hermes_cli.goals import load_goal
        state = load_goal(target_sid)
    elif kind == "loop":
        from hermes_cli.loops import load_loop
        state = load_loop(target_sid)
    else:
        raise ValueError("unsupported_control")
    cleared_goal = (
        kind == "goal" and action == "clear" and before_state is not None
        and getattr(before_state, "status", None) != "cleared"
        and state is not None and getattr(state, "status", None) == "cleared"
    )
    if result is False or (result is None and not cleared_goal):
        code = f"nothing_to_{action}"
        return {"result": {"ok": False, "error_code": code, "error": code}, "state": state}
    return {"result": result, "state": state}


def _session_title(session_id: str) -> str:
    db = _db()
    if db is None:
        return ""
    try:
        return str(db.get_session_title(session_id) or "")
    except Exception:
        return ""


def _affected_text(kind: str, action: str, target_sid: str, payload: Optional[Dict[str, Any]] = None) -> str:
    payload = payload or {}
    try:
        if kind == "goal":
            from hermes_cli.goals import GoalManager
            state = GoalManager(target_sid).state
            old = str(getattr(state, "goal", "") or "")
            if action == "replace":
                return f"goal: {old or '(none)'} -> {str(payload.get('goal') or '')}"
            return f"goal: {old or '(none)'}"
        if kind == "loop":
            from hermes_cli.loops import LoopManager
            state = LoopManager(target_sid).state
            prompt = str(getattr(state, "prompt", "") or "")
            cadence = state.cadence_label() if state is not None else ""
            return f"loop: {prompt or '(none)'}{f' ({cadence})' if cadence else ''}"
    except Exception:
        logger.debug("could not describe session-control target", exc_info=True)
    return ""


def _new_record(kind: str, action: str, target_sid: str, requester_sid: str, *,
                reason: str, payload: Optional[Dict[str, Any]], authority: Dict[str, Any],
                status: str, now: float, expires_at: Optional[float]) -> Dict[str, Any]:
    return {
        "id": uuid.uuid4().hex[:12], "kind": kind, "action": action,
        "target_session_id": target_sid, "requester_session_id": requester_sid,
        "requester_title": _session_title(requester_sid), "target_title": _session_title(target_sid),
        "reason": reason or "session-control", "payload": dict(payload or {}),
        "affected_text": _affected_text(kind, action, target_sid, payload),
        "authority": dict(authority), "status": status, "created_at": now,
        "expires_at": expires_at, "resolved_at": None, "error": None,
        "request_posted": status != "pending", "request_skipped": False,
        "target_notice_sent": False, "target_notice_skipped": False,
        "requester_notified": False, "requester_notification_skipped": False,
        "continuations_cleared": False, "continuation_enqueued": False,
        "outbox_done": False,
    }


@contextmanager
def _approved_authority(authority: Optional[Dict[str, Any]]):
    token = _CURRENT_AUTHORITY.set(authority if authority else None)
    try:
        yield
    finally:
        _CURRENT_AUTHORITY.reset(token)


def _has_gateway_route(session_id: str) -> bool:
    db = _db()
    if db is None:
        return False
    row = db.get_session(session_id)
    if not row:
        return False
    source = str(row.get("source") or "").strip().lower()
    chat_id = row.get("chat_id")
    if not source or chat_id in (None, ""):
        return False
    try:
        from gateway.config import Platform
        platform = Platform(source)
    except (TypeError, ValueError):
        return False
    return platform.value != "local"


def request_control(kind: str, action: str, target_sid: str, *, requester_sid: str,
                    reason: str = "", payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Create a durable, 24-hour pending approval request."""
    if (kind, action) not in CONTROLS:
        raise ValueError("unsupported_control")
    target_sid = resolve_target(target_sid)
    if not _has_gateway_route(target_sid):
        return {"ok": False, "error_code": "target_unroutable", "error": "target_unroutable"}
    now = _now()
    record = _new_record(kind, action, target_sid, requester_sid, reason=reason, payload=payload,
                         authority={"via": "button", "user_id": None}, status="pending", now=now,
                         expires_at=now + _REQUEST_TTL_SECONDS)
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
        if not _has_gateway_route(target_sid):
            raise ValueError("target_unroutable")
    except ValueError as exc:
        return {"ok": False, "error_code": str(exc), "error": str(exc)}
    quote_check = check_user_quote(requester_sid, user_quote)
    if isinstance(quote_check, str):
        code = quote_check
        if user_quote:
            return {"ok": False, "error_code": code, "error": code}
        try:
            record = request_control(kind, action, target_sid, requester_sid=requester_sid,
                                     reason=reason, payload=payload)
        except ValueError as exc:
            return {"ok": False, "error_code": str(exc), "error": str(exc)}
        if not record.get("id"):
            return record
        return {"ok": True, "status": "pending", "request_id": record["id"], "record": record}
    quote, message = quote_check
    authority = {"via": "quote", "quote": quote, "message": message}
    try:
        affected_before = _affected_text(kind, action, target_sid, payload)
        result = _manager_apply(kind, action, target_sid, reason=reason, payload=payload, authority=authority)
        manager_result = result.get("result")
        if isinstance(manager_result, dict) and not manager_result.get("ok", True):
            code = manager_result.get("error_code") or manager_result.get("error") or "apply_failed"
            if code.startswith("nothing_to_"):
                now = _now()
                record = _new_record(kind, action, target_sid, requester_sid, reason=reason, payload=payload,
                                     authority=authority, status="failed", now=now, expires_at=None)
                record["affected_text"] = affected_before
                record["resolved_at"], record["error"] = now, code
                _save_record(record)
                return {"ok": False, "status": "failed", "error_code": code, "error": code,
                        "record": record, **result}
            raise ValueError(code)
        now = _now()
        record = _new_record(kind, action, target_sid, requester_sid, reason=reason, payload=payload,
                             authority=authority, status="applied", now=now, expires_at=None)
        record["affected_text"] = affected_before
        record["resolved_at"] = now
        if result.get("continuation_prompt"):
            record["continuation_prompt"] = result["continuation_prompt"]
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
        manager_result = result.get("result")
        if isinstance(manager_result, dict) and not manager_result.get("ok", True):
            raise ValueError(manager_result.get("error_code") or manager_result.get("error") or "apply_failed")
        if result.get("continuation_prompt"):
            claimed["continuation_prompt"] = result["continuation_prompt"]
        claimed["status"] = "applied"
        claimed["error"] = None
    except Exception as exc:
        claimed["status"] = "failed"
        claimed["error"] = str(exc)
    _save_record(claimed)
    return claimed


def fail_request(request_id: str, error: str) -> Optional[Dict[str, Any]]:
    """CAS a pending request to failed so an unroutable target can notify its requester."""
    db = _db()
    if db is None:
        return None
    key, now = _record_key(request_id), _now()

    def _fail(conn):
        row = conn.execute("SELECT value FROM state_meta WHERE key = ?", (key,)).fetchone()
        if row is None:
            return None
        try:
            record = json.loads(row[0])
        except (TypeError, ValueError):
            return None
        if record.get("status") != "pending":
            return None
        record["status"], record["error"], record["resolved_at"] = "failed", error, now
        db.set_meta(key, json.dumps(record, ensure_ascii=False), cursor=conn)
        return record

    return db._execute_write(_fail)


def expire_request(request_id: str) -> Optional[Dict[str, Any]]:
    """Compare-and-set one pending request to expired inside its write transaction."""
    db = _db()
    if db is None:
        return None
    key, now = _record_key(request_id), _now()

    def _expire(conn):
        row = conn.execute("SELECT value FROM state_meta WHERE key = ?", (key,)).fetchone()
        if row is None:
            return None
        try:
            record = json.loads(row[0])
        except (TypeError, ValueError):
            return None
        if record.get("status") != "pending" or float(record.get("expires_at") or 0) > now:
            return None
        record["status"], record["resolved_at"] = "expired", now
        db.set_meta(key, json.dumps(record, ensure_ascii=False), cursor=conn)
        return record

    return db._execute_write(_expire)


def _recover_interrupted() -> None:
    db = _db()
    if db is None:
        return
    cutoff = _now() - 10 * 60

    def _recover(conn):
        rows = conn.execute(
            "SELECT key, value FROM state_meta WHERE key LIKE ?", (_CONTROL_PREFIX + "%",)
        ).fetchall()
        for key, raw in rows:
            try:
                record = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if record.get("status") == "applying" and float(record.get("resolved_at") or 0) <= cutoff:
                record["status"], record["error"] = "failed", "interrupted"
                db.set_meta(key, json.dumps(record, ensure_ascii=False), cursor=conn)

    db._execute_write(_recover)


def pending_outbox() -> list[Dict[str, Any]]:
    db = _db()
    if db is None:
        return []
    _recover_interrupted()
    out = []
    now = _now()
    for _key, raw in db.list_meta_prefix(_CONTROL_PREFIX):
        try:
            record = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if (record.get("status") in {"applied", "denied", "failed", "expired"}
                and not record.get("outbox_done")
                and record.get("resolved_at")
                and now - float(record["resolved_at"]) >= _REQUEST_TTL_SECONDS):
            mark_outbox(record.get("id", ""), "outbox_done")
            continue
        if not record.get("outbox_done") and (
                record.get("status") == "pending"
                or record.get("status") in {"applied", "denied", "failed", "expired"}):
            out.append(record)
    return out


def mark_outbox(request_id: str, flag: str, value: Any = True) -> Optional[Dict[str, Any]]:
    """Atomically set an outbox delivery flag, preserving concurrent flags."""
    db = _db()
    if db is None:
        return None
    key = _record_key(request_id)

    def _mark(conn):
        row = conn.execute("SELECT value FROM state_meta WHERE key = ?", (key,)).fetchone()
        if row is None:
            return None
        try:
            record = json.loads(row[0])
        except (TypeError, ValueError):
            return None
        record[flag] = value
        db.set_meta(key, json.dumps(record, ensure_ascii=False), cursor=conn)
        return record

    return db._execute_write(_mark)


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
    "resolve_request", "fail_request", "expire_request", "pending_outbox", "mark_outbox",
]
