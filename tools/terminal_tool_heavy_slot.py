"""Machine-wide throttling for local test suites and CI gates.

The host's ``heavy-slot`` executable owns the cross-process semaphore. Hermes only
decides which simple commands inside a local terminal command are heavy and routes
each of them through a small dispatcher function defined at the top of the command::

    cd repo && pytest -q   ->   _hermes_heavy_slot() {...}
                                cd repo && _hermes_heavy_slot 'hermes pytest' pytest -q

The rest of the command still runs in the session shell, so ``cd``, exports,
functions and background ``&`` behave exactly as before. The dispatcher sends an
external program through the helper and leaves a name the shell resolves itself
(alias, function, builtin) to the shell, so a user's own wrapper keeps working.
Only the heavy process waits for and holds a slot.

Not wrapped: non-local backends (Docker, SSH, Modal and other sandboxes),
commands already inside a slot (``HEAVY_SLOT_HELD``), the opt-out
``HERMES_HEAVY_SLOT=off``, hosts without the helper, text that only mentions a
runner (``grep pytest``, quoted strings, comments, heredoc bodies), and
targeted runs of 1-10 explicit test files with only single-run options.
"""

from __future__ import annotations

import logging
import os
import re
import shlex
from collections.abc import Collection, Mapping
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_OPT_OUT_VALUES = {"0", "off", "false", "no"}
# One leading underscore: the session snapshot re-dump skips `_x` functions, so the
# dispatcher is redefined by each rewritten command and never persists.
_DISPATCH = "_hermes_heavy_slot"
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
# Words that put the NEXT word in command position.
_KEYWORDS = {"if", "then", "else", "elif", "do", "while", "until", "!", "{", "time", "command", "exec", "nohup"}
# Cheap prefilter: every heavy form contains one of these, so most commands skip parsing.
_CANDIDATE = re.compile(r"test|jest|bin/ci|tox|nox")
_SHELLS = {"bash", "sh", "zsh"}
_PY_RUNNERS = {"pytest", "py.test"}
_JS_RUNNERS = {"vitest", "jest"}
# Package-script runners: `test` / `test:*` scripts are heavy, package management is not.
_PACKAGE_MANAGERS = {"npm", "pnpm", "yarn", "bun"}
_PACKAGE_VALUE_OPTIONS = {"--filter", "-F", "--dir", "-C", "--prefix", "--cwd", "--workspace", "-w"}
# Subcommands whose positional words are package names, never script names.
_PACKAGE_MANAGEMENT = {"add", "install", "i", "remove", "rm", "uninstall", "un", "update", "up", "upgrade",
                       "link", "unlink", "info", "view", "why", "outdated", "publish", "pack", "init", "create"}
# Task runners that fan a script out across a monorepo: `turbo run test`, `nx run-many -t test`, `lerna run test`.
_MONOREPO_RUNNERS = {"turbo", "nx", "lerna"}
_ENV_RUNNERS = {"uv", "poetry", "pipenv", "hatch", "pdm", "rye"}
_EXEC_RUNNERS = {"npx", "bunx", "uvx", "pnpx"}
_SUITE_RUNNERS = {"tox", "nox"}
# Repository CI profiles that only run fast local checks (mirrors ~/.config/shell/heavy-slot.sh).
_LIGHT_CI_PROFILES = {"preflight", "install-hooks", "list", "-h", "--help"}
# Options a targeted run may carry and still count as light. This is an allowlist on
# purpose: any other option (pytest -n/--dist, jest --maxWorkers, vitest --pool, a
# runner's own --workers or --jobs) may fan out, so it falls back to the slot.
_TARGETED_FLAGS = {"-q", "-qq", "-v", "-vv", "-vvv", "-x", "-s", "-l", "--quiet", "--verbose", "--exitfirst",
                   "--no-header", "--lf", "--ff", "--last-failed", "--failed-first", "--sw", "--stepwise",
                   "--run", "--no-watch", "--bail", "--silent", "--color", "--no-color", "--showlocals"}
_TARGETED_VALUE_FLAGS = {"-k", "-m", "-p", "-o", "-W", "-r", "--tb", "-t", "--testNamePattern",
                         "--file-timeout", "--file-retries"}
_TEST_FILE = re.compile(r"(\.py(::.*)?|\.(test|spec)\.[cm]?[jt]sx?)$")
_MAX_TARGETED_FILES = 10


def heavy_slot_enabled(environment: Mapping[str, str]) -> bool:
    """False inside an existing slot or when the operator opted out."""
    if environment.get("HEAVY_SLOT_HELD"):
        return False
    return str(environment.get("HERMES_HEAVY_SLOT", "")).strip().lower() not in _OPT_OUT_VALUES


def _unquote(word: str) -> Optional[str]:
    try:
        parts = shlex.split(word, posix=True)
    except ValueError:
        return None
    return parts[0] if len(parts) == 1 else None


