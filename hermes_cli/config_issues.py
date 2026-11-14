"""The issue record config validation emits (``hermes config check``, doctor, startup warnings).

Its own module so the facade (``hermes_cli.config``) and its check siblings share it without
a facade import from the siblings."""

from dataclasses import dataclass


@dataclass
class ConfigIssue:
    """A config problem, with optional operator-only diagnostic detail."""
    severity: str  # "error", "warning"
    message: str
    hint: str
    local_detail: str = ""


def _issue(issues: list[ConfigIssue], severity: str, message: str, hint: str) -> None:
    issues.append(ConfigIssue(severity, message, hint))
