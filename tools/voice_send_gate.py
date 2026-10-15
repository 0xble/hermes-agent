"""Deterministic approval floor for messages sent in Brian's voice.

This module intentionally does not use the normal approval bypasses. A send gets a
fresh, one-shot grant for its exact raw command or code text, and the grant is
consumed before dispatch. Read-only commands never enter this gate.
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


# Keep this as the one send-verb knowledge table.  The safety decision below
# does not depend on this table; it is retained for action descriptions and
# compatibility with callers that inspect known send verbs.
_SEND_TABLE = {
    "slack": {
        ("message",): {"send", "post", "reply", "edit"},
    },
    "imsg": {
        (): {"send"},
    },
    "gog": {
        (): {"send"},
        ("gmail",): {"send", "reply", "forward", "fwd", "reply-all", "replyall"},
        ("gmail", "drafts"): {"send", "post"},
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
_SHELL_OPERATORS = frozenset({";", "&&", "||", "|", "(", ")", "&", "{", "}"})
# These are the documented *long* options that take a value before a CLI
# command/subcommand.  The sets were audited against each CLI's --help output;
# short options and --name=value forms are deliberately not value-taking here.
# Treating only known value-taking options as consumers prevents an omitted
# boolean flag table from hiding the send verb.
_VALUE_TAKING_OPTIONS = {
    "slack": frozenset({"--fields", "--account", "--timeout", "--route", "--stale-after"}),
    "imsg": frozenset(),
    "gog": frozenset({
        "--color", "--home", "--account", "--client", "--access-token", "--quota-project",
        "--enable-commands", "--enable-commands-exact", "--disable-commands", "--select",
    }),
    "wacli": frozenset({"--account", "--lock-wait", "--store", "--timeout"}),
    "quo": frozenset({"--fields", "--account", "--api-key", "--base-url", "--timeout", "--format", "--store"}),
    "telegram": frozenset({"--fields", "--account", "--timeout"}),
}
# Known non-value-taking long options are parsed deterministically.  An
# unrecognised long option is ambiguous, so it is not cleared as read-only.
_KNOWN_OPTIONS = {
    "slack": frozenset({
        "--help", "--json", "--agent", "--fields", "--yes", "--version", "--account", "--timeout",
        "--route", "--explain-route", "--fresh", "--offline", "--stale-after",
    }),
    "imsg": frozenset({"--help", "--json", "--agent", "--fields", "--yes", "--version"}),
    "gog": frozenset({
        "--help", "--color", "--home", "--account", "--client", "--access-token", "--quota-project",
        "--enable-commands", "--enable-commands-exact", "--disable-commands", "--gmail-no-send", "--readonly",
        "--json", "--plain", "--wrap-untrusted", "--results-only", "--select", "--dry-run", "--force",
        "--no-input", "--verbose", "--version",
    }),
    "wacli": frozenset({
        "--account", "--events", "--full", "--help", "--json", "--lock-wait", "--read-only", "--store",
        "--timeout", "--version",
    }),
    "quo": frozenset({
        "--help", "--json", "--agent", "--fields", "--yes", "--version", "--account", "--api-key",
        "--base-url", "--timeout", "--format", "--store", "--index", "--no-index",
    }),
    "telegram": frozenset({"--help", "--json", "--agent", "--fields", "--yes", "--version", "--account", "--timeout"}),
}
_MAX_SCRIPT_BYTES = 1_000_000


def _tokens(command: str) -> list[str]:
    """Tokenize shell syntax while retaining command separators for recursion."""
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|(){}")
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


def _first_non_option_indices(tokens: list[str], start: int, cli: Optional[str] = None) -> tuple[int, ...]:
    """Return every plausible next command-token index after CLI options.

    Only documented long value-taking options consume the following token in the
    primary parse.  Unknown long options are explored both ways so a malformed
    or newly added flag cannot hide a send verb; known flags, short options, and
    ``--name=value`` forms do not consume a following token.
    """
    cli_name = (cli or "").lower()
    value_taking = _VALUE_TAKING_OPTIONS.get(cli_name, frozenset())
    known_options = _KNOWN_OPTIONS.get(cli_name, frozenset())
    pending = [start]
    seen: set[int] = set()
    candidates: set[int] = set()
    while pending:
        index = pending.pop()
        if index in seen:
            continue
        seen.add(index)
        if index >= len(tokens):
            continue
        token = tokens[index]
        if not token.startswith("-"):
            candidates.add(index)
            continue
        if token == "--":
            candidates.add(index + 1)
            continue
        if token.startswith("--") and "=" not in token and token in value_taking:
            pending.append(index + 2)
            continue
        if token.startswith("--") and "=" not in token and token not in known_options:
            # Unknown long options are ambiguous: check both whether the next
            # token was their value and whether it was the command token.
            pending.append(index + 1)
            pending.append(index + 2)
            continue
        # Known flags, short options, and --flag=value are self-contained for
        # classification purposes.
        pending.append(index + 1)
    return tuple(sorted(candidates))


def _first_non_option(tokens: list[str], start: int, cli: Optional[str] = None) -> Optional[int]:
    """Backward-compatible first candidate for callers outside this module."""
    candidates = _first_non_option_indices(tokens, start, cli)
    return candidates[0] if candidates else None


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
        prefix_indices = _first_non_option_indices(tokens, command_index + 1, cli)
        for prefix_index in prefix_indices:
            if not prefix:
                verb_indices = (prefix_index,)
            else:
                prefix_lower = tuple(part.lower() for part in prefix)
                if tuple(
                    part.lower() for part in tokens[prefix_index:prefix_index + len(prefix_lower)]
                ) != prefix_lower:
                    continue
                verb_indices = _first_non_option_indices(tokens, prefix_index + len(prefix_lower), cli)
            if any(
                verb_index < len(tokens) and tokens[verb_index].lower() in verbs
                for verb_index in verb_indices
            ):
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


_SUPPORTED_CLI_NAMES = frozenset(_SEND_TABLE)
_SUPPORTED_CLI_RE = re.compile(
    r"(?<![A-Za-z0-9_.-])(?:[^\s'\";|&(){}<>`]*?/)?"
    r"(slack|imsg|telegram|gog|wacli|quo)(?![A-Za-z0-9_.-])",
    re.IGNORECASE,
)

# These are intentionally explicit and conservative.  A CLI occurrence is
# cleared only when the parser proves the command word and one of these paths;
# every other occurrence is a send candidate.  The entries were checked against
# each installed CLI's --help output.
_READ_ONLY_PATHS = {
    "slack": frozenset({
        ("account", "show"), ("backup", "status"), ("backup", "verify"),
        ("canvas", "get"), ("canvas", "permalink"), ("channels",),
        ("doctor",), ("files", "search"), ("huddle", "transcript"),
        ("message", "get"), ("message", "list"), ("messages",),
        ("search",), ("search-all",), ("status",), ("user", "get"),
        ("user", "presence"), ("users",),
    }),
    "imsg": frozenset({
        ("account",), ("after",), ("chats",), ("group",), ("history",),
        ("nickname",), ("scheduled", "list"), ("search",), ("stats",),
        ("status",), ("whois",),
    }),
    "telegram": frozenset({
        ("account", "list"), ("account", "show"), ("agent-context",),
        ("chats",), ("doctor",), ("download",), ("messages",),
        ("pins", "status"), ("search",), ("topic", "icons"),
        ("topic", "list"),
    }),
    "gog": frozenset({
        ("gmail", "get"), ("gmail", "labels", "list"),
        ("gmail", "drafts", "list"), ("gmail", "drafts", "get"),
        ("gmail", "search"), ("gmail", "thread", "get"),
        ("drive", "search"), ("drive", "get"), ("drive", "list"),
        ("drive", "ls"), ("ls",), ("list",), ("search",), ("find",),
        ("open",), ("download",), ("people", "me"), ("me",),
        ("status",), ("whoami",),
    }),
    "wacli": frozenset({
        ("accounts",), ("calls",), ("chats",), ("contacts",), ("doctor",),
        ("groups",), ("history",), ("messages", "export"),
        ("messages", "list"), ("messages", "search"), ("messages", "show"),
        ("messages", "starred"), ("poll",), ("polls",), ("profile",),
        ("store",), ("version",),
    }),
    "quo": frozenset({
        ("account", "show"), ("backup", "status"), ("backup", "verify"),
        ("calls", "get"), ("calls", "recordings"), ("calls", "summary"),
        ("calls", "transcript"), ("calls", "voicemail"),
        ("contacts", "fields"), ("contacts", "get"), ("context",),
        ("conversations", "list"), ("doctor",), ("messages", "get"),
        ("numbers", "get"), ("search",), ("status",), ("sync",),
        ("timeline",), ("users", "get"), ("webhooks", "get"),
        ("whois",),
    }),
}
_SHORT_VALUE_OPTIONS = {
    "slack": frozenset({"-a", "-f", "-n"}),
    "imsg": frozenset(),
    "telegram": frozenset({"-a"}),
    "gog": frozenset({"-a", "-p", "-n", "-z"}),
    "wacli": frozenset(),
    "quo": frozenset({"-a", "-o"}),
}
_READ_ONLY_NO_POSITIONALS = frozenset({
    ("slack", ("account", "show")), ("slack", ("channels",)),
    ("slack", ("doctor",)), ("slack", ("messages",)), ("slack", ("status",)),
    ("slack", ("users",)), ("imsg", ("account",)), ("imsg", ("chats",)),
    ("imsg", ("stats",)), ("imsg", ("status",)), ("telegram", ("account", "list")),
    ("telegram", ("account", "show")), ("telegram", ("agent-context",)),
    ("telegram", ("chats",)), ("telegram", ("doctor",)),
    ("telegram", ("topic", "icons")), ("gog", ("ls",)), ("gog", ("list",)),
    ("gog", ("status",)), ("gog", ("me",)), ("gog", ("whoami",)),
    ("quo", ("conversations", "list")), ("quo", ("doctor",)),
    ("quo", ("status",)), ("wacli", ("accounts",)), ("wacli", ("calls",)),
    ("wacli", ("chats",)), ("wacli", ("contacts",)), ("wacli", ("doctor",)),
    ("wacli", ("groups",)), ("wacli", ("history",)), ("wacli", ("polls",)),
    ("wacli", ("profile",)), ("wacli", ("store",)), ("wacli", ("version",)),
})
_READ_ONLY_VALUE_OPTIONS: dict[tuple[str, tuple[str, ...]], frozenset[str]] = {
    ("slack", ("message", "list")): frozenset({"--filter"}),
}


def _raw_cli_count(text: str) -> int:
    return sum(1 for _ in _SUPPORTED_CLI_RE.finditer(text))


def _strict_tokens(text: str) -> Optional[list[str]]:
    """Return shell tokens, or None when tokenization failed."""
    try:
        lexer = shlex.shlex(text, posix=True, punctuation_chars=";&|(){}")
        lexer.whitespace_split = True
        return list(lexer)
    except (TypeError, ValueError):
        return None


def _cli_token_indices(tokens: list[str]) -> list[int]:
    return [index for index, token in enumerate(tokens)
            if Path(token).name.lower() in _SUPPORTED_CLI_NAMES]


def _skip_global_option(tokens: list[str], index: int, cli: str) -> Optional[int]:
    token = tokens[index]
    if token == "--":
        return index + 1
    if token.startswith("--"):
        if "=" in token:
            option = token.split("=", 1)[0]
            return index + 1 if option in _KNOWN_OPTIONS.get(cli, frozenset()) else None
        if token in _VALUE_TAKING_OPTIONS.get(cli, frozenset()):
            return index + 2
        if token in _KNOWN_OPTIONS.get(cli, frozenset()):
            return index + 1
        return None
    if token.startswith("-"):
        if token in {"-h", "-j", "-y", "-v", "-p"}:
            return index + 1
        if token in _SHORT_VALUE_OPTIONS.get(cli, frozenset()):
            return index + 2
        # An attached short value (for example -alpg) is not proven safe.
        return None
    return index


def _read_only_command_path(tokens: list[str], command_index: int, cli: str) -> bool:
    tail = tokens[command_index + 1:]
    if any(token.lower() in {"--help", "-h", "help", "--version", "version"} for token in tail):
        return True
    path: list[str] = []
    index = 0
    allowed = _READ_ONLY_PATHS.get(cli, frozenset())
    while index < len(tail):
        next_index = _skip_global_option(tail, index, cli)
        if next_index is None:
            return False
        if next_index != index:
            index = next_index
            continue
        token = tail[index]
        if token.startswith("-"):
            return False
        path.append(token.lower())
        index += 1
        if tuple(path) in allowed:
            command_path = tuple(path)
            command_key = (cli, command_path)
            read_only_values = _READ_ONLY_VALUE_OPTIONS.get(command_key, frozenset())
            remaining_tokens = tail[index:]
            remaining_index = 0
            while remaining_index < len(remaining_tokens):
                remaining = remaining_tokens[remaining_index]
                if remaining in read_only_values or any(
                    remaining.startswith(f"{option}=") for option in read_only_values
                ):
                    if "=" not in remaining and remaining_index + 1 >= len(remaining_tokens):
                        return False
                    remaining_index += 1 if "=" in remaining else 2
                    continue
                if remaining.startswith("--"):
                    option = remaining.split("=", 1)[0]
                    if option not in _KNOWN_OPTIONS.get(cli, frozenset()):
                        return False
                    if "=" not in remaining and option in _VALUE_TAKING_OPTIONS.get(cli, frozenset()):
                        if remaining_index + 1 >= len(remaining_tokens):
                            return False
                        remaining_index += 2
                    else:
                        remaining_index += 1
                    continue
                if remaining.startswith("-"):
                    if remaining not in {"-h", "-j", "-y", "-v", "-p"}:
                        return False
                    remaining_index += 1
                    continue
                if command_key in _READ_ONLY_NO_POSITIONALS:
                    return False
                remaining_index += 1
            return True
        if not any(candidate[:len(path)] == tuple(path) for candidate in allowed):
            return False
    return False


def _shell_c_source(segment: list[str]) -> tuple[Optional[str], Optional[int], Optional[int]]:
    command_index = _command_word_index(segment)
    if command_index >= len(segment) or Path(segment[command_index]).name.lower() not in _SHELL_NAMES:
        return None, None, None
    for index in range(command_index + 1, len(segment)):
        token = segment[index]
        if token == "--":
            if index + 1 < len(segment):
                return segment[index + 1], index + 1, len(segment)
            return None, None, None
        if token.startswith("-") and "c" in token[1:]:
            if index + 1 < len(segment):
                return segment[index + 1], index + 1, len(segment)
            return None, None, None
    return None, None, None


def _prove_shell_text_read_only(text: str, seen: Optional[set[str]] = None) -> bool:
    """Prove every supported CLI occurrence in shell text is read-only."""
    if _raw_cli_count(text) == 0:
        return True
    tokens = _strict_tokens(text)
    if tokens is None:
        return False
    seen = set() if seen is None else seen
    for segment in _segments(tokens):
        if not segment:
            continue
        source, source_index, source_end = _shell_c_source(segment)
        if source is not None and source_index is not None and source_end is not None:
            # Anything outside the -c payload is still part of the wrapper and
            # must not contain an unproven CLI occurrence.
            prefix = segment[:source_index]
            suffix = segment[source_end:]
            if _raw_cli_count(" ".join(prefix + suffix)):
                return False
            if not _prove_shell_text_read_only(source, seen):
                return False
            continue
        cli_indices = _cli_token_indices(segment)
        segment_count = _raw_cli_count(" ".join(segment))
        if segment_count != len(cli_indices):
            return False
        if not cli_indices:
            if segment_count:
                return False
            continue
        command_index = _command_word_index(segment)
        if cli_indices != [command_index]:
            return False
        cli = Path(segment[command_index]).name.lower()
        if not _read_only_command_path(segment, command_index, cli):
            return False
    return True


def _source_texts_for_command(command: str, tokens: list[str]) -> list[tuple[str, bool]]:
    """Return executed inline/file source as (text, shell_source)."""
    sources: list[tuple[str, bool]] = []
    for segment in _segments(tokens):
        source, _, _ = _shell_c_source(segment)
        if source is not None:
            sources.append((source, True))
        for interpreter, source in _inline_interpreter_sources(segment):
            if Path(interpreter).name.lower() not in _SHELL_NAMES:
                sources.append((source, False))
        for token, invoked_by_shell in _executed_file_targets(segment):
            try:
                path = Path(token).expanduser()
                key = str(path.resolve())
                if key in {str(Path(item).resolve()) for item, _ in sources}:
                    continue
                if not path.is_file() or path.stat().st_size > _MAX_SCRIPT_BYTES:
                    continue
                content = path.read_text(errors="replace")
            except (OSError, ValueError):
                continue
            sources.append((content, invoked_by_shell or _looks_like_shell_script(path, content)))
    return sources


def _command_is_read_only(command: str) -> bool:
    tokens = _strict_tokens(command)
    if tokens is None:
        return _raw_cli_count(command) == 0
    if not _prove_shell_text_read_only(command):
        return False
    for source, is_shell in _source_texts_for_command(command, tokens):
        if _raw_cli_count(source) == 0:
            continue
        if not is_shell or not _prove_shell_text_read_only(source):
            return False
    return True


def _classified_raw(tool_name: str, raw: str, args: Any, cli: str = "") -> dict[str, str]:
    return {
        "command": raw,
        "action_hash": hashlib.sha256(raw.encode()).hexdigest(),
        "cli": cli,
        "account": _command_account(args, cli or None) or "",
    }


def classify_voice_send(tool_name: str, args: Any) -> Optional[dict[str, str]]:
    """Classify every unproven supported-CLI occurrence as a voice send."""
    if tool_name in _NATIVE_SEND_TOOLS:
        normalized = json.dumps({"tool": tool_name, "args": args}, sort_keys=True, separators=(",", ":"), default=str)
        return {
            "command": normalized,
            "action_hash": hashlib.sha256(normalized.encode()).hexdigest(),
            "cli": tool_name,
            "account": _command_account(args, None) or "",
        }
    if tool_name in {"terminal", "shell"} and isinstance(args, dict):
        raw = args.get("command") or args.get("cmd")
        if isinstance(raw, str):
            tokens = _strict_tokens(raw)
            if tokens is None:
                if _raw_cli_count(raw):
                    return _classified_raw(tool_name, raw, args)
                return None
            if _raw_cli_count(raw) and not _command_is_read_only(raw):
                return _classified_raw(tool_name, raw, args)
            for source, is_shell in _source_texts_for_command(raw, tokens):
                if _raw_cli_count(source) and (not is_shell or not _command_is_read_only(source)):
                    return _classified_raw(tool_name, raw, args)
        return None
    if tool_name in {"execute_code", "execute_code_rpc"} and isinstance(args, dict):
        raw = args.get("code")
        if isinstance(raw, str) and _raw_cli_count(raw):
            return _classified_raw(tool_name, raw, args)
    return None


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


def _grant_records(row: Optional[dict]) -> list[dict]:
    """Return durable grant records, including the pre-multi-grant legacy shape."""
    if not isinstance(row, dict):
        return []
    grants = row.get("grants")
    if isinstance(grants, list):
        return [grant for grant in grants if isinstance(grant, dict)]
    if row.get("approved_at") or "consumed_at" in row:
        return [row]
    return []


def _grant_container(records: list[dict]) -> dict:
    return {"grants": records}


def _has_available_grant(row: Optional[dict]) -> bool:
    return any(record.get("approved_at") and not _consumed_timestamps(record)
               for record in _grant_records(row))


def consumed_grant_timestamps(db: Any, session_id: str, action_hash: str) -> list[float]:
    timestamps: list[float] = []
    for record in _grant_records(_read_grant(db, session_id, action_hash)):
        timestamps.extend(_consumed_timestamps(record))
    return timestamps


def _consumed_tool_call_ids(row: Optional[dict]) -> list[Optional[str]]:
    if not isinstance(row, dict):
        return []
    value = row.get("consumed_tool_call_ids")
    if isinstance(value, list):
        return [str(item) if item else None for item in value]
    legacy = row.get("tool_call_id")
    timestamps = _consumed_timestamps(row)
    if legacy and timestamps:
        return [str(legacy)] + [None] * (len(timestamps) - 1)
    return [None] * len(timestamps)


def consumed_grant_tool_call_ids(db: Any, session_id: str, action_hash: str) -> list[Optional[str]]:
    ids: list[Optional[str]] = []
    for record in _grant_records(_read_grant(db, session_id, action_hash)):
        ids.extend(_consumed_tool_call_ids(record))
    return ids


def has_consumed_grant(db: Any, session_id: str, action_hash: str) -> bool:
    return bool(consumed_grant_timestamps(db, session_id, action_hash))


def _append_consumed_at(record: dict, tool_call_id: Optional[str] = None) -> None:
    record["consumed_at"] = _consumed_timestamps(record) + [time.time()]
    ids = _consumed_tool_call_ids(record)
    ids.append(str(tool_call_id) if tool_call_id else None)
    record["consumed_tool_call_ids"] = ids
    if tool_call_id:
        record["tool_call_id"] = str(tool_call_id)


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


def _consume_existing_grant(db: Any, session_id: str, action_hash: str,
                            tool_call_id: Optional[str] = None) -> Optional[bool]:
    key = _meta_key(session_id, action_hash)

    def _consume(row):
        records = _grant_records(row)
        for record in records:
            if record.get("approved_at") and not _consumed_timestamps(record):
                updated = [dict(item) for item in records]
                index = records.index(record)
                _append_consumed_at(updated[index], tool_call_id)
                return _grant_container(updated)
        return None

    result = _transactional_grant_update(db, key, _consume)
    if result is not None:
        return result
    existing = _read_grant(db, session_id, action_hash)
    records = _grant_records(existing)
    for record in records:
        if record.get("approved_at") and not _consumed_timestamps(record):
            updated = [dict(item) for item in records]
            index = records.index(record)
            _append_consumed_at(updated[index], tool_call_id)
            if not _write_grant(db, session_id, action_hash, _grant_container(updated)):
                return None
            return True
    return False


def _store_consumed_grant(db: Any, session_id: str, action_hash: str, record: dict) -> Optional[bool]:
    key = _meta_key(session_id, action_hash)

    def _store(existing):
        # A fresh approval is a new one-shot grant even when an earlier grant
        # for this exact action was consumed. Appending inside the transaction
        # preserves atomicity for concurrent approvals and sends.
        return _grant_container([*(_grant_records(existing)), record])

    result = _transactional_grant_update(db, key, _store)
    if result is not None:
        return result
    existing = _read_grant(db, session_id, action_hash)
    records = [*(_grant_records(existing)), record]
    if not _write_grant(db, session_id, action_hash, _grant_container(records)):
        return None
    return True


def _block(action: str, reason: str) -> str:
    return f"BLOCKED: approval required before sending a message in Brian's voice ({reason}; action={action[:12]})."


def authorize_voice_send(agent: Any, tool_name: str, args: Any,
                         tool_call_id: Optional[str] = None) -> Optional[str]:
    """Approve/consume one exact send. Return a block message or ``None``."""
    classified = classify_voice_send(tool_name, args)
    if classified is None:
        return None
    session_id = str(getattr(agent, "session_id", "") or "")
    db = getattr(agent, "_session_db", None)
    action_hash = classified["action_hash"]

    existing = _read_grant(db, session_id, action_hash)
    if _has_available_grant(existing):
        consumed = _consume_existing_grant(db, session_id, action_hash, tool_call_id)
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
        "consumed_tool_call_ids": [],
    }
    _append_consumed_at(record, tool_call_id)
    stored = _store_consumed_grant(db, session_id, action_hash, record)
    if stored is None:
        return _block(action_hash, "grant store unavailable")
    if stored is False:
        return _block(action_hash, "grant already consumed")
    return None


__all__ = [
    "authorize_voice_send", "classify_voice_send", "consumed_grant_timestamps",
    "consumed_grant_tool_call_ids", "has_consumed_grant",
]
