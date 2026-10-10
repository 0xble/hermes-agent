"""Cross-session goal and loop controls with durable approval records."""
from __future__ import annotations

import hashlib
import json
import logging
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, Dict, Optional, Tuple

from hermes_cli.goals import (
    _REPLY_QUOTE_RE,
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


# Deterministic negation guard: a quoted span preceded (within three words) by, or containing, one
# of these tokens is refused so "do NOT clear the goal" cannot authorize a clear. "stop" is not a
# negation: it is itself a control verb.
_NEGATION_TOKENS = frozenset({"not", "don't", "dont", "never", "no", "shouldn't", "shouldnt",
                              "can't", "cant", "cannot", "won't", "wont", "didn't", "didnt", "doesn't"})
_NEGATION_WINDOW_WORDS = 3


def _negation_words(text: str) -> list[str]:
    return [w.replace("\u2019", "'").strip(".,;:!?\"'()[]").lower() for w in text.split()]


def _is_negated(quote: str, source: str) -> bool:
    """True when the quote contains a negation or one appears within 3 words before it."""
    def negates(words: list[str]) -> bool:
        return any(w in _NEGATION_TOKENS for w in words)

    if negates(_negation_words(quote)):
        return True
    start = 0
    while True:
        idx = source.find(quote, start)
        if idx < 0:
            return False
        prefix = source[:idx]
        if idx and not source[idx - 1].isspace():
            # The quote starts mid-word ("t clear ..." inside "don't clear ..."): judge the whole word.
            prefix += quote.split(" ", 1)[0]
        if negates(_negation_words(prefix)[-_NEGATION_WINDOW_WORDS:]):
            return True
        start = idx + 1


def check_user_quote(requester_session_id: str, quote: str, *, cursor=None) -> Tuple[str, str] | str:
    """Validate a quote against only the requester's latest typed user message."""
    normalized = _normalize(quote)
    if len(normalized) < _REVISION_QUOTE_MIN_CHARS:
        return "user_quote_too_short"
    db = _db()
    if db is None:
        return "user_quote_not_found"
    try:
        rows = db.messages_by_role(requester_session_id, "user", since=0.0, limit=500, cursor=cursor)
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
    # The gateway reply pointer repeats the assistant's words; only the user's own text authorizes.
    source = _normalize(_REPLY_QUOTE_RE.sub("", source_raw, count=1))
    if len(source) > _REVISION_SOURCE_MAX_CHARS:
        return "user_message_too_long"
    if normalized not in source:
        return "user_quote_not_found"
    if _is_negated(normalized, source):
        return "user_quote_negated"
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
                   authority: Optional[Dict[str, Any]] = None, cursor=None) -> Dict[str, Any]:
    payload = dict(payload or {})
    handler = CONTROLS.get((kind, action))
    if action == "replace" and kind == "goal":
        from hermes_cli.goals import GoalManager
        manager = GoalManager(target_sid, cursor=cursor)
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
        manager = GoalManager(target_sid, cursor=cursor)
        # Only a paused goal resumes: never revive a done/cleared goal or re-arm an active one.
        if manager.state is None or manager.state.status != "paused":
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
        before_state = load_goal(target_sid, cursor=cursor)
        # A cleared goal stays stored; pausing or clearing it again must not revive or re-announce it.
        if before_state is None or getattr(before_state, "status", None) not in {"active", "paused"}:
            code = f"nothing_to_{action}"
            return {"result": {"ok": False, "error_code": code, "error": code}, "state": before_state}
    result = handler(target_sid, reason, payload, authority, cursor)
    if kind == "goal":
        from hermes_cli.goals import load_goal
        state = load_goal(target_sid, cursor=cursor)
    elif kind == "loop":
        from hermes_cli.loops import load_loop
        state = load_loop(target_sid, cursor=cursor)
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


def _session_title(session_id: str, *, cursor=None) -> str:
    db = _db()
    if db is None:
        return ""
    try:
        if cursor is not None:
            row = cursor.execute("SELECT title FROM sessions WHERE id = ?", (session_id,)).fetchone()
            return str(row[0] or "") if row else ""
        return str(db.get_session_title(session_id) or "")
    except Exception:
        return ""


