"""Parent-only shared-knowledge writes for named delegated children.

Durable memory and installed skills are *shared* state: the parent owns them,
children read them. Before this module the rule lived only in the two
specialized tools (``memory``/``skill_manage``), so a child could reach the
same bytes through ``write_file``, ``patch``, ``terminal`` or ``execute_code``.
The bypass was reproduced against a disposable fixture home, not a claim.

What is actually enforced, and what is not, is deliberately explicit — see
:func:`boundary_report`. Overstating this boundary is worse than not having it:
the child runs in the parent's process, as the parent's user, with no OS
sandbox, so "enforced" here means *every write path Hermes itself mediates*,
not an operating-system permission.

Enforced (deterministic, tested):
  * ``memory`` / ``skill_manage`` specialized tools
  * ``write_file`` (all modes)
  * ``patch`` (replace + V4A Update/Add/Delete/Move, both endpoints)
  * ``terminal`` commands that reference or execute within a protected root
  * ``execute_code`` source that references a protected root

Not enforced (documented, not silently implied):
  * a subprocess that reconstructs a protected path at runtime from pieces
    the scanner cannot see (env indirection, base64, computed strings)
  * anything running outside Hermes's tool surface entirely

The command/source scan is deliberately *reference*-based rather than
write-verb-based: enumerating mutation verbs is a losing game, and a
read-only child never needs shell access to these roots — it has
``read_file``, ``memory`` reads, and ``skill_view``.
"""
from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from typing import Iterable

# Roots whose *contents* are parent-owned shared knowledge.  Resolved live
# (never cached across HERMES_HOME changes) because profile switches and the
# test harness both relocate the home mid-process.
_ROOT_RESOLVERS: tuple[str, ...] = (
    "tools.memory_tool:get_memory_dir",
    "hermes_constants:get_skills_dir",
)

# Home spellings a command can use instead of an absolute path.
_ENV_HOME_SPELLINGS: tuple[str, ...] = ("$HERMES_HOME", "${HERMES_HOME}")


def _resolve(dotted: str) -> Path:
    module_name, _, attr = dotted.partition(":")
    module = __import__(module_name, fromlist=[attr])
    return Path(getattr(module, attr)())


def protected_roots() -> tuple[Path, ...]:
    """Resolve every protected root, or fail rather than weaken the boundary."""
    return tuple(_resolve(dotted) for dotted in _ROOT_RESOLVERS)


def _real(path: Path | str) -> str:
    """realpath *path*, tolerating components that do not exist yet.

    A write to ``<memories>/MEMORY.md`` must be caught even when the file has
    not been created, and a symlinked home must not launder the target
    (the #41351 lesson: always realpath before matching).
    """
    raw = os.path.expanduser(str(path))
    try:
        return os.path.realpath(raw)
    except OSError:
        return os.path.abspath(raw)


def _within(candidate: str, root: str) -> bool:
    return candidate == root or candidate.startswith(root.rstrip(os.sep) + os.sep)


def is_protected_path(path: Path | str) -> bool:
    """True when *path* is a protected root or lives beneath one."""
    candidate = _real(path)
    return any(_within(candidate, _real(root)) for root in protected_roots())


def _protected_root_for(path: Path | str) -> str | None:
    candidate = _real(path)
    for root in protected_roots():
        real_root = _real(root)
        if _within(candidate, real_root):
            return real_root
    return None


def _denial(target: str, root: str, *, how: str) -> str:
    return (
        f"Parent-owned shared knowledge: refusing {how} to {target}.\n"
        f"{root} is written only by the parent agent; delegated children have "
        "read access (read_file, memory reads, skill_view) and return proposed "
        "changes to the parent instead."
    )


def write_denial_reason(
    paths: Iterable[Path | str], *, how: str = "a write",
) -> str | None:
    """Return a denial message when any of *paths* is parent-owned.

    Never raises, and fails CLOSED: a guard that cannot evaluate must not
    silently disappear for the child it exists to constrain. No-ops entirely
    outside a read-only-knowledge context, so the parent and ordinary (unnamed)
    delegation are unaffected.
    """
    if not _read_only_context():
        return None
    try:
        for path in paths:
            if path in (None, ""):
                continue
            root = _protected_root_for(path)
            if root is not None:
                return _denial(str(path), root, how=how)
        return None
    except Exception:
        return _UNEVALUATED


_UNEVALUATED = (
    "Parent-owned shared knowledge: the boundary could not be evaluated, so "
    "this delegated child's request is refused. Return the change to the parent."
)


