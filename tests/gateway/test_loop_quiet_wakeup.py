"""Quiet /loop wakeups through the real gateway fire path and response boundary.

A gateway /loop tick is fired by ``_loop_wakeup_fire_one`` from a real ``LoopManager``. The injected
event must be internal with ``reply_expected=False`` and carry a prompt that asks for a bare
``[SILENT]`` on a no-change tick; that event's ``[SILENT]`` reply is then suppressed at delivery
while a changed result is still delivered.
"""

import sys
from types import ModuleType, SimpleNamespace

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.response_filters import display_kind_for_event
from gateway.run import GatewayRunner
from gateway.run_turn import GatewayTurnMixin


@pytest.fixture()
def hermes_home(tmp_path, monkeypatch):
    from pathlib import Path

    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    from hermes_cli import goals
    from hermes_state_registry import close_all_under

    goals._DB_CACHE.clear()
    yield home
    goals._DB_CACHE.clear()
    close_all_under(home)


class _Adapter:
    def __init__(self):
        self.events = []

    async def handle_message(self, event):
        self.events.append(event)


def _runner(adapter):
    r = object.__new__(GatewayRunner)
    r.config = GatewayConfig(platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="t")})
    r.adapters = {Platform.TELEGRAM: adapter}
    r._profile_adapters = {}
    r._primary_profile_name = "default"
    r.session_store = None
    r._session_sources = None
    r._running_agents = {}
    r._running = True

    async def _inline(func, *args):
        return func(*args)

    r._run_in_executor_with_context = _inline
    return r


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


async def _deliver(event, final_response, monkeypatch):
    """Shape and deliver ``final_response`` for ``event`` as the turn path does, deriving the
    silence inputs (display kind, reply_expected) from the fired event itself."""
    _stub_gateway_run(monkeypatch)
    runner = _ShapeRunner()
    session = SimpleNamespace(session_id="loop-session")
    shaped, marker, _ = await runner._hmwa_shape_agent_response(
        {"final_response": final_response, "messages": [], "api_calls": 1},
        event.source, history=[], session_entry=session, session_key=None,
        _quick_key=None, run_generation=0, _run_start_session_id="loop-session",
        _platform_name="telegram", _msg_start_time=0.0,
        persist_user_display_kind=display_kind_for_event(event), reply_expected=event.reply_expected,
    )
    runner._delivery_adapter_for = lambda _source: None
    runner._should_send_voice_reply = lambda *_args, **_kwargs: False
    return await runner._hmwa_deliver_turn_response(
        event, event.source, session, None, 0,
        {"final_response": final_response}, [], shaped, None, marker, raw_response=shaped,
    )


@pytest.mark.asyncio
async def test_fired_loop_wakeup_asks_for_silence_and_its_silent_reply_is_not_delivered(
    hermes_home, monkeypatch,
):
    from hermes_cli.loops import LoopManager

    route = {"platform": "telegram", "chat_id": "42", "chat_type": "dm", "user_id": "42"}
    mgr = LoopManager(session_id="loop-sid")
    mgr.set("check the queue", interval_seconds=300, route=route)
    mgr.state.next_due_at = 0
    mgr._save()

    adapter = _Adapter()
    state = LoopManager(session_id="loop-sid").state
    await _runner(adapter)._loop_wakeup_fire_one("loop-sid", state, 1e12, set())

    assert len(adapter.events) == 1
    event = adapter.events[0]
    assert event.internal is True
    assert event.reply_expected is False
    assert "reply with exactly [SILENT] and nothing else" in event.text
    assert LoopManager(session_id="loop-sid").state.awaiting_response is True

    assert await _deliver(event, "[SILENT]", monkeypatch) == ""
    assert await _deliver(event, "Queue depth changed to 2.", monkeypatch) == "Queue depth changed to 2."
