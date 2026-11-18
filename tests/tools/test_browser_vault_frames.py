"""Private Camofox payment-frame transport: fail closed, no secret diagnostics."""
from unittest.mock import Mock

import pytest

from tools import browser_camofox as backend
from tools import browser_vault_frames as frames

MERCHANT = "https://merchant.example"
PROCESSOR = "https://js.stripe.com"


@pytest.fixture
def transport(monkeypatch):
    session = {"tab_id": "tab-one", "user_id": "owner"}
    frames._bindings.clear()
    monkeypatch.setattr(backend, "get_camofox_url", lambda: "http://127.0.0.1:9223")
    monkeypatch.setattr(backend, "is_camofox_mode", lambda: True)
    monkeypatch.setattr(backend, "_get_session", lambda task: session)
    post = Mock(return_value={"ok": True, "result": [{"frame_id": "opaque", "origin": PROCESSOR}]})
    monkeypatch.setattr(backend, "_post", post)
    return post, session


def test_discovers_using_existing_evaluate_route(transport):
    post, _ = transport
    assert frames.discover_payment_frames("task", MERCHANT) == [{"frame_id": "opaque", "origin": PROCESSOR}]
    post.assert_called_once_with("/tabs/tab-one/evaluate", body={
        "paymentOperation": "discover", "merchantOrigin": MERCHANT,
        "userId": "owner", "secret": True,
    }, allow_redirects=False)


def test_empty_success_is_distinct_from_capability_failure(transport):
    post, _ = transport
    post.return_value = {"ok": True, "result": []}
    assert frames.discover_payment_frames("task", MERCHANT) == []
    post.return_value = {"ok": True, "result": 2}  # older page-only server
    with pytest.raises(frames.PaymentFrameError):
        frames.discover_payment_frames("task", MERCHANT)


@pytest.mark.parametrize("result", [None, {}, [{"frame_id": "x", "origin": "https://evil.example"}],
    [{"frame_id": "x", "origin": PROCESSOR}, {"frame_id": "x", "origin": PROCESSOR}]])
def test_rejects_malformed_discovery(transport, result):
    transport[0].return_value = {"ok": True, "result": result}
    with pytest.raises(frames.PaymentFrameError):
        frames.discover_payment_frames("task", MERCHANT)


def test_write_uses_origin_and_document_binding_and_no_redirect(transport):
    post, _ = transport
    frames.discover_payment_frames("task", MERCHANT)
    post.reset_mock()
    post.return_value = {"ok": True, "result": {"filled": ["cc-number"]}}
    result = frames.evaluate_payment_frame("task", "opaque", "synthetic_fixture_write()", MERCHANT, PROCESSOR, secret=True)
    assert result == {"success": True, "result": {"filled": ["cc-number"]}, "error": None}
    post.assert_called_once_with("/tabs/tab-one/evaluate", body={
        "paymentOperation": "evaluate", "frameId": "opaque", "expression": "synthetic_fixture_write()",
        "merchantOrigin": MERCHANT, "frameOrigin": PROCESSOR, "userId": "owner", "secret": True,
    }, allow_redirects=False)


def test_inspection_also_suppresses_plugin_events(transport):
    frames.discover_payment_frames("task", MERCHANT)
    frames.evaluate_payment_frame("task", "opaque", "document.title", MERCHANT, PROCESSOR)
    assert transport[0].call_args.kwargs["body"]["secret"] is True


@pytest.mark.parametrize("merchant", [MERCHANT + "/", MERCHANT + "/checkout", "null", "https://u:p@merchant.example", "https://merchant.example?x=1"])
def test_exact_origin_refusal_never_calls_transport(transport, merchant):
    with pytest.raises(frames.PaymentFrameError):
        frames.discover_payment_frames("task", merchant)
    assert not transport[0].called


def test_unapproved_frame_origin_never_sends_expression(transport):
    result = frames.evaluate_payment_frame("task", "opaque", "secret fixture", MERCHANT, "https://js.stripe.com.evil.example", True)
    assert result["success"] is False
    assert not transport[0].called


