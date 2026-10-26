"""Read-only writer census through the existing control protocol, using owned fixtures only."""

import asyncio
import ctypes
import gc
import json
import os
import sys
import threading
import weakref
from types import SimpleNamespace

import pytest

from gateway.control_socket import GatewayControlServer, query_gateway_control
from gateway.writer_census import writer_census


class NS(SimpleNamespace):
    pass


@pytest.fixture
def writers(monkeypatch):
    threads = []
    stop = threading.Event()

    def start():
        thread = threading.Thread(target=stop.wait, name="hindsight-writer", daemon=True)
        thread._census_started_ns = 456 + len(threads)
        thread.start()
        threads.append(thread)
        return thread

    monkeypatch.setattr("gateway.writer_census.threading.enumerate", lambda: list(threads))
    monkeypatch.setattr("gateway.status._get_code_identity_fields", lambda: {"code_sha": "fixture-sha"})
    yield start, threads
    stop.set()
    for thread in threads:
        if isinstance(thread, threading.Thread):
            thread.join(timeout=5)
            assert not thread.is_alive()


def provider(writer=None, shutdown=False):
    event = threading.Event()
    if shutdown:
        event.set()
    return NS(name="hindsight", _writer_thread=writer, _sync_thread=writer, _shutting_down=event,
              _census_lifetime_ns=123,
              _session_turns=["PRIVATE MEMORY"], _client="SECRET TOKEN")


def agent(p):
    return NS(_memory_manager=NS(_providers=[p]), _session_messages=["PRIVATE PROMPT"])


def request(runner):
    server = GatewayControlServer(verb_handlers={"writer-census": lambda: writer_census(runner)})
    response = json.loads(server.handle_request_line(b'{"verb":"writer-census","id":7}'))
    assert response["ok"] and response["id"] == 7
    return response["result"]


@pytest.mark.parametrize("category", ["cached_idle", "turn_unfinished", "release_in_progress",
                                     "writerless_provider", "other_owner"])
def test_owner_categories_and_identity_deduplication(writers, category):
    start, threads = writers
    p = provider(None if category == "writerless_provider" else start())
    a = agent(p)
    runner = NS(_agent_cache={}, _sessions={})
    if category in {"cached_idle", "writerless_provider", "release_in_progress"}:
        runner._agent_cache["PRIVATE SESSION"] = (a, "signature", 0)
        if category == "release_in_progress":
            a._gateway_soft_release_started = True
    elif category == "turn_unfinished":
        a._gateway_turn_lifetime = (threading.Event(), threading.Event())
        runner._sessions["PRIVATE SESSION"] = NS(turn=NS(agent=a, ctx=NS(agent_holder=[a])))
    else:
        runner._deferred_agent_workers = {object(): a}
    result = request(runner)
    assert result["counts"][category] == 1
    assert result["counts"]["providers"] == 1
    assert result["counts"]["live_writers"] == (category != "writerless_provider")
    assert result["counts"]["matched"] == (category != "writerless_provider")
    assert result["health"] == "healthy"
    assert result["providers"][0]["category"] == category
    assert result["providers"][0]["owners"][0]["structure"]
    if threads:
        assert result["writers"][0]["native_id"] == threads[0].native_id
        assert result["writers"][0]["ident"] == threads[0].ident


@pytest.mark.parametrize("problem", ["unaccounted_writers", "duplicate_owner", "shutdown_but_alive"])
def test_unhealthy_populations_are_independent_and_visible(writers, problem):
    start, _ = writers
    thread = start()
    p = provider(thread, shutdown=problem == "shutdown_but_alive")
    a = agent(p)
    runner = NS(_agent_cache={"a": (a,)}, _sessions={"a": NS(turn=NS(agent=a, ctx=None))})
    if problem == "unaccounted_writers":
        runner._agent_cache.clear()
        runner._sessions.clear()
    elif problem == "duplicate_owner":
        runner._agent_cache["b"] = (agent(provider(thread)),)
    result = request(runner)
    assert result["health"] == "unhealthy"
    assert result["counts"][problem] == 1
    assert len(result[problem]) == 1
    assert result["counts"]["live_writers"] == 1
    assert result["counts"]["matched"] == (problem == "shutdown_but_alive")
    assert result["counts"]["providers"] == (0 if problem == "unaccounted_writers" else
                                             2 if problem == "duplicate_owner" else 1)


def test_snapshot_is_plain_data_private_and_does_not_retain_owners(writers):
    start, _ = writers

    class Owner:
        pass

    p = Owner()
    p.__dict__.update(provider(start()).__dict__)
    a = Owner()
    a.__dict__.update(agent(p).__dict__)
    manager = a._memory_manager
    runner = NS(_agent_cache={"PRIVATE SESSION": (a,)})
    refs = weakref.ref(a), weakref.ref(p)
    result = request(runner)
    wire = json.dumps(result)
    assert all(secret not in wire for secret in ["PRIVATE SESSION", "PRIVATE MEMORY", "SECRET TOKEN", "PRIVATE PROMPT"])
    assert result["pid"] == os.getpid()
    assert result["code_sha"] == "fixture-sha"
    assert result["process_start_time"] is not None
    assert result["identity_complete"]
    assert result["providers"][0]["lifetime_monotonic_ns"] == 123
    assert result["wall_time"] > 0 and result["monotonic_time"] > 0
    assert result["duration_seconds"] >= 0
    runner._agent_cache.clear()
    del a, p, manager
    gc.collect()
    assert all(ref() is None for ref in refs)


