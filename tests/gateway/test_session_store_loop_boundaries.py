"""Cold session routes must never acquire the guarded store lock on the loop."""
from __future__ import annotations

import asyncio
import os
from pathlib import Path
import subprocess
import sys
import textwrap
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import hermes_loop_guard
from gateway.config import GatewayConfig, Platform
from gateway.platforms.event import MessageEvent
from gateway.run import GatewayRunner
from gateway.session import SessionSource


@pytest.fixture(autouse=True)
def reset_loop_guard():
    hermes_loop_guard._reset_for_tests()
    yield
    hermes_loop_guard._reset_for_tests()


@pytest.mark.asyncio
@pytest.mark.parametrize("site", ["status", "model", "selection", "fast", "fast_picker"])
@pytest.mark.parametrize("persisted", [False, True])
async def test_cold_model_override_read_runs_off_loop(site, persisted, tmp_path, monkeypatch):
    import gateway.run as gateway_run
    import gateway.slash_commands_model as model_commands

    runner = GatewayRunner(GatewayConfig(sessions_dir=tmp_path / "sessions"))
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="123", user_id="u1")
    key = runner._session_key_for_source(source)
    runner.session_store.get_or_create_session(source)
    if persisted:
        runner.session_store.set_model_override(key, {"model": "persisted-model"})
    reads = []
    original = runner.session_store.get_model_override

    def read(session_key):
        reads.append(threading.get_ident())
        return original(session_key)  # actual LoopGuardedLock, not a pretend lock

    monkeypatch.setattr(runner.session_store, "get_model_override", read)
    monkeypatch.setattr(runner, "_normalize_source_for_session_key", lambda value: value)
    monkeypatch.setattr(gateway_run, "_resolve_gateway_model", lambda _: "default-model")
    monkeypatch.setattr(gateway_run, "_resolve_runtime_agent_kwargs",
                        lambda: {"api_key": "test-key", "provider": "openai"})
    monkeypatch.setattr(runner, "_perform_model_switch", AsyncMock(return_value=(None, "stop-before-switch")))
    monkeypatch.setattr(model_commands, "_fast_route_supports", lambda *args, **kwargs: True)
    loop_thread = threading.get_ident()
    original_state = runner._session_state
    original_commit = runner._commit_session_model_override
    ownership_errors = []

    def state(session_key):
        if runner._peek_session_state(session_key) is None and threading.get_ident() != loop_thread:
            ownership_errors.append("session state created off-loop")
        return original_state(session_key)

    def commit(*args):
        if threading.get_ident() != loop_thread:
            ownership_errors.append("model override committed off-loop")
        return original_commit(*args)

    monkeypatch.setattr(runner, "_session_state", state)
    monkeypatch.setattr(runner, "_commit_session_model_override", commit)
    hermes_loop_guard.set_loop_thread(loop_thread)
    try:
        if site == "status":
            await runner._handle_status_command(MessageEvent(text="/status", source=source))
        elif site == "model":
            await runner._handle_model_command(MessageEvent(text="/model next-model", source=source))
        elif site == "selection":
            await runner.resolve_session_model_selection(source, "next-model", session_key=key)
        elif site.startswith("fast"):
            real_apply = runner._apply_fast_selection

            def apply(*args, **kwargs):
                runner._session_model_overrides.pop(key, None)
                return real_apply(*args, **kwargs)

            monkeypatch.setattr(runner, "_apply_fast_selection", apply)
            if site == "fast_picker":
                async def picker(*args, **kwargs):
                    # A session boundary may evict state while the picker is visible.
                    runner._sessions_map().pop(key, None)
                    await kwargs["on_choice_selected"]("123", "normal")
                    return True

                monkeypatch.setattr(runner, "_try_send_choice_picker", picker)
                command = "/fast"
            else:
                command = "/fast normal"
            await runner._handle_fast_command(MessageEvent(text=command, source=source))
        assert reads and loop_thread not in reads, f"{site} store read on loop: {reads}"
        assert not ownership_errors, ownership_errors
        if not site.startswith("fast") and persisted:
            assert runner._session_model_override(key)["model"] == "persisted-model"
    finally:
        hermes_loop_guard.set_loop_thread(None)
        if runner._executor is not None:
            await asyncio.to_thread(runner._executor.shutdown)


