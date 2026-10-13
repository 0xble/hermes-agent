"""Shared guard for relay sends made from delegated child execution."""

from __future__ import annotations

import re
import shlex
from pathlib import Path
from typing import Any, Iterator

from tools.shell_heredoc import _find_heredoc_close, _scan_heredoc_command_unit, _span_end

CHILD_PARENT_RELAY_ERROR = (
    "child_parent_relay: put this in your final result; the parent receives it when you finish"
)


def _profile_name() -> str:
    try:
        from gateway.session_context import bound_session_env
        if profile := bound_session_env("HERMES_SESSION_PROFILE"):
            return profile
    except Exception:
        pass
    try:
        from hermes_cli.profiles import get_active_profile_name
        return str(get_active_profile_name() or "")
    except Exception:
        return ""


def _parent_session_id() -> str:
    from agent.delegation_context import delegated_child_parent_session_id
    return delegated_child_parent_session_id().strip()


def child_parent_relay_error(target: Any) -> str | None:
    """Return actionable denial text when a delegated child targets its own parent."""
    from agent.delegation_context import is_delegated_child_process_context

    if not is_delegated_child_process_context() or not isinstance(target, str):
        return None
    parent_session_id = _parent_session_id()
    profile = _profile_name().strip()
    if not parent_session_id or not profile:
        return None
    expected = f"hermes:{profile}/{parent_session_id}"
    return CHILD_PARENT_RELAY_ERROR if target.strip() == expected else None


def mcp_child_parent_relay_error(server_name: str, tool_name: str, args: Any) -> str | None:
    """Apply the guard to the Relay MCP server before its call server is acquired."""
    if server_name.strip().lower() not in {"relay", "mcp__relay"} or tool_name != "send":
        return None
    address = args.get("address") if isinstance(args, dict) else None
    return child_parent_relay_error(address)


_RELAY_SEND_HINT = re.compile(r"(?:^|[\s;&|()])(?:[^\s;&|()]+/)?relay\b[^;\n&|()]*\bsend\b")
_REDIRECTIONS = {"<", ">", "<<", "<<-", ">>", "<<<", "<&", ">&", "&>", "&>>", "<>"}
_RELAY_VALUE_OPTIONS = {"--fields", "--task", "--body-file"}
_RELAY_FLAG_OPTIONS = {"-h", "--help", "-j", "--json", "--agent", "-y", "--yes", "--version", "--steer", "--dry-run"}


def _heredoc_substitutions(body: str) -> Iterator[str]:
    """Only executable substitution spans matter; surrounding stdin stays data."""
    cursor = 0
    while cursor < len(body):
        if body[cursor] == "\\":
            cursor += 2
            continue
        if body[cursor] == "`":
            end = _span_end(body, cursor, "`")
            if end == len(body) and not body.endswith("`"):
                raise ValueError("unterminated heredoc command substitution")
            yield body[cursor + 1:end - 1]
            cursor = end
            continue
        if not body.startswith("$(", cursor):
            cursor += 1
            continue
        start = cursor + 2
        cursor = start
        depth = 1
        quote = ""
        while cursor < len(body) and depth:
            char = body[cursor]
            if char == "\\" and quote != "'":
                cursor += 2
                continue
            if quote:
                if char == quote:
                    quote = ""
            elif char in "'\"`":
                quote = char
            elif char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
            cursor += 1
        if depth:
            raise ValueError("unterminated heredoc command substitution")
        yield body[start:cursor - 1]


def _shell_headers(command: str, depth: int = 0) -> Iterator[str]:
    """Reuse the shell scanner's logical-line and exact heredoc delimiter handling.

    This is argument inspection, not blanket inert-body masking: stdin data is not
    argv even for an unquoted delimiter. Only command-substitution spans are shell.
    """
    if depth >= 32:
        raise ValueError("excessively nested heredoc substitutions")
    start = 0
    while start < len(command):
        end, specs, unknown, _post_operator, _owner = _scan_heredoc_command_unit(command, start)
        header = command[start:end]
        yield header
        if unknown:
            raise ValueError("ambiguous heredoc operator")
        start = end + 1
        for delimiter, strip_tabs, quoted in specs:
            close = _find_heredoc_close(command, start, delimiter, strip_tabs)
            if end == len(command) or close is None:
                raise ValueError("unterminated heredoc")
            body = command[start:close]
            if not quoted:
                for expansion in _heredoc_substitutions(body):
                    yield from _shell_headers(expansion, depth + 1)
            start = close


