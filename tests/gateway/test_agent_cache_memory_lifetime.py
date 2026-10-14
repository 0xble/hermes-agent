"""Cache eviction retires per-instance memory, not the resumable session's tools."""

from collections import OrderedDict
from types import SimpleNamespace
from unittest.mock import Mock
import threading
import time

import pytest

from agent.memory_manager import MemoryManager
from gateway.run import GatewayRunner
from gateway.run_turn_runner import TurnRunner
from plugins.memory.hindsight import HindsightMemoryProvider


@pytest.mark.parametrize("route", ["soft", "ttl", "cap", "signature", "cross_process"])
def test_eviction_drains_memory_once_and_refetches_fresh_manager(route, monkeypatch):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._agent_cache = OrderedDict()
    runner._agent_cache_lock = threading.Lock()
    runner._running_agents = {}
    runner._session_db = None
    runner._agent_cache_cap = lambda: 1
    runner._agent_cache_idle_ttl = lambda: 1
    runner._refresh_fallback_model = lambda: None
    key = "agent:main:test"
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
    provider.shutdown = Mock(wraps=provider.shutdown)
    manager._submit_background(lambda: provider._enqueue_retain(lambda: events.append("queued")))
    executor = manager._sync_executor
    executor_threads = list(executor._threads)
    committed, resume_retirement = threading.Event(), threading.Event()
    released = threading.Event()

    def commit(messages=None):
        assert runner._agent_cache.get(key, (None,))[0] is not agent
        events.append("commit")
        committed.set()
        assert resume_retirement.wait(5)

    agent = SimpleNamespace(
        _memory_manager=manager, _session_messages=[{"role": "user", "content": "hello"}],
        _last_activity_ts=time.time() - 60, commit_memory_session=commit,
        release_clients=lambda: (events.append("clients"), released.set()),
        close=Mock(side_effect=AssertionError("hard cleanup loses resumable tools")),
    )
    turn = TurnRunner.__new__(TurnRunner)
    turn._runner = runner
    turn._ctx = SimpleNamespace(session_key=key, session_id="sid", source=SimpleNamespace(),
                                user_config={}, enabled_toolsets=[], _interrupt_depth=0)
    turn._build_fresh_agent = lambda *a: SimpleNamespace(_memory_manager=MemoryManager())
    runner._agent_cache[key] = (agent, "old-signature", 0, "sid")
    signature = runner._agent_config_signature("m", {}, [], "", cache_keys=runner._extract_cache_busting_config({}))
    if route == "cross_process":
        runner._agent_cache[key] = (agent, signature, 0, "sid")
        turn._current_message_count = lambda: 1
    try:
        if route == "soft":
            runner._evict_cached_agent(key)
        elif route == "ttl":
            assert runner._sweep_idle_cached_agents() == 1
        elif route == "cap":
            runner._agent_cache["other"] = (SimpleNamespace(), "sig")
            with runner._agent_cache_lock:
                runner._enforce_agent_cache_cap()
        else:
            fresh, reused = turn._resolve_turn_agent({"model": "m", "runtime": {}}, "telegram", "", 5, {}, {})
            assert not reused and fresh._memory_manager is not manager
        assert committed.wait(5), "eviction did not commit the removed instance"
        # Retirement is still blocked, but lookup must build a different manager.
        if route in {"soft", "ttl", "cap"}:
            fresh, reused = turn._resolve_turn_agent({"model": "m", "runtime": {}}, "telegram", "", 5, {}, {})
            assert not reused and fresh._memory_manager is not manager
        resume_retirement.set()
        assert released.wait(5)
        provider.shutdown.assert_called_once_with()
        assert events.index("commit") < events.index("tail") < events.index("clients")
        assert "queued" in events
        assert not provider._writer_thread.is_alive()
        assert not provider._atexit_registered
        assert manager.shutdown_drain_state["status"] == "drained"
        for worker in executor_threads:
            worker.join(5)
            assert not worker.is_alive()
        runner._release_evicted_agent_soft(agent)
        provider.shutdown.assert_called_once_with()
        agent.close.assert_not_called()
    finally:
        resume_retirement.set()
        manager.shutdown_all()
