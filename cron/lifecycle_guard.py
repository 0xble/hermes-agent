"""Gateway lifecycle guard for cron job creation (#30719).

An agent running inside a gateway can schedule a cron job that calls
``hermes gateway restart`` (or ``launchctl kickstart ai.hermes.gateway``
or ``systemctl restart hermes-gateway``).  When the cron fires, the
gateway dies, the supervisor (launchd KeepAlive / systemd Restart=)
revives it, auto-resume picks up the offending session, and the resumed
turn re-runs the same logic — a SIGTERM-respawn loop every ~10 seconds
until manually broken.

This module rejects cron job specs whose prompt or script contains a
direct shell-level gateway-lifecycle command.  It is enforced at
``cron.jobs.create_job`` so it fires on every job-creation path: the
``hermes cron create`` CLI subcommand AND the agent's ``cronjob`` model
tool (which calls ``create_job`` directly, bypassing the CLI layer).

The pattern is intentionally command-shaped: it anchors on a concrete
command identifier (``hermes gateway``, ``launchctl ... hermes-gateway``,
``systemctl ... hermes-gateway``, ``pkill`` against the gateway) so it
cannot fire on prose.  A cron ``prompt`` is fed to a future LLM, not a
shell, so an over-broad substring match on English ("Kong API gateway
autoscaling and restart behavior") would produce a high false-positive
rate without preventing the actual foot-gun, which requires a real
command shape.

This is a defence-in-depth layer.  ``tools/terminal_tool.py`` blocks direct
commands and shell scripts they reference when ``_HERMES_GATEWAY=1``. It also
rejects ``launchctl submit`` in gateway sessions because launchd treats that
primitive as a persistent KeepAlive job, not a one-shot task. ``hermes gateway
stop|restart`` separately refuse to self-target from inside the gateway.
Blocking cron specs at creation time as well means the agent gets an immediate,
informative rejection instead of scheduling a job that will only fail
(silently) when it fires.
"""

from __future__ import annotations

import logging
import os
import re
import shlex
import stat
from pathlib import Path
from typing import Callable, Iterator, NamedTuple, Optional

from tools.shell_heredoc import partition_heredoc_bodies

logger = logging.getLogger(__name__)


class GatewayLifecycleBlocked(ValueError):
    """Raised when a cron job spec contains a gateway-lifecycle command."""


# Shell-level command shapes that target the gateway lifecycle. Each branch
# is anchored on a concrete command identifier so a match can only fire on
# actual shell-command-shaped strings, not on prose.
_GATEWAY_LIFECYCLE_PATTERN = re.compile(
    r"(?i)"
    # Branch A: `hermes gateway restart|stop` — the canonical foot-gun.
    # `start` is intentionally excluded: starting a gateway from inside a
    # gateway is benign (a no-op or "already running" error), and a
    # legitimate cron job might start a sibling profile's gateway.
    r"(?:hermes\s+gateway\s+(?:restart|stop))"
    # Branch B: launchctl ops on a hermes-gateway label. macOS launchd
    # labels look like `ai.hermes.gateway` / `hermes-gateway`. Requiring the
    # gateway identifier prevents blocking unrelated hermes services (e.g.
    # `launchctl unload ai.hermes.update-checker.plist`).
    # `submit` and `bootstrap` are included alongside the direct verbs
    # (kickstart/etc.): `launchctl submit -l ai.hermes.gateway-<suffix> --
    # <helper-script>` (or `launchctl bootstrap gui/<uid> <plist>`) creates
    # a NEW keepalive job wrapping an arbitrary helper, which is how a
    # blocked direct restart/kill gets laundered into a persistent restart
    # loop instead (#62891) — same foot-gun, indirect shape. Neutral-label
    # submissions that dodge this text anchor are caught separately by
    # `contains_launchctl_submit_command` (execution-aware, label-independent).
    #
    # `bootout`, `disable`, and `remove` are the *unrecoverable* verbs and
    # matter most. `unload`/`stop` merely SIGTERM the job, so launchd's
    # KeepAlive respawns the gateway ~30s later; `bootout` tears the job out
    # of the domain entirely (and `disable` marks it unloadable, `remove` is
    # the legacy spelling), so KeepAlive has nothing left to restart and the
    # gateway stays down until a human re-bootstraps it by hand. Omitting
    # them left the widest hole in this branch: an agent whose `hermes
    # gateway restart` was blocked would reach for `launchctl bootout` next
    # and take the gateway down permanently.
    r"|(?:launchctl\s+(?:kickstart|unload|load|stop|restart|submit|bootstrap|bootout|disable|remove)\b[^\n]*\bhermes[.\-]?gateway)"
    # Branch C: systemctl ops on a hermes-gateway unit.
    r"|(?:systemctl\s+(?:-\S+\s+)*(?:restart|stop|start)\b[^\n]*\bhermes[.\-]?gateway)"
    # Branch D: pkill / kill targeting the hermes gateway process. Both
    # token orders because real reproductions show both.
    r"|(?:p?kill\b[^\n]*\bhermes\b[^\n]*\bgateway)"
    r"|(?:p?kill\b[^\n]*\bgateway\b[^\n]*\bhermes)"
)


# A backslash immediately followed by a newline is a POSIX shell line
# continuation — the shell joins the two lines before parsing. Every branch
# above uses `[^\n]*` between its verb and the gateway identifier so the
# match can't span unrelated lines of a longer cron prompt/script, but that
# also means a real multi-line shell invocation split across continuation
# lines (e.g. `launchctl submit \` / `  -l ai.hermes.gateway-... \` / `  -- ...`,
# the exact reported shape in #62891) would otherwise slip past. Collapse
# continuations to a single space before matching, mirroring what the shell
# itself does, rather than loosening `[^\n]*` and risking false positives
# across genuinely separate lines.
_SHELL_LINE_CONTINUATION = re.compile(r"\\\r?\n[ \t]*")


def contains_gateway_lifecycle_command(text: str) -> bool:
    """Return True if *text* contains a gateway lifecycle command pattern."""
    if not text:
        return False
    normalized = _SHELL_LINE_CONTINUATION.sub(" ", text)
    return bool(_GATEWAY_LIFECYCLE_PATTERN.search(normalized))


_SHELL_EXECUTABLES = frozenset({"sh", "bash", "dash", "ksh", "zsh"})
_KNOWN_SHELL_INTERPRETER_DIRS = frozenset({"/bin", "/usr/bin"})
_SHELL_OPTIONS_WITH_VALUES = frozenset({"-O", "+O", "-o", "+o"})
_MAX_REFERENCED_SCRIPT_BYTES = 1024 * 1024
_MAX_REFERENCED_SCRIPT_DEPTH = 8
_CONTROL_CHARS = frozenset(";&|()")


# Directory names that sit directly under a `Library` path component and
# mark a FileProvider-backed subtree: `Mobile Documents` is iCloud Drive;
# `CloudStorage` hosts every third-party FileProvider domain (Dropbox,
# OneDrive, Google Drive, Box, ...) on modern macOS.
_CLOUD_PLACEHOLDER_MARKERS = frozenset({"Mobile Documents", "CloudStorage"})


def _is_cloud_placeholder_path(path: Path) -> bool:
    """Return True for paths inside a macOS FileProvider-backed subtree.

    ``O_NONBLOCK`` does not make regular-file reads non-blocking.  Opening an
    evicted FileProvider placeholder below ``~/Library/Mobile Documents``
    (iCloud Drive) or ``~/Library/CloudStorage`` (Dropbox / OneDrive /
    Google Drive and other third-party providers) can therefore wait
    indefinitely for hydration.  The lifecycle guard runs before a terminal
    command's timeout starts, so it must identify this boundary from path
    metadata and fail closed without opening the file.
    """
    parts = path.parts
    return any(
        parts[index - 1] == "Library" and part in _CLOUD_PLACEHOLDER_MARKERS
        for index, part in enumerate(parts)
        if index
    )

