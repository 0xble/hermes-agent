"""Delivery policy for engine diagnostics, never assistant or command responses."""

from gateway.display_config import resolve_display_setting


class DiagnosticText(str):
    """Producer-owned classification on the legacy two-argument status callback."""


def warning_notifications_enabled(platform, user_config=None) -> bool:
    """Return whether diagnostic notifications should reach the given platform."""
    if user_config is None:
        from hermes_cli.config_effective import load_user_config_effective

        try:
            user_config = load_user_config_effective()
        except Exception:
            user_config = {}
    if not isinstance(user_config, dict):
        user_config = {}
    platform_key = getattr(platform, "value", platform)
    return not bool(resolve_display_setting(
        user_config, str(platform_key), "suppress_warning_notifications", False
    ))
