from types import SimpleNamespace

from gateway.attended_model import (
    is_active,
    is_genuine_user_message,
    parse_attended_config,
    resolve_attended_model,
)


def _cfg(**model):
    return {"model": model}


def test_unset_attended_config_is_disabled_and_default_is_unchanged():
    cfg = {"model": {"default": "gpt-6.1-sol"}}
    assert parse_attended_config(cfg).enabled is False
    result = resolve_attended_model(
        {"default_model": "gpt-6.1-sol", "default_provider": "nous"}, 1000, cfg
    )
    assert (result.model, result.provider, result.reason) == ("gpt-6.1-sol", "nous", "default")


def test_attended_wins_over_channel_default_while_active():
    cfg = _cfg(
        default="gpt-6.1-sol", provider="nous",
        attended={"model": "claude-opus-5-5", "provider": "custom:claude", "reasoning_effort": "medium"},
    )
    result = resolve_attended_model(
        {
            "default_model": "gpt-6.1-sol", "default_provider": "nous",
            "channel_model": "channel-model", "last_user_activity_at": 1000,
        },
        1001, cfg,
    )
    assert (result.model, result.provider, result.reason, result.reasoning_effort) == (
        "claude-opus-5-5", "custom:claude", "attended", "medium"
    )


def test_topic_edit_model_override_beats_attended_while_active():
    cfg = _cfg(
        default="default", provider="p",
        attended={"model": "opus", "provider": "attended"},
    )
    result = resolve_attended_model(
        {
            "default_model": "default", "default_provider": "p",
            # telegram_topic commits this as the normal session override.
            "model_override": "topic-picked",
            "last_user_activity_at": 1000,
        },
        1001, cfg,
    )
    assert (result.model, result.provider, result.reason) == ("topic-picked", "p", "explicit")


def test_explicit_session_pick_beats_attended_and_expires_only_when_temporary():
    cfg = _cfg(attended={"model": "opus"}, idle_revert_minutes=1)
    state = {
        "default_model": "default", "default_provider": "p",
        "session_model": "pinned", "temporary_model": "temporary",
        "last_user_activity_at": 0,
    }
    assert resolve_attended_model(state, 30, cfg).model == "pinned"
    state.pop("session_model")
    assert resolve_attended_model(state, 30, cfg).model == "temporary"
    assert resolve_attended_model({"default_model": "default", "last_user_activity_at": 0}, 61, cfg).reason == "default"


def test_relay_and_internal_events_do_not_reset_activity():
    assert is_genuine_user_message(SimpleNamespace(text="hello", internal=False, metadata={}))
    assert not is_genuine_user_message(SimpleNamespace(text="hello\n[relay from=a receipt=b]", internal=False, metadata={}))
    assert not is_genuine_user_message(SimpleNamespace(text="[CRON job]", internal=False, metadata={}))
    assert not is_genuine_user_message(SimpleNamespace(text="wake", internal=True, metadata={}))
    assert not is_genuine_user_message(SimpleNamespace(text="edit", internal=False, metadata={"edited": True}))


def test_activity_window_boundary_is_expired():
    assert is_active(100, now=999, idle_revert_minutes=15) is True
    assert is_active(100, now=1000, idle_revert_minutes=15) is False