def _remove_line_continuations(source: str) -> str:
    """Shell removes backslash-newline outside single quotes before parsing words."""
    result: list[str] = []
    quote = ""
    cursor = 0
    while cursor < len(source):
        char = source[cursor]
        if char == "\\" and quote != "'" and cursor + 1 < len(source):
            following = source[cursor + 1]
            if following != "\n":
                result.extend((char, following))
            cursor += 2
            continue
        if char == quote:
            quote = ""
        elif not quote and char in "'\"":
            quote = char
        result.append(char)
        cursor += 1
    return "".join(result)


def _simple_commands(header: str) -> Iterator[str]:
    """Separate only unquoted shell operators, retaining quoting for shlex."""
    header = _remove_line_continuations(header)
    start = cursor = 0
    quote = ""
    while cursor < len(header):
        char = header[cursor]
        if char == "\\" and quote != "'":
            cursor += 2
            continue
        if quote:
            if char == quote:
                quote = ""
        elif char in "'\"`":
            quote = char
        elif char == "#" and (cursor == 0 or header[cursor - 1].isspace() or header[cursor - 1] in ";&|()"):
            yield header[start:cursor]
            return
        elif char in ";|&()":
            # >& / <& / &> belong to redirection, not a new command.
            if char != "&" or not (
                (cursor > 0 and header[cursor - 1] in "<>")
                or (cursor + 1 < len(header) and header[cursor + 1] == ">")
            ):
                yield header[start:cursor]
                start = cursor + 1
        cursor += 1
    if quote:
        raise ValueError("unterminated shell quote")
    yield header[start:]


def _shell_argv(tokens: list[str]) -> Iterator[str]:
    """Yield argv lazily; never inspect redirection-looking message arguments."""
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token.isdigit() and index + 1 < len(tokens) and tokens[index + 1] in _REDIRECTIONS:
            index += 1
            token = tokens[index]
        if token in _REDIRECTIONS:
            if index + 1 >= len(tokens):
                raise ValueError("incomplete redirection")
            index += 2
            continue
        yield token
        index += 1


def _relay_destination(simple_command: str) -> str | None:
    lexer = shlex.shlex(simple_command, posix=True, punctuation_chars="<>&")
    lexer.whitespace_split = True
    lexer.commenters = ""
    argv = iter(_shell_argv(list(lexer)))
    executable = next(argv, "")
    # These words introduce another command, not a positional argument. Ordinary
    # printf/echo arguments called 'relay' must never be treated as an invocation.
    prefixes = {"if", "then", "else", "elif", "while", "until", "do", "!", "time", "command", "exec", "env"}
    while executable in prefixes or re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", executable, re.DOTALL):
        executable = next(argv, "")
    if not executable or Path(executable).name != "relay":
        return None
    sending = False
    options = True
    for token in argv:
        if options and token == "--":
            options = False
        elif options and token.partition("=")[0] in _RELAY_VALUE_OPTIONS:
            if "=" not in token and next(argv, None) is None:
                raise ValueError("missing relay option value")
        elif options and token in _RELAY_FLAG_OPTIONS:
            pass
        elif options and token.startswith("-"):
            raise ValueError("unknown relay option")
        elif not sending:
            if token != "send":
                return None
            sending = True
        else:
            if "$" in token or "`" in token:
                raise ValueError("dynamic relay destination")
            return token
    if sending:
        raise ValueError("missing relay destination")
    return None


def cli_child_parent_relay_error(command: str) -> str | None:
    """Inspect each direct relay-send destination, never message data or later argv.

    Heredocs use the existing shell scanner before shlex sees any body text. An
    ambiguous apparent relay command fails closed only in a resolvable child context;
    unrelated commands and top-level terminal behavior are unchanged.
    """
    from agent.delegation_context import is_delegated_child_process_context

    if not is_delegated_child_process_context():
        return None
    expected = f"hermes:{_profile_name().strip()}/{_parent_session_id()}"
    if not child_parent_relay_error(expected):
        return None
    relay_seen = False
    try:
        for header in _shell_headers(command):
            relay_seen = relay_seen or bool(_RELAY_SEND_HINT.search(header))
            for simple_command in _simple_commands(header):
                destination = _relay_destination(simple_command)
                if destination is not None and child_parent_relay_error(destination):
                    return CHILD_PARENT_RELAY_ERROR
    except ValueError:
        if relay_seen or _RELAY_SEND_HINT.search(command):
            return CHILD_PARENT_RELAY_ERROR
    return None
