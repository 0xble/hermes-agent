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
async def test_stale_topic_binding_prune_runs_off_the_loop(tmp_path):
    from telegram.error import BadRequest

    from hermes_state import SessionDB
    from plugins.platforms.telegram.adapter import TelegramAdapter

    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session(session_id="sess", source="telegram", user_id="2027045491")
        db.bind_telegram_topic(chat_id="2027045491", thread_id="150565", user_id="2027045491",
                               session_key="agent:main:telegram:dm:2027045491:150565", session_id="sess")
        adapter = TelegramAdapter(PlatformConfig(enabled=True, token="test-token"))
        adapter._session_store = SimpleNamespace(_db=db)
        adapter._bot = SimpleNamespace(send_message=AsyncMock(side_effect=[
            BadRequest("Message thread not found"), BadRequest("Message thread not found"),
            SimpleNamespace(message_id=120)]))
        _arm()
        result = await adapter._send_with_retry(
            chat_id="2027045491", content="Completed response", reply_to="150565",
            metadata={"notify": True, "thread_id": "150565", "telegram_reply_to_message_id": "150565",
                      "telegram_dm_topic_reply_fallback": True},
        )
        hermes_loop_guard.set_loop_thread(None)
        assert result.success is True
        assert adapter._bot.send_message.await_count == 3
        # On the loop the guarded delete raised inside the best-effort prune and the row survived.
        assert db.get_telegram_topic_binding(chat_id="2027045491", thread_id="150565") is None
    finally:
        hermes_loop_guard.set_loop_thread(None)
        db.close()


@pytest.mark.asyncio
async def test_goal_continuation_recheck_runs_off_the_loop(monkeypatch):
    from hermes_cli import session_controls
    from gateway.turn_context import TurnContext

    runner = object.__new__(GatewayRunner)
    threads = []

    def refresh(event, session_id):
        threads.append(threading.get_ident())

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
async def test_topic_session_reasoning_pick_persists_off_the_loop(tmp_path, monkeypatch):
    from gateway.telegram_topic_sessions import _apply_session_settings

    runner = _reasoning_runner(tmp_path, monkeypatch)
    entry = SimpleNamespace(session_key="agent:main:telegram:dm:67890:42", was_auto_reset=True)
    loop_thread = _arm()
    await _apply_session_settings(runner, _source(), entry, None, {"enabled": True, "effort": "high"},
                                  announce=False)
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
async def test_unknown_turn_marker_release_runs_off_the_loop(monkeypatch, tmp_path):
    from gateway import run_runtime
    from gateway import runtime_bootstrap, runtime_ownership, session_authorities, zero_cut_standby
    import gateway.run as gateway_run

    class _Runner:
        _session_db = None

    class _Registry:
        launch = SimpleNamespace(epoch=1)

        def profile_ids(self):
            return frozenset({'default'})

        def served_profiles(self):
            return []

    runner = _Runner()
    runner.config = SimpleNamespace(multiplex_profiles=False)
    runner._client_owned = False
    threads = []
    monkeypatch.setattr(run_runtime, 'reserved_profile_homes', lambda _runner: [('default', tmp_path)])
    monkeypatch.setattr(runtime_ownership.process_ownership, 'owns', lambda _home: True)
    monkeypatch.setattr(gateway_run, '_write_runtime_status_quiet', lambda **_kwargs: None)
    monkeypatch.setattr(run_runtime, '_record_parked_profiles', lambda _parked: None)
    monkeypatch.setattr(run_runtime, '_prewarm_profile_stores', AsyncMock())
    monkeypatch.setattr(run_runtime, '_build_profile_authority', AsyncMock())
    monkeypatch.setattr(run_runtime, '_park_reserved_profile', lambda *_args: None)
    monkeypatch.setattr(session_authorities, 'SessionAuthorities', lambda *_args, **_kwargs: _Registry())
    monkeypatch.setattr(run_runtime, 'release_unknown_turn_markers',
                        lambda _runner: threads.append(threading.get_ident()))
    monkeypatch.setattr(zero_cut_standby, 'is_standby', lambda: False)
    monkeypatch.setattr(runtime_bootstrap, 'TicketStore', lambda *_args: object())
    loop_thread = _arm()
    await run_runtime.initialize_gateway_runtime(runner)
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


@pytest.mark.asyncio
async def test_gateway_start_arms_the_guard_and_stop_disarms_it(monkeypatch, tmp_path):
    """Stops a gateway that never registers its loop thread at startup, which would leave every
    loop-thread state.db wait unreported on the live gateway."""
    from hermes_loop_guard import LoopThreadBlockingError
    from hermes_state import SessionDB

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    for name in ("GATEWAY_ALLOW_ALL_USERS", "WEIXIN_ALLOW_ALL_USERS"):
        monkeypatch.delenv(name, raising=False)
    # An open-policy platform without its opt-in ends the real startup at the access gate, right
    # after the loop is bound and before any adapter or background service starts.
    config = GatewayConfig(
        platforms={Platform.WEIXIN: PlatformConfig(enabled=True, extra={"dm_policy": "open"})},
        sessions_dir=tmp_path / "sessions",
    )
    runner = GatewayRunner(config)
    try:
        assert await runner.start() is True
        assert runner.should_exit_cleanly is True

        with pytest.raises(LoopThreadBlockingError, match="SessionDB open"):
            SessionDB(db_path=tmp_path / "probe.db")
        worker_db = await asyncio.to_thread(SessionDB, db_path=tmp_path / "probe.db")
        await asyncio.to_thread(worker_db.close)
    finally:
        await runner.stop()

    loop_db = SessionDB(db_path=tmp_path / "probe.db")  # disarmed: shutdown drains on the loop
    loop_db.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("notify", [False, True])
