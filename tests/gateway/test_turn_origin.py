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
