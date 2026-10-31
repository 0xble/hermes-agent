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
runner (``grep pytest``, quoted strings, comments, heredoc bodies), targeted
runs of 1-10 explicit test files or unittest names with only single-run options
(redirections such as ``2>&1 | tail`` do not count as arguments), dependency
sync/installation, and type checks or lint runs, which hold a slot for seconds
(see ``maintenance/terminal-heavy-slot.md``).
Foreground terminal commands leave admission unbounded and hand the live shell
to the background registry at their foreground budget only while waiting for a
slot. Already-admitted work keeps its foreground deadline. Queue age survives and
compound shells never replay earlier side effects. Gate routing remains unchanged.
"""

from __future__ import annotations

import logging
import os
import re
import shlex
import tempfile
from contextlib import contextmanager
from collections.abc import Collection, Mapping
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_OPT_OUT_VALUES = {"0", "off", "false", "no"}
# One leading underscore: the session snapshot re-dump skips `_x` functions, so the
# dispatcher is redefined by each rewritten command and never persists.
_DISPATCH = "_hermes_heavy_slot"
_ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
# Words that put the NEXT word in command position and work with shell functions.
_KEYWORDS = {"if", "then", "else", "elif", "do", "while", "until", "!", "{", "time"}
# External prefix utilities: they exec their argument, so the dispatcher goes before them.
_EXEC_PREFIXES = {"env", "nice", "timeout", "gtimeout", "nohup"}
# Cheap prefilter: every heavy form contains one of these, so most commands skip parsing.
_CANDIDATE = re.compile(
    r"test|jest|bin/ci|tox|nox|check|lint|sync|install|evals|compileall|build|tsc|"
    r"npm|pnpm|yarn|bun|turbo|nx|lerna|uv|poetry|pipenv|hatch|pdm|rye"
)
_SHELLS = {"bash", "sh", "zsh"}
_PY_RUNNERS = {"pytest", "py.test"}
_JS_RUNNERS = {"vitest", "jest"}
# Package test/build/check scripts can fan out expensive work.
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
_INFO_FLAGS = {"--version", "--help", "-h"}
_WATCH_FLAGS = {"--watch", "--watch=true", "--watchAll", "--watchAll=true"}
# Options a targeted run may carry and still count as light. This is an allowlist on
# purpose: any other option (pytest -n/--dist, jest --maxWorkers, vitest --pool, a
# runner's own --workers or --jobs) may fan out, so it falls back to the slot.
_TARGETED_FLAGS = {"-q", "-qq", "-v", "-vv", "-vvv", "-x", "-s", "-l", "--quiet", "--verbose", "--exitfirst",
                   "--no-header", "--lf", "--ff", "--last-failed", "--failed-first", "--sw", "--stepwise",
                   "--run", "--no-watch", "--bail", "--silent", "--color", "--no-color", "--showlocals",
                   "--no-cov", "--disable-warnings", "--collect-only", "--co"}
_TARGETED_VALUE_FLAGS = {"-k", "-m", "-p", "-o", "-W", "-r", "--tb", "-t", "--testNamePattern",
                         "--file-timeout", "--file-retries", "--maxfail", "--junitxml", "--junit-xml",
                         "--durations", "--timeout", "--basetemp", "--rootdir", "--import-mode"}
# Short value options may carry their value attached: `-rA`, `-kfoo`, `-pno:cacheprovider`.
_TARGETED_ATTACHED = {flag for flag in _TARGETED_VALUE_FLAGS if len(flag) == 2}
_TEST_FILE = re.compile(r"(\.py(::.*)?|\.(test|spec)\.[cm]?[jt]sx?)$")
_MAX_TARGETED_FILES = 10
# unittest takes dotted test names, not paths: `python -m unittest test_a pkg.test_b.Case.test_x`.
_UNITTEST_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)*$")
_UNITTEST_FLAGS = {"-v", "-q", "-f", "-c", "-b", "--verbose", "--quiet", "--failfast", "--catch", "--buffer",
                   "--locals", "--durations"}
# Redirection words (`2>`, `>out`, `<<EOF`, `<<<`); group 1 is a target attached to the operator.
_REDIRECT = re.compile(r"^(?:\d+|\{[A-Za-z_][A-Za-z0-9_]*\})?(?:>>|>|<<<|<<-|<<|<>|<)(.*)$")


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
            if (arg in _TARGETED_FLAGS or (name != arg and name in _TARGETED_VALUE_FLAGS | {"--tb", "--color"})
                    or (not arg.startswith("--") and arg[:2] in _TARGETED_ATTACHED)):
                continue
            return False  # unknown option: may add workers, so keep the slot
        if not _TEST_FILE.search(arg):
            return False  # a directory, `run` subcommand target set, or anything broad
        files += 1
    return 1 <= files <= _MAX_TARGETED_FILES


def _is_targeted_unittest(args: list[str]) -> bool:
    """1-10 dotted test names, or ``discover`` limited to one literal file pattern."""
    if args[:1] == ["discover"]:
        pattern = None
        skip = None
        for arg in args[1:]:
            if skip:
                if skip in {"-p", "--pattern"}:
                    pattern = arg
                skip = None
            elif arg in {"-s", "--start-directory", "-p", "--pattern", "-t", "--top-level-directory", "-k"}:
                skip = arg
            elif arg.startswith("--pattern="):
                pattern = arg.split("=", 1)[1]
            elif arg not in _UNITTEST_FLAGS:
                return False
        # The default pattern `test*.py` imports every test module under the start directory.
        return pattern is not None and not any(char in pattern for char in "*?[")
    names = 0
    skip_value = False
    for arg in args:
        if skip_value:
            skip_value = False
        elif arg == "-k":
            skip_value = True
        elif arg.startswith("-"):
            if arg not in _UNITTEST_FLAGS:
                return False
        elif _UNITTEST_NAME.match(arg) or _TEST_FILE.search(arg):
            names += 1
        else:
            return False
    return 1 <= names <= _MAX_TARGETED_FILES


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


def _script_label(word: str) -> Optional[str]:
    # `lint` and `typecheck` scripts are deliberately absent: they release a slot in seconds.
    prefix = word.split(":", 1)[0]
    return prefix if prefix in {"test", "build", "check"} else None


def _is_watch_script(word: str) -> bool:
    return word == "watch" or word.endswith(":watch")


def _heavy_script(args: list[str]) -> Optional[str]:
    """Ignore package names and watch modes when finding a heavy package script."""
    if any(arg in _WATCH_FLAGS for arg in args):
        return None
    skip = False
    for arg in args:
        if skip:
            skip = False
        elif arg in _PACKAGE_VALUE_OPTIONS:
            skip = True  # `pnpm --filter test build`: `test` names a package
        elif not arg.startswith("-"):
            if _is_watch_script(arg):
                return None
            if label := _script_label(arg):
                return label
    return None


def _classify_python(args: list[str]) -> Optional[str]:
    if args[:1] == ["-m"] and len(args) > 1:
        module, rest = args[1], args[2:]
        if module in _PY_RUNNERS:
            return None if _is_targeted(rest) else "pytest"
        if module == "unittest":
            return None if _is_targeted_unittest(rest) else "unittest"
        if module in {"compileall", "build"}:
            return module
    if args and _basename(args[0]) == "run_tests_parallel.py":
        return "run_tests"
    if args and args[0].endswith("pm/launch.py") and args[1:2] == ["install"]:
        return "pm-install"
    return None


def _classify_launchers(executable: str, args: list[str]) -> Optional[str]:
    name = _basename(executable)
    if name.startswith("python"):
        return _classify_python(args)
    if name in {"node", "nodejs"} and args:
        return {"run-workspace-checks.mjs": "workspace-checks", "check.js": "check",
                "check.mjs": "check"}.get(_basename(args[0]))
    if name == "agentkit" and args[:1] == ["evals"] and args[1:2] in (["run"], ["admit"]):
        return f"evals-{args[1]}"
    if name == "go" and args[:1] in (["test"], ["build"]):
        return f"go-{args[0]}"
    if name == "check" and executable.endswith("bin/check"):
        return None if args[:1] in (["--help"], ["-h"], ["--list"]) else "check"
    # Dependency installs, `tsc` and `eslint` are not wrapped: they release a slot in seconds.
    if name == "playwright" and args[:1] == ["test"]:
        return "playwright"
    return None


def _classify_package(name: str, args: list[str]) -> Optional[str]:
    sub, rest = _first_positional(args, _PACKAGE_VALUE_OPTIONS)
    if any(word in _PACKAGE_MANAGEMENT for word in [sub, *rest][:3]) or sub == "ci":
        return None  # dependency installation and package metadata, never a script run
    if name == "bun" and sub == "test":
        return None if _is_targeted(rest) else "test"  # Bun's own test runner, not a package script
    if sub in {"exec", "dlx"}:
        inner, inner_rest = _first_positional(rest)
        return classify_words([inner, *inner_rest]) if inner else None
    return _heavy_script(args)


def _classify_environment(name: str, args: list[str]) -> Optional[str]:
    sub, rest = _first_positional(args, {"--project", "--directory", "--python", "-p", "--with", "--group",
                                        "--extra", "--package", "--env", "-e"})
    if sub == "run":
        inner, inner_rest = _first_positional(rest, {"--with", "--group", "--extra", "--package", "--python",
                                                   "-p", "--env", "-e", "--directory", "--project"})
        return classify_words([inner, *inner_rest]) if inner else None
    return None  # `sync`, `install`, `add` and `pip install` only realize the environment


def classify_words(words: list[str]) -> Optional[str]:
    """Label for a heavy simple command given its unquoted words (command first), else ``None``."""
    if not words:
        return None
    executable, args = words[0], words[1:]
    name = _basename(executable)
    if any(arg in _INFO_FLAGS for arg in args):
        return None
    if name in {"tsc", "vitest", "jest"} and any(
        arg in _WATCH_FLAGS or (arg == "-w" and name in {"tsc", "vitest"}) for arg in args
    ):
        return None

    if name in _SHELLS and args:
        if args[0].startswith("-") and "c" in args[0] and len(args) > 1:
            return classify_command(args[1])  # whole `bash -c '...'` payload runs in the slot
        if not args[0].startswith("-"):
            return classify_words(args)  # `bash scripts/run_tests.sh ...`
        return None
    if name in _PY_RUNNERS:
        return None if _is_targeted(args) else "pytest"
    launcher = _classify_launchers(executable, args)
    if launcher:
        return launcher
    if name in _JS_RUNNERS:
        rest = args[1:] if args[:1] in (["run"], ["related"]) else args
        return None if _is_targeted(rest) else name
    if name == "run_tests.sh":
        return "run_tests"  # always compiles the whole checkout, even with one test file
    if name in _SUITE_RUNNERS:
        return name
    if name in _ENV_RUNNERS:
        return _classify_environment(name, args)
    if name in _EXEC_RUNNERS:
        inner, rest = _first_positional(args, {"--package", "-p", "--from", "--with"})
        return classify_words([inner, *rest]) if inner else None
    if name in _PACKAGE_MANAGERS:
        return _classify_package(name, args)
    if name in _MONOREPO_RUNNERS:
        return _heavy_script(args)
    if executable.endswith("bin/ci") and _basename(executable) == "ci":
        profile = args[0] if args else "full"
        return None if profile in _LIGHT_CI_PROFILES else f"ci-{profile}"
    # git-guard's `ci-gate` takes its own slot around the repository gate (with a
    # better label), so it is deliberately not wrapped here.
    return None


def _command_start(words: list[str]) -> tuple[int, Optional[int]]:
    """``(runner, insert)``: index of the word that names the program, and where the dispatcher
    goes. ``insert`` is the first exec-style prefix utility (``env``, ``nice``, ``timeout``,
    ``nohup``), which can only exec a program, never a shell function, so the dispatcher must
    run them. ``None`` means the command must not be wrapped: ``command`` deliberately
    bypasses shell functions and ``exec`` replaces the session shell."""
    index = 0
    insert: Optional[int] = None
    while index < len(words):
        word = words[index]
        if word in {"command", "exec"}:
            return index, None
        if insert is None and word in _EXEC_PREFIXES:
            insert = index
        if _ASSIGNMENT.match(word) or word in _KEYWORDS:
            index += 1
        elif word == "nohup":
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
            break
    return index, (index if insert is None else insert)


def _simple_commands(command: str) -> list[list[tuple[int, str]]]:
    """Top-level simple commands as ``[(offset, raw_word), ...]``; heredoc bodies and redirections are skipped."""
    from tools.self_repo_guard import _mask_heredocs
    from tools.terminal_tool_sudo import _scan_shell

    masked, _ = _mask_heredocs(command)  # same length: body characters become spaces
    segments: list[list[tuple[int, str]]] = [[]]
    events = list(_scan_shell(masked))
    target = False  # the next word is a redirection target, not an argument
    for index, (kind, start, end, _at_start) in enumerate(events):
        text = masked[start:end]
        if kind == "op" and text == "&":
            # `2>&1`, `>&2` and `&>log` scan as `2>` `&` `1`: the `&` is part of the redirection.
            after = events[index + 1] if index + 1 < len(events) else None
            if (target and index and events[index - 1][2] == start) or (
                    after and after[0] == "word" and after[1] == end and masked[end] == ">"):
                continue
        if kind == "op" or (kind == "ws" and text == "\n"):
            segments.append([])
            target = False
        elif kind == "word":
            if target:
                target = False
            elif match := _REDIRECT.match(text):
                target = not match.group(1)
            else:
                segments[-1].append((start, text))
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
        start, insert = _command_start([w or "" for w in words])
        if start >= len(words) or any(w is None for w in words[start:]):
            continue  # unparseable word in the command itself: leave it alone
        label = classify_words(words[start:])  # type: ignore[arg-type]
        if label and insert is not None:
            found.append((segment[insert][0], label))
    return found


def _heavy_slot_executable(environment: Mapping[str, str]) -> Optional[str]:
    """The host helper: the terminal's PATH first, then ``~/.local/bin`` (absent from minimal service PATHs)."""
    from hermes_platform.resolver.core import LookupContext, locate_command

    found = locate_command("heavy-slot", LookupContext(path=environment.get("PATH")),
                           known_dirs=(str(Path.home() / ".local" / "bin"),))
    return found.command[0] if found.command else None


