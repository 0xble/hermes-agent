"""Agent lifecycle requests require one native card outside the Pacific maintenance window."""
from datetime import datetime
import json
from unittest.mock import Mock

import pytest

from tools.registry import ToolRegistry


@pytest.fixture
def lifecycle(monkeypatch):
    # Patch the wall-clock dependency, not the host timezone or datetime implementation.
    instant = [datetime.fromisoformat("2026-11-05T14:00:00-08:00").timestamp()]
    monkeypatch.setattr("time.time", lambda: instant[0])
    registry = ToolRegistry()
    handlers = {}
    for name in ("request_update", "request_restart"):
        handlers[name] = Mock(return_value=json.dumps({"success": True, "status": "accepted"}))
        registry.register(name=name, toolset="lifecycle", schema={"name": name}, handler=handlers[name])
    prompt = Mock(return_value="accept")
    monkeypatch.setattr("tools.approval_prompt.request_elicitation_consent", prompt)
    return registry, handlers, prompt, instant


@pytest.mark.parametrize("name", ["request_update", "request_restart"])
@pytest.mark.parametrize("choice,status", [("decline", "denied"), ("cancel", "timeout")])
def test_outside_window_deny_or_timeout_has_no_side_effect(lifecycle, name, choice, status):
    registry, handlers, prompt, _ = lifecycle
    prompt.return_value = choice
    result = json.loads(registry.dispatch(name, {"reason": "Apply tested fix"}))
    assert result["success"] is False
    assert result["status"] == status
    assert "nothing" in result["error"].lower()
    handlers[name].assert_not_called()
    prompt.assert_called_once()


@pytest.mark.parametrize("name", ["request_update", "request_restart"])
def test_outside_window_accept_dispatches_once_without_changing_arguments(lifecycle, name):
    registry, handlers, prompt, _ = lifecycle
    args = {"reason": "Apply tested fix"}
    if name == "request_update":
        args["branch"] = "fix/exact-branch"
    assert json.loads(registry.dispatch(name, args))["status"] == "accepted"
    handlers[name].assert_called_once_with(args)
    prompt.assert_called_once()
    message = prompt.call_args.args[0]
    assert "Apply tested fix" in message
    if name == "request_update":
        assert "fix/exact-branch" in message
    assert "2" in prompt.call_args.args[1] and "6" in prompt.call_args.args[1]


@pytest.mark.parametrize("name", ["request_update", "request_restart"])
@pytest.mark.parametrize("instant,card", [
    ("2026-11-05T01:59:59-08:00", True),
    ("2026-11-05T02:00:00-08:00", False),
    ("2026-11-05T05:59:59-08:00", False),
    ("2026-11-05T06:00:00-08:00", True),
    ("2026-07-05T09:00:00+00:00", False),  # 2 AM PDT (DST)
    ("2026-07-05T13:00:00+00:00", True),   # 6 AM PDT
])
def test_pacific_window_boundaries(lifecycle, name, instant, card):
    registry, handlers, prompt, clock = lifecycle
    clock[0] = datetime.fromisoformat(instant).timestamp()
    assert json.loads(registry.dispatch(name, {"reason": "Fix"}))["success"] is True
    assert prompt.call_count == int(card)
    handlers[name].assert_called_once()


def test_missing_approval_surface_fails_closed(lifecycle):
    from tools.approval_prompt import ApprovalUnavailable
    registry, handlers, prompt, _ = lifecycle
    prompt.side_effect = ApprovalUnavailable("no viewer attached")
    result = json.loads(registry.dispatch("request_restart", {"reason": "Fix"}))
    assert result["status"] == "approval_unavailable"
    handlers["request_restart"].assert_not_called()


@pytest.mark.parametrize("choice,expected", [("once", "accepted"), ("deny", "denied"), (None, "timeout")])
def test_native_card_round_trip(monkeypatch, tmp_path, choice, expected):
    from gateway.session_context import set_session_vars, clear_session_vars
    from tools import approval
    from tools import approval_context

    monkeypatch.setattr("time.time", lambda: datetime.fromisoformat("2026-11-05T14:00:00-08:00").timestamp())
    # Even permissive dangerous-command policy cannot substitute for this explicit card.
    monkeypatch.setattr(approval_context, "_get_approval_config", lambda: {"mode": "off", "timeout": 0})
    key = "lifecycle-native-card"
    cards = []

    def notify(card):
        cards.append(card)
        if choice is not None:
            assert approval.resolve_gateway_approval(key, choice, request_id=card["request_id"]) == 1

    tokens = set_session_vars(platform="telegram", session_key=key, chat_id="brian", cron_session="")
    approval.register_gateway_notify(key, notify)
    registry = ToolRegistry()
    handler = Mock(return_value='{"success": true, "status": "accepted"}')
    registry.register(name="request_restart", toolset="lifecycle", schema={"name": "request_restart"}, handler=handler)
    try:
        result = json.loads(registry.dispatch("request_restart", {"reason": "Verified fix"}))
    finally:
        approval.unregister_gateway_notify(key)
        clear_session_vars(tokens)
    assert result["status"] == expected
    assert handler.call_count == int(choice == "once")
    assert len(cards) == 1
    assert cards[0]["allow_session"] is False
    assert cards[0]["allow_permanent"] is False
    assert "Verified fix" in cards[0]["command"]
    assert not approval._gateway_queues.get(key)


def test_unrelated_tool_does_not_prompt(lifecycle):
    registry, _, prompt, _ = lifecycle
    handler = Mock(return_value='{"success": true}')
    registry.register(name="unrelated", toolset="other", schema={"name": "unrelated"}, handler=handler)
    assert json.loads(registry.dispatch("unrelated", {}))["success"]
    handler.assert_called_once_with({})
    prompt.assert_not_called()
