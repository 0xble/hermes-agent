"""Typed, read-only Hermes session state inspection for agents."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from gateway.session_context import bound_session_env, get_session_env
from hermes_constants import get_hermes_home, get_process_hermes_home, profile_name_for_home
from hermes_state import SessionDB
from tools.registry import registry, tool_error

_MAX_ROWS = 50
_MAX_BYTES = 64 * 1024
_MAX_FIELD_CHARS = 512
_MAX_ERROR_CHARS = 4096
_MAX_CONTENT_CHARS = 8192
_ACTIONS = ("resolve_topic", "inspect_session", "inspect_delivery")
_SQL_SHAPED = re.compile(
    r"(?:\bselect\b|\binsert\b|\bupdate\b|\bdelete\b|\bdrop\b|\bpragma\b|--|/\*|\*/|;|\bunion\b|\bor\s+1\s*=)",
    re.I,
)

SESSION_STATE_SCHEMA = {
    "name": "session_state",
    "description": (
        "Read-only typed inspection of Hermes session state. Use resolve_topic to map a Telegram DM "
        "chat and thread to its session, inspect_session for bounded session metadata and lineage, "
        "and inspect_delivery for bounded delivery status. This is for durable Hermes state that "
        "session_search does not expose. It never accepts SQL or arbitrary selectors, never returns "
        "delivery content unless include_content=true, and never creates or migrates state.db."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": list(_ACTIONS)},
            "chat_id": {"type": "string", "description": "Telegram DM chat id for resolve_topic."},
            "thread_id": {"type": "string", "description": "Telegram topic thread id for resolve_topic."},
            "session_id": {"type": "string", "description": "Exact Hermes session id for inspect_session or inspect_delivery."},
            "session_key": {"type": "string", "description": "Exact Hermes delivery session key for inspect_delivery."},
            "obligation_id": {"type": "string", "description": "Exact delivery obligation id for inspect_delivery."},
            "include_content": {"type": "boolean", "description": "Include capped delivery content when true."},
        },
        "required": ["action"],
        "additionalProperties": False,
    },
}


def _profile() -> str:
    """Resolve the served profile from the bound scope or the active state path."""
    value = bound_session_env("HERMES_SESSION_PROFILE", "")
    if (profile := str(value or "").strip()):
        return profile
    return profile_name_for_home(get_hermes_home()) or "default"


def _db_path() -> Path:
    return get_hermes_home() / "state.db"


def _delivery_db_path() -> Path:
    """Return the process-home ledger path, shared by multiplexed profiles."""
    return get_process_hermes_home() / "state.db"


def _selector(value: Any) -> str:
    value = str(value or "").strip()
    if _SQL_SHAPED.search(value):
        raise ValueError("SQL-shaped selectors are not accepted")
    return value


def _clip(value: Any, limit: int = _MAX_FIELD_CHARS) -> Any:
    if value is None or not isinstance(value, str):
        return value
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _empty(action: str, reason: str = "state_db_absent") -> dict[str, Any]:
    return {"ok": True, "action": action, "found": False, "reason": reason}


def _bounded(value: Any) -> Any:
    """Keep output under the fixed byte cap without emitting partial JSON."""
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode("utf-8")) <= _MAX_BYTES:
        return value
    if isinstance(value, dict):
        reduced = dict(value)
        reduced["truncated"] = True
        rows = reduced.get("rows")
        while isinstance(rows, list) and len(rows) > 1:
            rows = rows[: len(rows) // 2]
            reduced["rows"] = rows
            encoded = json.dumps(reduced, ensure_ascii=False, separators=(",", ":"))
            if len(encoded.encode("utf-8")) <= _MAX_BYTES:
                return reduced
    return {"ok": False, "error": "result exceeds fixed byte cap", "truncated": True}


def _table_columns(db: SessionDB) -> set[str]:
    """Read the delivery table shape without invoking any schema repair."""
    rows = db._read_all("PRAGMA table_info(delivery_obligations)", ())
    return {str(row[1]) for row in rows}


def _session_for_profile(db: SessionDB, session_id: str, profile: str) -> dict[str, Any] | None:
    row = db.get_session(session_id)
    if not row:
        return None
    row_profile = str(row.get("profile_name") or "").strip()
    if not row_profile:
        # Legacy rows belong to their store, not the caller's bound profile.
        row_profile = profile_name_for_home(Path(db.db_path).parent) or "default"
    return row if row_profile == profile else None


def _inspect_session(db: SessionDB, session_id: str, profile: str) -> dict[str, Any]:
    row = _session_for_profile(db, session_id, profile)
    if not row:
        return _empty("inspect_session", "session_not_found")
    fields = (
        "id", "source", "profile_name", "session_key", "title", "started_at", "last_activity_at",
        "ended_at", "end_reason", "message_count", "thread_id", "chat_id", "model", "parent_session_id",
    )
    session = {key: _clip(row.get(key)) for key in fields}
    session["profile_name"] = _clip(str(row.get("profile_name") or "").strip() or profile)
    session["archived"] = bool(row.get("archived"))
    if "auto_archived" in row:
        session["auto_archived"] = bool(row["auto_archived"])
    return {"ok": True, "action": "inspect_session", "found": True, "session": session}


def _inspect_session_action(db: SessionDB, args: dict[str, Any], profile: str) -> dict[str, Any]:
    session_id = _selector(args.get("session_id"))
    if not session_id:
        raise ValueError("session_id is required for inspect_session")
    return _inspect_session(db, session_id, profile)


def _delivery_selector(db: SessionDB, args: dict[str, Any], profile: str) -> tuple[str, str] | None:
    selectors = [
        ("obligation_id", _selector(args.get("obligation_id"))),
        ("session_key", _selector(args.get("session_key"))),
        ("session_id", _selector(args.get("session_id"))),
    ]
    present = [(name, value) for name, value in selectors if value]
    if len(present) > 1:
        raise ValueError("inspect_delivery accepts exactly one of obligation_id, session_key, or session_id")
    if not present:
        raise ValueError("obligation_id, session_key, or session_id is required")
    name, value = present[0]
    if name == "session_id":
        row = _session_for_profile(db, value, profile)
        if not row:
            return None
        value = _selector(row.get("session_key"))
        if not value:
            return None
        name = "session_key"
    return name, value


def _inspect_delivery(
    profile_db: SessionDB, delivery_db: SessionDB, args: dict[str, Any], profile: str
) -> dict[str, Any]:
    """Resolve session selectors in the served profile, then read the process-home ledger."""
    columns = _table_columns(delivery_db)
    required = {
        "obligation_id", "session_key", "platform", "chat_id", "thread_id", "state", "attempts",
        "created_at", "updated_at", "owner_pid", "last_error", "adapter_profile",
    }
    if not required.issubset(columns):
        return _empty("inspect_delivery", "optional_table_absent" if not columns else "optional_table_unsupported")
    selected = [
        "obligation_id", "session_key", "platform", "chat_id", "thread_id", "state", "attempts",
        "created_at", "updated_at", "owner_pid", "last_error", "adapter_profile",
    ]
    optional = ("resume_marker_session_id", "resume_marker_token", "resume_marker_marked_at", "resume_turn_id")
    selected.extend(column for column in optional if column in columns)
    include_content = bool(args.get("include_content")) and "content" in columns
    if include_content:
        selected.append("content")
    selector = _delivery_selector(profile_db, args, profile)
    if selector is None:
        return _empty("inspect_delivery", "session_not_found")
    selector_name, selector_value = selector
    # The ledger's legacy NULL owner is the default adapter, as is a blank stamp.
    profile_clause = " AND COALESCE(NULLIF(TRIM(adapter_profile), ''), 'default') = ?"
    params: list[Any] = [selector_value, profile, _MAX_ROWS]
    rows = delivery_db._read_all(
        "SELECT " + ", ".join(selected) + " FROM delivery_obligations WHERE " + selector_name + " = ?"
        + profile_clause + " ORDER BY updated_at DESC, obligation_id DESC LIMIT ?",
        params,
    )
    if not rows:
        return _empty("inspect_delivery", "delivery_not_found")
    safe_rows = []
    limits = {"last_error": _MAX_ERROR_CHARS, "content": _MAX_CONTENT_CHARS}
    for row in rows:
        safe = {column: _clip(row[column], limits.get(column, _MAX_FIELD_CHARS)) for column in selected}
        safe_rows.append(safe)
    return {"ok": True, "action": "inspect_delivery", "found": True, "rows": safe_rows}


def _resolve_topic(db: SessionDB, args: dict[str, Any], profile: str) -> dict[str, Any]:
    topic_columns = _table_columns_for_topic(db)
    if not topic_columns:
        return _empty("resolve_topic", "optional_table_absent")
    if not {"profile_name", "chat_id", "thread_id", "session_key", "session_id"}.issubset(topic_columns):
        return _empty("resolve_topic", "optional_table_unsupported")
    chat_id = _selector(args.get("chat_id") or get_session_env("HERMES_SESSION_CHAT_ID"))
    thread_id = _selector(args.get("thread_id") or get_session_env("HERMES_SESSION_THREAD_ID"))
    if not chat_id or not thread_id:
        raise ValueError("chat_id and thread_id are required for resolve_topic")
    binding = db.get_telegram_topic_binding(chat_id=chat_id, thread_id=thread_id, profile_name=profile)
    if not binding:
        return _empty("resolve_topic", "topic_not_found")
    fields = ("profile_name", "chat_id", "thread_id", "session_key", "session_id", "managed_mode", "linked_at", "updated_at")
    return {
        "ok": True,
        "action": "resolve_topic",
        "found": True,
        "binding": {key: _clip(binding.get(key)) for key in fields},
    }


def _table_columns_for_topic(db: SessionDB) -> set[str]:
    """Probe topic bindings without calling their migration-on-read compatibility path."""
    rows = db._read_all("PRAGMA table_info(telegram_dm_topic_bindings)", ())
    return {str(row[1]) for row in rows}


_ACTION_HANDLERS = {
    "resolve_topic": _resolve_topic,
    "inspect_session": _inspect_session_action,
}


def session_state_tool(args: dict, *, profile: str | None = None, **_kwargs: Any) -> str:
    action = str(args.get("action") or "").strip().lower()
    if action not in _ACTIONS:
        return tool_error("action must be resolve_topic, inspect_session, or inspect_delivery")
    try:
        for key in ("chat_id", "thread_id", "session_id", "session_key", "obligation_id"):
            if key in args:
                _selector(args.get(key))
    except ValueError as exc:
        return tool_error(str(exc))
    if action == "inspect_delivery":
        selectors = tuple(
            key for key in ("obligation_id", "session_key", "session_id") if str(args.get(key) or "").strip()
        )
        if len(selectors) > 1:
            return tool_error("inspect_delivery accepts exactly one of obligation_id, session_key, or session_id")
    # Native calls keep the bound turn scope. A standalone CLI supplies its selected home owner.
    profile = _profile() if profile is None else profile
    profile_path = _db_path()
    delivery_path = _delivery_db_path() if action == "inspect_delivery" else profile_path
    if not delivery_path.is_file():
        return json.dumps(_empty(action), separators=(",", ":"))
    opened: list[SessionDB] = []
    try:
        delivery_db = SessionDB(db_path=delivery_path, read_only=True)
        opened.append(delivery_db)
        if action != "inspect_delivery":
            result = _ACTION_HANDLERS[action](delivery_db, args, profile)
        elif profile_path.resolve() == delivery_path.resolve():
            result = _inspect_delivery(delivery_db, delivery_db, args, profile)
        elif str(args.get("session_id") or "").strip() and not profile_path.is_file():
            result = _empty(action, "session_not_found")
        else:
            profile_db = delivery_db
            if str(args.get("session_id") or "").strip():
                profile_db = SessionDB(db_path=profile_path, read_only=True)
                opened.append(profile_db)
            result = _inspect_delivery(profile_db, delivery_db, args, profile)
        return json.dumps(_bounded(result), ensure_ascii=False, separators=(",", ":"))
    except ValueError as exc:
        return tool_error(str(exc))
    except Exception as exc:
        return json.dumps({"ok": False, "action": action, "error": f"state read failed: {type(exc).__name__}"})
    finally:
        for db in opened:
            db.close()


registry.register(
    name="session_state", toolset="session_state", schema=SESSION_STATE_SCHEMA,
    handler=session_state_tool, emoji="▣",
)