def _read_only_context() -> bool:
    """True while a named child is running.

    An unusable import answers False, which is the only safe answer: the guard
    must never fire for the parent, and this module cannot tell a child from a
    parent without that import. It is not a hole in practice — delegation
    imports ``agent.delegation_context`` to enter the context in the first
    place, so no child can exist while this import is broken. Fail-closed
    applies once we know we ARE in a child (see ``_UNEVALUATED``).
    """
    try:
        from agent.delegation_context import is_read_only_knowledge_context

        return is_read_only_knowledge_context()
    except Exception:
        return False


@lru_cache(maxsize=256)
def _root_spellings(literal_root: str, real_root: str) -> tuple[str, ...]:
    """Every way a command string can name this root without computing it.

    The model writes ``~/.hermes/skills`` or ``$HERMES_HOME/memories`` far more
    often than the realpath, so matching the resolved path alone would miss the
    common case entirely.
    """
    spellings = {literal_root, real_root}
    user_home = os.path.expanduser("~")
    for base in (literal_root, real_root):
        if user_home and base.startswith(user_home + os.sep):
            spellings.add("~" + base[len(user_home):])
    leaf = Path(literal_root).name
    for env in _ENV_HOME_SPELLINGS:
        spellings.add(f"{env}/{leaf}")
    return tuple(sorted(s for s in spellings if s))


def _command_roots(text: str) -> str | None:
    """Return the protected root *text* references, if any."""
    for root in protected_roots():
        real_root = _real(root)
        for spelling in _root_spellings(str(root), real_root):
            if spelling in text:
                return real_root
    return None


def _relative_command_root(command: str, cwd: Path | str) -> str | None:
    """Conservative literal references, including quoted interpreter source."""
    for protected in protected_roots():
        spelling = os.path.relpath(protected, cwd)
        if spelling not in (".", "..") and spelling in command:
            return _real(protected)
    return None


def command_denial_reason(command: str, *, tool: str = "terminal", cwd: str | None = None) -> str | None:
    """Deny a shell command / interpreter source that reaches shared knowledge.

    Reference-based on purpose (see the module docstring): a read-only child
    has no legitimate ``{tool}`` reason to touch these roots, and a write-verb
    allowlist would be a bypass surface rather than a boundary.
    """
    if not _read_only_context() or not isinstance(command, str) or not command:
        return None
    try:
        root = _command_roots(command)
        if root is None and cwd is not None:
            import shlex
            # Literal paths and literal cd transitions only. Runtime-computed
            # paths remain outside this reference guard's documented contract.
            tokens = list(shlex.shlex(command, posix=True, punctuation_chars=";&|<>()"))
            bases = {Path(cwd)}
            root = _relative_command_root(command, cwd)
            for index, token in enumerate(tokens):
                if root is not None:
                    break
                if token == "cd" and index + 1 < len(tokens):
                    target = tokens[index + 1]
                    if "$" not in target and "`" not in target:
                        bases.update((base / os.path.expanduser(target)).resolve() for base in tuple(bases))
                        if len(bases) > 64:
                            return _UNEVALUATED
                if not token or token.startswith("-") or any(ch in token for ch in "$`\n"):
                    continue
                for base in tuple(bases):
                    root = (_relative_command_root(command, base)
                            or _protected_root_for(base / os.path.expanduser(token)))
                    if root is not None:
                        break
                if root is not None:
                    break
    except Exception:
        return _UNEVALUATED
    if root is None:
        return None
    return (
        f"Parent-owned shared knowledge: refusing a {tool} command that "
        f"references {root}.\n"
        "Delegated children read shared knowledge through read_file / memory "
        "reads / skill_view and return proposed changes to the parent. This "
        "guard denies the whole command, read or write, because a shell "
        "write cannot be distinguished from a shell read reliably."
    )


def boundary_report() -> dict:
    """Nonsecret description of the boundary actually in force.

    Recorded in delegation evidence so nobody has to infer the guarantee from
    prose. ``shell`` is reported as ``command_scan`` — never ``sandbox`` — so
    the honest limit travels with the claim.
    """
    return {
        "active": _read_only_context(),
        "protected_roots": [_real(root) for root in protected_roots()],
        "enforced_tool_paths": [
            "memory", "skill_manage", "write_file", "patch",
            "terminal", "execute_code",
        ],
        "shell_enforcement": "command_scan_and_execution_cwd",
        "not_enforced": [
            "runtime-computed paths inside a subprocess (no OS sandbox)",
            "writes made outside Hermes's tool surface",
        ],
    }