def wrap_heavy_command(
    command: str, *, env_type: str, environment: Optional[Mapping[str, str]] = None,
    max_wait_seconds: Optional[int] = None, admission_dir: Optional[str] = None,
) -> str:
    """Prefix heavy local commands with ``heavy-slot`` and optionally bound its queue wait."""
    if env_type != "local" or not command:
        return command
    try:
        return _wrap_local(command, environment, max_wait_seconds=max_wait_seconds, admission_dir=admission_dir)
    except Exception:  # throttling is best-effort and must never break the terminal
        logger.debug("heavy-slot wrapping failed; running the command unwrapped", exc_info=True)
        return command


def _wrap_local(
    command: str, environment: Optional[Mapping[str, str]], *, max_wait_seconds: Optional[int],
    admission_dir: Optional[str] = None,
) -> str:
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
    return _dispatcher(executable, max_wait_seconds=max_wait_seconds, admission_dir=admission_dir) + rewritten


@contextmanager
def foreground_heavy_command(command: str, *, env_type: str, environment=None):
    """Track only pending helper admission, not elapsed payload execution.

    The admitted payload removes its marker before exec. The directory lives
    only until the foreground wait returns, including adoption, so background
    commands need no cleanup callback and later dispatches skip tracking.
    """
    if env_type != "local":
        yield command, None
        return
    with tempfile.TemporaryDirectory(prefix="hermes-admission-") as directory:
        rewritten = wrap_heavy_command(command, env_type=env_type, environment=environment,
                                       admission_dir=directory)
        yield rewritten, lambda: any(Path(directory).iterdir())


