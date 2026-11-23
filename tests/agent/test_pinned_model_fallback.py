"""Pinned delegation retries pooled-proxy billing errors without changing model."""

from typing import Any
from unittest.mock import patch

import pytest

from tests.agent import _shared_cooldown_stub as harness
from tests.tools.test_delegate import _make_mock_parent
from tools.delegate_tool import _build_child_agent


_BILLING_BODY = {
    "error": {
        "type": "insufficient_quota",
        "code": "credit_balance_exhausted",
        "message": "You have no credits remaining",
    }
}


def _build_pinned_child(
    primary, *, failures: int, monkeypatch, allow_billing_fallback=False, owned_chain=True,
):
    """Build a child through the production delegate constructor."""
    primary.primary_available = True
    primary.billing_failures = failures
    monkeypatch.setitem(harness._FAILURE_BODIES, 429, _BILLING_BODY)
    parent = _make_mock_parent()
    fallback = [{
        "provider": "custom", "model": harness.FALLBACK_MODEL,
        "base_url": primary.url, "api_key": "fixture", "api_mode": "chat_completions",
    }]
    parent._fallback_chain = fallback
    routing_cfg = {"model": harness.PRIMARY_MODEL, "allow_billing_fallback": allow_billing_fallback}
    if owned_chain:
        routing_cfg["fallback_providers"] = fallback
    with patch("tools.delegate_tool._load_config", return_value={}):
        child: Any = _build_child_agent(
            task_index=0, goal="say hello", context=None, toolsets=[],
            model=harness.PRIMARY_MODEL, max_iterations=2, parent_agent=parent, task_count=1,
            override_provider="custom", override_base_url=primary.url,
            override_api_key="fixture", override_api_mode="chat_completions",
            routing_cfg=routing_cfg,
        )
    return child, fallback


def test_pinned_delegate_retries_pooled_billing_on_same_route(tmp_path, monkeypatch):
    """A dead pooled credential must not break PR #1355's same-model pin."""
    home = tmp_path / "hermes"
    harness.write_home_config(home)
    monkeypatch.setenv("HERMES_HOME", str(home))
    with harness.StubProvider() as primary:
        child, fallback = _build_pinned_child(primary, failures=1, monkeypatch=monkeypatch)
        monkeypatch.setattr("agent.proxy_request_class._enabled_for_route", lambda *_args: True)
        try:
            child._disable_streaming = True
            result = child.run_conversation("hello")
        finally:
            child.close()

    assert child._fallback_chain == fallback
    assert result["completed"] is True
    assert result["final_response"] == f"OK from {harness.PRIMARY_MODEL}"
    assert [request["model"] for request in primary.requests if request["model"] is not None] == [
        harness.PRIMARY_MODEL, harness.PRIMARY_MODEL,
    ]
    assert fallback[0]["model"] == harness.FALLBACK_MODEL


def test_non_proxy_billing_is_terminal_without_retry(tmp_path, monkeypatch):
    """The same OpenAI-compatible 429 stays terminal on an unopted-in route."""
    home = tmp_path / "hermes"
    harness.write_home_config(home)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setitem(harness._FAILURE_BODIES, 429, _BILLING_BODY)
    with harness.StubProvider() as primary:
        primary.primary_available = False
        primary.billing_failures = 1
        result = harness.run_turn(home, primary.url, "nonproxy")

    assert result["failed"] is True
    assert len([request for request in primary.requests if request["model"] is not None]) == 1
    assert [request["model"] for request in primary.requests if request["model"] is not None] == [harness.PRIMARY_MODEL]


def test_pooled_billing_stops_after_one_retry(tmp_path, monkeypatch):
    """Two consecutive pool billing failures produce exactly two same-route calls."""
    home = tmp_path / "hermes"
    harness.write_home_config(home)
    monkeypatch.setenv("HERMES_HOME", str(home))
    with harness.StubProvider() as primary:
        child, _fallback = _build_pinned_child(primary, failures=2, monkeypatch=monkeypatch)
        monkeypatch.setattr("agent.proxy_request_class._enabled_for_route", lambda *_args: True)
        try:
            child._disable_streaming = True
            result = child.run_conversation("hello")
        finally:
            child.close()

    assert result["completed"] is True
    assert result["final_response"] == f"OK from {harness.FALLBACK_MODEL}"
    assert [request["model"] for request in primary.requests if request["model"] is not None] == [
        harness.PRIMARY_MODEL, harness.PRIMARY_MODEL, harness.FALLBACK_MODEL,
    ]