# Executables whose arguments are DATA, not commands: search patterns, SQL
# statements, log filters. None of these can execute their argument text, so
# a lifecycle-shaped string inside their arguments (a grep pattern hunting
# for `systemctl restart hermes-gateway` in syslog, a SQL LIKE literal over a
# restart-events table) is diagnostics, not a lifecycle command. Deliberately
# conservative: no `awk` (system()), no `sed` (`s///e`), no `echo`/`printf`
# (their output can rewrite a script executed later), and no `mysql`
# (`\\!` and `system` escapes).
_DATA_SINK_EXECUTABLES = frozenset(
    {
        "ack", "ag", "egrep", "fgrep", "grep", "journalctl", "psql", "rg",
        "sqlite3",
    }
)
# Argument shapes that can smuggle execution back INTO a data sink: command
# and process substitution anywhere, sqlite3 dot-commands (`.shell ...`),
# psql backslash escapes (`\! ...`). Any hit disables masking for the whole
# segment — fail closed to the plain regex verdict.
_UNSAFE_DATA_ARG_MARKERS = ("`", "$(", "<(", ">(", "\\!")
# A data sink piped into a shell/interpreter can feed matched lines straight
# to execution (`grep 'systemctl restart hermes-gateway' f | sh`); never mask
# such a line.
_PIPE_TO_INTERPRETER = re.compile(
    r"\|\s*&?\s*(?:sudo\s+)?(?:sh|bash|dash|ksh|zsh|xargs|eval|source)\b"
)

# Executable-image magic numbers: ELF, PE/COFF, Mach-O (universal + thin,
# both endiannesses). A referenced file starting with one of these is a
# compiled binary, never a shell script — don't read or scan it at all.
_BINARY_MAGIC_PREFIXES = (
    b"\x7fELF",
    b"MZ",
    b"\xca\xfe\xba\xbe",
    b"\xcf\xfa\xed\xfe",
    b"\xce\xfa\xed\xfe",
    b"\xfe\xed\xfa\xce",
    b"\xfe\xed\xfa\xcf",
)
_BINARY_SNIFF_BYTES = 4096
_LIFECYCLE_INERT_HEREDOC_CONSUMERS = frozenset()




_ReadRemoteScriptFn = Callable[[str], Optional[str]]


class _ShellToken(NamedTuple):
    text: str
    raw: str
    quoted: bool


_REDIRECTION_WORD = re.compile(
    r"^(?:\d*|\{[A-Za-z_][A-Za-z0-9_]*\})(?:<<<|<<|<>|<&|>>|>&|>\||<|>)"
    r"(?P<target>.*)$"
)
_GRAMMAR_WITH_REDIRECTION = re.compile(
    r"^(?P<grammar>\]\]|esac|done|fi|then|do|else|in)"
    r"(?P<redirect>(?:\d*)(?:<<<|<<|<>|<&|>>|>&|>\||<|>).*?)$"
)
_REDIRECTION_NEEDS_OPERAND = "__HERMES_REDIRECTION_NEEDS_OPERAND__"
_REDIRECTION_COMPLETE = "__HERMES_REDIRECTION_COMPLETE__"


def _lex_shell_line(line: str) -> list[_ShellToken]:
    """Tokenize one shell line with POSIX concatenation and raw quote provenance."""
    raw_tokens: list[str] = []
    current: list[str] = []
    single_quoted = False
    double_quoted = False
    index = 0

    def flush() -> None:
        if current:
            raw_tokens.append("".join(current))
            current.clear()

    while index < len(line):
        character = line[index]
        if single_quoted:
            current.append(character)
            if character == "'":
                single_quoted = False
            index += 1
            continue
        if double_quoted:
            current.append(character)
            if character == "\\" and index + 1 < len(line):
                index += 1
                current.append(line[index])
            elif character == '"':
                double_quoted = False
            index += 1
            continue
        if character == "\\":
            current.append(character)
            if index + 1 >= len(line):
                raise ValueError("trailing shell escape")
            index += 1
            current.append(line[index])
            index += 1
            continue
        if character == "'":
            current.append(character)
            single_quoted = True
            index += 1
            continue
        if character == '"':
            current.append(character)
            double_quoted = True
            index += 1
            continue
        if character.isspace():
            flush()
            index += 1
            continue
        if character == "#" and not current:
            break
        if character in "<>":
            descriptor = ""
            if current and (
                all(part.isdigit() for part in current)
                or re.fullmatch(
                    r"\{[A-Za-z_][A-Za-z0-9_]*\}", "".join(current)
                )
            ):
                descriptor = "".join(current)
                current.clear()
            else:
                flush()
            operator = character
            index += 1
            if index < len(line):
                next_character = line[index]
                if character == "<" and next_character == "<":
                    operator += next_character
                    index += 1
                    if index < len(line) and line[index] == "<":
                        operator += "<"
                        index += 1
                elif next_character in {">", "&", "|"}:
                    operator += next_character
                    index += 1
            raw_tokens.append(descriptor + operator)
            continue
        if character in _CONTROL_CHARS:
            flush()
            punctuation: list[str] = []
            while index < len(line) and line[index] in _CONTROL_CHARS:
                punctuation.append(line[index])
                index += 1
            raw_tokens.append("".join(punctuation))
            continue
        current.append(character)
        index += 1

    if single_quoted or double_quoted:
        raise ValueError("unclosed shell quote")
    flush()

    tokens: list[_ShellToken] = []
    for raw in raw_tokens:
        if raw and set(raw) <= _CONTROL_CHARS:
            tokens.append(_ShellToken(raw, raw, False))
            continue
        normalized = shlex.split(raw, comments=False, posix=True)
        if len(normalized) != 1:
            if not normalized:
                tokens.append(_ShellToken("", raw, True))
                continue
            raise ValueError("ambiguous shell token")
        text = normalized[0]
        quoted = text != raw
        if not quoted:
            shell_token = _ShellToken(text, raw, False)
            executes_substitution = _tokens_execute_shell_substitution([shell_token])
            grammar_match = _GRAMMAR_WITH_REDIRECTION.fullmatch(text)
            if grammar_match is not None:
                grammar = grammar_match.group("grammar")
                redirect = grammar_match.group("redirect")
                redirect_match = _REDIRECTION_WORD.fullmatch(redirect)
                tokens.append(_ShellToken(grammar, grammar, False))
                marker = (
                    _REDIRECTION_NEEDS_OPERAND
                    if redirect_match is not None and not redirect_match.group("target")
                    else _REDIRECTION_COMPLETE
                )
                tokens.append(_ShellToken(marker, redirect, False))
                if executes_substitution:
                    tokens.append(_ShellToken("$(", raw, False))
                continue
            redirect_match = _REDIRECTION_WORD.fullmatch(text)
            if redirect_match is not None:
                marker = (
                    _REDIRECTION_NEEDS_OPERAND
                    if not redirect_match.group("target")
                    else _REDIRECTION_COMPLETE
                )
                tokens.append(_ShellToken(marker, raw, False))
                if executes_substitution:
                    tokens.append(_ShellToken("$(", raw, False))
                continue
        tokens.append(_ShellToken(text, raw, quoted))
    return tokens

def _tokens_execute_shell_substitution(tokens: list[_ShellToken]) -> bool:
    """Return whether shell evaluation of *tokens* can execute another command."""
    source_parts: list[str] = []
    for token in tokens:
        if source_parts and (
            source_parts[-1].endswith("${") or source_parts[-1].endswith("${|")
        ):
            source_parts.append(" ")
        source_parts.append(token.raw)
    source = "".join(source_parts)
    single_quoted = False
    double_quoted = False
    index = 0
    while index < len(source):
        character = source[index]
        if character == "\\" and not single_quoted:
            index += 2
            continue
        if character == "'" and not double_quoted:
            single_quoted = not single_quoted
            index += 1
            continue
        if character == '"' and not single_quoted:
            double_quoted = not double_quoted
            index += 1
            continue
        if not single_quoted:
            if character == "`":
                return True
            if source.startswith("$((", index):
                index += 3
                continue
            if source.startswith("$(", index):
                return True
            if source.startswith("${", index):
                command_index = index + 2
                if command_index < len(source) and source[command_index] == "|":
                    command_index += 1
                if command_index < len(source) and source[command_index].isspace():
                    return True
            if not double_quoted and source.startswith(("<(", ">(", "=("), index):
                return True
        index += 1
    return False


