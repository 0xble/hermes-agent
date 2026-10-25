"""The same configured Slack capabilities reach replies and cron agents."""

from types import SimpleNamespace

import pytest

from agent.system_prompt import platform_hint


@pytest.mark.parametrize("rich", [True, False, "true", "false", "on", "off"])
@pytest.mark.parametrize("platform", ["slack", "cron"])
def test_hint_matches_rich_renderer_config(monkeypatch, rich, platform):
    from gateway.session_context import _VAR_MAP
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: {
        "platforms": {"slack": {"extra": {"rich_blocks": rich}}}})
    token = _VAR_MAP["HERMES_CRON_AUTO_DELIVER_PLATFORM"].set("slack")
    try:
        hint = platform_hint(SimpleNamespace(platform=platform, _platform_hint_overrides=None))
    finally:
        _VAR_MAP["HERMES_CRON_AUTO_DELIVER_PLATFORM"].reset(token)
    enabled = rich in (True, "true", "on")
    assert ("aligned pipe tables" in hint) is enabled
    assert ("thumb:alt" in hint) is enabled
    assert ("cards YAML list" in hint) is enabled
    assert ("Tables fall back to monospace" in hint) is not enabled
    assert "MEDIA:" in hint
    assert "tables are NOT supported" not in hint


def test_top_level_slack_extra_overrides_nested(monkeypatch):
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: {
        "gateway": {"platforms": {"slack": {"extra": {"rich_blocks": True}}}},
        "platforms": {"slack": {"extra": {"rich_blocks": False}}}})
    hint = platform_hint(SimpleNamespace(platform="slack"))
    assert "thumb:alt" not in hint


def test_native_markdown_precedence_does_not_advertise_local_card_syntax(monkeypatch):
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: {
        "platforms": {"slack": {"extra": {"rich_blocks": True, "markdown_blocks": True}}}})
    assert "cards YAML list" not in platform_hint(SimpleNamespace(platform="slack"))
