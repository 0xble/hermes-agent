"""Deterministic approval floor for messages sent in Brian's voice.

This module intentionally does not use the normal approval bypasses. A send gets a
fresh, one-shot grant for its exact normalized command, and the grant is consumed
before dispatch. Read-only commands never enter this gate.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import time
from pathlib import Path
from typing import Any, Optional

from tools import approval


# Keep this as the one classifier table. A match requires the CLI name and a send verb.
_SEND_TABLE = {
    "slack": {
        ("message",): {"send", "post", "reply", "edit"},
    },
    "imsg": {
        (): {"send"},
    },
    "gog": {
        ("gmail",): {"send", "reply"},
        ("gmail", "drafts"): {"send"},
    },
    "wacli": {
        (): {"send"},
    },
    "quo": {
        (): {"send"},
        ("message",): {"send"},
    },
    "telegram": {
        (): {"send"},
    },
}
_NATIVE_SEND_TOOLS = {
    "mcp__telegram__send", "mcp__slack__send", "mcp__imsg__send",
    "mcp__quo__send", "telegram_send", "slack_send", "imsg_send", "quo_send",
}
_META_PREFIX = "voice_send_grant:"
_SHELL_NAMES = frozenset({"bash", "sh", "zsh", "dash", "ksh", "fish"})
_INTERPRETER_NAMES = _SHELL_NAMES | frozenset({
    "python", "python3", "python2", "node", "ruby", "perl", "deno", "bun", "uv", "uvx",
})
_SHELL_OPERATORS = frozenset({";", "&&", "||", "|", "(", ")"})
_MAX_SCRIPT_BYTES = 1_000_000


def _tokens(command: str) -> list[str]:
    """Tokenize shell syntax while retaining command separators for recursion."""
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|()")
        lexer.whitespace_split = True
        return list(lexer)
    except (TypeError, ValueError):
        return []


def _segments(tokens: list[str]) -> list[list[str]]:
    segments: list[list[str]] = [[]]
    for token in tokens:
        if token in _SHELL_OPERATORS:
            if segments[-1]:
                segments.append([])
        else:
            segments[-1].append(token)
    return [segment for segment in segments if segment]


def _is_assignment(token: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", token))


def _command_word_index(tokens: list[str]) -> int:
    """Return the command word after common launch prefixes."""
    index = 0
    while index < len(tokens):
        name = Path(tokens[index]).name.lower()
        if name == "env":
            index += 1
            while index < len(tokens) and (
                tokens[index].startswith("-") or _is_assignment(tokens[index])
            ):
                index += 1
            continue
        if name in {"nohup", "exec", "command", "time", "sudo"}:
            index += 1
            continue
        break
    return index


def _is_interpreter_name(name: str) -> bool:
    return name in _INTERPRETER_NAMES or bool(re.fullmatch(r"python\d+(?:\.\d+)+", name))


def _executed_file_targets(tokens: list[str]) -> list[tuple[str, bool]]:
    """Return files whose contents are actually executed by a shell segment.

    The boolean marks an invocation through a shell interpreter, where shell
    tokenization is the useful fallback and source-text matching is not used.
    """
    targets: list[tuple[str, bool]] = []
    for segment in _segments(tokens):
        index = _command_word_index(segment)
        if index >= len(segment):
            continue
        command = Path(segment[index]).name.lower()
        if _is_interpreter_name(command):
            arg = index + 1
            while arg < len(segment):
                token = segment[arg]
                if token == "--":
                    arg += 1
                    break
                if token == "-m" or token.startswith("-m"):
                    # Module execution (notably ``python -m pytest file.py``)
                    # does not execute the following path as a script.
                    arg = len(segment)
                    break
                if token.startswith("-"):
                    # ``-c``/``-e`` consume source text, not a script path.
                    if token in {"-c", "-e"} or (command in _SHELL_NAMES and "c" in token[1:]):
                        arg = len(segment)
                        break
                    arg += 1
                    continue
                if command == "uv" and token.lower() == "run":
                    arg += 1
                    continue
                if command == "uv" and token.lower() != "run":
                    arg = len(segment)
                    break
                targets.append((token, command in _SHELL_NAMES))
                break
            continue
        targets.append((segment[index], False))
    return targets


def _looks_like_shell_script(path: Path, content: str) -> bool:
    if path.suffix.lower() in {".sh", ".bash", ".zsh", ".dash", ".ksh", ".fish"}:
        return True
    first_line = content.lstrip().splitlines()[:1]
    return bool(first_line and re.search(r"#!.*\b(?:bash|sh|zsh|dash|ksh|fish)\b", first_line[0], re.I))


def _find_send_in_segment(tokens: list[str]) -> tuple[bool, Optional[str], Optional[str]]:
    for i, token in enumerate(tokens):
        cli = Path(token).name.lower()
        table = _SEND_TABLE.get(cli)
        if not table:
            continue
        tail = [part.lower() for part in tokens[i + 1:]]
        # CLI help is explicitly read-only, even when invoked on a send verb.
        if "--help" in tail or "-h" in tail:
            continue
        for prefix, verbs in table.items():
            if not prefix:
                if any(part in verbs for part in tail):
                    return True, cli, _account_from_tokens(tokens)
                continue
            prefix_lower = tuple(part.lower() for part in prefix)
            for start in range(len(tail) - len(prefix_lower) + 1):
                if tuple(tail[start:start + len(prefix_lower)]) != prefix_lower:
                    continue
                after = tail[start + len(prefix_lower):]
                if any(part in verbs for part in after):
                    return True, cli, _account_from_tokens(tokens)
    return False, None, None


def _account_from_tokens(tokens: list[str]) -> Optional[str]:
    for i, token in enumerate(tokens):
        lower = token.lower()
        if lower == "--account" and i + 1 < len(tokens):
            return tokens[i + 1]
        if lower.startswith("--account="):
            return token.split("=", 1)[1]
    return None


def _find_send(tokens: list[str], seen_paths: Optional[set[str]] = None) -> tuple[bool, Optional[str], Optional[str]]:
    """Find sends in shell segments, ``*-c`` payloads, and bounded script files."""
    seen_paths = set() if seen_paths is None else seen_paths
    for segment in _segments(tokens):
        found, cli, account = _find_send_in_segment(segment)
        if found:
            return True, cli, account

        # Recurse into sh/bash/zsh -c '...' (including bash -lc / zsh -ic).
        for i, token in enumerate(segment[:-1]):
            if Path(token).name.lower() not in _SHELL_NAMES:
                continue
            j = i + 1
            while j < len(segment) and segment[j].startswith("-") and segment[j] != "--":
                option = segment[j][1:]
                if "c" in option:
                    if j + 1 < len(segment):
                        found, cli, account = _find_send(_tokens(segment[j + 1]), seen_paths)
                        if found:
                            return True, cli, account or _account_from_tokens(segment)
                    break
                j += 1

        # Inspect only files that this segment would execute: the command word
        # itself, or the first script argument of an interpreter. Reading every
        # existing path token makes ordinary ``git diff``/``pytest`` commands
        # look like sends merely because their source contains send words.
        for token, invoked_by_shell in _executed_file_targets(segment):
            try:
                path = Path(token).expanduser()
                key = str(path.resolve())
                if key in seen_paths or not path.is_file() or path.stat().st_size > _MAX_SCRIPT_BYTES:
                    continue
                seen_paths.add(key)
                content = path.read_text(errors="replace")
            except (OSError, ValueError):
                continue
            found, cli, account = _find_send(_tokens(content), seen_paths)
            if not found and not invoked_by_shell and not _looks_like_shell_script(path, content):
                # Python/JSON-like source often keeps the command words inside
                # one quoted expression, which shell tokenization cannot see.
                # Inspect source text without executing or importing it, but only
                # for an actually executed non-shell script.
                match = re.search(
                    r"(?i)(?:^|[\"'\[,(\s])"
                    r"(slack|imsg|gog|wacli|quo|telegram)"
                    r"(?:[\"'\],)\s]+|\s+)"
                    r".{0,240}?\b(send|post|reply|edit)\b",
                    content,
                )
                if match:
                    found, cli, account = True, match.group(1).lower(), _account_from_tokens(segment)
            if found:
                return True, cli, account or _account_from_tokens(segment)
    return False, None, None


def _command_account(args: Any, token_account: Optional[str]) -> Optional[str]:
    if token_account:
        return token_account
    if isinstance(args, dict):
        for key in ("account", "profile"):
            value = args.get(key)
            if value is not None and str(value).strip():
                return str(value)
    return None


def classify_voice_send(tool_name: str, args: Any) -> Optional[dict[str, str]]:
    """Return normalized action metadata for a known send, else ``None``."""
    if tool_name in _NATIVE_SEND_TOOLS:
        normalized = json.dumps({"tool": tool_name, "args": args}, sort_keys=True, separators=(",", ":"), default=str)
        return {
            "command": normalized,
            "action_hash": hashlib.sha256(normalized.encode()).hexdigest(),
            "cli": tool_name,
            "account": _command_account(args, None) or "",
        }
    if tool_name not in {"terminal", "shell"} or not isinstance(args, dict):
        return None
    command = args.get("command") or args.get("cmd")
    if not isinstance(command, str):
        return None
    tokens = _tokens(command)
    is_send, cli, account = _find_send(tokens)
    if not is_send:
        return None
    normalized = " ".join(tokens)
    return {
        "command": normalized,
        "action_hash": hashlib.sha256(normalized.encode()).hexdigest(),
        "cli": cli or "",
        "account": _command_account(args, account) or "",
    }


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