def _iter_command_segments(command: str) -> Iterator[list[str]]:
    """Yield executable shell segments while discarding inert shell grammar."""
    normalized = command.replace("\\\n", "")
    bash_grammar = True
    shell_grammar = True
    first_line = normalized.partition("\n")[0]
    if first_line.startswith("#!"):
        try:
            interpreter_tokens = shlex.split(first_line[2:])
        except ValueError:
            interpreter_tokens = []
        interpreter_names = {Path(token).name for token in interpreter_tokens}
        shell_grammar = bool(interpreter_names & _SHELL_EXECUTABLES)
        bash_grammar = any(
            _is_known_shell_interpreter(token)
            and Path(token).name in {"bash", "zsh", "ksh"}
            for token in interpreter_tokens
        )
    case_depth = 0
    awaiting_case_in = False
    case_header_tokens: list[_ShellToken] = []
    expecting_case_pattern = False
    awaiting_loop_do = False
    loop_header_tokens: list[_ShellToken] = []
    awaiting_time_command = False
    in_arithmetic_command = False
    arithmetic_tokens: list[_ShellToken] = []
    in_double_bracket = False
    double_bracket_tokens: list[_ShellToken] = []

    def control(value: _ShellToken) -> bool:
        return (
            bool(value.text)
            and not value.quoted
            and set(value.text) <= _CONTROL_CHARS
        )

    synthetic_separator = _ShellToken(";", ";", False)
    synthetic_unresolved = _ShellToken("$(", "$(", False)
    synthetic_true = _ShellToken("true", "true", False)

    def separate(filtered: list[_ShellToken]) -> None:
        if not filtered or not control(filtered[-1]):
            filtered.append(synthetic_separator)

    if not shell_grammar:
        for line in normalized.splitlines() or [normalized]:
            try:
                tokens = _lex_shell_line(line)
            except ValueError:
                continue
            segment: list[str] = []
            for token in tokens:
                if control(token):
                    if segment:
                        yield segment
                        segment = []
                    continue
                segment.append(token.text)
            if segment:
                yield segment
        return

    for line in normalized.splitlines() or [normalized]:
        try:
            tokens = _lex_shell_line(line)
        except ValueError:
            if (
                case_depth
                or awaiting_case_in
                or expecting_case_pattern
                or awaiting_loop_do
                or awaiting_time_command
                or in_arithmetic_command
                or in_double_bracket
            ):
                yield ["$("]
                case_depth = 0
                awaiting_case_in = False
                case_header_tokens = []
                expecting_case_pattern = False
                awaiting_loop_do = False
                loop_header_tokens = []
                awaiting_time_command = False
                in_arithmetic_command = False
                arithmetic_tokens = []
                in_double_bracket = False
                double_bracket_tokens = []
            continue

        filtered: list[_ShellToken] = []
        command_start = True
        index = 0
        while index < len(tokens):
            token = tokens[index]

            if awaiting_time_command:
                if not token.quoted and token.text.startswith("-"):
                    if _tokens_execute_shell_substitution([token]):
                        separate(filtered)
                        filtered.append(synthetic_unresolved)
                        separate(filtered)
                    index += 1
                    continue
                awaiting_time_command = False
                command_start = True

            if in_arithmetic_command:
                arithmetic_tokens.append(token)
                if not token.quoted and "))" in token.text:
                    if _tokens_execute_shell_substitution(arithmetic_tokens):
                        separate(filtered)
                        filtered.append(synthetic_unresolved)
                        separate(filtered)
                    else:
                        filtered.append(synthetic_true)
                    in_arithmetic_command = False
                    arithmetic_tokens = []
                    command_start = False
                index += 1
                continue

            if token.text == "((" and not token.quoted and command_start:
                in_arithmetic_command = True
                arithmetic_tokens = []
                index += 1
                continue

            if in_double_bracket:
                if token.text == "]]" and not token.quoted:
                    if _tokens_execute_shell_substitution(double_bracket_tokens):
                        # Test operands are inert except for explicit command or
                        # process substitution. Keep those forms fail-closed.
                        filtered.extend([synthetic_separator, synthetic_unresolved])
                    double_bracket_tokens = []
                    in_double_bracket = False
                    command_start = False
                else:
                    double_bracket_tokens.append(token)
                index += 1
                continue

            if (
                token.text == "[["
                and not token.quoted
                and command_start
                and bash_grammar
            ):
                # ``[[ ... ]]`` operands are patterns and values, not command
                # positions. Replace the compound command with a neutral
                # executable token so `||` inside it cannot split a glob or
                # parameter expansion into a fake command segment.
                filtered.append(synthetic_true)
                in_double_bracket = True
                double_bracket_tokens = []
                command_start = False
                index += 1
                continue

            if expecting_case_pattern:
                if token.text == "esac" and not token.quoted:
                    case_depth = max(0, case_depth - 1)
                    expecting_case_pattern = False
                    separate(filtered)
                    command_start = True
                    index += 1
                    continue

                pattern: list[_ShellToken] = []
                while index < len(tokens):
                    token = tokens[index]
                    pattern.append(token)
                    index += 1
                    if ")" in token.text and not token.quoted:
                        break
                if _tokens_execute_shell_substitution(pattern):
                    # Shell command/process substitution inside a case pattern
                    # executes while matching. Preserve fail-closed behavior by
                    # surfacing an unresolved executable marker to the caller.
                    filtered.extend([synthetic_separator, synthetic_unresolved])
                if pattern and ")" in pattern[-1].text and not pattern[-1].quoted:
                    expecting_case_pattern = False
                    separate(filtered)
                    command_start = True
                continue

            if awaiting_case_in:
                case_header_tokens.append(token)
                if token.text == "in" and not token.quoted:
                    if _tokens_execute_shell_substitution(case_header_tokens):
                        filtered.extend([synthetic_separator, synthetic_unresolved])
                    awaiting_case_in = False
                    case_header_tokens = []
                    case_depth += 1
                    expecting_case_pattern = True
                    command_start = True
                index += 1
                continue

            if awaiting_loop_do:
                loop_header_tokens.append(token)
                if token.text == "do" and not token.quoted:
                    if _tokens_execute_shell_substitution(loop_header_tokens):
                        filtered.extend([synthetic_separator, synthetic_unresolved])
                    awaiting_loop_do = False
                    loop_header_tokens = []
                    separate(filtered)
                    command_start = True
                index += 1
                continue

            if token.text in {
                _REDIRECTION_NEEDS_OPERAND,
                _REDIRECTION_COMPLETE,
            }:
                needs_operand = token.text == _REDIRECTION_NEEDS_OPERAND
                index += 1
                if (
                    needs_operand
                    and index < len(tokens)
                    and not control(tokens[index])
                    and tokens[index].text != "$("
                ):
                    operand = tokens[index]
                    if _tokens_execute_shell_substitution([operand]):
                        separate(filtered)
                        filtered.append(synthetic_unresolved)
                        separate(filtered)
                    index += 1
                continue

            if token.text == "$(" and not token.quoted:
                separate(filtered)
                filtered.append(token)
                separate(filtered)
                command_start = True
                index += 1
                continue

            if token.text == "case" and not token.quoted and command_start:
                awaiting_case_in = True
                case_header_tokens = []
                separate(filtered)
                command_start = True
                index += 1
                continue

            if (
                (
                    token.text == "for"
                    or (token.text == "select" and bash_grammar)
                )
                and not token.quoted
                and command_start
            ):
                awaiting_loop_do = True
                loop_header_tokens = []
                separate(filtered)
                command_start = True
                index += 1
                continue

            if token.text == "time" and not token.quoted and command_start:
                awaiting_time_command = True
                separate(filtered)
                command_start = True
                index += 1
                continue

            if token.text == "coproc" and not token.quoted and command_start:
                separate(filtered)
                index += 1
                compound_starters = {
                    "if",
                    "while",
                    "until",
                    "for",
                    "select",
                    "case",
                    "{",
                    "(",
                }
                if (
                    index + 1 < len(tokens)
                    and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", tokens[index].text)
                    and tokens[index + 1].text in compound_starters
                ):
                    index += 1
                command_start = True
                continue

            if token.text == "esac" and not token.quoted and case_depth:
                case_depth -= 1
                separate(filtered)
                command_start = True
                index += 1
                continue

            if (
                not token.quoted
                and (
                    token.text in {"{", "}"}
                    or (
                        command_start
                        and token.text
                        in {
                            "if",
                            "elif",
                            "while",
                            "until",
                            "then",
                            "do",
                            "else",
                            "fi",
                            "done",
                            "!",
                            "function",
                        }
                    )
                )
            ):
                separate(filtered)
                command_start = True
                index += 1
                continue

            filtered.append(token)
            if (
                case_depth
                and not token.quoted
                and token.text in {";;", ";&", ";;&"}
            ):
                expecting_case_pattern = True
                command_start = True
            elif control(token):
                command_start = True
            else:
                command_start = False
            index += 1

        segment: list[str] = []
        for token in filtered:
            if control(token):
                if segment:
                    yield segment
                    segment = []
                continue
            segment.append(token.text)
        if segment:
            yield segment

    if (
        case_depth
        or awaiting_case_in
        or expecting_case_pattern
        or awaiting_loop_do
        or awaiting_time_command
        or in_arithmetic_command
        or in_double_bracket
    ):
        yield ["$("]


