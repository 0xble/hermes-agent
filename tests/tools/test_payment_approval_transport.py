"""Payment consent keeps missing transport separate from human rejection, offline."""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent.vault_store import VaultItemMeta
from gateway.session_context import clear_session_vars, set_session_vars
from tools import approval, approval_context, approval_prompt, browser_vault_tool as vault
from tools.registry import registry


@pytest.fixture
def payment(monkeypatch):
    meta = VaultItemMeta(id="mg:card", kind="payment", label="Test Card", origin=None, created_at="")
    backend = SimpleNamespace(
        name="manager", display_name="Manager", binds_cards_to_page=True, needs_unlock=False,
        get_meta=lambda _: meta, is_unlocked=lambda: True,
        resolve_secret=Mock(side_effect=AssertionError("Unapproved card must never be resolved")),
    )
    monkeypatch.setattr("agent.vault_backends.backend_for_handle", lambda _: backend)
    monkeypatch.setattr(vault, "_focus_bound_origin", lambda *_: None)
    monkeypatch.setattr(vault, "_payment_retry_until", {})
    monkeypatch.setattr(vault, "_eval_js", lambda _, expression: {
        "success": True, "result": "https://shop.test/pay" if "location.href" in expression else
        json.dumps([{"autocomplete": "cc-number", "index": 0, "type": "text"}]),
    })
    write = Mock(side_effect=AssertionError("Unapproved card must never be written"))
    monkeypatch.setattr(vault, "_eval_js_secret", write)
    monkeypatch.setattr(approval, "_gateway_queues", {})
    monkeypatch.setattr(approval, "_gateway_notify_cbs", {})
    tokens = set_session_vars(platform="telegram", session_key="payment-approval-test")
    key_token = approval_context.set_current_session_key("payment-approval-test")
    try:
        yield backend.resolve_secret, write
    finally:
        approval.unregister_gateway_notify("payment-approval-test")
        approval_context.reset_current_session_key(key_token)
        clear_session_vars(tokens)


def fill():
    return json.loads(registry.dispatch("browser_vault", {"action": "fill", "handle": "mg:card"},
                                        task_id="payment-approval-test"))


@pytest.mark.parametrize("transport", ["missing", "notify_failed", "dispatch_failed"])
def test_unavailable_payment_can_return_to_an_active_turn_without_becoming_a_refusal(payment, monkeypatch, transport):
    resolve, write = payment
    if transport != "missing":
        def unavailable(_):
            raise RuntimeError("offline transport failed")
        approval.register_gateway_notify("payment-approval-test", unavailable)
    if transport == "dispatch_failed":
        with monkeypatch.context() as dispatch:
            dispatch.setattr(approval_prompt._gw, "_await_gateway_decision",
                             Mock(side_effect=RuntimeError("offline dispatch failed")))
            blocked = fill()
    else:
        blocked = fill()
    assert blocked["success"] is False
    assert blocked["error_type"] == "approval_unavailable"
    assert "No user decision was received" in blocked["error"]
    assert "active top-level turn" in blocked["error"]
    resolve.assert_not_called()
    write.assert_not_called()

    # The parent can re-open consent once it owns a live channel. Transport failure
    # must not latch the same retry refusal as an explicit human deny.
    prompts = []
    def reject(data):
        prompts.append(data)
        assert approval.resolve_gateway_approval("payment-approval-test", "deny",
                                                 request_id=data["request_id"]) == 1
    approval.register_gateway_notify("payment-approval-test", reject)
    assert fill()["error_type"] == "payment_declined"
    assert fill()["error_type"] == "payment_retry_refused"
    assert len(prompts) == 1
    resolve.assert_not_called()
    write.assert_not_called()


@pytest.mark.parametrize("choice", ["deny", "cancelled", "once"])
def test_available_consent_retains_human_decisions(monkeypatch, choice):
    monkeypatch.setattr(approval_context, "_is_gateway_approval_context", lambda: True)
    monkeypatch.setattr(approval_context, "_is_single_query_approval_context", lambda: False)
    monkeypatch.setattr(approval, "_gateway_notify_cb", lambda _: lambda _: None)
    monkeypatch.setattr(approval_prompt._gw, "_await_gateway_decision", lambda *_args, **_kwargs: {
        "resolved": choice != "cancelled", "cancelled": choice == "cancelled", "choice": choice,
    })
    assert approval_prompt.request_elicitation_consent("test", "test") == {
        "deny": "decline", "cancelled": "cancel", "once": "accept",
    }[choice]
