"""Terminal error and session-context helpers."""

from typing import Any


def _redact_terminal_error_text(value: Any) -> str:
    """Force-redact text before serializing a terminal error envelope."""
    from agent.redact import redact_sensitive_text

    return redact_sensitive_text("" if value is None else str(value), force=True)


def _current_session_key() -> str:
    """Return the active gateway/WebUI session key, or empty outside sessions."""
    from gateway.session_context import get_session_env

    return get_session_env("HERMES_SESSION_KEY", "")


def _current_session_profile() -> str:
    """Return the active session's Hermes profile name, or empty outside sessions."""
    from gateway.session_context import get_session_env

    return get_session_env("HERMES_SESSION_PROFILE", "")