@pytest.mark.asyncio
@pytest.mark.parametrize("site", ["topic", "heartbeat", "heartbeat-restore"])
async def test_session_owner_peek_runs_off_loop(site, monkeypatch):
    from gateway.telegram_topic_sessions import _topic_exists
    from gateway.run_heartbeat_restore import restore_heartbeat_watches

    reads = []
    lock = hermes_loop_guard.LoopGuardedLock("session-store lock")

    def peek(key):
        reads.append(threading.get_ident())
        with lock:
            return "sid"

    source = SessionSource(platform=Platform.TELEGRAM, chat_id="123", user_id="u1")
    runner = object.__new__(GatewayRunner)
    runner.session_store = SimpleNamespace(peek_session_id=peek)
    runner._session_db = None
    runner._delivery_adapter_for = lambda _: None
    runner._warm_goals_session_db = AsyncMock()
    runner._run_in_executor_with_context = AsyncMock(return_value=[("k", source, "sid")])
    runner._register_heartbeat_watch = lambda *args: None
    loop_thread = threading.get_ident()
    hermes_loop_guard.set_loop_thread(loop_thread)
    if site == "topic":
        assert await _topic_exists(runner, source, "42")
    elif site == "heartbeat":
        await runner._heartbeat_poll_watch({}, "k", source, "sid")
    else:
        await restore_heartbeat_watches(runner)
    assert reads and loop_thread not in reads


@pytest.mark.asyncio
async def test_heartbeat_owner_lookup_runs_off_loop(tmp_path):
    from gateway.run_heartbeat_acceptance import resolve_heartbeat_owner

    runner = GatewayRunner(GatewayConfig(sessions_dir=tmp_path / "sessions"))
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="123", user_id="u1")
    entry = runner.session_store.get_or_create_session(source)
    event = MessageEvent(text="heartbeat", source=source)
    event._heartbeat_session_id = entry.session_id
    hermes_loop_guard.set_loop_thread(threading.get_ident())
    try:
        assert await resolve_heartbeat_owner(runner, event, entry)
    finally:
        hermes_loop_guard.set_loop_thread(None)


@pytest.mark.asyncio
async def test_webhook_completion_peeks_session_off_loop(tmp_path, monkeypatch):
    pytest.importorskip("aiohttp")
    from gateway.platforms.webhook import WebhookAdapter

    runner = GatewayRunner(GatewayConfig(sessions_dir=tmp_path / "sessions"))
    source = SessionSource(platform=Platform.WEBHOOK, chat_id="delivery", user_id="hook")
    entry = runner.session_store.get_or_create_session(source)
    original = runner.session_store.peek_session_id
    peeks = []

    def peek(key):
        peeks.append(threading.get_ident())
        return original(key)

    monkeypatch.setattr(runner.session_store, "peek_session_id", peek)
    end_session = AsyncMock()
    adapter = SimpleNamespace(gateway_runner=SimpleNamespace(
        session_store=runner.session_store,
        _session_db=SimpleNamespace(end_session=end_session),
        _session_key_for_source=runner._session_key_for_source,
    ))
    loop_thread = threading.get_ident()
    hermes_loop_guard.set_loop_thread(loop_thread)
    try:
        await WebhookAdapter._end_webhook_session(adapter, MessageEvent(text="done", source=source), "delivery")
        assert peeks and loop_thread not in peeks
        end_session.assert_awaited_once_with(entry.session_id, "webhook_complete")
    finally:
        hermes_loop_guard.set_loop_thread(None)


@pytest.mark.asyncio
async def test_run_turn_checks_heartbeat_owner_off_loop(tmp_path, monkeypatch):
    runner = GatewayRunner(GatewayConfig(sessions_dir=tmp_path / "sessions"))
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="123", user_id="u1")
    entry = runner.session_store.get_or_create_session(source)
    key = entry.session_key
    event = MessageEvent(text="heartbeat", source=source)
    event._heartbeat_resolved_session_id = "stale-owner"
    reads = []
    original = runner.session_store.lookup_by_session_key

    def lookup(session_key):
        reads.append(threading.get_ident())
        return original(session_key)

    monkeypatch.setattr(runner.session_store, "lookup_by_session_key", lookup)
    monkeypatch.setattr(runner, "_hmwa_resolve_session", AsyncMock(return_value=(source, entry, key)))
    prepared = runner._PreparedTurn(
        history=[], context_prompt="", message_text="heartbeat",
        persist_user_message=None, persist_user_timestamp=None, persist_user_display_kind=None)
    monkeypatch.setattr(runner, "_hmwa_prepare_turn", AsyncMock(return_value=(prepared, [])))
    monkeypatch.setattr(runner.hooks, "emit", AsyncMock())
    monkeypatch.setattr(runner, "_clear_session_env", lambda _tokens: None)
    loop_thread = threading.get_ident()
    hermes_loop_guard.set_loop_thread(loop_thread)
    try:
        result = await runner._handle_message_with_agent(event, source, key, 1)
        assert result is None  # obsolete heartbeat cannot enter the agent runner
        assert reads and loop_thread not in reads
        assert not getattr(event, "_heartbeat_execution_started", False)
    finally:
        hermes_loop_guard.set_loop_thread(None)


