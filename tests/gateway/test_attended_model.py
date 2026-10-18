import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from gateway.attended_model import (
    AttendedResolution,
    format_attended_switch_back_notice,
    format_attended_switch_notice,
    friendly_model_name,
    is_active,
    is_genuine_user_message,
    parse_attended_config,
    resolve_attended_model,
)
from gateway.run_inbound import GatewayInboundMixin
from gateway.run_turn import GatewayTurnMixin


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


def test_attended_switch_notice_uses_card_style_and_friendly_name():
    assert friendly_model_name("claude-opus-5-5") == "Opus 5.5"
    assert friendly_model_name("claude-3-5-sonnet-20241022") == "3.5 Sonnet"
    assert friendly_model_name("claude-3-opus-20240229") == "3 Opus"
    assert friendly_model_name("gpt-6-astra") == "GPT-6 Astra"
    assert format_attended_switch_notice("claude-opus-5-5", "high") == (
        "⇄ Switched to Opus 5.5 while you're active."
    )
    assert format_attended_switch_notice(
        "claude-opus-5-5", {"enabled": True, "effort": "high"},
        idle_model="gpt-6.1-sol", idle_minutes=15,
    ) == "⇄ Switched to Opus 5.5 while you're active. Back to GPT-6.1 Sol after 15m idle."
    # No return clause when the idle route is the same model.
    assert format_attended_switch_notice(
        "claude-opus-5-5", "high", idle_model="claude-opus-5-5", idle_minutes=15,
    ) == "⇄ Switched to Opus 5.5 while you're active."


def test_attended_switch_back_notice_uses_idle_model_and_window():
    assert format_attended_switch_back_notice("gpt-6.1-sol", 15) == (
        "⇄ Back to GPT-6.1 Sol after 15m idle."
    )


def test_new_session_starts_attended_by_default_without_activity_stamp():
    cfg = _cfg(
        default="gpt-6.1-sol", provider="nous",
        attended={"model": "claude-opus-5-5", "provider": "custom:claude"},
    )
    result = resolve_attended_model(
        {"default_model": "gpt-6.1-sol", "default_provider": "nous", "new_session": True},
        1001, cfg,
    )
    assert (result.model, result.reason, result.active, result.new_session) == (
        "claude-opus-5-5", "attended", True, True,
    )
    disabled = _cfg(
        default="gpt-6.1-sol", provider="nous",
        attended={"model": "claude-opus-5-5", "provider": "custom:claude", "start_new_sessions": False},
    )
    assert resolve_attended_model(
        {"default_model": "gpt-6.1-sol", "default_provider": "nous", "new_session": True},
        1001, disabled,
    ).reason == "default"


def test_attended_resolution_names_idle_return_model():
    cfg = _cfg(attended={"model": "attended", "provider": "attended-provider"})
    active = resolve_attended_model(
        {"default_model": "default", "default_provider": "p", "last_user_activity_at": 1000}, 1001, cfg,
    )
    assert active.idle_model == "default"
    channel = resolve_attended_model(
        {"default_model": "default", "channel_model": "channel", "last_user_activity_at": 1000}, 1001, cfg,
    )
    assert channel.idle_model == "channel"


def test_unknown_claude_id_keeps_raw_display_value():
    assert friendly_model_name("claude-future-family-build") == "claude-future-family-build"


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


def test_dict_reasoning_override_resolves_to_effort_label():
    """Persisted /reasoning picks are dicts; the route must carry the bare effort name."""
    from hermes_constants import parse_reasoning_effort

    cfg = _cfg(attended={"model": "attended", "provider": "attended-provider", "reasoning_effort": "high"})
    for scope in ("session_reasoning", "temporary_reasoning", "reasoning_override"):
        decision = resolve_attended_model(
            {"default_model": "default", "last_user_activity_at": 1000,
             scope: {"enabled": True, "effort": "xhigh"}},
            1001, cfg,
        )
        assert decision.reasoning_effort == "xhigh"
        assert parse_reasoning_effort(decision.reasoning_effort) == {"enabled": True, "effort": "xhigh"}
    cfg_without_effort = _cfg(attended={"model": "attended", "provider": "attended-provider"})
    for source in ("default_reasoning", "attended_reasoning", "explicit_model_reasoning"):
        state = {
            "default_model": "default", "last_user_activity_at": 1000,
            source: {"enabled": True, "effort": "xhigh"},
        }
        if source == "explicit_model_reasoning":
            state["once_model"] = "manual"
        decision = resolve_attended_model(state, 1001, cfg_without_effort)
        assert decision.reasoning_effort == "xhigh"

    disabled = resolve_attended_model(
        {"default_model": "default", "last_user_activity_at": 1000,
         "session_reasoning": {"enabled": False}},
        1001, cfg,
    )
    assert disabled.reasoning_effort == "none"