def test_busy_lock_and_container_race_are_incomplete_not_retried(writers):
    class RacingDict(dict):
        def items(self):
            raise RuntimeError("dictionary changed size during iteration: PRIVATE")

    lock = threading.Lock()
    lock.acquire()
    try:
        result = request(NS(_agent_cache={"a": (agent(provider()),)}, _agent_cache_lock=lock,
                            _sessions=RacingDict()))
    finally:
        lock.release()
    assert result["incomplete"]
    assert result["health"] == "incomplete"
    assert "_agent_cache:lock_busy" in result["raced"]
    assert "_sessions:changed_during_copy" in result["raced"]
    assert "PRIVATE" not in json.dumps(result)


def test_real_release_thread_arguments_and_api_parked_manager(writers):
    start, threads = writers
    p, parked = provider(start()), provider(start())
    manager = NS(_providers=[p])
    runner = NS(_agent_cache={})

    def _run_release_in_profile_scope(*args):
        pass

    def _shutdown_memory_manager(*args):
        pass

    # The production release wrapper stores these exact target/args containers until exit.
    threads.append(NS(name="agent-pressure-mem-private", _args=(
        _run_release_in_profile_scope, _shutdown_memory_manager, (manager,), "PRIVATE SESSION"),
        is_alive=lambda: True))
    registry = NS(_entries={('PRIVATE HOME', 'PRIVATE SESSION'): (NS(_providers=[parked]), None, 0)},
                  _lock=threading.Lock())
    runner.adapters = {"api": NS(_memory_sessions=registry)}
    result = request(runner)
    assert result["counts"]["release_in_progress"] == 1
    assert result["counts"]["other_owner"] == 1
    assert result["counts"]["matched"] == 2
    assert not result["incomplete"]
    assert "PRIVATE" not in json.dumps(result)


def test_checked_out_api_manager_and_async_retirement(writers):
    start, threads = writers
    checked_out, retiring = provider(start()), provider(start())
    adapter = NS(_memory_sessions=NS(_entries={}, _lock=threading.Lock()),
                 _shutdown_interruptible_agents={1: agent(checked_out)})
    threads.append(NS(name="api-server-memory-evict", _args=(NS(_providers=[retiring]), None),
                      is_alive=lambda: True))
    result = request(NS(adapters={"api": adapter}))
    assert result["counts"]["other_owner"] == 1
    assert result["counts"]["release_in_progress"] == 1
    assert result["counts"]["matched"] == 2
    structures = {owner["structure"] for p in result["providers"] for owner in p["owners"]}
    assert "adapters._shutdown_interruptible_agents" in structures
    assert "api_server_memory_sessions.retirement_thread.args" in structures


def test_replacement_writer_is_not_hidden_by_current_handle(writers):
    start, _ = writers
    old, current = start(), start()
    p = provider(current)
    result = request(NS(_agent_cache={"a": (agent(p),)}))
    assert result["counts"]["live_writers"] == 2
    assert result["counts"]["matched"] == 1
    assert result["unaccounted_writers"] == [id(old)]
    assert result["health"] == "unhealthy"
    assert {w["started_monotonic_ns"] for w in result["writers"]} == {456, 457}


def test_pressure_batch_and_detached_manager_gaps_stay_visible(writers):
    _, threads = writers
    threads.append(NS(name="agent-cache-pressure", _args=([],)))
    result = request(NS())
    assert result["incomplete"]
    assert "agent_cache_pressure:stack_local_owners_not_scanned" in result["raced"]


@pytest.mark.platforms("posix")
def test_existing_socket_client_returns_census(tmp_path, writers):
    async def exercise():
        server = GatewayControlServer(tmp_path, verb_handlers={"writer-census": lambda: writer_census(NS())})
        assert await server.start()
        try:
            result = await asyncio.to_thread(query_gateway_control, tmp_path, "writer-census")
            assert result["counts"]["providers"] == 0
            assert result["health"] == "healthy"
        finally:
            await server.stop()

    asyncio.run(exercise())


def test_lifetime_stamps_do_not_retain_provider_and_replacement_changes_stamp():
    from plugins.memory.hindsight import HindsightMemoryProvider

    p = HindsightMemoryProvider()
    other = HindsightMemoryProvider()
    assert p._census_lifetime_ns != other._census_lifetime_ns
    stop = threading.Event()
    p._writer_loop = stop.wait
    p._ensure_writer()
    first = p._writer_thread
    try:
        assert first._census_started_ns > p._census_lifetime_ns
    finally:
        stop.set()
        first.join(timeout=5)
    assert not first.is_alive()
    p._ensure_writer()
    second = p._writer_thread
    try:
        assert second._census_started_ns > first._census_started_ns
    finally:
        second.join(timeout=5)
    assert not second.is_alive()
    ref = weakref.ref(p)
    del p
    gc.collect()
    assert ref() is None


@pytest.mark.platforms("linux", "macos")
def test_native_id_matches_os_thread_inventory(writers):
    start, _ = writers
    thread = start()
    if sys.platform == "darwin":
        libproc = ctypes.CDLL("/usr/lib/libproc.dylib")
        libproc.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64,
                                        ctypes.c_void_p, ctypes.c_int]
        libproc.proc_pidinfo.restype = ctypes.c_int
        buf = (ctypes.c_uint64 * 4096)()
        size = libproc.proc_pidinfo(os.getpid(), 6, 0, buf, ctypes.sizeof(buf))
        assert size > 0
        tids = set(buf[:size // ctypes.sizeof(ctypes.c_uint64)])
    else:
        tids = {int(entry) for entry in os.listdir(f"/proc/{os.getpid()}/task")}
    assert thread.native_id in tids
    assert request(NS())["writers"][0]["native_id"] in tids