def _dispatcher(executable: str, *, max_wait_seconds: Optional[int] = None,
                admission_dir: Optional[str] = None) -> str:
    """Shell function the rewritten commands call.

    A name the session shell resolves itself (alias, function, builtin) runs exactly as
    before: the user's own wrapper decides whether and how to throttle. Only an external
    program goes through the helper, labelled with the cwd it runs in. Prefix assignments
    (``FOO=1 pytest``) reach either path because the shell applies them to the call.
    """
    resolve = '$(type -t -- "$1" 2>/dev/null || whence -w -- "$1" 2>/dev/null)'  # bash, then zsh
    shell_owned = 'alias|function|builtin|keyword|*": alias"|*": function"|*": builtin"|*": reserved"'
    run_in_shell = '_hermes_hs_name=$1; shift; eval "$_hermes_hs_name \\"\\$@\\""'
    wait_env = ""
    if max_wait_seconds is not None:
        wait_env = f"HEAVY_SLOT_MAX_WAIT={shlex.quote(str(max_wait_seconds))} "
    payload = '"$@"'
    track = ""
    if admission_dir is not None:
        # Per-invocation files also cover pipelines and repeated heavy commands.
        # mktemp may fail after adoption removed the directory. In that case the
        # payload still runs normally and does not recreate tracking state.
        template = shlex.quote(str(Path(admission_dir) / "wait.XXXXXXXX"))
        # The admission trampoline hides argv from the helper's classifier.
        # Preserve its exact-head bin/ci eligibility via its existing label form.
        # Prefix utilities remain ordinary work, just as without the trampoline.
        gate_label = (
            '_hermes_hs_gate=$1; _hermes_hs_profile=${2-}; _hermes_hs_sha=${3-${CI_EXPECTED_SHA-}}; '
            'case "${1##*/}" in bash|sh|zsh) _hermes_hs_gate=${2-}; '
            '_hermes_hs_profile=${3-}; _hermes_hs_sha=${4-${CI_EXPECTED_SHA-}} ;; esac; '
            'case "$_hermes_hs_gate" in bin/ci|*/bin/ci) '
            'case "$_hermes_hs_profile" in gate|ready) '
            'if [ "${#_hermes_hs_sha}" -eq 40 ]; then '
            'case "$_hermes_hs_sha" in *[!0-9a-f]*) ;; *) '
            '_hermes_hs_label="bin/ci $_hermes_hs_profile $_hermes_hs_sha" ;; esac; fi ;; esac ;; esac; '
        )
        track = gate_label + f'_hermes_hs_wait=$(mktemp {template} 2>/dev/null) || _hermes_hs_wait=; '
        admitted = shlex.quote('rm -f -- "$1"; shift; exec "$@"')
        payload = f'sh -c {admitted} hermes-admitted "$_hermes_hs_wait" "$@"'
    run_in_slot = f'{wait_env}{shlex.quote(executable)} --label "$_hermes_hs_label (in $PWD)" -- {payload}'
    if admission_dir is not None:
        # A helper failing before admission must not leave a pending marker.
        run_in_slot = (f'{track}if {run_in_slot}; then _hermes_hs_rc=0; else _hermes_hs_rc=$?; fi; '
                       'rm -f -- "$_hermes_hs_wait"; return "$_hermes_hs_rc"')
    return (f'{_DISPATCH}() {{ _hermes_hs_label=$1; shift; case "{resolve}" in '
            f'{shell_owned}) {run_in_shell} ;; *) {run_in_slot} ;; esac; }}\n')
