"""Regression coverage for quiet /loop wakeups through the gateway response boundary."""

import sys
from types import ModuleType, SimpleNamespace

import pytest

from gateway.run_turn import GatewayTurnMixin


class _ShapeRunner(GatewayTurnMixin):
    def __init__(self):
        self.async_session_store = SimpleNamespace(clear_resume_pending=self._noop)

    async def _noop(self, *_args, **_kwargs):
        return None

    async def _clear_restart_failure_count(self, *_args, **_kwargs):
        return None


def _stub_gateway_run(monkeypatch):
    module = ModuleType("gateway.run")
    module._is_gateway_hidden_reasoning_incomplete_turn = lambda _result: False
    module._normalize_empty_agent_response = lambda _result, response, history_len: response
    module._sanitize_gateway_final_response = lambda _platform, response, interrupted: response
    module._should_clear_resume_pending_after_turn = lambda _result: False
    monkeypatch.setitem(sys.modules, "gateway.run", module)


@pytest.mark.asyncio
async def test_loop_wakeup_silence_is_suppressed_but_changed_result_is_delivered(monkeypatch):
    _stub_gateway_run(monkeypatch)
    runner = _ShapeRunner()
    source = SimpleNamespace(chat_id="chat-loop", platform=SimpleNamespace(value="telegram"))
    session = SimpleNamespace(session_id="loop-session")

    silent, silent_marker, _ = await runner._hmwa_shape_agent_response(
        {"final_response": "[SILENT]", "messages": [], "api_calls": 1},
        source, history=[], session_entry=session, session_key=None,
        _quick_key=None, run_generation=0, _run_start_session_id="loop-session",
        _platform_name="telegram", _msg_start_time=0.0,
        persist_user_display_kind="internal_notification", reply_expected=False,
    )
    changed, changed_marker, _ = await runner._hmwa_shape_agent_response(
        {"final_response": "Queue depth changed to 2.", "messages": [], "api_calls": 1},
        source, history=[], session_entry=session, session_key=None,
        _quick_key=None, run_generation=0, _run_start_session_id="loop-session",
        _platform_name="telegram", _msg_start_time=0.0,
        persist_user_display_kind="internal_notification", reply_expected=False,
    )

    runner._delivery_adapter_for = lambda _source: None
    runner._should_send_voice_reply = lambda *_args, **_kwargs: False
    event = SimpleNamespace(metadata={}, internal=True, source=source)
    delivered_silent = await runner._hmwa_deliver_turn_response(
        event, source, session, None, 0,
        {"final_response": "[SILENT]"}, [], silent, None, silent_marker,
        raw_response=silent,
    )
    delivered_changed = await runner._hmwa_deliver_turn_response(
        event, source, session, None, 0,
        {"final_response": changed}, [], changed, None, changed_marker,
        raw_response=changed,
    )

    assert delivered_silent == ""
    assert delivered_changed == "Queue depth changed to 2."
