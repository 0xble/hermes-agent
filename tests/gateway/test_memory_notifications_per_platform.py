"""Per-platform resolution of display.memory_notifications (#59364 narrow backport).

Brian's config sets ``display.platforms.slack.memory_notifications: false`` to
silence memory/self-improvement pushes on Slack only. The gateway resolves the
setting through ``resolve_display_setting`` so the per-platform override wins
over the global value, and other platforms keep the global/default behavior.
"""

from gateway.display_config import resolve_display_setting


def _cfg(**display):
    return {"display": display}


def test_slack_false_override_wins_over_global_on():
    cfg = _cfg(
        memory_notifications="on",
        platforms={"slack": {"memory_notifications": False}},
    )
    assert resolve_display_setting(cfg, "slack", "memory_notifications") is False


def test_other_platform_falls_back_to_global():
    cfg = _cfg(
        memory_notifications="verbose",
        platforms={"slack": {"memory_notifications": False}},
    )
    assert (
        resolve_display_setting(cfg, "telegram", "memory_notifications")
        == "verbose"
    )


def test_unset_everywhere_returns_fallback():
    assert (
        resolve_display_setting(_cfg(), "slack", "memory_notifications", None)
        is None
    )


def test_gateway_call_site_bool_normalisation_matches_off():
    # Mirrors the run.py call site: bool False must become "off".
    val = resolve_display_setting(
        _cfg(platforms={"slack": {"memory_notifications": False}}),
        "slack",
        "memory_notifications",
    )
    if isinstance(val, bool):
        val = "on" if val else "off"
    assert val == "off"
