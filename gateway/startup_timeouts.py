"""Side-effect-free gateway startup timeout helpers."""

import os


STARTUP_RESTORE_DRAIN_TIMEOUT_SECS_DEFAULT = 30.0


def startup_restore_drain_timeout_secs() -> float:
    """Return the bounded startup restore/MCP admission timeout in seconds."""
    raw = os.environ.get("HERMES_STARTUP_RESTORE_DRAIN_TIMEOUT")
    if raw is None or raw == "":
        return STARTUP_RESTORE_DRAIN_TIMEOUT_SECS_DEFAULT
    try:
        return float(raw)
    except (TypeError, ValueError):
        return STARTUP_RESTORE_DRAIN_TIMEOUT_SECS_DEFAULT