def _affected_text(kind: str, action: str, raw: Optional[str], payload: Optional[Dict[str, Any]] = None) -> str:
    payload = payload or {}
    try:
        if kind == "goal":
            from hermes_cli.goals import GoalState
            state = GoalState.from_json(raw) if raw else None
            old = str(getattr(state, "goal", "") or "")
            if action == "replace":
                return f"goal: {old or '(none)'} -> {str(payload.get('goal') or '')}"
            return f"goal: {old or '(none)'}"
        if kind == "loop":
            from hermes_cli.loops import LoopState
            state = LoopState.from_json(raw) if raw else None
            prompt = str(getattr(state, "prompt", "") or "")
            cadence = state.cadence_label() if state is not None else ""
            return f"loop: {prompt or '(none)'}{f' ({cadence})' if cadence else ''}"
    except (TypeError, ValueError, AttributeError):
        logger.debug("could not describe session-control target", exc_info=True)
    return ""


def _definition_fingerprint(kind: str, raw: Optional[str]) -> str:
    """Bind the authored definition, not progress counters or scheduler bookkeeping.

    v2 also covers contracts/revisions and cadence/conditions. Old text-only cards
    cannot authorize a v2 definition and must be requested again.
    """
    if not raw or kind not in {"goal", "loop"}:
        return ""
    try:
        data = json.loads(raw)
        text_key = "goal" if kind == "goal" else "prompt"
        if not isinstance(data, dict) or not isinstance(data.get(text_key), str) or not data[text_key]:
            return ""
        fields = (
            ("goal", "created_at", "max_turns", "contract", "subgoals", "revisions") if kind == "goal"
            else ("prompt", "created_at", "mode", "interval_seconds", "times", "until", "max_ticks", "route", "revisions")
        )
        definition = {field: data.get(field) for field in fields}
        if kind == "goal":
            definition["gates"] = [
                {field: gate.get(field) for field in ("command", "timeout_seconds", "max_retries")}
                for gate in (data.get("gates") or [])
            ]
        encoded = json.dumps(definition, sort_keys=True, ensure_ascii=False, allow_nan=False)
        return "v2:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    except (TypeError, ValueError, AttributeError):
        return ""


def goal_continuation_is_current(metadata: Any, session_id: str = "", *, cursor=None) -> bool:
    """One fail-closed fence for every goal continuation, regardless of its producer."""
    if not isinstance(metadata, dict):
        return False
    sid = metadata.get("goal_continuation_session_id")
    expected = metadata.get("goal_continuation_fingerprint")
    if not isinstance(sid, str) or not sid or (session_id and sid != session_id):
        return False
    if not isinstance(expected, str) or not expected.startswith("v2:"):
        return False
    try:
        db = _db() if cursor is None else None
        raw = _meta_value(cursor, _definition_key("goal", sid)) if cursor is not None else (
            db.get_meta(_definition_key("goal", sid)) if db is not None else None
        )
        return bool(raw and json.loads(raw).get("status") == "active"
                    and expected == _definition_fingerprint("goal", raw))
    except Exception:
        logger.debug("goal continuation definition check failed", exc_info=True)
        return False


def _meta_value(cursor, key: str) -> Optional[str]:
    row = cursor.execute("SELECT value FROM state_meta WHERE key = ?", (key,)).fetchone()
    return row[0] if row else None

def _definition_key(kind: str, session_id: str) -> str:
    return f"{'goal' if kind == 'goal' else 'loop'}:{session_id}"


def _bind_continuation(record: Dict[str, Any], result: Dict[str, Any], cursor) -> None:
    if result.get("continuation_prompt"):
        record["continuation_prompt"] = result["continuation_prompt"]
        record["continuation_fingerprint"] = _definition_fingerprint(
            record["kind"], _meta_value(cursor, _definition_key(record["kind"], record["target_session_id"]))
        )