async def test_process_completion_acknowledgement_runs_off_the_loop(monkeypatch, tmp_path, notify):
    import tools.process_registry as pr_module
    from tests.gateway.test_background_process_notifications import _build_runner, _FakeRegistry, _watcher_dict

    threads = []
    session = SimpleNamespace(output_buffer="ok\n", exited=True, exit_code=0, command="build", started_at=None)
    registry = _FakeRegistry([session])
    # Acknowledging loads retained results, whose ownership check opens state.db (_owns_result).
    registry.acknowledge_survivor_completion = lambda session_id: threads.append(threading.get_ident())
    monkeypatch.setattr(pr_module, "process_registry", registry)
    runner = _build_runner(monkeypatch, tmp_path, "concise")
    runner._enqueue_process_completion_notification = AsyncMock(return_value=True)
    runner._launching_turn_active = AsyncMock(return_value=False)
    loop_thread = _arm()
    await runner._run_process_watcher({**_watcher_dict(), "notify_on_complete": notify})
    assert threads and loop_thread not in threads


@pytest.mark.asyncio
async def test_ws_backend_heartbeat_registration_runs_off_the_loop(monkeypatch):
    from tui_gateway import server, ws as ws_mod

    threads = []
    # The first heartbeat refresh writes the backend's row through the state.db writer.
    monkeypatch.setattr(server, "_start_backend_heartbeat_refresher", lambda: threads.append(threading.get_ident()))
    monkeypatch.setattr(server, "resolve_skin", lambda: "default")
    monkeypatch.setattr(server, "_ensure_skin_watcher", lambda: None)
    monkeypatch.setattr(server, "register_live_transport", lambda *_a, **_k: None)
    monkeypatch.setattr(server, "_WS_ORPHAN_REAP_GRACE_S", 0)

    class _WS:
        async def accept(self):
            pass

        async def send_text(self, line):
            pass

        async def receive_text(self):
            raise ws_mod._WebSocketDisconnect()

        async def close(self):
            pass

    loop_thread = _arm()
    await ws_mod.handle_ws(_WS())
    assert threads and loop_thread not in threads


def test_cron_rpc_restore_and_admission_reads_run_off_the_loop(tmp_path, monkeypatch):
    from cron import jobs
    from gateway import run, session_cron, session_local_recovery
    from gateway.session import SessionStore
    from gateway.session_authority import SessionAuthority
    from hermes_state import SessionDB
    from tests.fakes.runtime_owner import begin_runtime_epoch

    monkeypatch.setenv('HERMES_HOME', str(tmp_path))
    monkeypatch.setenv('HERMES_MODEL', 'fixture')
    monkeypatch.setattr(run, '_load_gateway_config', lambda: {'model': {}, 'platform_toolsets': {'cli': []}})
    threads = {}

    def _probe(name, real):
        def _call(*args, **kwargs):
            threads.setdefault(name, []).append(threading.get_ident())
            return real(*args, **kwargs)
        return _call

    # The route restore takes the session-store lock; the admission reads can fall back to the writer.
    monkeypatch.setattr(session_local_recovery, '_restore_local_entry',
                        _probe('restore', session_local_recovery._restore_local_entry))
    monkeypatch.setattr(session_cron, 'get_session_admission', _probe('admission', session_cron.get_session_admission))
    with jobs.use_cron_store(tmp_path / 'cron'):
        job = jobs.create_job(prompt='original', schedule='every 1h', deliver='telegram:123')
        db = SessionDB(tmp_path / 'state.db')
        runner = SimpleNamespace(_draining=False, session_store=SessionStore(tmp_path / 'sessions', GatewayConfig()),
                                 adapters={})
        authority = SessionAuthority(runner, profile_id=str(tmp_path), instance_id='test', db=db,
                                     epoch=begin_runtime_epoch(db, instance_id='test'))
        monkeypatch.setattr(authority, '_schedule', lambda ref: None)
        runner.session_authority = authority

        async def probe():
            params = {'job_id': job['id'], 'request_id': 'fire', 'extra_prompt': None}
            receipt = await session_cron.operation(authority, 'submit', params)
            status = await session_cron.operation(authority, 'status', {
                'session_id': receipt['session_id'], 'admission_id': receipt['admission_id']})
            assert status['status'] == 'queued'
            return threading.get_ident()

        try:
            loop_thread = asyncio.run(probe())
        finally:
            db.close()
    assert set(threads) == {'restore', 'admission'}
    assert all(loop_thread not in idents for idents in threads.values()), threads
