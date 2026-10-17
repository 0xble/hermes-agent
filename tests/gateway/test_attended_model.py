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


def test_precedence_once_session_temporary_attended_default():
    cfg = _cfg(
        default="default", provider="default-provider",
        attended={"model": "attended", "provider": "attended-provider", "reasoning_effort": "medium"},
    )
    state = {
        "once_model": "once", "session_model": "session", "temporary_model": "temporary",
        "last_user_activity_at": 1000, "default_reasoning": "low",
    }
    assert resolve_attended_model(state, 1001, cfg).model == "once"
    state.pop("once_model")
    assert resolve_attended_model(state, 1001, cfg).model == "session"
    state.pop("session_model")
    assert resolve_attended_model(state, 1001, cfg).model == "temporary"
    state.pop("temporary_model")
    assert resolve_attended_model(state, 1001, cfg).model == "attended"
    assert resolve_attended_model({"default_model": "default", "default_provider": "p"}, 1001, cfg).model == "default"


def test_channel_model_is_default_but_attended_beats_it():
    cfg = _cfg(attended={"model": "attended", "provider": "attended-provider"})
    active = resolve_attended_model(
        {"default_model": "default", "default_provider": "p", "channel_model": "channel",
         "channel_provider": "channel-provider", "last_user_activity_at": 1000}, 1001, cfg,
    )
    assert (active.model, active.provider, active.reason) == ("attended", "attended-provider", "attended")
    idle = resolve_attended_model(
        {"default_model": "default", "default_provider": "p", "channel_model": "channel",
         "channel_provider": "channel-provider", "last_user_activity_at": 0}, 1001, cfg,
    )
    assert (idle.model, idle.provider, idle.reason) == ("channel", "channel-provider", "default")


def test_plain_model_pick_uses_that_models_reasoning_not_attended_effort():
    cfg = _cfg(attended={"model": "attended", "reasoning_effort": "xhigh"})
    result = resolve_attended_model(
        {"temporary_model": "picked", "default_reasoning": "low", "last_user_activity_at": 1000},
        1001, cfg,
    )
    assert (result.model, result.reasoning_effort, result.reason) == ("picked", "low", "explicit")


def test_attended_uses_attended_model_reasoning_when_not_explicitly_configured():
    cfg = _cfg(attended={"model": "attended"})
    result = resolve_attended_model(
        {"last_user_activity_at": 1000, "default_reasoning": "xhigh", "attended_reasoning": "medium"},
        1001, cfg,
    )
    assert result.reasoning_effort == "medium"


def test_reasoning_scope_precedence_and_active_effort():
    cfg = _cfg(attended={"model": "attended", "reasoning_effort": "medium"})
    state = {
        "session_reasoning": "high", "temporary_reasoning": "low",
        "last_user_activity_at": 1000, "default_reasoning": "minimal",
    }
    assert resolve_attended_model(state, 1001, cfg).reasoning_effort == "high"
    state.pop("session_reasoning")
    assert resolve_attended_model(state, 1001, cfg).reasoning_effort == "low"
    state.pop("temporary_reasoning")
    assert resolve_attended_model(state, 1001, cfg).reasoning_effort == "medium"
    assert resolve_attended_model({"default_reasoning": "minimal"}, 1001, cfg).reasoning_effort == "minimal"


def test_decision_key_changes_with_route_not_with_live_fallback_model():
    cfg = _cfg(attended={"model": "attended", "provider": "p2"})
    decision = resolve_attended_model({"last_user_activity_at": 1000}, 1001, cfg)
    assert decision.decision_key == ("attended", "p2", "", "attended", True)
    # The key represents the desired route. A fallback model is intentionally not an input.
    assert "fallback" not in decision.decision_key


def test_temporary_reasoning_expires_with_idle_but_session_reasoning_does_not():
    cfg = _cfg(attended={"model": "attended", "reasoning_effort": "medium"}, idle_revert_minutes=1)
    temporary = {"temporary_reasoning": "high", "last_user_activity_at": 0}
    assert resolve_attended_model(temporary, 30, cfg).reasoning_effort == "high"
    # Expiry is performed by the session-state layer; once the temporary pick is cleared,
    # the resolver immediately returns the default lane.
    temporary.pop("temporary_reasoning")
    assert resolve_attended_model(temporary, 61, cfg).reasoning_effort == ""
    pinned = {"session_reasoning": "high", "last_user_activity_at": 0}
    assert resolve_attended_model(pinned, 61, cfg).reasoning_effort == "high"


def test_once_reasoning_beats_session_and_temporary_reasoning():
    cfg = _cfg(attended={"model": "attended", "reasoning_effort": "medium"})
    result = resolve_attended_model(
        {"once_reasoning": "max", "session_reasoning": "high", "temporary_reasoning": "low",
         "last_user_activity_at": 1000}, 1001, cfg,
    )
    assert result.reasoning_effort == "max"


def test_unset_keys_preserve_default_route_and_reasoning():
    cfg = {"model": {"default": "gpt-6.1-sol", "provider": "nous"}}
    result = resolve_attended_model(
        {"default_model": "gpt-6.1-sol", "default_provider": "nous", "default_reasoning": "xhigh"},
        1000, cfg,
    )
    assert result.decision_key == ("gpt-6.1-sol", "nous", "xhigh", "default", False)


def test_invalid_attended_effort_is_ignored_without_changing_default():
    cfg = _cfg(attended={"model": "attended", "reasoning_effort": "not-a-level"})
    result = resolve_attended_model({"last_user_activity_at": 1000}, 1001, cfg)
    assert result.reasoning_effort == ""


def test_two_session_state_inputs_remain_independent():
    cfg = _cfg(attended={"model": "attended"})
    active = resolve_attended_model({"last_user_activity_at": 1000}, 1001, cfg)
    idle = resolve_attended_model({"last_user_activity_at": 0}, 1001, cfg)
    assert (active.reason, idle.reason) == ("attended", "default")
