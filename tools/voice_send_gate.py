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


def _first_non_option(tokens: list[str], start: int) -> Optional[int]:
    """Find the next command token, ignoring option switches but not their values.

    CLI options before a subcommand are common (for example ``slack --account X
    message send``). Once the known prefix is matched, only the first non-option
    token may be the verb; later positional arguments must not turn a read-only
    command into a send.
    """
    index = start
    while index < len(tokens):
        token = tokens[index]
        if not token.startswith("-"):
            return index
        # Options in the CLI/prefix portion generally consume one value unless
        # written as --name=value. This keeps ``--account lpg message`` from
        # treating ``lpg`` as the subcommand.
        if "=" not in token and index + 1 < len(tokens) and not tokens[index + 1].startswith("-"):
            index += 2
        else:
            index += 1
    return None


def _find_send_in_segment(tokens: list[str]) -> tuple[bool, Optional[str], Optional[str]]:
    """Match only a CLI's command word and its first subcommand verb."""
    command_index = _command_word_index(tokens)
    if command_index >= len(tokens):
        return False, None, None
    cli = Path(tokens[command_index]).name.lower()
    table = _SEND_TABLE.get(cli)
    if not table:
        return False, None, None
    tail = [part.lower() for part in tokens[command_index + 1:]]
    # CLI help is explicitly read-only, even when invoked on a send verb.
    if "--help" in tail or "-h" in tail:
        return False, None, None
    for prefix, verbs in table.items():
        prefix_index = _first_non_option(tokens, command_index + 1)
        if not prefix:
            verb_index = prefix_index
        else:
            prefix_lower = tuple(part.lower() for part in prefix)
            if prefix_index is None or tuple(
                part.lower() for part in tokens[prefix_index:prefix_index + len(prefix_lower)]
            ) != prefix_lower:
                continue
            verb_index = _first_non_option(tokens, prefix_index + len(prefix_lower))
        if verb_index is not None and tokens[verb_index].lower() in verbs:
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


def _source_send_match(content: str) -> tuple[bool, Optional[str]]:
    """Bounded source-text fallback for executed non-shell code."""
    match = re.search(
        r"(?i)(?:^|[\"'\[,(\s])"
        r"(slack|imsg|gog|wacli|quo|telegram)"
        r"(?:[\"'\],)\s]+|\s+)"
        r".{0,240}?\b(send|post|reply|edit)\b",
        content,
    )
    return (bool(match), match.group(1).lower() if match else None)


def _inline_interpreter_sources(tokens: list[str]) -> list[tuple[str, str]]:
    """Return ``(interpreter, source)`` for inline interpreter invocations."""
    sources: list[tuple[str, str]] = []
    for segment in _segments(tokens):
        index = _command_word_index(segment)
        if index >= len(segment):
            continue
        command = Path(segment[index]).name.lower()
        if not _is_interpreter_name(command):
            continue
        arg = index + 1
        while arg < len(segment):
            token = segment[arg]
            if token == "--":
                arg += 1
                break
            if command == "deno" and token.lower() == "eval":
                if arg + 1 < len(segment):
                    sources.append((command, segment[arg + 1]))
                break
            if command == "node" and token in {"-e", "--eval"}:
                if arg + 1 < len(segment):
                    sources.append((command, segment[arg + 1]))
                break
            if command in {"python", "python2", "python3", "ruby", "perl", "bun"} or re.fullmatch(
                r"python\d+(?:\.\d+)+", command
            ):
                if token in {"-c", "-e"}:
                    if arg + 1 < len(segment):
                        sources.append((command, segment[arg + 1]))
                    break
            if token.startswith("-"):
                arg += 1
                continue
            # A non-option script/module argument means this is not inline code.
            break
    return sources


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

        for source_command, source in _inline_interpreter_sources(segment):
            found, cli = _source_send_match(source)
            if found:
                return True, cli, _account_from_tokens(segment)

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
                found, cli = _source_send_match(content)
                if found:
                    found, account = True, _account_from_tokens(segment)
                    cli = cli or ""
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