def _command_token_index(segment: list[str]) -> Optional[int]:
    """Return the executable token index after simple env assignments."""
    for index, token in enumerate(segment):
        if re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", token):
            continue
        return index
    return None


def contains_launchctl_submit_command(command: str) -> bool:
    """Detect an executed ``launchctl submit``/``bootstrap``, not quoted text.

    Label-independent by design: the label of a submitted/bootstrapped job is
    chosen by whoever writes it, so a neutral name (``ai.hermes.svc-reload-tmp``)
    defeats any label-anchored regex (#62891, second reproduction). Both verbs
    register a NEW persistent launchd job (``submit`` jobs get KeepAlive
    semantics; ``bootstrap`` loads an arbitrary plist), which is never safe to
    do from inside the gateway process.
    """
    for segment in _iter_command_segments(command):
        index = _command_token_index(segment)
        if index is None:
            continue
        if Path(segment[index]).name == "launchctl":
            arguments = segment[index + 1 :]
            if arguments and arguments[0].lower() in {"submit", "bootstrap"}:
                return True
    return False


def _mask_data_sink_arguments(text: str) -> str:
    """Replace data-sink executables' arguments with a neutral placeholder.

    The lifecycle regex is command-shaped, but it cannot tell an EXECUTED
    ``systemctl restart hermes-gateway`` from the same characters appearing
    as *data* — a grep/rg pattern, a journalctl filter, a SQL string literal
    passed to sqlite3/psql. Those diagnostics commands were being rejected
    (false positives blocking legitimate cron prompts), e.g.::

        grep -c 'systemctl restart hermes-gateway' /var/log/syslog
        sqlite3 db "SELECT msg FROM log WHERE msg LIKE '%systemctl restart hermes-gateway%'"

    This masker shell-tokenizes each line and, for command segments whose
    executable is a known data sink (``_DATA_SINK_EXECUTABLES``), replaces
    every argument with ``arg``. The caller then re-runs the lifecycle regex
    on the masked text: a match that survives masking sits OUTSIDE any data
    argument and is a real command.

    Strictly fail-closed: masking is skipped (leaving the original,
    regex-matching text in place) whenever the line pipes into a shell or
    interpreter, any argument carries an execution-capable marker
    (substitution, sqlite3 ``.``-commands, psql ``\\!``), or the line cannot
    be tokenized at all. Masking can therefore only ever ALLOW a command the
    plain regex would have blocked — never block one it would have allowed —
    so it runs solely as a second-pass exemption check.
    """
    lines_out: list[str] = []
    changed = False
    for line in text.splitlines() or [text]:
        if _PIPE_TO_INTERPRETER.search(line):
            lines_out.append(line)
            continue
        try:
            lexer = shlex.shlex(line, posix=True, punctuation_chars=";&|()")
            lexer.whitespace_split = True
            lexer.commenters = "#"
            tokens = list(lexer)
        except ValueError:
            lines_out.append(line)
            continue

        segments: list[list[str]] = []
        current: list[str] = []
        for token in tokens:
            if token and set(token) <= _CONTROL_CHARS:
                segments.append(current)
                segments.append([token])
                current = []
                continue
            current.append(token)
        segments.append(current)

        rebuilt: list[str] = []
        for segment in segments:
            if not segment:
                continue
            index = _command_token_index(segment)
            executable_name = Path(segment[index]).name if index is not None else ""
            arguments = segment[index + 1 :] if index is not None else []
            inert_command_lookup = (
                executable_name == "command"
                and bool(arguments)
                and arguments[0] in {"-v", "-V"}
            )
            if index is not None and (
                executable_name in _DATA_SINK_EXECUTABLES or inert_command_lookup
            ):
                if not any(
                    argument.startswith(".")
                    or any(marker in argument for marker in _UNSAFE_DATA_ARG_MARKERS)
                    for argument in arguments
                ):
                    changed = True
                    rebuilt.extend(segment[: index + 1])
                    rebuilt.extend("arg" for _ in arguments)
                    continue
            rebuilt.extend(segment)
        lines_out.append(" ".join(rebuilt))
    if not changed:
        return text
    return "\n".join(lines_out)


def _lifecycle_command_scan_with_data_exemption(text: str) -> bool:
    """Lifecycle-regex scan that exempts matches living inside data arguments.

    Two-pass: the cheap regex first (the overwhelmingly common no-match case
    pays nothing extra); on a raw match, re-scan with data-sink arguments
    masked out. Only a match that survives masking — i.e. one in actual
    command position — blocks.
    """
    if not contains_gateway_lifecycle_command(text):
        return False
    normalized = _SHELL_LINE_CONTINUATION.sub(" ", text)
    return contains_gateway_lifecycle_command(_mask_data_sink_arguments(normalized))


def _direct_lifecycle_scan(command: str) -> bool:
    """Pure-string direct scans: lifecycle regex (data-exempted) + submit."""
    return _lifecycle_command_scan_with_data_exemption(
        command
    ) or contains_launchctl_submit_command(command)


def _expand_candidate_path(candidate: str) -> Optional[Path]:
    """Sanitize a tokenized path candidate at the ingestion boundary.

    Candidate tokens come from shlex-splitting arbitrary command text —
    including text recursively decoded from binaries or remote reads — so
    they can carry NUL bytes or other junk no real filesystem path can
    contain. Every OS-facing ``Path`` operation downstream (``expanduser``,
    ``os.open``, ``resolve``) raises a *different* exception for the same
    junk (``ValueError: embedded null byte``, ``RuntimeError: Could not
    determine home directory`` when HOME is unset under launchd, OSError
    for over-long paths). Rejecting here — once, before any OS call — is
    the whole-class fix; catching per-syscall was the whack-a-mole that
    produced #76762, #77703, #77780, and #78256.

    Returns ``None`` for candidates that cannot be a real path (nothing to
    scan), otherwise the ``expanduser()``-expanded ``Path``.
    """
    if not candidate or "\x00" in candidate:
        return None
    try:
        return Path(candidate).expanduser()
    except (ValueError, RuntimeError, OSError):
        return None


