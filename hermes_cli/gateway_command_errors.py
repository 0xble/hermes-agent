"""User-facing copy for ``hermes gateway`` service failures.

``hermes_cli/gateway.py`` is a facade; this sibling owns the small exception -> guidance table so
expected systemd failures and explicit launchd inspection refusals end as a next step
instead of a traceback. Unexpected runtime errors remain visible.
"""

from __future__ import annotations

import subprocess


class SystemctlUnavailableError(RuntimeError):
    """``systemctl`` is not installed (Alpine, minimal containers, some WSL setups)."""

    def __init__(self) -> None:
        super().__init__("systemctl is not available on this system")


_JOURNAL_HINT = 'journalctl --user -u hermes-gateway --since "5 min ago"'

_SYSTEMCTL_FAILED_LINES = (
    "Could not {verb} the gateway service; systemd reported an error.",
    "See why with `hermes gateway status --deep` or `{journal}`.",
    "To reinstall the service run `hermes gateway install --force`.",
)

_NO_SYSTEMCTL_LINES = (
    "This system has no systemd, so Hermes cannot install a background service here.",
    "Run the gateway directly with `hermes gateway run` (keep it alive with tmux or screen).",
)


def _verb_for(exc: subprocess.CalledProcessError) -> str:
    cmd = exc.cmd if isinstance(exc.cmd, (list, tuple)) else str(exc.cmd).split()
    for token in cmd:
        if token in ("start", "stop", "restart"):
            return token
    return "start"


class LaunchdServiceError(RuntimeError):
    """An inspectable launchd refusal; CLI callers show guidance rather than a traceback."""


class GatewayInstallError(RuntimeError):
    """An expected install refusal that the gateway CLI can present without a traceback."""


def explain_service_failure(exc: BaseException) -> list[str] | None:
    """Lines for known service/install failures; None preserves unexpected exceptions."""
    if isinstance(exc, (GatewayInstallError, LaunchdServiceError)):
        return [str(exc)]
    if isinstance(exc, SystemctlUnavailableError):
        return list(_NO_SYSTEMCTL_LINES)
    if isinstance(exc, subprocess.CalledProcessError):
        lines = [line.format(verb=_verb_for(exc), journal=_JOURNAL_HINT) for line in _SYSTEMCTL_FAILED_LINES]
        lines.append(f"Details: {exc}")
        return lines
    return None