@pytest.mark.asyncio
async def test_async_hydration_rejects_stale_read_after_edit(tmp_path, monkeypatch):
    """Off-loop reads must retain the synchronous hydration's epoch/edit-pending fence."""
    runner = GatewayRunner(GatewayConfig(sessions_dir=tmp_path / "sessions"))
    entered = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()

    def load(key):
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(5)
        return {"model": "stale-model"}

    monkeypatch.setattr(runner, "_load_session_model_override", load)
    hydrate = asyncio.create_task(runner._rehydrate_session_model_override_async("k"))
    try:
        await asyncio.wait_for(entered.wait(), 5)
        runner._bump_session_model_override_epoch("k")
        runner._session_model_overrides["k"] = {"model": "new-model"}
        release.set()
        await asyncio.wait_for(hydrate, 5)
        assert runner._session_model_override("k")["model"] == "new-model"
    finally:
        release.set()
        await asyncio.gather(hydrate, return_exceptions=True)
        if runner._executor is not None:
            await asyncio.to_thread(runner._executor.shutdown)



@pytest.mark.asyncio
@pytest.mark.parametrize("site", ["chat", "completions", "responses", "runs"])
@pytest.mark.parametrize("persisted", [False, True])
async def test_api_route_validation_reads_store_off_loop(site, persisted, tmp_path, monkeypatch):
    pytest.importorskip("aiohttp")
    from unittest.mock import AsyncMock, MagicMock
    import gateway.run as gateway_run
    from gateway.config import PlatformConfig
    from gateway.platforms.api_server import APIServerAdapter

    runner = GatewayRunner(GatewayConfig(sessions_dir=tmp_path / "sessions"))
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="123", user_id="u1")
    key = runner._session_key_for_source(source)
    runner.session_store.get_or_create_session(source)
    if persisted:
        runner.session_store.set_model_override(key, {"model": "persisted-model"})
    reads = []
    original = runner.session_store.get_model_override
    monkeypatch.setattr(runner.session_store, "get_model_override", lambda k: (reads.append(threading.get_ident()), original(k))[1])
    monkeypatch.setattr(gateway_run, "_gateway_runner_ref", lambda: runner)
    adapter = APIServerAdapter(PlatformConfig(enabled=True, extra={"key": "test-key"}))
    monkeypatch.setattr(adapter, "_resolve_route", lambda _model: {"provider": "configured"})
    monkeypatch.setattr(adapter, "_get_existing_session_or_404", AsyncMock(return_value=({}, None)))
    monkeypatch.setattr(adapter, "_persist_session_runtime_lock", lambda *args: True)
    original_conflict = adapter._request_route_conflict_error

    def conflict(**kwargs):
        original_conflict(**kwargs)
        return "route validation boundary reached"

    monkeypatch.setattr(adapter, "_request_route_conflict_error", conflict)
    body = {"model": "model", "provider": "requested", "message": "hi", "input": "hi",
            "messages": [{"role": "user", "content": "hi"}]}
    request = MagicMock()
    request.headers = {"X-Hermes-Session-Key": key, "Authorization": "Bearer test-key"}
    request.path = "/v1/runs" if site == "runs" else "/v1/chat/completions"
    request.app = {}
    request.get.side_effect = lambda _key, default=None: default
    request.match_info = {"session_id": "test-session"}
    request.json = AsyncMock(return_value=body)
    loop_thread = threading.get_ident()
    original_state = runner._session_state
    original_commit = runner._commit_session_model_override
    ownership_errors = []

    def state(session_key):
        if runner._peek_session_state(session_key) is None and threading.get_ident() != loop_thread:
            ownership_errors.append("session state created off-loop")
        return original_state(session_key)

    def commit(*args):
        if threading.get_ident() != loop_thread:
            ownership_errors.append("model override committed off-loop")
        return original_commit(*args)

    monkeypatch.setattr(runner, "_session_state", state)
    monkeypatch.setattr(runner, "_commit_session_model_override", commit)
    hermes_loop_guard.set_loop_thread(loop_thread)
    try:
        if site == "chat":
            _, response = await adapter._prepare_session_chat(request)
        elif site == "completions":
            response = await adapter._handle_chat_completions(request)
        elif site == "responses":
            response = await adapter._handle_responses(request)
        else:
            response = await adapter._handle_runs(request)
        assert response.status == 400
        assert reads and loop_thread not in reads, f"{site} store read on loop: {reads}"
        assert not ownership_errors, ownership_errors
        assert runner._peek_session_state(key) is not None
        if persisted:
            assert runner._session_model_override(key)["model"] == "persisted-model"
    finally:
        hermes_loop_guard.set_loop_thread(None)
        if adapter._session_db is not None:
            adapter._session_db.close()