def test_backend_and_missing_tab_fail_without_fallback(transport, monkeypatch):
    monkeypatch.setattr(backend, "is_camofox_mode", lambda: False)
    with pytest.raises(frames.PaymentFrameError):
        frames.discover_payment_frames("task", MERCHANT)
    assert not transport[0].called
    monkeypatch.setattr(backend, "is_camofox_mode", lambda: True)
    transport[1]["tab_id"] = None
    assert not frames.evaluate_payment_frame("task", "opaque", "x", MERCHANT, PROCESSOR)["success"]
    assert not transport[0].called


def test_secret_transport_errors_are_not_echoed(transport, caplog):
    frames.discover_payment_frames("task", MERCHANT)
    transport[0].side_effect = RuntimeError("synthetic-secret-expression-and-value")
    result = frames.evaluate_payment_frame("task", "opaque", "synthetic-secret-expression-and-value", MERCHANT, PROCESSOR, True)
    assert not result["success"]
    assert "synthetic-secret" not in str(result) + caplog.text
    with pytest.raises(frames.PaymentFrameError) as error:
        frames.discover_payment_frames("task", MERCHANT)
    assert "synthetic-secret" not in str(error.value)


def test_other_task_or_switched_tab_cannot_reuse_ids(transport):
    frames.discover_payment_frames("task", MERCHANT)
    transport[0].reset_mock()
    assert not frames.evaluate_payment_frame("foreign-task", "opaque", "x", MERCHANT, PROCESSOR)["success"]
    transport[1]["tab_id"] = "other-tab"
    assert not frames.evaluate_payment_frame("task", "opaque", "x", MERCHANT, PROCESSOR)["success"]
    assert not transport[0].called


def test_undiscovered_or_wrong_merchant_never_sends_expression(transport):
    post, _ = transport
    assert not frames.evaluate_payment_frame("task", "opaque", "synthetic-secret", MERCHANT, PROCESSOR)["success"]
    assert not post.called
    frames.discover_payment_frames("task", MERCHANT)
    post.reset_mock()
    assert not frames.evaluate_payment_frame("task", "opaque", "synthetic-secret", "https://different.example", PROCESSOR)["success"]
    assert not frames.evaluate_payment_frame("task", "forged", "synthetic-secret", MERCHANT, PROCESSOR)["success"]
    assert not post.called


def test_failed_rediscovery_revokes_local_binding(transport):
    post, _ = transport
    frames.discover_payment_frames("task", MERCHANT)
    post.return_value = {"ok": True, "result": None}
    with pytest.raises(frames.PaymentFrameError):
        frames.discover_payment_frames("task", MERCHANT)
    post.reset_mock()
    assert not frames.evaluate_payment_frame("task", "opaque", "synthetic-secret", MERCHANT, PROCESSOR)["success"]
    assert not post.called


def test_profile_and_server_bindings_do_not_cross(transport, monkeypatch):
    post, _ = transport
    frames.discover_payment_frames("task", MERCHANT)
    post.reset_mock()
    with monkeypatch.context() as profile:
        profile.setattr(frames, "hermes_home_key", lambda: "different-profile")
        assert not frames.evaluate_payment_frame("task", "opaque", "synthetic-secret", MERCHANT, PROCESSOR)["success"]
        assert not post.called
    monkeypatch.setattr(backend, "get_camofox_url", lambda: "http://different-camofox")
    assert not frames.evaluate_payment_frame("task", "opaque", "synthetic-secret", MERCHANT, PROCESSOR)["success"]
    assert not post.called


def test_tab_switch_during_request_is_rejected(transport):
    post, session = transport
    def switched(*args, **kwargs):
        session["tab_id"] = "other-tab"
        return {"ok": True, "result": []}
    post.side_effect = switched
    with pytest.raises(frames.PaymentFrameError):
        frames.discover_payment_frames("task", MERCHANT)
