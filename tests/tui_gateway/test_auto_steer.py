"""The TUI auto policy uses the same immutable receipt and queue handoff as messaging."""
import re
import threading
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent.steer_deferral import defer_message
from tui_gateway import server


@pytest.mark.parametrize("redirect", [True, False])
@pytest.mark.parametrize("failed_side_effect", [None, "persist", "emit"])
def test_auto_redirects_then_steers_and_defers_once(monkeypatch, redirect, failed_side_effect):
    monkeypatch.setattr(server, "_load_busy_input_mode", lambda: "auto")
    monkeypatch.setattr(server, "_session_compression_in_flight", lambda _: False)
    monkeypatch.setattr(server, "_persist_queued_user_row", Mock(
        side_effect=OSError("row write failed") if failed_side_effect == "persist" else None))
    monkeypatch.setattr(server, "_emit", Mock(
        side_effect=OSError("transport write failed") if failed_side_effect == "emit" else None))
    agent = SimpleNamespace(_current_turn_id="turn", _supports_active_turn_redirect=True,
                            redirect=Mock(return_value=redirect), steer=Mock(return_value=True),
                            interrupt=Mock())
    transport = object()
    session = dict(agent=agent, running=True, history_lock=threading.Lock(), history=[],
                   attached_images=[], inflight_turn={"user": "original"})
    result = server._handle_busy_submit("rid", "sid", session, "unrelated", transport)
    assert result["result"]["status"] == "steered"
    method = agent.redirect if redirect else agent.steer
    key = re.search(r"Auto-steer message_id: ([a-f0-9-]+)", method.call_args.args[0]).group(1)
    assert not session.get("queued_prompt")
    for _ in range(2):
        assert defer_message(agent, {"message_id": key})["accepted"]
    assert session["queued_prompt"]["text"] == "unrelated"
    assert session["queued_prompt"]["transport"] is transport
    assert not session.get("queued_prompts")
    agent.interrupt.assert_not_called()
    server._emit.assert_called_once_with("message.interim", "sid", {"text": "Queued after this."})


@pytest.mark.parametrize("first", ["original", "unrelated"])
def test_later_queued_text_cannot_merge_or_scrub_a_deferred_envelope(first):
    session = {"inflight_turn": {"user": "original"}}
    first_transport, second_transport = object(), object()
    deferred = server._enqueue_prompt(session, first, first_transport, preserve_event=True)
    later = server._enqueue_prompt(session, "later queued input", second_transport)

    assert session["queued_prompt"] is deferred
    assert deferred["text"] == first and deferred["transport"] is first_transport
    assert session["queued_prompts"] == [later]
    assert later["text"] == "later queued input" and later["transport"] is second_transport


def test_auto_forced_queue_does_not_offer_deferral(monkeypatch):
    monkeypatch.setattr(server, "_load_busy_input_mode", lambda: "auto")
    monkeypatch.setattr(server, "_persist_queued_user_row", lambda *args: None)
    agent = SimpleNamespace(redirect=Mock(), steer=Mock(), interrupt=Mock())
    session = dict(agent=agent, running=True, history_lock=threading.Lock(), attached_images=[])
    assert server._handle_busy_submit("rid", "sid", session, "next", None, queued=True)["result"]["status"] == "queued"
    agent.redirect.assert_not_called()
    agent.steer.assert_not_called()
    agent.interrupt.assert_not_called()