def _basename(word: str) -> str:
    return word.rsplit("/", 1)[-1]


def _is_targeted(args: list[str]) -> bool:
    """1-10 explicit test files with only known single-run options: cheap enough to run unslotted."""
    files = 0
    skip_value = False
    for arg in args:
        if skip_value:
            skip_value = False
            continue
        if arg == "--":
            continue
        if arg in _TARGETED_VALUE_FLAGS:
            skip_value = True
            continue
        if arg.startswith("-"):
            name = arg.split("=", 1)[0]
            if arg in _TARGETED_FLAGS or (name != arg and name in _TARGETED_VALUE_FLAGS | {"--tb", "--color"}):
                continue
            return False  # unknown option: may add workers, so keep the slot
        if not _TEST_FILE.search(arg):
            return False  # a directory, `run` subcommand target set, or anything broad
        files += 1
    return 1 <= files <= _MAX_TARGETED_FILES


def _first_positional(args: list[str], value_options: Collection[str] = ()) -> tuple[Optional[str], list[str]]:
    skip = False
    for index, arg in enumerate(args):
        if skip:
            skip = False
            continue
        if arg in value_options:
            skip = True
            continue
        if arg.startswith("-"):
            continue
        return arg, args[index + 1:]
    return None, []


def _is_test_script(word: Optional[str]) -> bool:
    return word is not None and (word == "test" or word.startswith("test:"))


def _has_test_script(args: list[str]) -> bool:
    """Whether any positional word (not an option or its value) is a `test` / `test:*` script."""
    skip = False
    for arg in args:
        if skip:
            skip = False
        elif arg in _PACKAGE_VALUE_OPTIONS:
            skip = True  # `pnpm --filter test build`: `test` names a package
        elif not arg.startswith("-") and _is_test_script(arg):
            return True
    return False


def classify_words(words: list[str]) -> Optional[str]:
    """Label for a heavy simple command given its unquoted words (command first), else ``None``."""
    if not words:
        return None
    executable, args = words[0], words[1:]
    name = _basename(executable)

    if name in _SHELLS and args:
        if args[0].startswith("-") and "c" in args[0] and len(args) > 1:
            return classify_command(args[1])  # whole `bash -c '...'` payload runs in the slot
        if not args[0].startswith("-"):
            return classify_words(args)  # `bash scripts/run_tests.sh ...`
        return None
    if name in _PY_RUNNERS:
        return None if _is_targeted(args) else "pytest"
    if name.startswith("python") and len(args) >= 2 and args[0] == "-m" and args[1] in _PY_RUNNERS:
        return None if _is_targeted(args[2:]) else "pytest"
    if name in _JS_RUNNERS:
        rest = args[1:] if args[:1] in (["run"], ["related"]) else args
        return None if _is_targeted(rest) else name
    if name == "run_tests.sh":
        return None if _is_targeted(args) else "run_tests"
    if name in _SUITE_RUNNERS:
        return name
    if name in _ENV_RUNNERS:
        sub, rest = _first_positional(args, {"--project", "--directory", "--python", "-p", "--with", "--group",
                                              "--extra", "--package", "--env", "-e"})
        if sub == "run":
            inner, inner_rest = _first_positional(rest, {"--with", "--group", "--extra", "--package", "--python",
                                                         "-p", "--env", "-e", "--directory", "--project"})
            return classify_words([inner, *inner_rest]) if inner else None
        return None
    if name in _EXEC_RUNNERS:
        inner, rest = _first_positional(args, {"--package", "-p", "--from", "--with"})
        return classify_words([inner, *rest]) if inner else None
    if name in _PACKAGE_MANAGERS:
        sub, rest = _first_positional(args, _PACKAGE_VALUE_OPTIONS)
        if any(word in _PACKAGE_MANAGEMENT for word in [sub, *rest][:3] if word):
            return None  # `pnpm add -D vitest`, `yarn workspace app add test`: the words name packages
        if sub in {"exec", "dlx"}:
            inner, inner_rest = _first_positional(rest)
            return classify_words([inner, *inner_rest]) if inner else None
        # Script runs in every spelling (`pnpm test`, `npm run test`, `yarn workspace app test`,
        # `yarn workspaces foreach run test`, `pnpm -r test:unit`): any positional test script.
        return "test" if _has_test_script(args) else None
    if name in _MONOREPO_RUNNERS:
        return "test" if _has_test_script(args) else None
    if executable.endswith("bin/ci") and _basename(executable) == "ci":
        profile = args[0] if args else "full"
        return None if profile in _LIGHT_CI_PROFILES else f"ci-{profile}"
    # git-guard's `ci-gate` takes its own slot around the repository gate (with a
    # better label), so it is deliberately not wrapped here.
    return None