def _resolve_terminal_script_path(candidate: str, cwd: Optional[str]) -> Optional[Path]:
    path = _expand_candidate_path(candidate)
    if path is None:
        return None
    if not path.is_absolute():
        try:
            path = Path(cwd or Path.cwd()) / path
        except OSError:
            # Path.cwd() can raise when the process cwd was deleted.
            return None
    return path


class _UnresolvedExecutableReference(Exception):
    """A shell-expanded executable reference cannot be inspected statically."""


def _has_unresolved_shell_expansion(candidate: str) -> bool:
    return any(character in candidate for character in ("$", "`", "*", "?", "["))


def _is_safe_unresolved_test_executable(executable: str, _arguments: list[str]) -> bool:
    """Allow the POSIX test builtin token; dynamic executables stay fail-closed."""
    return executable == "["


def _resolve_executable_reference(
    candidate: str,
    cwd: Optional[str],
    *,
    fail_on_unresolved: bool,
    remote: bool,
) -> Optional[Path]:
    if _has_unresolved_shell_expansion(candidate):
        if fail_on_unresolved:
            raise _UnresolvedExecutableReference(candidate)
        return None
    if remote:
        path = Path(candidate)
        if candidate.startswith("~") or path.is_absolute():
            return path
        return Path(cwd or ".") / path
    return _resolve_terminal_script_path(candidate, cwd)


def _is_known_shell_interpreter(executable: str) -> bool:
    path = Path(executable)
    return (
        path.is_absolute()
        and path.name in _SHELL_EXECUTABLES
        and str(path.parent) in _KNOWN_SHELL_INTERPRETER_DIRS
    )


def _iter_referenced_shell_scripts(
    command: str,
    *,
    cwd: Optional[str] = None,
    fail_on_unresolved: bool = False,
    remote: bool = False,
) -> Iterator[tuple[Path, Optional[str]]]:
    """Yield referenced scripts with their explicit shell dialect, if any."""
    for segment in _iter_command_segments(command):
        index = _command_token_index(segment)
        if index is None:
            continue
        executable = segment[index]
        executable_name = Path(executable).name
        if fail_on_unresolved and _has_unresolved_shell_expansion(executable):
            if _is_safe_unresolved_test_executable(executable, segment[index + 1 :]):
                continue
            raise _UnresolvedExecutableReference(executable)

        # A path-qualified command is itself executable input, even when its
        # basename resembles a trusted shell or the `source` builtin. Scan it
        # before applying interpreter-specific argument rules so a local or
        # remote wrapper named `sh`, `bash`, or `source` cannot evade the walk.
        path_qualified = bool(executable.strip("/")) and "/" in executable
        if path_qualified and not _is_known_shell_interpreter(executable):
            resolved = _resolve_executable_reference(
                executable,
                cwd,
                fail_on_unresolved=fail_on_unresolved,
                remote=remote,
            )
            if resolved is not None:
                yield (
                    resolved,
                    (
                        executable_name
                        if executable_name in _SHELL_EXECUTABLES
                        and _is_known_shell_interpreter(executable)
                        else "sh"
                        if executable_name in {".", "source"}
                        else None
                    ),
                )

        if executable_name in {".", "source"}:
            if len(segment) > index + 1:
                resolved = _resolve_executable_reference(
                    segment[index + 1],
                    cwd,
                    fail_on_unresolved=fail_on_unresolved,
                    remote=remote,
                )
                if resolved is not None:
                    yield resolved, "sh"
            continue

        if executable_name in _SHELL_EXECUTABLES:
            arguments = segment[index + 1 :]
            arg_index = 0
            while arg_index < len(arguments):
                argument = arguments[arg_index]
                if argument == "--":
                    arg_index += 1
                    break
                if argument in {"-c", "--command"}:
                    break
                if argument in _SHELL_OPTIONS_WITH_VALUES:
                    arg_index += 2
                    continue
                if argument.startswith("-"):
                    arg_index += 1
                    continue
                break
            if arg_index < len(arguments) and arguments[arg_index] not in {
                "-c",
                "--command",
            }:
                resolved = _resolve_executable_reference(
                    arguments[arg_index],
                    cwd,
                    fail_on_unresolved=fail_on_unresolved,
                    remote=remote,
                )
                if resolved is not None:
                    dialect = (
                        executable_name
                        if _is_known_shell_interpreter(executable)
                        else "sh"
                    )
                    yield resolved, dialect
            continue

        # A bare "/" token is pathlib's division operator in Python sources
        # (e.g. `Path.home() / ".hermes"`), not an executable reference.
        # Resolving it walks to the filesystem root and fails the
        # regular-file check below, hard-blocking innocent .py scripts
        # (#77131). Skip pure-separator tokens.
        if not path_qualified and executable.endswith((".sh", ".bash", ".zsh")):
            resolved = _resolve_executable_reference(
                executable,
                cwd,
                fail_on_unresolved=fail_on_unresolved,
                remote=remote,
            )
            if resolved is not None:
                yield resolved, "sh"


def _iter_shell_command_payloads(command: str) -> Iterator[str]:
    """Yield ``-c`` code with the selected shell dialect preserved."""
    for segment in _iter_command_segments(command):
        index = _command_token_index(segment)
        if index is None or Path(segment[index]).name not in _SHELL_EXECUTABLES:
            continue
        executable = segment[index]
        executable_name = Path(executable).name
        payload_dialect = (
            executable_name
            if _is_known_shell_interpreter(executable)
            else "sh"
        )
        arguments = segment[index + 1 :]
        for arg_index, argument in enumerate(arguments[:-1]):
            if argument in {"-c", "--command"}:
                yield f"#!/bin/{payload_dialect}\n{arguments[arg_index + 1]}"
                break


_EXECUTION_WRAPPERS = frozenset({"command", "env", "exec", "nice", "nohup", "setsid", "sudo", "timeout"})
_EXECUTION_CARRIERS = frozenset({"chroot", "doas", "find", "parallel", "watch", "xargs"})
_SUDO_OPTIONS_WITH_VALUES = frozenset(
    {
        "-C", "-D", "-g", "-h", "-p", "-R", "-T", "-u",
        "--chdir", "--close-from", "--group", "--host", "--other-user",
        "--prompt", "--role", "--type", "--user",
    }
)
_ENV_OPTIONS_WITH_VALUES = frozenset(
    {
        "-C", "-S", "-a", "-u", "--argv0", "--chdir", "--split-string", "--unset",
    }
)
_EXEC_OPTIONS_WITH_VALUES = frozenset({"-a"})


def _token_basename(token: str) -> str:
    try:
        return Path(token).name.lower()
    except (OSError, ValueError):
        return ""


def _unwrap_execution_wrapper(tokens: list[str]) -> list[str]:
    """Return argv after deterministic wrappers without scanning data arguments."""
    remaining = list(tokens)
    while remaining and _token_basename(remaining[0]) in _EXECUTION_WRAPPERS:
        wrapper = _token_basename(remaining.pop(0))
        if wrapper == "command" and remaining and remaining[0] in {"-v", "-V"}:
            return []

        index = 0
        if wrapper == "timeout":
            while index < len(remaining) and remaining[index].startswith("-"):
                option = remaining[index].split("=", 1)[0]
                index += 1
                if option in {"-k", "--kill-after", "-s", "--signal"} and "=" not in remaining[index - 1] and index < len(remaining):
                    index += 1
            # The mandatory duration precedes the executed command.
            if index < len(remaining):
                index += 1
            remaining = remaining[index:]
            continue

        if wrapper == "nice":
            while index < len(remaining):
                token = remaining[index]
                if token == "--":
                    index += 1
                    break
                if re.match(r"^-\d+$", token):
                    index += 1
                    continue
                option = token.split("=", 1)[0]
                if option not in {"-n", "--adjustment"}:
                    break
                index += 1
                if "=" not in token and index < len(remaining):
                    index += 1
            remaining = remaining[index:]
            continue

        if wrapper == "setsid":
            while index < len(remaining) and remaining[index].startswith("-"):
                if remaining[index] == "--":
                    index += 1
                    break
                index += 1
            remaining = remaining[index:]
            continue

        while index < len(remaining):
            token = remaining[index]
            if wrapper in {"env", "sudo"} and re.match(
                r"^[A-Za-z_][A-Za-z0-9_]*=", token
            ):
                index += 1
                continue
            if token == "--":
                index += 1
                break
            if not token.startswith("-") or token == "-":
                break
            option = token.split("=", 1)[0]
            index += 1
            options_with_values = {
                "env": _ENV_OPTIONS_WITH_VALUES,
                "exec": _EXEC_OPTIONS_WITH_VALUES,
                "sudo": _SUDO_OPTIONS_WITH_VALUES,
            }.get(wrapper, frozenset())
            if option in options_with_values and "=" not in token and index < len(remaining):
                index += 1
        remaining = remaining[index:]
    return remaining