@pytest.mark.asyncio
async def test_new_topic_first_turn_session_setup_is_off_loop(tmp_path, monkeypatch):
    runner = GatewayRunner(GatewayConfig(sessions_dir=tmp_path / "sessions"))
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="123", user_id="u1",
                           chat_type="dm", thread_id="311210")
    event = MessageEvent(text="First topic turn", source=source)
    monkeypatch.setattr(runner, "_is_telegram_topic_lane", lambda _source: True)
    hermes_loop_guard.set_loop_thread(threading.get_ident())
    try:
        resolved = await runner._hmwa_resolve_session(event, source)
        assert resolved is not None
        _, entry, key = resolved
        await runner._hmwa_open_session(entry, key, source)
        assert entry.session_key == runner._session_key_for_source(source)
        assert await runner.async_session_store.peek_session_id(key) == entry.session_id
    finally:
        hermes_loop_guard.set_loop_thread(None)


@pytest.mark.parametrize("outcome,code", [("success", 0), ("failed", 1), ("restart", 75)])
@pytest.mark.parametrize("entrypoint", ["module", "cli"])
def test_gateway_process_exits_before_asyncio_executor_join(outcome, code, entrypoint, tmp_path):
    """A default-executor job and non-daemon worker must not outlive teardown."""
    script = textwrap.dedent('''
        import asyncio, sys, threading
        import gateway.run as run
        outcome, entrypoint = sys.argv[1:]
        block, started = threading.Event(), threading.Event()
        def stuck():
            started.set()
            block.wait(12)
        async def fake_start(*args, **kwargs):
            asyncio.get_running_loop().run_in_executor(None, stuck)
            while not started.is_set():
                await asyncio.sleep(0)
            threading.Thread(target=lambda: block.wait(12), daemon=False).start()
            print("Gateway stopped", flush=True)
            if outcome == "restart":
                raise SystemExit(75)
            return outcome == "success"
        run.start_gateway = fake_start
        sys.argv = ["gateway.run"]
        if entrypoint == "module":
            run.main()
        else:
            import hermes_cli.gateway as cli
            from types import SimpleNamespace
            for name in ("_guard_official_docker_root_gateway", "_guard_named_profile_under_multiplexer",
                         "_attach_to_host_gateway_or_guard", "_guard_supervised_gateway_conflict",
                         "_guard_existing_gateway_process_conflict"):
                setattr(cli, name, lambda *args, **kwargs: None)
            cli.supports_systemd_services = lambda: False
            cli.sys.stdin = SimpleNamespace(isatty=lambda: False)
            cli.run_gateway()
    ''')
    env = dict(os.environ, HERMES_HOME=str(tmp_path / "home"),
               HERMES_RUNTIME_DIR=str(tmp_path / "runtime"))
    child = subprocess.Popen([sys.executable, "-c", script, outcome, entrypoint], stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True, env=env,
                             cwd=Path(__file__).resolve().parents[2])
    try:
        try:
            stdout, stderr = child.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            # Finite test workers release themselves; never signal a running gateway.
            stdout, stderr = child.communicate(timeout=20)
            pytest.fail(f"Gateway stopped but process remained alive: {stdout}\n{stderr}")
        assert "Gateway stopped" in stdout, stderr
        assert child.returncode == code, (stdout, stderr)
    finally:
        if child.poll() is None:
            child.communicate(timeout=30)
