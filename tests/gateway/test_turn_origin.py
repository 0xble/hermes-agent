import pytest

from gateway.config import Platform
from gateway.platforms.event import MessageEvent, turn_origin_for_event
from gateway.session import SessionSource
from gateway.session_context import clear_session_vars, get_session_env, set_session_vars


def test_default_deny_for_unidentified_non_internal_event():
    assert turn_origin_for_event(MessageEvent(text="event")) == "unknown"
    assert turn_origin_for_event(_event(source_is_bot=True)) == "unknown"


def _event(*, internal=False, metadata=None, relay=False, source_is_bot=False):
    source = SessionSource(
        platform=Platform.LOCAL,
        chat_id="chat",
        delivered_via_upstream_relay=relay,
        is_bot=source_is_bot,
    )
    return MessageEvent(text="event", source=source, internal=internal, metadata=metadata or {})


def test_turn_origin_covers_all_values_and_defaults_deny():
    cases = [
        ("user", _event()),
        ("relay", _event(relay=True)),
        ("process", _event(internal=True, metadata={"notification_origin": "process_registry_synthetic"})),
        ("goal_loop", _event(internal=True, metadata={"goal_continuation": True})),
        ("cron", _event(internal=True, metadata={"notification_origin": "cron_delivery"})),
        ("unknown", _event(internal=True)),
    ]
    for expected, event in cases:
        assert turn_origin_for_event(event) == expected


def test_merged_human_notification_is_user():
    notification = _event(internal=True, metadata={"notification_origin": "process_registry_synthetic"})
    human = _event()
    notification._hermes_absorbed_events = [human]  # type: ignore[attr-defined]
    assert turn_origin_for_event(notification) == "user"


def test_turn_origin_is_task_local_session_env():
    tokens = set_session_vars(turn_origin="goal_loop")
    try:
        assert get_session_env("HERMES_TURN_ORIGIN") == "goal_loop"
    finally:
        clear_session_vars(tokens)
    assert get_session_env("HERMES_TURN_ORIGIN") == ""


def test_session_env_origin_defaults_deny():
    tokens = set_session_vars(turn_origin="not-an-origin")
    try:
        assert get_session_env("HERMES_TURN_ORIGIN") == "unknown"
    finally:
        clear_session_vars(tokens)


def test_set_turn_origin_rebinds_after_session_vars_and_defaults_deny():
    from gateway.session_context import set_turn_origin

    tokens = set_session_vars()
    try:
        set_turn_origin("user")
        assert get_session_env("HERMES_TURN_ORIGIN") == "user"
        set_turn_origin("bogus")
        assert get_session_env("HERMES_TURN_ORIGIN") == "unknown"
    finally:
        clear_session_vars(tokens)


def test_webhook_ingress_is_not_user():
    source = SessionSource(
        platform=Platform.WEBHOOK,
        chat_id="webhook:route:1",
        chat_type="webhook",
        user_id="webhook:route",
    )
    event = MessageEvent(text="payload", source=source)
    assert turn_origin_for_event(event) == "unknown"


def test_legacy_loop_wakeup_marker_is_goal_loop():
    assert turn_origin_for_event(_event(internal=True, metadata={"hermes_loop_wakeup": True})) == "goal_loop"


def test_non_internal_loop_wakeup_marker_is_not_user():
    for marker in ("loop_wakeup", "hermes_loop_wakeup"):
        assert turn_origin_for_event(_event(metadata={marker: True})) == "goal_loop"


def test_none_absorbed_events_are_ignored():
    event = _event(metadata={"notification_origin": "process_registry_synthetic"})
    event._hermes_absorbed_events = None  # type: ignore[attr-defined]
    assert turn_origin_for_event(event) == "process"


def test_non_internal_event_with_notification_origin_is_not_user():
    for origin, expected in (
        ("relay_delivery", "relay"),
        ("cron_delivery", "cron"),
        ("process_registry_synthetic", "process"),
        ("delegation_result", "process"),
    ):
        assert turn_origin_for_event(_event(metadata={"notification_origin": origin})) == expected
    assert turn_origin_for_event(_event(metadata={"cron_delivery": True})) == "cron"


@pytest.mark.asyncio
@pytest.mark.parametrize(("head", "pending", "expected"), [
    ("user", {"internal": True, "metadata": {"notification_origin": "process_registry_synthetic"}}, "process"),
    ("process", {"internal": False, "metadata": {}}, "user"),
])
async def test_queued_followup_runs_with_its_own_origin_and_restores_head(head, pending, expected):
    """A drained follow-up is its own turn: plugins must see its origin, not the chain head's."""
    from types import SimpleNamespace
    from unittest.mock import AsyncMock, MagicMock

    from gateway.config import GatewayConfig
    from gateway.run import GatewayRunner
    from gateway.session_context import bound_turn_origin, set_turn_origin

    seen = []

    async def _run_agent(**_kwargs):
        seen.append(bound_turn_origin())
        return {"final_response": "done", "messages": []}

    runner = object.__new__(GatewayRunner)
    runner.config = GatewayConfig(group_sessions_per_user=False)
    runner.adapters = {}
    runner._MAX_INTERRUPT_DEPTH = 8
    runner._run_agent = _run_agent
    runner._run_agent_deliver_first_response = AsyncMock()
    runner._session_key_for_source = MagicMock(return_value="agent:main:local:dm:chat")
    runner._prepare_profile_scoped_inbound_message_text = AsyncMock(return_value="the follow-up")
    runner._reply_anchor_for_event = MagicMock(return_value=None)
    runner._delivery_adapter_for = MagicMock(return_value=None)
    runner._refresh_agent_cache_message_count = AsyncMock()
    runner._persist_prompt_pins = AsyncMock()
    runner._pinned_channel_inputs = lambda key, prompt, source, internal=False: (prompt, source)
    pending_event = _event(**pending)
    turn_ctx = SimpleNamespace(
        source=pending_event.source, session_id="sid", session_key="agent:main:local:dm:chat",
        run_generation=1, _interrupt_depth=0, history=[], _status_thread_metadata=None,
        context_prompt=None, result_holder=[None],
    )
    tokens = set_session_vars(turn_origin=head)
    try:
        await GatewayRunner._run_agent_queued_followup(
            runner, turn_ctx, adapter=None, pending="the follow-up", pending_event=pending_event,
            response="resp", result={"interrupted": True, "messages": []}, stream_task=None,
        )
        assert seen == [expected]
        assert bound_turn_origin() == head
    finally:
        set_turn_origin("unknown")
        clear_session_vars(tokens)


def test_relay_footer_on_owner_account_message_is_relay_not_user():
    footer = "[relay from=hermes:default/20261020_142933_e1bb870f receipt=7ad08b87 task=reply-pacing-e2e]"
    relayed = MessageEvent(
        text=f"Title\nRun the checks.\nFrom: Reply Pacing\n{footer}\n",
        source=SessionSource(platform=Platform.TELEGRAM, chat_id="chat", user_id="owner"),
    )
    assert turn_origin_for_event(relayed) == "relay"


def test_relay_footer_quoted_mid_message_stays_user():
    typed = MessageEvent(
        text="why did [relay from=hermes:default/x receipt=y] show up?\nplease check",
        source=SessionSource(platform=Platform.TELEGRAM, chat_id="chat", user_id="owner"),
    )
    assert turn_origin_for_event(typed) == "user"
