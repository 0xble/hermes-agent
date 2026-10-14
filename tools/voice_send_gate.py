"""Deterministic approval floor for messages sent in Brian's voice.

This module intentionally does not use the normal approval bypasses. A send gets a
fresh, one-shot grant for its exact normalized command, and the grant is consumed
before dispatch. Read-only commands never enter this gate.
"""
from __future__ import annotations

import hashlib
import json
import os
import shlex
import time
from pathlib import Path
from typing import Any, Optional

from tools import approval


# Keep this as the one classifier table. A match requires the CLI name and a send verb.
_SEND_TABLE = {
    "slack": {"message": {"send", "post", "reply"}},
    "imsg": {"": {"send"}},
    "gog": {"gmail": {"send", "reply"}},
    "wacli": {"": {"send"}},
    "quo": {"": {"send"}, "message": {"send"}},
}
_NATIVE_SEND_TOOLS = {
    "mcp__telegram__send", "mcp__slack__send", "mcp__imsg__send",
    "mcp__quo__send", "telegram_send", "slack_send", "imsg_send", "quo_send",
}
_META_PREFIX = "voice_send_grant:"


def _tokens(command: str) -> list[str]:
    try:
        return shlex.split(command, posix=True)
    except ValueError:
        return []


def _find_send(tokens: list[str]) -> bool:
    for i, token in enumerate(tokens):
        name = Path(token).name.lower()
        table = _SEND_TABLE.get(name)
        if not table:
            continue
        for prefix, verbs in table.items():
            if prefix:
                try:
                    p = tokens.index(prefix, i + 1)
                except ValueError:
                    continue
                tail = tokens[p + 1:]
            else:
                tail = tokens[i + 1:]
            if any(v.lower() in verbs for v in tail):
                return True
    return False


def _script_paths(tokens: list[str]) -> list[Path]:
    paths: list[Path] = []
    for i, token in enumerate(tokens):
        if token in {"bash", "sh", "zsh", "dash"} and i + 1 < len(tokens):
            candidate = tokens[i + 1]
            if not candidate.startswith("-"):
                paths.append(Path(candidate).expanduser())
    return paths


def classify_voice_send(tool_name: str, args: Any) -> Optional[dict[str, str]]:
    """Return ``command``/``action_hash`` for a known send, else ``None``."""
    if tool_name in _NATIVE_SEND_TOOLS:
        normalized = json.dumps({"tool": tool_name, "args": args}, sort_keys=True, separators=(",", ":"), default=str)
        return {"command": normalized, "action_hash": hashlib.sha256(normalized.encode()).hexdigest()}
    if tool_name not in {"terminal", "shell"} or not isinstance(args, dict):
        return None
    command = args.get("command") or args.get("cmd")
    if not isinstance(command, str):
        return None
    tokens = _tokens(command)
    is_send = _find_send(tokens)
    if not is_send:
        # Best-effort script inspection, deliberately bounded and read-only.
        for path in _script_paths(tokens):
            try:
                if path.is_file() and path.stat().st_size <= 1_000_000 and _find_send(_tokens(path.read_text(errors="replace"))):
                    is_send = True
                    break
            except OSError:
                continue
    if not is_send:
        return None
    normalized = " ".join(tokens)
    return {"command": normalized, "action_hash": hashlib.sha256(normalized.encode()).hexdigest()}


def _meta_key(session_id: str, action_hash: str) -> str:
    return f"{_META_PREFIX}{session_id}:{action_hash}"


def _read_grant(db: Any, session_id: str, action_hash: str) -> Optional[dict]:
    if db is None or not session_id:
        return None
    try:
        value = db.get_meta(_meta_key(session_id, action_hash))
        return json.loads(value) if value else None
    except Exception:
        return None


def _write_grant(db: Any, session_id: str, action_hash: str, record: dict) -> bool:
    if db is None or not session_id:
        return False
    try:
        db.set_meta(_meta_key(session_id, action_hash), json.dumps(record, sort_keys=True))
        return True
    except Exception:
        return False


def has_consumed_grant(db: Any, session_id: str, action_hash: str) -> bool:
    row = _read_grant(db, session_id, action_hash)
    return bool(row and row.get("consumed_at"))


def _block(action: str, reason: str) -> str:
    return f"BLOCKED: approval required before sending a message in Brian's voice ({reason}; action={action[:12]})."


def authorize_voice_send(agent: Any, tool_name: str, args: Any) -> Optional[str]:
    """Approve/consume one exact send. Return a block message or ``None``."""
    classified = classify_voice_send(tool_name, args)
    if classified is None:
        return None
    session_id = str(getattr(agent, "session_id", "") or "")
    db = getattr(agent, "_session_db", None)
    action_hash = classified["action_hash"]
    existing = _read_grant(db, session_id, action_hash)
    if existing and existing.get("approved_at") and not existing.get("consumed_at"):
        existing["consumed_at"] = time.time()
        if not _write_grant(db, session_id, action_hash, existing):
            return _block(action_hash, "grant store unavailable")
        return None

    # A send cannot proceed without a durable grant store, even if a prompt exists.
    if db is None or not session_id:
        return _block(action_hash, "no durable approval store")

    callback, is_cli, is_gateway, is_ask = approval._presence(None)
    if not (is_cli or is_gateway or is_ask):
        return _block(action_hash, "no human approval bridge")

    description = "Send this exact message in Brian's voice"
    result = approval._human_decision(
        approval._ACTION_GATE,
        command=classified["command"], description=description,
        pattern_key=f"voice_send:{action_hash}", pattern_keys=[f"voice_send:{action_hash}"],
        session_key=approval.get_current_session_key(), approval_callback=callback,
        is_cli=is_cli, is_gateway=is_gateway, is_ask=is_ask, one_shot=True,
    )
    if not result.get("approved"):
        return _block(action_hash, "not approved")
    record = {
        "action_hash": action_hash, "session_id": session_id,
        "approved_at": time.time(),
        "approver": os.getenv("HERMES_APPROVER_ID", "gateway-user"),
        "consumed_at": None,
    }
    if not _write_grant(db, session_id, action_hash, record):
        return _block(action_hash, "grant store unavailable")
    record["consumed_at"] = time.time()
    if not _write_grant(db, session_id, action_hash, record):
        return _block(action_hash, "grant consumption failed")
    return None


def evidence_grant_status(session_id: str, action_hash: str) -> bool:
    """Read the current session DB for goal-judge's deterministic preflight."""
    try:
        from hermes_cli.goals import _get_session_db
        return has_consumed_grant(_get_session_db(), session_id, action_hash)
    except Exception:
        return False


__all__ = ["authorize_voice_send", "classify_voice_send", "evidence_grant_status", "has_consumed_grant"]