def _new_record(kind: str, action: str, target_sid: str, requester_sid: str, *,
                reason: str, payload: Optional[Dict[str, Any]], authority: Dict[str, Any],
                status: str, now: float, expires_at: Optional[float], affected_text: str, cursor=None) -> Dict[str, Any]:
    return {
        "id": uuid.uuid4().hex[:12], "kind": kind, "action": action,
        "target_session_id": target_sid, "requester_session_id": requester_sid,
        "requester_title": _session_title(requester_sid, cursor=cursor), "target_title": _session_title(target_sid, cursor=cursor),
        "reason": reason or "session-control", "payload": dict(payload or {}),
        "affected_text": affected_text,
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


# Platforms whose adapter renders Approve/Deny buttons for ``send_control_request``.
_APPROVABLE_SOURCES = frozenset({"telegram"})


def _session_source(session_id: str) -> str:
    db = _db()
    row = db.get_session(session_id) if db is not None else None
    return str((row or {}).get("source") or "").strip().lower()


def _has_approval_surface(session_id: str) -> bool:
    return _session_source(session_id) in _APPROVABLE_SOURCES


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
    if not _has_approval_surface(target_sid):
        return {"ok": False, "error_code": "target_unapprovable", "error": "target_unapprovable"}
    now = _now()
    db = _db()

    def create(conn):
        # Card description and binding come from one SQLite snapshot, never two manager loads.
        raw = _meta_value(conn, _definition_key(kind, target_sid))
        record = _new_record(kind, action, target_sid, requester_sid, reason=reason, payload=payload,
                             authority={"via": "button", "user_id": None}, status="pending", now=now,
                             expires_at=now + _REQUEST_TTL_SECONDS,
                             affected_text=_affected_text(kind, action, raw, payload), cursor=conn)
        record["target_fingerprint"] = _definition_fingerprint(kind, raw)
        _save_record(record, cursor=conn)
        return record

    return db._execute_write(create)


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
    def apply(conn):
        # Recheck after acquiring the write lock: a newer message must not inherit an older quote.
        checked = check_user_quote(requester_sid, user_quote, cursor=conn)
        if isinstance(checked, str):
            return {"ok": False, "error_code": checked, "error": checked}
        quote, message = checked
        authority = {"via": "quote", "quote": quote, "message": message}
        raw = _meta_value(conn, _definition_key(kind, target_sid))
        affected_before = _affected_text(kind, action, raw, payload)
        conn.execute("SAVEPOINT session_control_quote")
        result = _manager_apply(kind, action, target_sid, reason=reason, payload=payload,
                                authority=authority, cursor=conn)
        manager_result = result.get("result")
        code = ""
        if isinstance(manager_result, dict) and not manager_result.get("ok", True):
            code = manager_result.get("error_code") or manager_result.get("error") or "apply_failed"
            conn.execute("ROLLBACK TO session_control_quote")
        conn.execute("RELEASE session_control_quote")
        now = _now()
        record = _new_record(kind, action, target_sid, requester_sid, reason=reason, payload=payload,
                             authority=authority, status="failed" if code else "applied", now=now,
                             expires_at=None, affected_text=affected_before, cursor=conn)
        record["resolved_at"], record["error"] = now, code or None
        _bind_continuation(record, result, conn)
        _save_record(record, cursor=conn)
        return {"ok": not bool(code), "status": record["status"], "record": record,
                **({"error_code": code, "error": code} if code else {}), **result}

    try:
        return _db()._execute_write(apply)
    except Exception as exc:
        return {"ok": False, "error_code": "apply_failed", "error": str(exc)}

def resolve_request(request_id: str, decision: str, user_id: str) -> Optional[Dict[str, Any]]:
    """Validate, mutate through the shared manager, and settle in one write transaction."""
    if decision not in {"approve", "deny"}:
        return None
    db = _db()
    if db is None:
        return None
    key, now = _record_key(request_id), _now()

    def resolve(conn):
        raw = _meta_value(conn, key)
        try:
            record = json.loads(raw) if raw else None
        except (TypeError, ValueError):
            return None
        if not isinstance(record, dict) or record.get("status") != "pending":
            return None
        if float(record.get("expires_at") or 0) <= now:
            record["status"], record["resolved_at"] = "expired", now
            _save_record(record, cursor=conn)
            return None
        record["authority"] = {"via": "button", "user_id": str(user_id)}
        record["resolved_at"] = now
        if decision == "deny":
            record["status"] = "denied"
            _save_record(record, cursor=conn)
            return record

        expected = record.get("target_fingerprint")
        kind, action = record.get("kind"), record.get("action")
        live = _meta_value(conn, _definition_key(kind, record.get("target_session_id", "")))
        if ((kind, action) not in CONTROLS or not isinstance(expected, str)
                or not expected.startswith("v2:") or expected != _definition_fingerprint(kind, live)):
            record["status"], record["error"] = "failed", "target_changed"
            _save_record(record, cursor=conn)
            return record

        # Roll back even a partially saved manager action before persisting a failure receipt.
        conn.execute("SAVEPOINT session_control_apply")
        try:
            result = _manager_apply(kind, action, record["target_session_id"],
                                    reason=record.get("reason") or "session-control",
                                    payload=record.get("payload"), authority=record["authority"], cursor=conn)
            manager_result = result.get("result")
            if isinstance(manager_result, dict) and not manager_result.get("ok", True):
                raise ValueError(manager_result.get("error_code") or manager_result.get("error") or "apply_failed")
            _bind_continuation(record, result, conn)
            record["status"], record["error"] = "applied", None
        except Exception as exc:
            conn.execute("ROLLBACK TO session_control_apply")
            record["status"], record["error"] = "failed", str(exc)
        finally:
            conn.execute("RELEASE session_control_apply")
        _save_record(record, cursor=conn)
        return record

    return db._execute_write(resolve)


def continuation_is_current(request_id: str) -> bool:
    """Discard superseded outbox prompts, including legacy prompts without a definition binding."""
    db = _db()
    if db is None:
        return False

    def validate(conn):
        raw = _meta_value(conn, _record_key(request_id))
        try:
            record = json.loads(raw) if raw else None
        except (TypeError, ValueError):
            return False
        if (not isinstance(record, dict) or record.get("status") != "applied"
                or record.get("kind") != "goal" or record.get("action") not in {"resume", "replace"}):
            return False
        if record.get("continuation_discarded"):
            return False
        valid = goal_continuation_is_current({
            "goal_continuation_session_id": record["target_session_id"],
            "goal_continuation_fingerprint": record.get("continuation_fingerprint"),
        }, cursor=conn)
        if not valid:
            record["continuation_enqueued"] = True
            record["continuation_discarded"] = "target_changed"
            _save_record(record, cursor=conn)
        return bool(valid)

    return db._execute_write(validate)

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
    ("goal", "pause"): lambda sid, reason, payload, authority, cursor=None: __import__("hermes_cli.goals", fromlist=["GoalManager"]).GoalManager(sid, cursor=cursor).pause(reason=reason or "session-control"),
    ("goal", "resume"): lambda sid, reason, payload, authority, cursor=None: __import__("hermes_cli.goals", fromlist=["GoalManager"]).GoalManager(sid, cursor=cursor).resume(),
    ("goal", "clear"): lambda sid, reason, payload, authority, cursor=None: __import__("hermes_cli.goals", fromlist=["GoalManager"]).GoalManager(sid, cursor=cursor).clear(),
    ("loop", "pause"): lambda sid, reason, payload, authority, cursor=None: __import__("hermes_cli.loops", fromlist=["LoopManager"]).LoopManager(sid, cursor=cursor).pause(reason=reason or "session-control"),
    ("loop", "resume"): lambda sid, reason, payload, authority, cursor=None: __import__("hermes_cli.loops", fromlist=["LoopManager"]).LoopManager(sid, cursor=cursor).resume(),
    ("loop", "stop"): lambda sid, reason, payload, authority, cursor=None: __import__("hermes_cli.loops", fromlist=["LoopManager"]).LoopManager(sid, cursor=cursor).clear(),
    ("goal", "replace"): None,
}

__all__ = [
    "CONTROLS", "resolve_target", "check_user_quote", "apply_control", "request_control",
    "resolve_request", "fail_request", "expire_request", "pending_outbox", "mark_outbox", "continuation_is_current",
]