def _dynamic_executable_targets_gateway_lifecycle(argv: list[str]) -> bool:
    if not argv or not any(marker in argv[0] for marker in ("$", "`")):
        return False
    dynamic_args = [argument.lower() for argument in argv[1:]]
    if len(dynamic_args) >= 2 and dynamic_args[0] == "gateway":
        if dynamic_args[1] in {"restart", "stop"}:
            return True
        if dynamic_args[1] == "run" and "--replace" in dynamic_args[2:]:
            return True
    if dynamic_args and dynamic_args[0] in {
        "bootout", "disable", "kickstart", "load", "remove",
        "restart", "start", "stop", "unload",
    } and re.search(
        r"\bhermes[.\-]?gateway\b",
        " ".join(dynamic_args[1:]),
        re.IGNORECASE,
    ):
        return True
    return bool(dynamic_args and dynamic_args[0] in {"submit", "bootstrap"})


def contains_executed_gateway_lifecycle_command(command: str, *, _depth: int = 0) -> bool:
    """Detect lifecycle actions from executable argv, not source vocabulary."""
    if not command or _depth >= _MAX_REFERENCED_SCRIPT_DEPTH:
        return False

    for segment in _iter_command_segments(command):
        index = _command_token_index(segment)
        if index is None:
            continue
        raw_argv = segment[index:]
        if _token_basename(raw_argv[0]) in _EXECUTION_CARRIERS and any(
            any(marker in argument for marker in ("$", "`", "*", "?"))
            for argument in raw_argv[1:]
        ):
            return True
        if _token_basename(raw_argv[0]) == "env":
            for argument_index, argument in enumerate(raw_argv[1:], 1):
                split_payload = None
                if argument in {"-S", "--split-string"}:
                    if argument_index + 1 < len(raw_argv):
                        split_payload = raw_argv[argument_index + 1]
                elif argument.startswith("--split-string="):
                    split_payload = argument.split("=", 1)[1]
                if split_payload and any(
                    marker in split_payload for marker in ("$", "`")
                ):
                    return True
                if split_payload and contains_executed_gateway_lifecycle_command(
                    split_payload,
                    _depth=_depth + 1,
                ):
                    return True
        if _dynamic_executable_targets_gateway_lifecycle(raw_argv):
            return True
        if _token_basename(raw_argv[0]) == "eval":
            if any(marker in argument for argument in raw_argv[1:] for marker in ("$", "`")):
                return True
            if contains_executed_gateway_lifecycle_command(
                " ".join(raw_argv[1:]),
                _depth=_depth + 1,
            ):
                return True
            continue
        argv = _unwrap_execution_wrapper(raw_argv)
        if not argv:
            continue
        if _dynamic_executable_targets_gateway_lifecycle(argv):
            return True
        executable = _token_basename(argv[0])
        lowered = [argument.lower() for argument in argv[1:]]
        unresolved_arguments = [
            index
            for index, argument in enumerate(argv[1:])
            if any(marker in argument for marker in ("$", "`", "*", "?"))
        ]

        if executable == "hermes" and lowered:
            gateway_position_is_dynamic = 0 in unresolved_arguments
            gateway_position_is_explicit = lowered[0] == "gateway"
            if (
                gateway_position_is_dynamic or gateway_position_is_explicit
            ) and unresolved_arguments:
                return True

        if executable == "launchctl" and unresolved_arguments:
            launchctl_verb = lowered[0] if lowered else ""
            if 0 in unresolved_arguments or launchctl_verb in {
                "asuser", "bootstrap", "bootout", "disable", "kickstart",
                "load", "remove", "restart", "stop", "submit", "unload",
            }:
                return True

        if executable == "systemctl" and unresolved_arguments:
            lifecycle_verbs = {"restart", "start", "stop"}
            safe_read_verbs = {
                "cat", "is-active", "is-enabled", "list-units", "show", "status",
            }
            if any(argument in lifecycle_verbs for argument in lowered):
                return True
            if not any(argument in safe_read_verbs for argument in lowered):
                return True

        if executable == "hermes" and len(lowered) >= 2 and lowered[0] == "gateway":
            if lowered[1] in {"restart", "stop"}:
                return True
            if lowered[1] == "run" and "--replace" in lowered[2:]:
                return True

        if executable == "systemctl":
            for verb_index, argument in enumerate(lowered):
                if argument not in {"restart", "stop", "start"}:
                    continue
                if re.search(
                    r"\bhermes[.\-]?gateway\b",
                    " ".join(lowered[verb_index + 1 :]),
                    re.IGNORECASE,
                ):
                    return True

        if executable == "launchctl" and lowered:
            verb = lowered[0]
            if verb in {"submit", "bootstrap"}:
                return True
            if verb in {
                "bootout", "disable", "kickstart", "load", "remove",
                "restart", "stop", "unload",
            } and re.search(
                r"\bhermes[.\-]?gateway\b",
                " ".join(lowered[1:]),
                re.IGNORECASE,
            ):
                return True

        if executable in {"kill", "pkill"}:
            target = " ".join(lowered)
            if "hermes" in target and "gateway" in target:
                return True

    return any(
        contains_executed_gateway_lifecycle_command(payload, _depth=_depth + 1)
        for payload in _iter_shell_command_payloads(command)
    )


def _resolve_script_directory(script_path: str) -> Optional[str]:
    """Return the directory *script_path* resolves to, handling relative names."""
    try:
        path = _resolve_script_path(script_path)
        if path is not None and path.is_absolute():
            return str(path.parent)
    except Exception:
        pass
    return None