def test_pinned_delegate_falls_back_after_proxy_503(tmp_path, monkeypatch):
    """A transient proxy 503 must leave the owned chain available to a pinned child."""
    home = tmp_path / "hermes"
    harness.write_home_config(home)
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setitem(harness._FAILURE_BODIES, 503, {
        "error": {"message": "upstream connect error or disconnect/reset before headers", "type": "server_error"},
    })
    with harness.StubProvider() as primary:
        primary.failure_status = 503
        primary.retry_after = None
        child, fallback = _build_pinned_child(primary, failures=0, monkeypatch=monkeypatch)
        primary.primary_available = False
        child._noninteractive_transient_retry_window_seconds = 0
        try:
            child._disable_streaming = True
            result = child.run_conversation("hello")
        finally:
            child.close()

    assert child._fallback_chain == fallback
    assert result["completed"] is True
    assert result["final_response"] == f"OK from {harness.FALLBACK_MODEL}"
    assert [request["model"] for request in primary.requests if request["model"] is not None] == [
        harness.PRIMARY_MODEL, harness.FALLBACK_MODEL,
    ]


def test_pinned_delegate_inherits_billing_fallback_only_when_enabled(tmp_path, monkeypatch):
    home = tmp_path / "hermes"
    harness.write_home_config(home)
    monkeypatch.setenv("HERMES_HOME", str(home))
    with harness.StubProvider() as primary:
        child, fallback = _build_pinned_child(
            primary, failures=2, monkeypatch=monkeypatch,
            allow_billing_fallback=True, owned_chain=False)
        try:
            child._disable_streaming = True
            result = child.run_conversation("hello")
        finally:
            child.close()

    assert result["completed"] is True
    assert result["final_response"] == f"OK from {harness.FALLBACK_MODEL}"
    assert child._fallback_chain == fallback
    assert [request["model"] for request in primary.requests if request["model"] is not None] == [
        harness.PRIMARY_MODEL, harness.FALLBACK_MODEL,
    ]


def test_pinned_child_keeps_cross_model_chain_only_when_billing_fallback_enabled():
    from types import SimpleNamespace
    from agent.agent_init_fallback import _pin_init_fallbacks

    chain = [{"provider": "anthropic", "model": "claude-sonnet-5-5"}]
    disabled = SimpleNamespace(model="gpt-6.2-sol", allow_billing_fallback=False)
    enabled = SimpleNamespace(model="gpt-6.2-sol", allow_billing_fallback=True)
    assert _pin_init_fallbacks(disabled, chain, True) == []
    assert _pin_init_fallbacks(enabled, chain, True) == chain
    assert disabled._pinned_fallback_model == disabled.model
    assert enabled._pinned_fallback_model == enabled.model


def test_owned_chain_never_pins():
    from types import SimpleNamespace
    from agent.agent_init_fallback import _pin_init_fallbacks

    chain = [{"provider": "anthropic", "model": "claude-sonnet-5-5"}]
    disabled = SimpleNamespace(model="gpt-6.2-sol", allow_billing_fallback=False)
    enabled = SimpleNamespace(model="gpt-6.2-sol", allow_billing_fallback=True)
    assert _pin_init_fallbacks(disabled, chain, True, fallback_chain_owned=True) == chain
    assert _pin_init_fallbacks(enabled, chain, True, fallback_chain_owned=True) == chain
    assert disabled._pinned_fallback_model is None
    assert enabled._pinned_fallback_model is None




@pytest.mark.parametrize("served", ["other-model", None])
def test_delegation_fallback_result_and_log(caplog, served):
    from types import SimpleNamespace

    from agent.fallback_attribution import note_fallback
    from tools.delegate_tool_child_run import _SchemaOutcome, _build_result_entry

    child = SimpleNamespace(model="other-model", provider="custom", _fallback_activated=True)
    caplog.set_level("INFO")
    note_fallback(child, "primary-model", "other-model", "custom", "billing")
    result = _build_result_entry(child, {
        "final_response": "done" if served else "billing failure",
        "completed": bool(served), "failed": not served,
    }, 0, 0.1, _SchemaOutcome(None, None, [], 0))
    assert result["route"]["requested_model"] == "primary-model"
    assert result["route"]["served_model"] == served
    assert result["route"]["fallback_reason"] == "billing"
    assert f"requested_model=primary-model served_model={served} reason=billing" in caplog.text
