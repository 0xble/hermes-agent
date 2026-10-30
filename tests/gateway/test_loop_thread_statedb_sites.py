"""Gateway loop paths that reached state.db, named by the loop guard on the live gateway.

Each case runs the real loop-side caller with the blocking state.db step replaced by a probe that
records its thread (the step itself often swallows errors, so the guard's raise alone would not
fail the test). The step must run off the event-loop thread.
"""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import hermes_loop_guard
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.event import MessageEvent
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from tests.gateway.restart_test_helpers import make_restart_runner
from tests.waiting import async_wait_until


@pytest.fixture(autouse=True)
def _guard_reset():
    hermes_loop_guard._reset_for_tests()
    yield
    hermes_loop_guard._reset_for_tests()


def _arm() -> int:
    """Register the running test loop's thread as the gateway loop; return its ident."""
    ident = threading.get_ident()
    hermes_loop_guard.set_loop_thread(ident)
    return ident


def _source() -> SessionSource:
    return SessionSource(platform=Platform.TELEGRAM, chat_id="67890", chat_type="dm", user_id="12345",
                         user_name="tester", thread_id="42")




@pytest.mark.asyncio
async def test_goal_continuation_recheck_runs_off_the_loop(monkeypatch):
    import hermes_cli.session_controls as session_controls
    from gateway.turn_context import TurnContext

    runner = object.__new__(GatewayRunner)
    threads = []

    def refresh(event, session_id):
        threads.append(threading.get_ident())
        return None

    monkeypatch.setattr(session_controls, "refresh_goal_continuation", refresh)

    async def delivered(*_args, **_kwargs):
        # Pin the first-response delivery outcome: an undelivered response defers the follow-up
        # before the goal recheck, which would let this test pass without exercising it.
        return True

    runner._run_agent_deliver_first_response = delivered
    source = _source()
    ctx = TurnContext(source=source, context_prompt="", channel_prompt=None, session_key="k",
                      session_id="sess", run_generation=1, history=[])
    pending = MessageEvent(text="[Continuing toward your standing goal]\nGoal: ship", source=source, metadata={"goal_continuation": True})
    assert runner._is_goal_continuation_event(pending)
    loop_thread = _arm()
    out = await runner._run_agent_queued_followup(ctx, None, pending.text, pending, "done",
                                                 {"final_response": "done"}, None)
    assert out == {"final_response": "done"}  # the stale continuation was discarded
    assert threads and loop_thread not in threads


class _RecordingStore:
    def __init__(self):
        self.threads = []

    def set_reasoning_override(self, session_key, override):
        self.threads.append(threading.get_ident())


def _reasoning_runner(tmp_path, monkeypatch):
    import gateway.run as gateway_run

    home = tmp_path / "hermes"
    home.mkdir()
    (home / "config.yaml").write_text("agent:\n  reasoning_effort: medium\n", encoding="utf-8")
    monkeypatch.setattr(gateway_run, "_hermes_home", home)
    runner = object.__new__(GatewayRunner)
    runner.adapters = {}
    runner._reasoning_config = None
    runner._session_reasoning_overrides = {}
    runner._show_reasoning = False
    runner._running_agents = {}
    runner._session_db = None
    runner.session_store = _RecordingStore()
    return runner


@pytest.mark.asyncio
async def test_typed_reasoning_pick_persists_off_the_loop(tmp_path, monkeypatch):
    runner = _reasoning_runner(tmp_path, monkeypatch)
    event = MessageEvent(text="/reasoning high", source=_source())
    loop_thread = _arm()
    await runner._handle_reasoning_command(event)
    assert runner.session_store.threads and loop_thread not in runner.session_store.threads




@pytest.mark.asyncio
async def test_process_watcher_lookup_runs_off_the_loop(tmp_path, monkeypatch):
    import gateway.run as gateway_run
    import tools.process_registry as pr_module

    (tmp_path / "config.yaml").write_text("display:\n  background_process_notifications: off\n",
                                          encoding="utf-8")
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    threads = []

    class _Registry:
        def get(self, session_id):
            # A finished process falls back to retained results, whose ownership check opens state.db.
            threads.append(threading.get_ident())
            return None

    monkeypatch.setattr(pr_module, "process_registry", _Registry())
    runner = GatewayRunner(GatewayConfig())
    loop_thread = _arm()
    await runner._run_process_watcher({"session_id": "proc_test", "check_interval": 0,
                                       "platform": "telegram", "chat_id": "123"})
    assert threads and loop_thread not in threads


@pytest.mark.asyncio
async def test_startup_restore_drain_resolves_keys_off_the_loop():
    runner, _ = make_restart_runner()
    threads = []

    def normalize(source):
        threads.append(threading.get_ident())  # Telegram topic recovery reads state.db
        return source

    runner._normalize_source_for_session_key = normalize
    runner._session_key_for_source = lambda source: f"key:{source.thread_id}"
    accepted = []

    async def handle(event):
        event._gateway_accepted = True
        accepted.append(event.text)
        if event.text == "first":  # inbound queued while the drain is replaying
            runner._startup_restore_queue.append(MessageEvent(text="late", source=_source()))

    adapter = SimpleNamespace(handle_message=handle)
    runner._intake_adapter_for = lambda source: adapter
    runner._startup_restore_queue = [MessageEvent(text="first", source=_source())]
    loop_thread = _arm()
    await runner._drain_startup_restore_queue()
    await async_wait_until(lambda: accepted == ["first", "late"], what="both restore admissions")
    await runner._get_replay_scheduler().wait_idle()
    assert accepted == ["first", "late"]
    assert threads and loop_thread not in threads


@pytest.mark.asyncio
async def test_served_profiles_prime_their_state_db_off_the_loop(tmp_path, monkeypatch):
    from hermes_constants import get_hermes_home

    launch = tmp_path / "launch"
    work = tmp_path / "profiles" / "work"
    for home in (launch, work):
        home.mkdir(parents=True)
    monkeypatch.setenv("HERMES_HOME", str(launch))
    opened = []

    def opener(kind):
        def _open(*_args):
            opened.append((kind, str(get_hermes_home()), threading.get_ident()))
        return _open

    runner = object.__new__(GatewayRunner)
    runner.session_store = SimpleNamespace(_open_session_db_for_active_scope=opener("store"))
    runner._open_session_db_for_active_scope = opener("runner")
    runner._served_profile_homes = {"default": launch, "work": work}
    loop_thread = _arm()
    await runner._start_prime_session_db_after_ready()
    work_opens = [(kind, thread) for kind, home, thread in opened if home == str(work)]
    assert {kind for kind, _ in work_opens} == {"store", "runner"}
    assert all(thread != loop_thread for _kind, _home, thread in opened)
