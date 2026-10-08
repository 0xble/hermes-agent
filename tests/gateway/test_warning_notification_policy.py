"""Diagnostic warning classification and per-platform delivery policy."""

from agent.status_output import StatusOutputMixin
from gateway.config import Platform
from gateway.run import _prepare_gateway_status_message
from gateway.warning_notifications import DiagnosticText, warning_notifications_enabled


FALLBACK_NOTICE = "⚠️ Model fallback: claude-opus-5-5 via custom unavailable; using gpt-6.1-sol via custom:codex-proxy."


def _config(*, slack_suppressed: bool) -> dict:
    return {"display": {"platforms": {"slack": {
        "suppress_warning_notifications": slack_suppressed,
    }}}}


def test_success_fallback_notice_is_classified_and_suppressed_only_on_slack(monkeypatch):
    emitted = []

    class Agent(StatusOutputMixin):
        def _emit_status(self, message):
            emitted.append(message)

    agent = Agent()
    agent.__dict__["_pending_fallback_notice"] = [FALLBACK_NOTICE]
    agent._emit_pending_fallback_notice()

    assert len(emitted) == 1
    assert isinstance(emitted[0], DiagnosticText)

    config = _config(slack_suppressed=True)
    monkeypatch.setattr("gateway.run._load_gateway_config", lambda: config)
    assert _prepare_gateway_status_message(Platform.SLACK, "lifecycle", emitted[0]) is None
    assert _prepare_gateway_status_message(Platform.TELEGRAM, "lifecycle", emitted[0]) == FALLBACK_NOTICE


def test_success_fallback_notice_delivers_when_slack_suppression_is_off(monkeypatch):
    config = _config(slack_suppressed=False)
    monkeypatch.setattr("gateway.run._load_gateway_config", lambda: config)
    notice = DiagnosticText(FALLBACK_NOTICE)
    assert _prepare_gateway_status_message(Platform.SLACK, "lifecycle", notice) == FALLBACK_NOTICE
    assert warning_notifications_enabled(Platform.SLACK, config)


def test_terminal_failure_line_stays_deliverable_when_slack_suppression_is_on(monkeypatch):
    config = _config(slack_suppressed=True)
    monkeypatch.setattr("gateway.run._load_gateway_config", lambda: config)
    failure = "❌ Rate limited after 3 retries"
    delivered = _prepare_gateway_status_message(Platform.SLACK, "lifecycle", failure)
    assert delivered is not None
    assert "rate-limiting" in delivered
    assert not isinstance(failure, DiagnosticText)