def _read_referenced_script(path: Path) -> tuple[Optional[str], bool]:
    """Return ``(text, unsafe)`` using bounded, regular-file-only reads.

    This is the shared choke point for every local script read the guard
    performs (the terminal walk in ``_contains_unsafe_gateway_action`` AND
    the cron-script scan in ``_read_script_for_scanning``), so the
    cloud-placeholder refusal lives here: a FileProvider path must never be
    opened — not even to discover whether the file is hydrated — because an
    evicted placeholder's ``open()`` can hang preflight indefinitely
    (#88052). The lexical check covers direct cloud paths; the resolved
    check covers local launchers that are symlinks into a cloud subtree.
    """
    if _is_cloud_placeholder_path(path):
        return None, True
    try:
        resolved = path.resolve(strict=False)
    except (OSError, ValueError):
        # OSError: unreadable/long paths. ValueError: embedded NUL byte
        # from a binary's decoded contents tokenized as a path — a
        # guarded path must never crash the guard (#76762).
        resolved = path
    if _is_cloud_placeholder_path(resolved):
        return None, True
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(path, flags)
    except (OSError, ValueError):
        # OSError: unreadable / missing / over-long paths. ValueError: an
        # embedded NUL byte in *path* itself — a binary's decoded bytes
        # tokenized into a bogus script path by the recursion (#77703). A
        # guarded read must never crash the guard, so treat either as
        # "nothing to scan" (mirrors the resolve() ValueError guard below).
        return None, False
    try:
        metadata = os.fstat(descriptor)
        if stat.S_ISDIR(metadata.st_mode):
            # A directory cannot feed a shell. Treat it as absent so a remote
            # reader still gets a chance when local and remote paths collide.
            return None, False
        if not stat.S_ISREG(metadata.st_mode):
            # Directories are not scripts. Docker Desktop writes
            # ``fpath=(~/.docker/completions …)`` into ``~/.zshrc``; the
            # walk then treats that dir as a referenced script and used
            # to fail-closed, blocking ``source ~/.zshrc`` (#86753).
            # Devices/sockets stay fail-closed.
            if stat.S_ISDIR(metadata.st_mode):
                return None, False
            return None, True
        # Sniff a small prefix first: files that are clearly compiled
        # binaries (executable magic, or NUL bytes in the head) are never
        # shell scripts, so skip them WITHOUT reading the rest — reading a
        # megabyte of machine code just to discard it wastes the guard's
        # budget and (pre-#77703) fed decoded garbage into the recursion.
        data = os.read(descriptor, _BINARY_SNIFF_BYTES)
        if data.startswith(_BINARY_MAGIC_PREFIXES) or b"\x00" in data:
            return None, False
        # Read the remainder (bounded). Loop because os.read may return
        # short for non-regular-file-backed descriptors.
        while len(data) <= _MAX_REFERENCED_SCRIPT_BYTES:
            chunk = os.read(
                descriptor, _MAX_REFERENCED_SCRIPT_BYTES + 1 - len(data)
            )
            if not chunk:
                break
            data += chunk
    except OSError:
        return None, False
    finally:
        os.close(descriptor)
    # A NUL byte in the first chunk means this is a binary (ELF/Mach-O/
    # PE), not a shell script — scanning its decoded contents would
    # tokenize machine code and feed junk paths into the recursion
    # (including a `ValueError: embedded null byte` from Path.resolve,
    # #76762). Treat it as "nothing to scan" rather than unsafe: a binary
    # executed by the user is not a referenced *shell script*.
    if b"\x00" in data:
        return None, False
    if len(data) > _MAX_REFERENCED_SCRIPT_BYTES:
        return None, True
    return data.decode("utf-8", errors="replace"), False


def _sanitize_remote_script_text(text: Optional[str]) -> tuple[Optional[str], bool]:
    """Apply the local-read contract to text from a ``read_remote_script`` callback.

    The recursion boundary must not trust its callbacks: any backend (SSH,
    Modal, Daytona, or a future one) can hand back raw binary bytes decoded
    as text, or arbitrarily large output. Mirror
    ``_read_referenced_script``'s semantics exactly — NUL bytes mean binary
    (nothing to scan, checked first, #77703), oversized text fails closed
    like an oversized local file (#76762) — so remote and local reads can
    never diverge again. The size check re-encodes to compare *bytes*
    (matching the local read and the ``head -c`` wire bound): a >1 MiB
    multibyte file truncated at the byte cap decodes to fewer characters
    than bytes, and a character-count check would scan the truncated text
    instead of failing closed. Enforced here rather than inside each
    callback so the guarantee holds for every callback, not just the ones
    we hardened.
    """
    # ``None`` means the remote backend could not read the path (missing,
    # permission denied, transport error, or an adapter that swallowed the
    # real error).  A remote lifecycle guard must fail closed in that case:
    # treating an unreadable script as safe defeats the whole inspection.
    # An empty string, on the other hand, is a successfully read empty script
    # and is safe to ignore.
    if text is None:
        return None, True
    if not text:
        return None, False
    if "\x00" in text:
        return None, False
    if len(text.encode("utf-8", errors="replace")) > _MAX_REFERENCED_SCRIPT_BYTES:
        return None, True
    return text, False


def _script_declares_shell(text: str) -> bool:
    first_line = text.partition("\n")[0]
    if not first_line.startswith("#!"):
        return False
    try:
        tokens = shlex.split(first_line[2:])
    except ValueError:
        return False
    return any(Path(token).name in _SHELL_EXECUTABLES for token in tokens)


def _contains_unsafe_gateway_action(
    command: str,
    *,
    cwd: Optional[str],
    depth: int,
    shell_context: bool,
    visited: set[tuple[Path, Optional[str]]],
    read_remote_script: Optional[_ReadRemoteScriptFn] = None,
    execution_only: bool = False,
    fail_on_unresolved: bool = True,
) -> bool:
    if depth >= _MAX_REFERENCED_SCRIPT_DEPTH:
        return True

    if shell_context:
        command, heredoc_bodies, heredoc_unsafe = partition_heredoc_bodies(
            command,
            inert_consumers=_LIFECYCLE_INERT_HEREDOC_CONSUMERS,
            preserve_shell_dialect=True,
        )
        if heredoc_unsafe:
            return True
    else:
        heredoc_bodies = ()
    direct_unsafe = _direct_lifecycle_scan(command)
    if execution_only:
        direct_unsafe = direct_unsafe or contains_executed_gateway_lifecycle_command(
            command
        )
    if direct_unsafe:
        return True

    for body in heredoc_bodies:
        body_shell_context = body == "$(" or _script_declares_shell(body)
        if _contains_unsafe_gateway_action(
            body,
            cwd=cwd,
            depth=depth + 1,
            shell_context=body_shell_context,
            visited=visited,
            read_remote_script=read_remote_script,
            execution_only=execution_only,
            fail_on_unresolved=fail_on_unresolved,
        ):
            return True

    for payload in _iter_shell_command_payloads(command):
        if _contains_unsafe_gateway_action(
            payload,
            cwd=cwd,
            depth=depth + 1,
            shell_context=True,
            visited=visited,
            read_remote_script=read_remote_script,
            execution_only=execution_only,
            fail_on_unresolved=fail_on_unresolved,
        ):
            return True

    try:
        referenced_scripts = list(
            _iter_referenced_shell_scripts(
                command,
                cwd=cwd,
                fail_on_unresolved=shell_context and fail_on_unresolved,
                remote=read_remote_script is not None,
            )
        )
    except _UnresolvedExecutableReference:
        # The shell will choose the executable or script only after expansion.
        # Cron validation stays fail-closed. Live terminal execution may allow
        # unresolved benign test-runner variables because the shell, not this
        # static guard, owns their actual resolution.
        return fail_on_unresolved

    remote = read_remote_script is not None
    for script_path, referenced_shell_dialect in referenced_scripts:
        # Do not touch a FileProvider path even to discover whether the file
        # is hydrated. The lexical check covers direct cloud paths; the
        # resolved check below covers local launchers that are symlinks into
        # a cloud subtree. _read_referenced_script repeats both checks as the
        # shared choke point, so every caller stays covered even if this
        # walk-level short-circuit is bypassed.
        if remote:
            resolved = script_path
        else:
            if _is_cloud_placeholder_path(script_path):
                return True
            try:
                resolved = script_path.resolve(strict=False)
            except (OSError, ValueError):
                # OSError: unreadable/long paths. ValueError: embedded NUL byte
                # from a binary's decoded contents tokenized as a path. A
                # guarded path must never crash the guard (#76762).
                resolved = script_path
            if _is_cloud_placeholder_path(resolved):
                return True
        visit_key = (resolved, referenced_shell_dialect)
        if visit_key in visited:
            continue
        visited.add(visit_key)
        if remote:
            try:
                remote_text = read_remote_script(str(script_path))
            except Exception:
                # Callback failures are indistinguishable from an unreadable
                # remote script and therefore must not turn into a safe
                # verdict.
                logger.warning(
                    "remote lifecycle script read failed for %s; failing closed",
                    script_path,
                    exc_info=True,
                )
                return True
            script_text, unsafe = _sanitize_remote_script_text(remote_text)
        else:
            script_text, unsafe = _read_referenced_script(script_path)
        if unsafe:
            return True
        if not script_text:
            continue
        # Relative references inside a script resolve against that script's
        # directory in the same execution environment.
        script_dir = (
            str(script_path.parent)
            if remote
            else _resolve_script_directory(str(resolved)) or cwd
        )
        if referenced_shell_dialect:
            script_text = f"#!/bin/{referenced_shell_dialect}\n{script_text}"
        child_shell_context = bool(referenced_shell_dialect) or _script_declares_shell(
            script_text
        )
        if _contains_unsafe_gateway_action(
            script_text,
            cwd=script_dir,
            depth=depth + 1,
            shell_context=child_shell_context,
            visited=visited,
            read_remote_script=read_remote_script,
            execution_only=execution_only,
            fail_on_unresolved=fail_on_unresolved,
        ):
            return True
    return False