def _consumed_timestamps(row: Optional[dict]) -> list[float]:
    """Normalize current list-based and legacy scalar grant records."""
    if not isinstance(row, dict):
        return []
    value = row.get("consumed_at")
    values = value if isinstance(value, list) else ([value] if value else [])
    result = []
    for timestamp in values:
        try:
            result.append(float(timestamp))
        except (TypeError, ValueError):
            continue
    return result


def consumed_grant_timestamps(db: Any, session_id: str, action_hash: str) -> list[float]:
    return _consumed_timestamps(_read_grant(db, session_id, action_hash))


def has_consumed_grant(db: Any, session_id: str, action_hash: str) -> bool:
    return bool(consumed_grant_timestamps(db, session_id, action_hash))


def _append_consumed_at(record: dict) -> None:
    record["consumed_at"] = _consumed_timestamps(record) + [time.time()]


def _transactional_grant_update(db: Any, key: str, update) -> Optional[bool]:
    """Apply a grant update under the SessionDB write transaction.

    ``SessionDB._execute_write`` takes BEGIN IMMEDIATE, so the read, decision,
    and write are one atomic operation across concurrent callers and processes.
    The small fallback keeps lightweight test doubles and older embedding
    callers working; production SessionDB instances always take the durable
    transaction path.
    """
    execute_write = getattr(db, "_execute_write", None)
    if not callable(execute_write):
        return None

    def _apply(conn):
        row = conn.execute("SELECT value FROM state_meta WHERE key = ?", (key,)).fetchone()
        current = None
        if row is not None and row[0]:
            try:
                current = json.loads(row[0])
            except (TypeError, ValueError):
                current = None
        replacement = update(current)
        if replacement is None:
            return False
        encoded = json.dumps(replacement, sort_keys=True)
        if row is None:
            conn.execute("INSERT INTO state_meta (key, value) VALUES (?, ?)", (key, encoded))
        else:
            conn.execute("UPDATE state_meta SET value = ? WHERE key = ?", (encoded, key))
        return True

    try:
        return bool(execute_write(_apply))
    except Exception:
        # A real transactional store was present but could not complete. Fail
        # closed; never fall back to a non-atomic read/write after contention.
        return False


def _consume_existing_grant(db: Any, session_id: str, action_hash: str) -> Optional[bool]:
    key = _meta_key(session_id, action_hash)

    def _consume(record):
        if not isinstance(record, dict) or not record.get("approved_at"):
            return None
        if _consumed_timestamps(record):
            return None
        updated = dict(record)
        _append_consumed_at(updated)
        return updated

    result = _transactional_grant_update(db, key, _consume)
    if result is not None:
        return result
    existing = _read_grant(db, session_id, action_hash)
    if existing and existing.get("approved_at") and not _consumed_timestamps(existing):
        updated = dict(existing)
        _append_consumed_at(updated)
        if not _write_grant(db, session_id, action_hash, updated):
            return None
        return True
    return False


def _store_consumed_grant(db: Any, session_id: str, action_hash: str, record: dict) -> Optional[bool]:
    key = _meta_key(session_id, action_hash)

    def _store(existing):
        # Two concurrent approvals for the same exact action must not both
        # replace the record after one has already claimed it.
        if isinstance(existing, dict) and _consumed_timestamps(existing):
            return None
        return record

    result = _transactional_grant_update(db, key, _store)
    if result is not None:
        return result
    if not _write_grant(db, session_id, action_hash, record):
        return None
    return True


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
    if existing and existing.get("approved_at") and not _consumed_timestamps(existing):
        consumed = _consume_existing_grant(db, session_id, action_hash)
        if consumed is True:
            return None
        if consumed is None:
            return _block(action_hash, "grant store unavailable")
        # Another concurrent caller claimed this one-shot grant. Do not open a
        # second approval flow from the losing invocation.
        return _block(action_hash, "grant already consumed")

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
        "consumed_at": [],
    }
    _append_consumed_at(record)
    stored = _store_consumed_grant(db, session_id, action_hash, record)
    if stored is None:
        return _block(action_hash, "grant store unavailable")
    if stored is False:
        return _block(action_hash, "grant already consumed")
    return None


__all__ = ["authorize_voice_send", "classify_voice_send", "consumed_grant_timestamps", "has_consumed_grant"]
