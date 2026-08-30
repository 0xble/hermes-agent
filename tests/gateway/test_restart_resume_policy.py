from types import SimpleNamespace

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.run import build_resume_recovery_note, resolve_restart_resume_policy


def _adapter(*, interactive: bool = True, override: str | None = None):
    extra = {} if override is None else {"restart_resume_policy": override}
    return SimpleNamespace(
        interactive_resume=interactive,
        config=PlatformConfig(extra=extra),
    )


def test_global_continue_policy_applies_to_interactive_adapter():
    config = GatewayConfig(restart_resume_policy="continue")

    assert resolve_restart_resume_policy(config, _adapter()) == "continue"


def test_platform_override_wins_over_global_policy():
    config = GatewayConfig(restart_resume_policy="continue")

    assert resolve_restart_resume_policy(config, _adapter(override="ask")) == "ask"


def test_noninteractive_adapter_keeps_safe_continue_default():
    config = GatewayConfig(restart_resume_policy="ask")

    assert resolve_restart_resume_policy(config, _adapter(interactive=False)) == "continue"


@pytest.mark.parametrize("value", ["", "discard", True])
def test_invalid_global_policy_fails_at_config_construction(value):
    with pytest.raises(ValueError, match="restart_resume_policy"):
        GatewayConfig(restart_resume_policy=value)


def test_invalid_platform_override_fails_at_config_construction():
    platform = PlatformConfig(extra={"restart_resume_policy": "discard"})

    with pytest.raises(ValueError, match="restart_resume_policy"):
        GatewayConfig(platforms={Platform.TELEGRAM: platform})


def test_continue_guidance_is_platform_neutral_and_replay_safe():
    note = build_resume_recovery_note(
        "restart_timeout",
        "",
        restart_resume_policy="continue",
    )

    assert "CONTINUE the interrupted task" in note
    assert "first step that has no recorded result" in note
    assert "non-interactive platform" not in note
    assert "ask what they would like to do next" not in note
    assert "do NOT re-execute or verify it" in note


def test_gateway_config_round_trips_global_policy():
    config = GatewayConfig.from_dict({"restart_resume_policy": "continue"})

    assert config.restart_resume_policy == "continue"
    assert config.to_dict()["restart_resume_policy"] == "continue"