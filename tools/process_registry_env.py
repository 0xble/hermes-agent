"""Child environment construction for the process registry.

The registry facade is already over its file-size cap, so the terminal child
builder lives here. Behavior stays owned by the existing sanitizer and the
default-home compatibility strip.
"""
from __future__ import annotations

from tools.environments.local import (
    _sanitize_subprocess_env, _strip_default_profile_home, _terminal_env,
)


def child_spawn_env(env_vars: dict) -> dict:
    """Sanitized terminal child env; PYTHONUNBUFFERED so poll sees progress."""
    from agent.session_client_env import toolchain_environ

    env = _strip_default_profile_home(
        _terminal_env(_sanitize_subprocess_env(toolchain_environ(), env_vars))
    )
    env["PYTHONUNBUFFERED"] = "1"
    return env
