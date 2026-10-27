"""Cache eviction retires per-instance memory, not the resumable session's tools."""

from collections import OrderedDict
from types import SimpleNamespace
from unittest.mock import Mock
import threading
import time

import pytest

from agent.memory_manager import MemoryManager
from agent.memory_provider import MemoryProvider
from gateway.run import GatewayRunner
from gateway.run_turn_runner import TurnRunner
from gateway.turn_context import TurnContext
from plugins.memory.hindsight import HindsightMemoryProvider
from run_agent import AIAgent


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
    retiring, resume_retirement = threading.Event(), threading.Event()
    released = threading.Event()
    real_shutdown = provider.shutdown

    def blocked_shutdown():
        assert runner._agent_cache.get(key, (None,))[0] is not agent
        retiring.set()
        assert resume_retirement.wait(5)
        real_shutdown()

    provider.shutdown = Mock(side_effect=blocked_shutdown)
    manager._submit_background(lambda: provider._enqueue_retain(lambda: events.append("queued")))
    executor = manager._sync_executor
    executor_threads = list(executor._threads)
    # Same-session evictions (/model, fallback drift, signature, cross-process) must not fire
    # on_session_end; only idle TTL and the LRU cap commit the transcript first (base behavior).
    commits_session = route in {"ttl", "cap"}

    def commit(messages=None):
        assert runner._agent_cache.get(key, (None,))[0] is not agent
        events.append("commit")

    agent = SimpleNamespace(
        _memory_manager=manager, _session_messages=[{"role": "user", "content": "hello"}],
        _last_activity_ts=time.time() - 60, commit_memory_session=commit,
        release_clients=lambda: (events.append("clients"), released.set()),
        close=Mock(side_effect=AssertionError("hard cleanup loses resumable tools")),
    )
    turn = TurnRunner.__new__(TurnRunner)
    turn._runner = runner
    turn._ctx = TurnContext(session_key=key, session_id="sid", source=SimpleNamespace(),
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
        assert retiring.wait(5), "eviction did not retire the removed instance"
        # Retirement is still blocked, but lookup must build a different manager.
        if route in {"soft", "ttl", "cap"}:
            fresh, reused = turn._resolve_turn_agent({"model": "m", "runtime": {}}, "telegram", "", 5, {}, {})
            assert not reused and fresh._memory_manager is not manager
        resume_retirement.set()
        assert released.wait(5)
        provider.shutdown.assert_called_once_with()
        assert events.index("tail") < events.index("clients")
        if commits_session:
            assert events.count("commit") == 1 and events.index("commit") < events.index("tail")
        else:
            assert "commit" not in events
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


def test_soft_eviction_commit_serializes_with_session_shutdown(monkeypatch):
    runner = GatewayRunner.__new__(GatewayRunner)
    manager = Mock()
    agent = SimpleNamespace(_memory_manager=manager, _session_messages=[{"role": "user", "content": "hello"}])
    agent.shutdown_memory_provider = AIAgent.shutdown_memory_provider.__get__(agent, type(agent))
    monkeypatch.setattr("run_agent._notify_context_engine_session_end", lambda *_args: None)
    commit_started = threading.Event()
    allow_commit = threading.Event()
    events = []

    def commit(_messages=None):
        events.append("commit-start")
        commit_started.set()
        assert allow_commit.wait(5)
        events.append("commit-end")

    agent.commit_memory_session = commit
    manager.on_session_end.side_effect = lambda _messages: events.append("shutdown-hook")
    manager.shutdown_all.side_effect = lambda: events.append("shutdown")

    agent.release_clients = lambda: None
    commit_thread = threading.Thread(target=runner._commit_then_release_soft, args=(agent, "key"))
    commit_thread.start()
    assert commit_started.wait(5)
    shutdown_thread = threading.Thread(target=agent.shutdown_memory_provider, args=([{"role": "user"}],))
    shutdown_thread.start()
    shutdown_thread.join(0.3)
    assert shutdown_thread.is_alive(), "/new shutdown claimed the manager while the eviction commit was running"
    assert not manager.on_session_end.called
    allow_commit.set()
    commit_thread.join(5)
    shutdown_thread.join(5)

    # Eviction committed and detached atomically, so /new found no manager: session end ran exactly once.
    assert events == ["commit-start", "commit-end", "shutdown"]
    manager.on_session_end.assert_not_called()


class _CountingProvider(MemoryProvider):
    """Records lifecycle hooks; ``shutdown_delay`` models a slow bounded worker join."""

    def __init__(self, shutdown_delay=0.0):
        self.session_ends = 0
        self.shutdowns = 0
        self.shutdown_delay = shutdown_delay
        self.shut_down = threading.Event()

    @property
    def name(self):
        return "counting"

    def is_available(self):
        return True

    def initialize(self, session_id="", **kwargs):
        pass

    def get_tool_schemas(self):
        return []

    def on_session_end(self, messages):
        self.session_ends += 1

    def shutdown(self):
        time.sleep(self.shutdown_delay)
        self.shutdowns += 1
        self.shut_down.set()


def _cache_runner():
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._agent_cache = OrderedDict()
    runner._agent_cache_lock = threading.Lock()
    runner._running_agents = {}
    return runner


def _real_agent(provider):
    manager = MemoryManager()
    manager.add_provider(provider)
    agent = AIAgent.__new__(AIAgent)
    agent._memory_manager = manager
    agent._session_messages = [{"role": "user", "content": "hello"}]
    agent.session_id = "sid"
    agent.context_compressor = None
    agent.release_clients = lambda: None
    agent.close = lambda: None  # tool teardown is out of scope; shutdown_memory_provider is real
    return agent


def _wait(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and not predicate():
        time.sleep(0.01)
    return predicate()


def test_reset_cleanup_then_eviction_fires_session_end_and_shutdown_once():
    """/new and /reset: _cleanup_agent_resources already ran shutdown_memory_provider()."""
    runner = _cache_runner()
    provider = _CountingProvider()
    agent = _real_agent(provider)
    key = "agent:main:reset"
    runner._agent_cache[key] = (agent, "sig")

    GatewayRunner._cleanup_agent_resources(runner, agent)
    runner._evict_cached_agent(key)
    assert _wait(lambda: agent._memory_manager is None)
    # Also the commit-first routes (TTL / cap / pressure) must skip an already-shut-down agent.
    runner._commit_then_release_soft(agent, key)
    time.sleep(0.05)

    assert (provider.session_ends, provider.shutdowns) == (1, 1)


def test_eviction_during_timed_out_reset_cleanup_still_shuts_down_once():
    """/new cleanup timed out inside on_session_end; eviction detaches meanwhile, yet the manager retires once."""
    runner = _cache_runner()
    entered, release = threading.Event(), threading.Event()

    class _BlockingEnd(_CountingProvider):
        def on_session_end(self, messages):
            entered.set()
            assert release.wait(5)
            super().on_session_end(messages)

    provider = _BlockingEnd()
    agent = _real_agent(provider)
    manager = agent._memory_manager
    manager._submit_background(lambda: None)
    executor_threads = list(manager._sync_executor._threads)
    key = "agent:main:reset-timeout"
    runner._agent_cache[key] = (agent, "sig")

    # The worker _cleanup_old_agent_for_reset leaves running once its 30s wait_for gives up.
    cleanup = threading.Thread(target=GatewayRunner._cleanup_agent_resources, args=(runner, agent), daemon=True)
    cleanup.start()
    try:
        assert entered.wait(5)
        runner._evict_cached_agent(key)
        assert _wait(lambda: agent._memory_manager is None)
        time.sleep(0.05)
        assert provider.shutdowns == 0, "eviction retired providers mid-on_session_end"
    finally:
        release.set()
    cleanup.join(5)
    assert not cleanup.is_alive()

    assert provider.shut_down.wait(5)
    manager.shutdown_all()  # idempotent: a racing second retirement is a no-op
    assert (provider.session_ends, provider.shutdowns) == (1, 1)
    assert manager._sync_executor is None and manager.shutdown_drain_state["status"] == "drained"
    for worker in executor_threads:
        worker.join(5)
        assert not worker.is_alive()


def test_model_switch_eviction_shuts_down_without_session_end():
    """/model and fallback drift continue the SAME session: retire workers, no session-end hook."""
    runner = _cache_runner()
    provider = _CountingProvider()
    agent = _real_agent(provider)
    key = "agent:main:model"
    runner._agent_cache[key] = (agent, "sig")

    runner._evict_cached_agent(key)

    assert provider.shut_down.wait(5)
    assert (provider.session_ends, provider.shutdowns) == (0, 1)
    assert agent._memory_manager is None


def test_pressure_batch_does_not_wait_on_slow_memory_shutdown(monkeypatch):
    """malloc_trim must not be held behind each evicted manager's bounded shutdown join."""
    runner = _cache_runner()
    trimmed = threading.Event()
    monkeypatch.setattr("hermes_cli.mem_trim.trim_memory", lambda **kw: trimmed.set())
    providers = [_CountingProvider(shutdown_delay=2.0) for _ in range(3)]
    plan = [(f"agent:main:p{i}", _real_agent(p)) for i, p in enumerate(providers)]
    agents = [a for _, a in plan]

    started = time.monotonic()
    runner._release_pressure_batch(plan)
    elapsed = time.monotonic() - started

    assert elapsed < 1.8, f"pressure batch blocked {elapsed:.2f}s on memory shutdown"
    assert trimmed.is_set() and plan == []
    assert all(a._memory_manager is None for a in agents)
    assert all(p.session_ends == 1 for p in providers)  # pressure eviction still commits the transcript
    # Shutdowns run in parallel off the batch thread, so all three finish within ~one delay.
    for p in providers:
        assert p.shut_down.wait(4)
    assert all(p.shutdowns == 1 for p in providers)


class _RecordingManager(MemoryManager):
    def __init__(self):
        super().__init__()
        self.session_end_threads, self.shutdown_threads = [], []

    def on_session_end(self, messages):
        self.session_end_threads.append(threading.current_thread().name)

    def shutdown_all(self):
        self.shutdown_threads.append(threading.current_thread().name)
        super().shutdown_all()


@pytest.mark.parametrize("first", ["shutdown", "eviction"])
def test_shutdown_claim_and_eviction_detach_are_one_atomic_handoff(first):
    """Fire the other party exactly inside the first party's claim (after its first write). Without the shared
    handoff lock, eviction landing between the shutdown flag and the manager capture leaked the manager."""
    runner = _cache_runner()
    manager = _RecordingManager()
    racers = []

    class _Racing(AIAgent):
        def __setattr__(self, name, value):
            object.__setattr__(self, name, value)
            trigger = ("_memory_provider_shutdown", True) if first == "shutdown" else ("_memory_manager", None)
            if racers or (name, value) != trigger:
                return
            other = (self.shutdown_memory_provider if first == "eviction"
                     else lambda: runner._release_evicted_agent_soft(self))
            racer = threading.Thread(target=other, name="racer", daemon=True)
            racers.append(racer)
            racer.start()
            racer.join(0.2)
            assert racer.is_alive(), "second party entered the handoff mid-claim"

    agent = _Racing.__new__(_Racing)
    agent.__dict__.update(_memory_manager=manager, _session_messages=[], session_id="sid",
                          context_compressor=None, release_clients=lambda: None, close=lambda: None)
    if first == "shutdown":
        agent.shutdown_memory_provider([])
    else:
        runner._release_evicted_agent_soft(agent)
    racers[0].join(5)
    assert not racers[0].is_alive()

    assert agent._memory_manager is None
    if first == "shutdown":  # claimed: eviction only detaches; on_session_end then shutdown_all, both by us
        assert (manager.session_end_threads, manager.shutdown_threads) == (["MainThread"], ["MainThread"])
    else:  # detached first: eviction retires it; the late shutdown sees None and fires no session end
        assert (manager.session_end_threads, manager.shutdown_threads) == ([], ["MainThread"])
