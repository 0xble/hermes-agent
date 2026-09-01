"""Advisory guard for nonstandard ``git worktree add`` destinations.

Hermes-managed worktrees live under a repository's ``.worktrees/`` directory.
This module detects direct shell invocations that create a worktree elsewhere
and returns an actionable warning. It does not block the command because Git
supports arbitrary worktree locations and callers may have a legitimate
reason to use one.
"""

from __future__ import annotations

import os
import shlex
from pathlib import Path
from typing import Optional

from tools.shell_heredoc import strip_inert_heredoc_bodies

_SHELL_SEPARATORS = {"&&", "||", ";", "|", "&", "\n"}
_OPTIONS_WITH_VALUES = {
    "-b",
    "-B",
    "--reason",
    "--expire",
}
_GIT_OPTIONS_WITH_VALUES = {
    "-C",
    "-c",
    "--git-dir",
    "--work-tree",
    "--namespace",
    "--exec-path",
}


def _tokens(command: str) -> list[str]:
    """Best-effort shell tokenization with command separators preserved."""
    try:
        lexer = shlex.shlex(
            strip_inert_heredoc_bodies(command),
            posix=True,
            punctuation_chars=";&|\n",
        )
        lexer.whitespace = " \t\r"
        lexer.whitespace_split = True
        lexer.commenters = ""
        return list(lexer)
    except (TypeError, ValueError):
        return []


def _segments(tokens: list[str]) -> list[list[str]]:
    segments: list[list[str]] = []
    current: list[str] = []
    for token in tokens:
        if token in _SHELL_SEPARATORS:
            if current:
                segments.append(current)
                current = []
        else:
            current.append(token)
    if current:
        segments.append(current)
    return segments


def _git_worktree_destination(segment: list[str]) -> Optional[tuple[str, Optional[str]]]:
    """Return ``(destination, git_dash_c)`` for a direct worktree-add segment."""
    if not segment:
        return None

    index = 0
    while index < len(segment) and (
        "=" in segment[index] and not segment[index].startswith(("/", "./", "../"))
    ):
        index += 1
    while index < len(segment) and segment[index] in {"command", "env", "sudo"}:
        wrapper = segment[index]
        index += 1
        if wrapper == "env":
            while index < len(segment):
                token = segment[index]
                if token in {"-u", "--unset", "-C", "--chdir", "-S", "--split-string"}:
                    index += 2
                    continue
                if token.startswith("-") or ("=" in token and not token.startswith("=")):
                    index += 1
                    continue
                break
        elif wrapper == "sudo":
            sudo_value_options = {"-C", "-D", "-g", "-h", "-p", "-R", "-r", "-t", "-T", "-U", "-u"}
            while index < len(segment) and segment[index].startswith("-"):
                token = segment[index]
                index += 2 if token in sudo_value_options else 1
    if index >= len(segment) or Path(segment[index]).name != "git":
        return None
    index += 1

    git_dash_c: Optional[str] = None
    while index < len(segment):
        token = segment[index]
        if token == "worktree":
            break
        if token in _GIT_OPTIONS_WITH_VALUES:
            if index + 1 >= len(segment):
                return None
            if token == "-C":
                git_dash_c = segment[index + 1]
            index += 2
            continue
        if token.startswith("-C") and token != "-C":
            git_dash_c = token[2:]
            index += 1
            continue
        if token.startswith("-"):
            index += 1
            continue
        return None
    if index >= len(segment) or segment[index] != "worktree":
        return None
    index += 1
    if index >= len(segment) or segment[index] != "add":
        return None
    index += 1

    while index < len(segment):
        token = segment[index]
        if token == "--":
            index += 1
            break
        if token in _OPTIONS_WITH_VALUES:
            index += 2
            continue
        if any(token.startswith(prefix + "=") for prefix in _OPTIONS_WITH_VALUES if prefix.startswith("--")):
            index += 1
            continue
        if token.startswith("-"):
            index += 1
            continue
        break

    if index >= len(segment):
        return None
    return segment[index], git_dash_c


def _is_under_dot_worktrees(path_text: str, cwd: str, git_dash_c: Optional[str]) -> bool:
    """Return whether a literal or dynamic destination names ``.worktrees``."""
    # Resolve literals to catch paths such as ``foo/../.worktrees/task``. For
    # dynamic shell expressions, only accept an explicit `.worktrees` component
    # without traversal. Everything else fails toward warning.
    if any(marker in path_text for marker in ("$", "`", "$(")):
        raw_parts = Path(path_text).parts
        return ".worktrees" in raw_parts and ".." not in raw_parts
    try:
        base = Path(cwd).expanduser()
        if git_dash_c:
            git_base = Path(os.path.expanduser(git_dash_c))
            base = git_base if git_base.is_absolute() else base / git_base
        destination = Path(os.path.expanduser(path_text))
        resolved = destination if destination.is_absolute() else base / destination
        return ".worktrees" in resolved.resolve(strict=False).parts
    except (OSError, RuntimeError, ValueError):
        return False


def nonstandard_worktree_add_warning(command: str, cwd: str) -> Optional[str]:
    """Return one warning when a direct command adds outside ``.worktrees/``."""
    for segment in _segments(_tokens(command)):
        found = _git_worktree_destination(segment)
        if found is None:
            continue
        destination, git_dash_c = found
        if _is_under_dot_worktrees(destination, cwd, git_dash_c):
            continue
        return (
            "Worktree path warning: this command creates a Git worktree at "
            f"`{destination}`, outside a `.worktrees/` directory. Hermes-managed "
            "and agent-created worktrees should use "
            "`<repo>/.worktrees/<name>` so ownership, ignore rules, and cleanup "
            "stay predictable. If the external location was intentional, document "
            "the exception. Otherwise move or recreate the worktree under the "
            "repository's `.worktrees/` directory."
        )
    return None
