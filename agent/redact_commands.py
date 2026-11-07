"""Classify terminal commands that can print secret-bearing files or environments."""

from __future__ import annotations


def _command_segments(command: str) -> list[str]:
    """Pipeline/sequence segments, split only on unquoted ``| ; &`` so an
    ``awk '{print $1; print $2}'`` program or ``grep 'foo|bar'`` pattern stays one
    segment. Backslash is not an escape (Windows ``C:\\Users\\...``)."""
    segments: list[str] = []
    buf: list[str] = []
    quote: str | None = None
    for ch in command:
        if quote:
            buf.append(ch)
            if ch == quote:
                quote = None
            continue
        if ch in "'\"":
            quote = ch
            buf.append(ch)
            continue
        if ch in "|;&":
            seg = "".join(buf).strip()
            if seg:
                segments.append(seg)
            buf = []
            continue
        buf.append(ch)
    seg = "".join(buf).strip()
    if seg:
        segments.append(seg)
    return segments


def _is_under_hermes_home(path: str) -> bool:
    """True when an absolute ``config.yaml`` path sits under the active Hermes home or root.

    The default home's basename is an installation detail — ``.hermes`` on POSIX, ``hermes``
    under ``AppData/Local`` on Windows — and a resolved path never spells ``$HERMES_HOME``,
    so the literal-segment test in ``_is_secret_file_arg`` cannot see a native Windows path.
    Compare against the resolved homes instead. Only reached for a ``config.yaml`` basename,
    so the resolve cost stays off the per-token command scan.
    """
    from agent.redact import os
    from agent.file_safety import _hermes_dirs

    try:
        target = os.path.normcase(os.path.realpath(os.path.expanduser(path)))
    except (OSError, ValueError):
        return False
    for home in _hermes_dirs():
        try:
            base = os.path.normcase(os.path.realpath(str(home)))
        except (OSError, ValueError):
            continue
        if target == base or target.startswith(base + os.sep):
            return True
    return False


def _is_secret_file_arg(arg: str) -> bool:
    """``.env``-style or shell rc basename anywhere; ``config.yaml`` only under a
    ``.hermes`` directory, ``$HERMES_HOME``, or the resolved Hermes home (never arbitrary
    YAML). The resolved-home arm is what covers native Windows, where the home directory
    is ``%LOCALAPPDATA%\\hermes`` and carries no ``.hermes`` segment."""
    from agent.redact import _HERMES_HOME_PREFIXES, _HOME_PREFIXES, _ENV_FILE_BASENAMES, _SHELL_RC_BASENAMES, _is_under_hermes_home
    path = arg.strip("\"'").replace("\\", "/")
    hermes_home = False
    for prefix in _HERMES_HOME_PREFIXES:
        if path.startswith(prefix):
            path = path[len(prefix):]
            hermes_home = True
            break
    for prefix in _HOME_PREFIXES:
        if path.startswith(prefix):
            path = path[len(prefix):]
            break
    if "$" in path:
        return False
    parts = [part.lower() for part in path.split("/") if part]
    if not parts:
        return False
    if parts[-1] in _ENV_FILE_BASENAMES or parts[-1] in _SHELL_RC_BASENAMES:
        return True
    # ``config.yaml`` plus the ``config.yaml.good.<stamp>`` / ``.corrupt.<stamp>`` copies Hermes
    # writes under ``backups/config/`` — same contents, same secrets.
    if parts[-1] != "config.yaml" and not parts[-1].startswith(("config.yaml.good.", "config.yaml.corrupt.")):
        return False
    return hermes_home or ".hermes" in parts[:-1] or _is_under_hermes_home(path)


def _command_reads_secret_file(command: str | None) -> bool:
    """True if ``command`` reads a secret-bearing file (see ``_is_secret_file_arg``) to
    stdout. Defense-in-depth, not a boundary: indirect reads (``sudo cat .env``, ``$(cat
    .env)``, unresolved variable paths) are not detected, matching ``is_env_dump_command``."""
    from agent.redact import _command_segments, _FILE_READ_COMMANDS, _PATTERN_FIRST_COMMANDS, _is_secret_file_arg
    if not command or not isinstance(command, str):
        return False
    for seg in _command_segments(command):
        tokens = seg.split()  # not shlex: it mangles Windows paths (``C:\Users\...\.env``)
        if not tokens:
            continue
        reader = tokens[0].rsplit("/", 1)[-1].lower()
        if reader not in _FILE_READ_COMMANDS:
            continue
        positional = [arg for arg in tokens[1:] if not arg.startswith("-")]
        if reader in _PATTERN_FIRST_COMMANDS:
            positional = positional[1:]
        if any(_is_secret_file_arg(arg) for arg in positional):
            return True
    return False


def is_env_dump_command(command: str | None) -> bool:
    """True if any pipeline/sequence segment starts with an _ENV_DUMP_COMMANDS
    token. Conservative: unrecognized → False (callers fall back to code_file=True)."""
    from agent.redact import _command_segments, shlex, _ENV_DUMP_COMMANDS
    if not command or not isinstance(command, str):
        return False
    for seg in _command_segments(command):
        try:
            tokens = shlex.split(seg)
        except ValueError:
            tokens = seg.split()
        if tokens and tokens[0] in _ENV_DUMP_COMMANDS:
            return True
    return False
