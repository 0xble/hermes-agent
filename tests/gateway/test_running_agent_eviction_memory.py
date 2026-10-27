"""A removed running instance retires only after its worker and turn have drained."""

import asyncio
from collections import OrderedDict
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from agent.memory_manager import MemoryManager
from gateway.run import GatewayRunner
from gateway.run_turn_runner import TurnRunner
from gateway.turn_context import TurnContext
from plugins.memory.hindsight import HindsightMemoryProvider
from tests.gateway.restart_test_helpers import make_restart_runner, make_restart_source


async def wait_set(event):
    assert await asyncio.to_thread(event.wait, 5), "lifecycle stage did not finish"


@pytest.fixture
def running_turn(monkeypatch):
    runner, _ = make_restart_runner()
    key = "agent:main:running-eviction-test"
    runner._agent_cache = OrderedDict()
    runner._agent_cache_lock = threading.Lock()
    runner._session_db = None
    runner._agent_cache_cap = lambda: 8
    runner._refresh_fallback_model = lambda: None
    runner._fallback_baseline_model = lambda session_key, source, agent, *, policy_model=None: "primary"
    runner._is_intentional_model_switch = lambda *_: False
    runner._get_proxy_url = lambda: None
    runner._session_state(key).persistent.run_generation = 1
    events = []
    provider = HindsightMemoryProvider()
    provider._auto_retain = True
    provider._retain_every_n_turns = 3
    provider._turn_counter = 1
    provider._session_turns = ["unsent tail"]
    monkeypatch.setattr(provider, "_resolve_retain_target", lambda _: ("doc", None))
    monkeypatch.setattr(provider, "_make_turn_retain_job", lambda *a, **kw: lambda: events.append("tail"))
    manager = MemoryManager()
    manager.add_provider(provider)
    queued = threading.Event()
    manager._submit_background(lambda: (provider._enqueue_retain(lambda: events.append("queued")), queued.set()))
    assert queued.wait(5)
    assert provider._writer_thread is not None
    threads = list(manager._sync_executor._threads) + [provider._writer_thread]
    assert len(threads) >= 2 and all(thread.is_alive() for thread in threads)
    shutdown = provider.shutdown
    provider.shutdown = Mock(wraps=shutdown)
    released = threading.Event()
    agent = SimpleNamespace(
        model="primary", _memory_manager=manager, _session_messages=[{"role": "user", "content": "hi"}],
        commit_memory_session=Mock(), close=Mock(),
        release_clients=Mock(side_effect=lambda: (events.append("clients"), released.set())),
    )
    source = make_restart_source()
    ctx = TurnContext(source=source, session_key=key, session_id="sid", run_generation=1,
                      user_config={}, enabled_toolsets=[], _run_still_current=lambda: True)
    selector = TurnRunner.__new__(TurnRunner)
    selector._runner, selector._ctx = runner, ctx
    selector._build_fresh_agent = lambda *_: agent
    started, resume = threading.Event(), threading.Event()
    post_started, post_resume = asyncio.Event(), asyncio.Event()
    outcome = ["success"]
    worker_holder = []

    def run_sync():
        selected, reused = selector._resolve_turn_agent(
            {"model": "primary", "runtime": {}}, "telegram", "", 5, {}, {},
        )
        assert selected is agent and not reused
        ctx.agent_holder[0] = agent
        runner._session_state(key).turn.agent = agent
        started.set()
        assert resume.wait(5), "worker was not resumed"
        if outcome[0] == "error":
            raise RuntimeError("turn failed")
        ctx.result_holder[0] = {"final_response": "finished", "completed": True}
        return ctx.result_holder[0]

    async def background(*_):
        await asyncio.Future()

    async def postprocess(*_):
        post_started.set()
        await post_resume.wait()

    display = SimpleNamespace(user_config={}, _native_slack_task_cards=False, needs_progress_queue=False,
                              log_mode_enabled=False)
    runner._run_agent_display_settings = lambda *_: display
    runner._run_agent_build_turn_context = lambda *a, **kw: (ctx, SimpleNamespace(run_sync=run_sync), None)
    runner._run_agent_bind_turn_wiring = lambda *_: None
    runner._run_agent_start_streaming_tts = lambda *_: None
    runner._run_agent_track_agent = background
    runner._run_agent_monitor_for_interrupt = background
    runner._run_agent_notify_long_running = background
    runner._run_agent_stream_consumer_task = background
    runner._run_agent_finalize_streaming_tts = postprocess
    runner._run_agent_schedule_bubble_cleanup = lambda *_: None
    runner._run_agent_drain_pending = AsyncMock(return_value=(None, None))
    runner._run_agent_mark_streamed_delivery = AsyncMock()
    start_worker = runner._run_agent_start_turn_worker

    def capture_worker(*args):
        worker = start_worker(*args)
        worker_holder.append(worker)
        return worker

    runner._run_agent_start_turn_worker = capture_worker
    monkeypatch.setenv("HERMES_AGENT_TIMEOUT", "0")
    monkeypatch.setattr("gateway.run_turn_silence_recovery.silence_recovery_due", lambda *a, **kw: False)
    harness = SimpleNamespace(**locals())
    yield harness
    resume.set()
    post_resume.set()
    manager.shutdown_all()
    runner._shutdown_executor(drain_timeout=5)