def contains_gateway_lifecycle_command_or_referenced_script(
    command: str,
    *,
    cwd: Optional[str] = None,
    read_remote_script: Optional[_ReadRemoteScriptFn] = None,
    execution_only: bool = False,
    fail_on_unresolved: bool = True,
) -> bool:
    """Detect lifecycle/submit commands, including bounded nested scripts.

    Total by construction: this function returns a verdict for *every*
    input and never raises. The direct scans below are pure string
    operations; the referenced-script walk touches the filesystem, remote
    backends, and shlex on arbitrary decoded bytes, so it is best-effort
    defense-in-depth — any unexpected failure inside it is logged and
    treated as "walk found nothing" rather than crashing the caller.

    This is the contract #76762 established ("a guarded path must never
    crash the guard") enforced at the boundary instead of per-syscall: a
    guard crash propagates out of ``tools/terminal_tool.py`` and breaks
    every terminal command until the gateway restarts (#77780, #78256),
    which is strictly worse than either verdict.
    """
    try:
        # Includes the direct regex/submit scans at depth 0.
        return _contains_unsafe_gateway_action(
            command,
            cwd=cwd,
            depth=0,
            shell_context=True,
            visited=set(),
            read_remote_script=read_remote_script,
            execution_only=execution_only,
            fail_on_unresolved=fail_on_unresolved,
        )
    except Exception:
        logger.warning(
            "lifecycle guard referenced-script walk failed; "
            "falling back to direct-scan verdict",
            exc_info=True,
        )
        # A remote callback is part of the trust boundary.  If any part of
        # remote inspection fails unexpectedly, do not fall back to a
        # direct-only scan: that would incorrectly permit a lifecycle action
        # hidden in an unreadable referenced script.
        if read_remote_script is not None:
            return True
        # Pure string scans of the top-level command — cannot raise.
        try:
            if execution_only:
                return _direct_lifecycle_scan(
                    command
                ) or contains_executed_gateway_lifecycle_command(command)
            return _direct_lifecycle_scan(command)
        except Exception:
            # The data-argument masker tokenizes arbitrary text; if even
            # that fails, fall to the raw regex + submit scan so the guard
            # stays total.
            return contains_gateway_lifecycle_command(
                command
            ) or contains_launchctl_submit_command(command)




def _resolve_script_path(script_path: str) -> Optional[Path]:
    """Resolve a cron ``script`` value the same way the scheduler does.

    The scheduler (``cron.scheduler``) resolves a bare/relative script path
    under ``<HERMES_HOME>/scripts/`` and only accepts absolute paths as-is.
    We MUST mirror that here so the guard scans the file that will actually
    run — otherwise a job whose script lives at the scheduler's real location
    (``~/.hermes/scripts/restart.sh``) but is passed as the bare name
    ``restart.sh`` would read as a nonexistent relative path and silently
    scan prompt-only content, letting the command through.

    Returns ``None`` for values that cannot be a real path (NUL bytes,
    unexpandable ``~``) — the same ingestion contract as
    ``_expand_candidate_path``; such a value can never name a file the
    scheduler would execute, so there is nothing to scan.
    """
    from hermes_constants import get_hermes_home

    raw = _expand_candidate_path(script_path)
    if raw is None:
        return None
    if raw.is_absolute():
        return raw
    try:
        return get_hermes_home() / "scripts" / raw
    except (RuntimeError, OSError):
        # get_hermes_home() falls back to Path.home(), which raises when
        # neither HERMES_HOME nor HOME is resolvable (launchd/systemd
        # environments) — same ingestion contract: nothing to scan.
        return None


def _read_script_for_scanning(script_path: str) -> str:
    """Read a cron script with the bounded terminal-script scanner.

    Non-regular or oversized inputs fail closed by returning a lifecycle-shaped
    sentinel, while missing/unreadable/unresolvable paths remain empty so
    ordinary scheduler path validation can report them.
    """
    resolved = _resolve_script_path(script_path)
    if resolved is None:
        return ""
    script_text, unsafe = _read_referenced_script(resolved)
    if unsafe:
        return "hermes gateway restart"
    return script_text or ""


def check_gateway_lifecycle(
    prompt: Optional[str],
    script: Optional[str] = None,
) -> None:
    """Raise ``GatewayLifecycleBlocked`` if *prompt* or *script* contains a
    gateway-lifecycle command pattern.

    ``prompt`` is scanned directly.  ``script``, when supplied, is read from
    disk and concatenated for the scan.  Both are considered together so a
    job cannot slip through by splitting the command across the prompt and
    the script.

    Callers should let the exception propagate when they want the create to
    fail with a ``ValueError``-shaped error (the agent's ``cronjob`` tool
    surfaces this as a tool error; the CLI prints it in red and exits 1).
    """
    combined = prompt or ""
    python_script = False
    if script:
        resolved_script = _resolve_script_path(script)
        if resolved_script is not None:
            try:
                real_script = resolved_script.resolve(strict=False)
            except (OSError, ValueError):
                real_script = resolved_script
            if _is_cloud_placeholder_path(resolved_script) or _is_cloud_placeholder_path(
                real_script
            ):
                # Attribute the refusal correctly: the script is not known to
                # contain a lifecycle command — it lives on a cloud-synced
                # FileProvider path (iCloud Drive / ~/Library/CloudStorage)
                # that the guard refuses to open because an evicted
                # placeholder can hang preflight indefinitely (#88052).
                # Fail closed with the real reason instead of implying a
                # dangerous lifecycle command.
                raise GatewayLifecycleBlocked(
                    "Blocked: the cron script lives on a cloud-synced path "
                    "(iCloud Drive / ~/Library/CloudStorage). Opening an "
                    "evicted FileProvider placeholder can hang the guard's "
                    "preflight scan indefinitely, so it is refused without "
                    "being read. Move the script to a local, non-cloud path "
                    "(e.g. ~/.hermes/scripts/) and recreate the job."
                )
        python_script = resolved_script is not None and resolved_script.suffix == ".py"
        script_text = _read_script_for_scanning(script)
        if script_text:
            combined = f"{combined}\n{script_text}"

    if python_script:
        # Python is executed by the interpreter, never through a POSIX
        # shell: the shell-script reference walk is a false-positive
        # generator on Python sources (pathlib's "/" operator resolves to
        # the filesystem root and trips the regular-file check, blocking
        # every innocent .py cron script, #77131). The direct command
        # regex below still scans the full text, so a literal
        # `hermes gateway restart` embedded in a .py script is still
        # blocked. Non-regular/oversized script files still fail closed
        # via the lifecycle-shaped sentinel in _read_script_for_scanning.
        unsafe = _lifecycle_command_scan_with_data_exemption(combined)
    else:
        script_dir = _resolve_script_directory(script) if script else None
        unsafe = contains_gateway_lifecycle_command_or_referenced_script(
            combined,
            cwd=script_dir,
        )
    if unsafe:
        raise GatewayLifecycleBlocked(
            "Blocked: cron job contains a gateway lifecycle command or persistent "
            "launchctl submit operation. This is blocked to prevent agent-driven "
            "SIGTERM-respawn loops under launchd/systemd supervision "
            "(#30719). Run `hermes gateway restart` from a shell outside "
            "the running gateway instead."
        )
