"""A reviewed report goes through the real delivery lane and transcript mirror."""
import asyncio
from concurrent.futures import Future
from unittest.mock import MagicMock

from gateway.config import Platform, PlatformConfig
from gateway.platforms.base import SendResult
from hermes_state import SessionDB


def test_final_send_preserves_envelope_and_separates_report_from_assistant_decision(tmp_path, monkeypatch):
    from cron.scheduler_delivery import _deliver_result
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("hermes_state.DEFAULT_DB_PATH", tmp_path / "state.db")
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session("subject-session", source="telegram")
    db.append_message("subject-session", "user", "invisible review prompt")
    db.append_message("subject-session", "assistant", "PRESENT")
    subject = {"chat_id": "42", "thread_id": "139328", "user_id": "42",
               "session_id": "subject-session", "session_key": "subject-key"}
    monkeypatch.setattr("cron.scheduler_review._resolve_review_subject", lambda review: subject)
    monkeypatch.setattr("cron.scheduler_review._run_review_hop", lambda *a: "PRESENT")
    monkeypatch.setattr("cron.scheduler.load_config", lambda: {"cron": {"wrap_response": True}})
    config = MagicMock()
    config.platforms = {Platform.TELEGRAM: PlatformConfig(enabled=True)}
    config.get_home_channel = lambda platform: None
    monkeypatch.setattr("gateway.config.load_gateway_config", lambda: config)
    sent = []
    router = MagicMock()
    async def send(target, text, metadata, transport=None):
        sent.append((text, metadata))
        return SendResult(success=True, message_id="bot-message")
    router._deliver_to_platform = send
    monkeypatch.setattr("gateway.delivery.DeliveryRouter", lambda *a, **kw: router)
    def run(coro, loop):
        future = Future()
        future.set_result(asyncio.run(coro))
        return future
    monkeypatch.setattr(asyncio, "run_coroutine_threadsafe", run)
    loop = MagicMock()
    loop.is_running.return_value = True
    job = {"id": "job", "name": "Daily Report", "deliver": "local", "attach_to_session": False,
           "review": {"contract": "reports/stripe-brief.yaml", "subject": "139328"}}
    text = "# Stripe Priorities\n**Focus:** billing\n## Priorities\n- Ship it"
    assert _deliver_result(job, text, adapters={Platform.TELEGRAM: MagicMock()}, loop=loop) is None
    assert len(sent) == 1
    assert sent[0][0] == text
    assert str(sent[0][1]["thread_id"]) == "139328"
    messages = db.get_messages("subject-session")
    assert [message["role"] for message in messages] == ["user", "assistant", "user"]
    assert messages[-1]["content"] == "[Cron delivery: Daily Report]\n" + text
    db.close()


def test_unresolved_subject_persists_incident_and_receipt(tmp_path, monkeypatch):
    import json
    from unittest.mock import Mock
    from cron.incidents import list_incidents
    from cron.scheduler_review import _deliver_reviewed_result
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("cron.executions.EXECUTIONS_FILE", tmp_path / "cron/executions.db")
    send = Mock()
    monkeypatch.setattr("cron.scheduler_delivery._deliver_result", send)
    error = _deliver_reviewed_result({"id": "missing-subject", "deliver": "local", "review": {
        "subject": "missing", "contract": "reports/stripe-brief.yaml"}}, "original", {}, None)
    assert "state.db missing" in error
    send.assert_not_called()
    incidents = list_incidents()
    assert incidents[0]["job_id"] == "missing-subject"
    receipt = json.loads((tmp_path / "cron/review-receipts.jsonl").read_text(encoding="utf-8-sig"))
    assert receipt["decision"] == "UNRESOLVED"
    assert receipt["text_sent"] is None