@pytest.mark.asyncio
@pytest.mark.parametrize("route", ["model", "fallback_model", "fallback_provider"])
async def test_running_eviction_waits_for_turn_then_retires_exact_instance(running_turn, route):
    h = running_turn
    task = asyncio.create_task(h.runner._run_agent_inner(
        "hi", "", [], h.source, "sid", h.key, run_generation=1,
    ))
    try:
        await wait_set(h.started)
        assert h.runner._agent_cache[h.key][0] is h.ctx.agent_holder[0] is h.agent
        assert id(h.agent) in h.runner._running_agent_ids()
        assert h.agent._memory_manager is h.manager
        if route == "model":
            h.runner._evict_cached_agent(h.key)
            assert h.key not in h.runner._agent_cache
        elif route == "fallback_model":
            h.agent.model = "fallback"
        else:
            h.agent._provider_fallback_active = True
        assert not h.released.is_set()
        assert all(thread.is_alive() for thread in h.threads)
        h.resume.set()
        await asyncio.wait_for(h.post_started.wait(), 5)
        # Production fallback eviction happens after the sync result, while the running slot
        # still references the exact removed object. Outer postprocessing still needs it.
        assert h.key not in h.runner._agent_cache
        assert id(h.agent) in h.runner._running_agent_ids()
        assert not h.released.is_set()
        successor = SimpleNamespace(_memory_manager=MemoryManager())
        h.runner._agent_cache[h.key] = (successor, "successor")
        h.runner._session_state(h.key).persistent.run_generation = 2
        h.runner._session_state(h.key).turn.agent = successor
        h.post_resume.set()
        await asyncio.wait_for(task, 5)
        await wait_set(h.released)
        h.provider.shutdown.assert_called_once_with()
        h.agent.release_clients.assert_called_once_with()
        h.agent.commit_memory_session.assert_not_called()
        h.agent.close.assert_not_called()
        assert h.manager.shutdown_drain_state["status"] == "drained"
        assert h.agent._memory_manager is None
        assert h.events.index("tail") < h.events.index("clients")
        assert "queued" in h.events
        for thread in h.threads:
            thread.join(5)
            assert not thread.is_alive()
        assert not h.provider._atexit_registered
        assert h.runner._agent_cache[h.key][0] is successor
        assert h.runner._session_state(h.key).turn.agent is successor
        assert successor._memory_manager is not None
        # Concurrent completion/eviction callers must not release clients twice either.
        h.runner._release_evicted_agent_soft(h.agent)
        h.agent.release_clients.assert_called_once_with()
    finally:
        h.resume.set()
        h.post_resume.set()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["error", "cancel"])
async def test_failed_or_cancelled_turn_defers_evicted_agent_until_real_worker_exit(running_turn, outcome):
    h = running_turn
    h.outcome[0] = outcome
    task = asyncio.create_task(h.runner._run_agent_inner(
        "hi", "", [], h.source, "sid", h.key, run_generation=1,
    ))
    try:
        await wait_set(h.started)
        h.runner._evict_cached_agent(h.key)
        assert not h.released.is_set()
        if outcome == "cancel":
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            # asyncio completion is not evidence that the blocking turn has drained.
            assert not h.worker_holder[0].worker_done.is_set()
            assert not h.released.is_set()
            assert all(thread.is_alive() for thread in h.threads)
        h.resume.set()
        if outcome == "error":
            with pytest.raises(RuntimeError, match="turn failed"):
                await task
        else:
            await h.worker_holder[0].executor_task
        await wait_set(h.released)
        h.provider.shutdown.assert_called_once_with()
        h.agent.release_clients.assert_called_once_with()
        h.agent.commit_memory_session.assert_not_called()
        h.agent.close.assert_not_called()
        for thread in h.threads:
            thread.join(5)
            assert not thread.is_alive()
    finally:
        h.resume.set()
        h.post_resume.set()
        await asyncio.gather(task, return_exceptions=True)


def join_release_threads(prefix):
    for thread in [t for t in threading.enumerate() if t.name.startswith(prefix)]:
        thread.join(5)
        assert not thread.is_alive()


@pytest.mark.asyncio
async def test_sweep_during_undrained_turn_commits_session_end_once_at_drain(running_turn):
    h = running_turn
    task = asyncio.create_task(h.runner._run_agent_inner(
        "hi", "", [], h.source, "sid", h.key, run_generation=1,
    ))
    try:
        await wait_set(h.started)
        # Inactivity-timeout shape: the outer turn returns and frees its running slot while the
        # blocking worker is still inside the agent, so the cached agent becomes sweepable.
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        worker = h.worker_holder[0]
        assert worker.turn_done.is_set() and not worker.worker_done.is_set()
        h.runner._release_running_agent_state(h.key)
        assert h.runner._agent_cache[h.key][0] is h.agent
        h.agent._last_activity_ts = 0
        h.runner._agent_cache_idle_ttl = lambda: 0
        assert h.runner._sweep_idle_cached_agents() == 1
        join_release_threads("agent-cache-idle-")
        assert h.key not in h.runner._agent_cache
        h.agent.commit_memory_session.assert_not_called()
        assert not h.released.is_set()
        h.resume.set()
        await worker.executor_task
        await wait_set(h.released)
        join_release_threads("agent-turn-release-")
        h.agent.commit_memory_session.assert_called_once_with([{"role": "user", "content": "hi"}])
        h.provider.shutdown.assert_called_once_with()
        h.agent.release_clients.assert_called_once_with()
        h.runner._commit_then_release_soft(h.agent, h.key)
        h.agent.commit_memory_session.assert_called_once()
        h.agent.release_clients.assert_called_once_with()
    finally:
        h.resume.set()
        h.post_resume.set()
        await asyncio.gather(task, return_exceptions=True)
