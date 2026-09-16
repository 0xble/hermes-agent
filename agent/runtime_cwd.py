"""Single source of truth for the agent working directory.

`TERMINAL_CWD` is the runtime carrier for the configured working directory (`terminal.cwd`
is bridged to it once at gateway/cron startup; the local CLI leaves it unset and relies on
the launch dir). Reading it in one place keeps the system prompt, tool surfaces, and
context-file discovery agreeing on where the agent lives. Multi-session gateways can pin a
logical cwd via `_SESSION_CWD`.
"""

import logging
import os
from contextvars import ContextVar, Token
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_UNSET: Any = object()

_SESSION_CWD: ContextVar = ContextVar("HERMES_SESSION_CWD", default=_UNSET)

# The package/source root (<root>/agent/runtime_cwd.py). A backend launched from or
# self-spawned into this tree (desktop default) must never let an os.getcwd() fallback
# inject this repo's contributor AGENTS.md as project context.
_PACKAGE_ROOT = Path(__file__).resolve().parent.parent


def _is_install_tree(p: Path) -> bool:
    """True only when ``p`` IS the package root or sits inside it — ancestors
    (a home dir containing the checkout) are legitimate workspaces."""
    try:
        p = p.resolve()
    except Exception:
        return False
    return p == _PACKAGE_ROOT or _PACKAGE_ROOT in p.parents


def set_session_cwd(cwd: str | None) -> Token:
    """Pin the logical cwd for the current context.

    ``None`` means this session pins nothing and leaves the binding unset, so ``terminal.cwd``
    (carried as TERMINAL_CWD and read through the per-turn terminal scope) still applies. An
    explicit empty string is the opposite: it suppresses any configured directory, the same as
    ``clear_session_cwd``.
    """
    if cwd is None:
        return _SESSION_CWD.set(_UNSET)
    return _SESSION_CWD.set(cwd.strip())


def clear_session_cwd() -> None:
    """Suppress any configured directory for this context.

    The empty binding is deliberate and distinct from "never pinned": a session that cleared its
    cwd must not silently pick up a foreign, unscoped TERMINAL_CWD from the process environment.
    """
    _SESSION_CWD.set("")


def scope_terminal_cwd() -> str:
    """Scope-aware TERMINAL_CWD value (may be empty) — every cwd consumer reads through this.

    Under gateway multiplexing the per-turn terminal scope carries the active profile's cwd;
    the process-global env var may hold another profile's. Only an ImportError falls back: an
    active refusal scope must raise, not silently resolve the launch profile's cwd.
    """
    try:
        from tools.terminal_scope import terminal_env
    except ImportError:
        return os.environ.get("TERMINAL_CWD", "")
    return terminal_env("TERMINAL_CWD", "")


def resolve_tool_cwd() -> str:
    """Bound session cwd wins, including an explicit empty isolation scope."""
    value = _SESSION_CWD.get()
    return scope_terminal_cwd().strip() if value is _UNSET else str(value or "").strip()


def _existing_dir(raw: str, label: str) -> Path | None:
    p = Path(raw).expanduser()
    if p.is_dir():
        return p
    logger.warning("%s does not exist: %s", label, raw)
    return None


def _resolve_configured_cwd(*, override_is_final: bool) -> Path | None:
    """Session override, then TERMINAL_CWD; each validated as a real directory.

    ``override_is_final``: a set-but-missing session override yields None instead of falling
    through to TERMINAL_CWD, so context-file discovery can never pick up another scope's tree.
    An empty override means "this session pins nothing", and TERMINAL_CWD is already read
    through the per-turn terminal scope, so falling through there is scope-safe.
    """
    override = _SESSION_CWD.get()
    if override is not _UNSET:
        raw = str(override or "").strip()
        if not raw:
            # Explicitly suppressed for this context (clear_session_cwd).
            return None
        p = _existing_dir(raw, "configured working directory")
        if p is not None or override_is_final:
            return p
    raw = scope_terminal_cwd().strip()
    return _existing_dir(raw, "TERMINAL_CWD") if raw else None


def resolve_agent_cwd() -> Path:
    """Configured cwd, else the launch dir (os.getcwd()'s OSError on a deleted cwd deliberately propagates)."""
    return _resolve_configured_cwd(override_is_final=False) or Path(os.getcwd())


def resolve_context_cwd() -> Path | None:
    """Configured cwd for context-file discovery, or None (build_context_files_prompt then falls back to the
    launch dir). An existing configured path is honored verbatim — including the Hermes source tree, a
    legitimate workspace when developing Hermes; fallback-directory policy lives in the caller."""
    return _resolve_configured_cwd(override_is_final=True)