def _command_start(words: list[str]) -> int:
    """Index of the word in command position, after assignments, keywords and prefix commands."""
    index = 0
    while index < len(words):
        word = words[index]
        if _ASSIGNMENT.match(word) or word in _KEYWORDS:
            index += 1
        elif word == "env":
            index += 1
            while index < len(words) and (words[index].startswith("-") or _ASSIGNMENT.match(words[index])):
                index += 1 + (words[index] in {"-u", "--unset", "-C", "--chdir"})
        elif word in {"nice", "timeout", "gtimeout"}:
            index += 1
            while index < len(words) and words[index].startswith("-"):
                index += 1 + (words[index] in {"-n", "-s", "-k", "--signal", "--kill-after"})
            if word != "nice" and index < len(words):
                index += 1  # the duration
        else:
            return index
    return index


def _simple_commands(command: str) -> list[list[tuple[int, str]]]:
    """Top-level simple commands as ``[(offset, raw_word), ...]``; heredoc bodies are never scanned."""
    from tools.self_repo_guard import _mask_heredocs
    from tools.terminal_tool_sudo import _scan_shell

    masked, _ = _mask_heredocs(command)  # same length: body characters become spaces
    segments: list[list[tuple[int, str]]] = [[]]
    for kind, start, end, _at_start in _scan_shell(masked):
        if kind == "op" or (kind == "ws" and masked[start:end] == "\n"):
            segments.append([])
        elif kind == "word":
            segments[-1].append((start, masked[start:end]))
    return [segment for segment in segments if segment]


def classify_command(command: str) -> Optional[str]:
    """First heavy label among the command's simple commands, else ``None``."""
    for _, label in _heavy_positions(command):
        return label
    return None


def _heavy_positions(command: str) -> list[tuple[int, str]]:
    if not _CANDIDATE.search(command):
        return []
    found: list[tuple[int, str]] = []
    for segment in _simple_commands(command):
        words = [_unquote(raw) for _, raw in segment]
        start = _command_start([w or "" for w in words])
        if start >= len(words) or any(w is None for w in words[start:]):
            continue  # unparseable word in the command itself: leave it alone
        label = classify_words(words[start:])  # type: ignore[arg-type]
        if label:
            found.append((segment[start][0], label))
    return found


def _heavy_slot_executable(environment: Mapping[str, str]) -> Optional[str]:
    """The host helper: the terminal's PATH first, then ``~/.local/bin`` (absent from minimal service PATHs)."""
    from hermes_platform.resolver.core import LookupContext, locate_command

    found = locate_command("heavy-slot", LookupContext(path=environment.get("PATH")),
                           known_dirs=(str(Path.home() / ".local" / "bin"),))
    return found.command[0] if found.command else None


def wrap_heavy_command(command: str, *, env_type: str, environment: Optional[Mapping[str, str]] = None) -> str:
    """Prefix each heavy simple command in a local terminal command with ``heavy-slot``."""
    if env_type != "local" or not command:
        return command
    try:
        return _wrap_local(command, environment)
    except Exception:  # throttling is best-effort and must never break the terminal
        logger.debug("heavy-slot wrapping failed; running the command unwrapped", exc_info=True)
        return command


def _wrap_local(command: str, environment: Optional[Mapping[str, str]]) -> str:
    effective = dict(os.environ)
    if environment:
        effective.update(environment)
    if not heavy_slot_enabled(effective):
        return command
    positions = _heavy_positions(command)
    if not positions:
        return command
    executable = _heavy_slot_executable(effective)
    if executable is None:
        return command
    rewritten = command
    for offset, label in reversed(positions):
        rewritten = rewritten[:offset] + f"{_DISPATCH} {shlex.quote('hermes ' + label)} " + rewritten[offset:]
    return _dispatcher(executable) + rewritten


def _dispatcher(executable: str) -> str:
    """Shell function the rewritten commands call.

    A name the session shell resolves itself (alias, function, builtin) runs exactly as
    before: the user's own wrapper decides whether and how to throttle. Only an external
    program goes through the helper, labelled with the cwd it runs in. Prefix assignments
    (``FOO=1 pytest``) reach either path because the shell applies them to the call.
    """
    resolve = '$(type -t -- "$1" 2>/dev/null || whence -w -- "$1" 2>/dev/null)'  # bash, then zsh
    shell_owned = 'alias|function|builtin|keyword|*": alias"|*": function"|*": builtin"|*": reserved"'
    run_in_shell = '_hermes_hs_name=$1; shift; eval "$_hermes_hs_name \\"\\$@\\""'
    run_in_slot = f'{shlex.quote(executable)} --label "$_hermes_hs_label (in $PWD)" -- "$@"'
    return (f'{_DISPATCH}() {{ _hermes_hs_label=$1; shift; case "{resolve}" in '
            f'{shell_owned}) {run_in_shell} ;; *) {run_in_slot} ;; esac; }}\n')
