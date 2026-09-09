"""The live turn must apply platform display overrides, including on cached agents."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from gateway.config import Platform
from gateway.display_config import resolve_display_setting
from gateway.run_turn_runner import TurnRunner
from gateway.turn_context import TurnContext


@pytest.fixture
def wire_notifications(monkeypatch):
    def wire(agent, config, platform):
        ctx = TurnContext(
            source=SimpleNamespace(platform=platform),
            user_config=config,
            resolve_display_setting=resolve_display_setting,
            _hooks_ref=SimpleNamespace(loaded_hooks=False),
        )
        runner = Mock(
            _service_tier=None,
            _consume_pending_turn_sidecar_notes=lambda key: [],
        )
        turn = TurnRunner(runner, ctx)
        # Adjacent callbacks need no transport or background worker for this contract.
        monkeypatch.setattr(turn, "_merge_turn_request_overrides", Mock())
        monkeypatch.setattr(turn, "_make_bg_review_callbacks", lambda: (None, None))
        monkeypatch.setattr(turn, "_attach_session_title_callback", Mock())
        turn._wire_turn_agent_callbacks(agent, None, None, None, None, False)
        assert ctx.agent_holder[0] is agent
        return agent.memory_notifications

    return wire


@pytest.mark.parametrize("config,platform,expected", [
    ({"display": {"memory_notifications": "on", "platforms": {"slack": {"memory_notifications": False}}}}, Platform.SLACK, "off"),
    ({"display": {"memory_notifications": "off", "platforms": {"slack": {"memory_notifications": True}}}}, Platform.SLACK, "on"),
    ({"display": {"memory_notifications": "off", "platforms": {"slack": {"memory_notifications": "VERBOSE"}}}}, Platform.SLACK, "verbose"),
    ({"display": {"memory_notifications": "verbose", "platforms": {"slack": {"memory_notifications": False}}}}, Platform.TELEGRAM, "verbose"),
    ({"display": {"memory_notifications": False}}, Platform.TELEGRAM, "off"),
    ({"display": {"memory_notifications": True}}, Platform.TELEGRAM, "on"),
    ({}, Platform.SLACK, "on"),
    ({"display": {"platforms": {"cli": {"memory_notifications": False}}}}, Platform.LOCAL, "off"),
])
def test_turn_wires_platform_notification_precedence(wire_notifications, config, platform, expected):
    assert wire_notifications(SimpleNamespace(), config, platform) == expected


def test_cached_agent_receives_current_turn_notification_setting(wire_notifications):
    agent = SimpleNamespace()
    disabled = {"display": {"platforms": {"slack": {"memory_notifications": False}}}}
    assert wire_notifications(agent, disabled, Platform.SLACK) == "off"
    assert wire_notifications(agent, {}, Platform.SLACK) == "on"
