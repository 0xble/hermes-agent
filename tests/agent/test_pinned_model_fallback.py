"""Exercise a pinned real agent against two independent loopback routes."""

from unittest.mock import patch

import pytest

from tests.agent import _shared_cooldown_stub as harness


@pytest.mark.parametrize("same_model_route", [False, True])
def test_pinned_model_skips_other_family_on_billing_failure(tmp_path, monkeypatch, same_model_route):
    home = tmp_path / "hermes"
    harness.write_home_config(home)
    monkeypatch.setitem(harness._FAILURE_BODIES, 503, {"error": {"message": (
        "auth_unavailable: no auth available; last upstream error: "
        "Your credit balance is too low to access the Anthropic API"
    )}})
    with harness.StubProvider() as primary, harness.StubProvider() as other, harness.StubProvider() as alternate:
        primary.failure_status = 503
        primary.retry_after = None
        alternate.primary_available = True
        alternate_entry = (
            ', {"provider": "custom", "model": "primary-model", '
            f'"base_url": {alternate.url!r}, "api_key": "fixture"}}'
        ) if same_model_route else ""
        child = harness.CHILD.replace(
            '"base_url": url, "api_key": "fixture", "api_mode": "chat_completions"}]',
            f'"base_url": {other.url!r}, "api_key": "fixture"}}{alternate_entry}]',
        ).replace('fallback_model=fallback,', 'fallback_model=fallback, model_pinned=True,').replace(
            '"notices": notices,',
            '"notices": notices, "requested_model": result.get("requested_model"), '
            '"served_model": result.get("served_model"), '
            '"reason": result.get("fallback_reason") or result.get("failure_reason"),',
        )
        with patch.object(harness, "CHILD", child):
            result = harness.run_turn(home, primary.url, "nonstream")

        assert primary.requests
        assert not other.requests
        assert result["failed"] is not same_model_route
        assert result["reason"] == "billing"
        if same_model_route:
            assert result["requested_model"] == result["served_model"] == "primary-model"
            assert result["final_response"] == "OK from primary-model"
            assert [r["model"] for r in alternate.requests if r["model"] is not None] == ["primary-model"]
        else:
            assert "credit" in result["final_response"].lower()


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
