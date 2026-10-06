"""Machine-wide throttling for local test and CI commands.

The Mac Studio's ``heavy-slot`` executable owns the cross-process semaphore. Hermes
only decides which local terminal commands are heavy and wraps them once; the
``HEAVY_SLOT_HELD`` marker makes nested commands run directly inside the slot.
Remote and sandbox terminal backends are intentionally untouched.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
from collections.abc import Mapping
from pathlib import Path
from typing import Optional

_HEAVY_PROFILES = {"pytest", "vitest", "ci", "ci-gate", "test"}
_CI_PROFILES = {"gate", "preflight", "affected"}
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")


def _split_shell_segments(command: str) -> list[str]:
    """Split at shell command boundaries without trying to execute the input."""
    return [segment.strip() for segment in re.split(r"&&|\|\||[;|&\n]", command) if segment.strip()]


def _command_words(segment: str) -> list[str]:
    """Return conservative shell words for one command segment."""
    try:
        words = shlex.split(segment, posix=True)
    except ValueError:
        return []
    while words and _ASSIGNMENT.match(words[0]):
        words.pop(0)
    # Common command-position wrappers. Keep this finite so prose or arguments
    # cannot accidentally turn an ordinary command into a heavy run.
    while words and words[0] in {"command", "exec", "sudo", "time", "nice"}:
        words.pop(0)
        while words and _ASSIGNMENT.match(words[0]):
            words.pop(0)
    if words and words[0] == "env":
        words.pop(0)
        while words and (words[0] == "--" or words[0].startswith("-") or _ASSIGNMENT.match(words[0])):
            if words[0] == "--":
                words.pop(0)
                break
            words.pop(0)
    return words


def _basename(word: str) -> str:
    return word.rsplit("/", 1)[-1]


def _is_test_script(word: str) -> bool:
    return word == "test" or word.startswith("test:")


def classify_heavy_command(command: str) -> Optional[str]:
    """Return a stable label for a recognized heavy command, else ``None``."""
    for segment in _split_shell_segments(command):
        words = _command_words(segment)
        if not words:
            continue
        executable = words[0]
        name = _basename(executable)
        args = words[1:]

        if name in {"pytest", "py.test", "vitest"}:
            return name
        if name.startswith("python") and len(args) >= 2 and args[0] == "-m" and args[1] in {"pytest", "py.test"}:
            return "pytest"
        if name in {"uv", "poetry", "pipenv", "hatch", "tox", "nox", "npx"}:
            if any(token in {"pytest", "py.test", "vitest"} for token in args):
                return "pytest" if "pytest" in args or "py.test" in args else "vitest"
        if name in {"npm", "pnpm", "yarn", "bun"}:
            if any(_is_test_script(token) for token in args):
                return "test"
            if "vitest" in args:
                return "vitest"
        if name == "turbo" and any(_is_test_script(token) for token in args):
            return "test"
        if (executable == "./bin/ci" or executable.endswith("/bin/ci")) and args and args[0] in _CI_PROFILES:
            return f"ci-{args[0]}"
        if name == "ci-gate" or executable.endswith("/ci-gate"):
            return "ci-gate"
    return None


def _heavy_slot_executable(environment: Mapping[str, str]) -> Optional[str]:
    """Find the host helper without requiring a login-shell PATH."""
    home_candidate = Path.home() / ".local" / "bin" / "heavy-slot"
    if home_candidate.is_file() and os.access(home_candidate, os.X_OK):
        return str(home_candidate)
    path_candidate = shutil.which("heavy-slot", path=environment.get("PATH"))
    return path_candidate if path_candidate and os.access(path_candidate, os.X_OK) else None


def wrap_heavy_command(
    command: str,
    *,
    env_type: str,
    environment: Optional[Mapping[str, str]] = None,
) -> str:
    """Wrap a recognized local test/CI command in the machine-wide slot helper."""
    if env_type != "local":
        return command
    effective_environment = dict(os.environ)
    if environment is not None:
        effective_environment.update(environment)
    if effective_environment.get("HEAVY_SLOT_HELD"):
        return command
    profile = classify_heavy_command(command)
    if profile is None:
        return command
    executable = _heavy_slot_executable(effective_environment)
    if executable is None:
        return command
    label = f"hermes-terminal {profile}"
    return f"{shlex.quote(executable)} --label {shlex.quote(label)} -- bash -c {shlex.quote(command)}"