def _attended_route_runner(state):
    def handoff(_session_key, *, conversation=None, runner=None):
        del runner
        for key, value in (conversation or {}).items():
            setattr(state.conversation, key, value)

    runner = SimpleNamespace(
        _session_state=lambda _key: state,
        _peek_session_state=lambda _key: state,
        _load_attended_activity_stamp=lambda _key: state.conversation.last_user_activity_at,
        _handoff_attended_state=handoff,
        _session_reasoning_override=lambda _key: None,
        _load_reasoning_config=lambda _model: {},
    )
    runner._attended_resolution_for_session = lambda **kwargs: (
        GatewayTurnMixin._attended_resolution_for_session(runner, **kwargs)
    )
    runner._ensure_attended_new_session_activity = lambda *args, **kwargs: (
        GatewayTurnMixin._ensure_attended_new_session_activity(runner, *args, **kwargs)
    )
    return runner


def test_first_user_message_preserves_new_session_boundary_before_activity_stamp():
    cfg = _cfg(
        default="gpt-6.1-sol", provider="nous",
        attended={"model": "claude-opus-5-5", "provider": "custom:claude"},
    )
    state = SimpleNamespace(conversation=SimpleNamespace(
        attended_new_session=False, last_user_activity_at=None,
        last_attended_decision=None, last_resolved_model=None, pending_attended_notice=None,
        model_override=None, model_override_scope="session",
        reasoning_override=None, reasoning_override_scope="session",
    ))
    entry = SimpleNamespace(
        session_key="k", session_id="sid", created_at="created", updated_at="created",
        was_auto_reset=False, is_fresh_reset=False,
    )

    async def mark_activity(_session_key):
        entry.updated_at = "updated"

    inbound = SimpleNamespace(
        async_session_store=SimpleNamespace(
            lookup_by_session_key=AsyncMock(return_value=entry),
            mark_user_activity=AsyncMock(side_effect=mark_activity),
        ),
        _session_state=lambda _key: state,
        _session_key_for_source=lambda _source: "k",
    )
    event = SimpleNamespace(text="hello", internal=False, metadata={})
    source = SimpleNamespace(platform=SimpleNamespace(value="telegram"), user_id="u")

    with patch("gateway.run._load_gateway_config", return_value=cfg):
        assert asyncio.run(
            GatewayInboundMixin._mark_attended_user_activity(inbound, event, source, "k")
        ) is True

    assert state.conversation.attended_new_session is True
    assert entry.created_at != entry.updated_at

    turn = SimpleNamespace(
        _session_state=lambda _key: state,
        _clear_conversation_scope=lambda _key, reason: None,
        _evict_cached_agent=lambda _key: None,
        hooks=SimpleNamespace(emit=AsyncMock()),
    )
    was_auto_reset, is_new_session = asyncio.run(
        GatewayTurnMixin._hmwa_open_session(turn, entry, "k", source)
    )
    assert (was_auto_reset, is_new_session) == (False, True)

    route_runner = _attended_route_runner(state)
    with patch("gateway.run._resolve_gateway_model", return_value="gpt-6.1-sol"):
        route = GatewayTurnMixin._attended_route_for_session(
            route_runner, source=None, session_key="k", user_config=cfg,
            override=None, last_model="",
        )
    assert route["model"] == "claude-opus-5-5"
    assert state.conversation.pending_attended_notice is None


def test_pre_turn_idle_expiry_stages_switch_back_notice_once():
    cfg = _cfg(
        default="gpt-6.1-sol", provider="nous", idle_revert_minutes=15,
        attended={"model": "claude-opus-5-5", "provider": "custom:claude"},
    )
    state = SimpleNamespace(conversation=SimpleNamespace(
        attended_new_session=False, last_user_activity_at=0,
        last_attended_decision=AttendedResolution(
            "claude-opus-5-5", "custom:claude", "high", "attended", True,
            idle_model="gpt-6.1-sol",
        ),
        pending_attended_notice=None, model_override=None, model_override_scope="session",
        reasoning_override=None, reasoning_override_scope="session",
    ))
    route_runner = _attended_route_runner(state)
    with patch("gateway.run._resolve_gateway_model", return_value="gpt-6.1-sol"):
        route = GatewayTurnMixin._attended_route_for_session(
            route_runner, source=None, session_key="k", user_config=cfg,
            override=None, last_model="claude-opus-5-5",
        )
    assert route is None
    assert state.conversation.pending_attended_notice == format_attended_switch_back_notice(
        "gpt-6.1-sol", 15,
    )
