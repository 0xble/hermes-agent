"""Per-turn and per-message loop paths the live loop guard named on 2026-10-25.

Each case drives the real loop-side caller on a registered loop thread with the session-store or
state.db step replaced by a probe that records its thread. Most steps swallow errors (so the
guard's raise alone would not fail the test); the probe proves the step ran off the loop.
"""

from __future__ import annotations

import asyncio
import copy
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import hermes_yaml as yaml

import hermes_loop_guard
from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.run import GatewayRunner
from gateway.session import SessionSource
from gateway.turn_context import TurnContext


@pytest.fixture(autouse=True)
def _guard_reset():
    hermes_loop_guard._reset_for_tests()
    yield
    hermes_loop_guard._reset_for_tests()


def _arm() -> int:
    ident = threading.get_ident()
    hermes_loop_guard.set_loop_thread(ident)
    return ident


class _Probe:
    """Callable recording the thread of each call, returning ``value``."""

    def __init__(self, value=None):
        self.value = value
        self.threads: list[int] = []

    def __call__(self, *_args, **_kwargs):
        self.threads.append(threading.get_ident())
        return self.value

    def off_loop(self, loop_thread: int) -> bool:
        return bool(self.threads) and loop_thread not in self.threads


def _source(**overrides) -> SessionSource:
    fields = dict(platform=Platform.TELEGRAM, chat_id="123", user_id="owner", chat_type="dm")
    fields.update(overrides)
    return SessionSource(**fields)


@pytest.fixture
def lane(tmp_path, monkeypatch):
    """A real runner on an attended-routing profile (the production per-turn path)."""
    home = tmp_path / "owner"
    home.mkdir()
    cfg = {"model": {"default": "base", "provider": "baseline", "attended": {
        "model": "attended-model", "provider": "attended-provider", "idle_revert_minutes": 1,
    }}}
    (home / "config.yaml").write_text(yaml.safe_dump(cfg))
    runner = GatewayRunner(GatewayConfig(sessions_dir=tmp_path / "sessions", multiplex_profiles=True))
    source = _source()
    entry = runner.session_store.get_or_create_session(source)
    monkeypatch.setattr(runner, "_resolve_profile_home_for_source", lambda _: home)
    monkeypatch.setattr(runner, "_normalize_source_for_session_key", lambda source: source)
    monkeypatch.setattr(runner, "_evict_cached_agent", lambda _: None)
    monkeypatch.setattr("gateway.run._resolve_runtime_agent_kwargs", lambda: {
        "provider": "baseline", "api_key": "fixture", "api_mode": "chat_completions",
    })
    monkeypatch.setattr("gateway.run._resolve_runtime_agent_kwargs_for_provider", lambda provider, **_: {
        "provider": provider, "api_key": "fixture", "api_mode": "chat_completions",
    })
    monkeypatch.setattr("gateway.run._credential_pool_for_provider", lambda _: None)
    monkeypatch.setattr("gateway.run._resolve_gateway_model",
                        lambda config=None: (config or cfg)["model"]["default"])
    yield SimpleNamespace(runner=runner, source=source, key=entry.session_key, cfg=cfg)
    hermes_loop_guard.set_loop_thread(None)
    if runner._executor:
        runner._executor.shutdown(wait=True)


@pytest.mark.asyncio
async def test_attended_route_resolution_reads_route_state_off_the_loop(lane, monkeypatch):
    store = lane.runner.session_store
    route_active = _Probe(True)
    monkeypatch.setattr(store, "get_attended_route_active", route_active)
    loop_thread = _arm()
    await lane.runner._resolve_session_agent_runtime_async(
        source=lane.source, session_key=lane.key, user_config=copy.deepcopy(lane.cfg))
    assert route_active.off_loop(loop_thread)


@pytest.mark.asyncio
async def test_turn_assembly_hydrates_reasoning_pick_off_the_loop(lane, monkeypatch):
    store = lane.runner.session_store
    store.set_reasoning_override(lane.key, {"enabled": True, "effort": "high"})
    reads = _Probe({"enabled": True, "effort": "high"})
    monkeypatch.setattr(store, "get_reasoning_override", reads)
    loop_thread = _arm()
    pick = await lane.runner._session_reasoning_override_async(lane.key)
    assert pick == {"enabled": True, "effort": "high"}
    assert reads.off_loop(loop_thread)
    display = SimpleNamespace(**vars(TurnContext(
        user_config=copy.deepcopy(lane.cfg), resolve_display_setting=lambda *_: False,
    )), platform_key="telegram")
    reads.threads.clear()
    _, turn, _ = lane.runner._run_agent_build_turn_context(
        display, None, message="hi", source=lane.source, session_key=lane.key, run_generation=None,
        reasoning_override=pick,
    )
    assert turn._model_assignment["reasoning_override"] == pick
    assert not reads.threads  # assembly itself never reads the store


@pytest.mark.asyncio
async def test_hygiene_activity_stamp_persists_off_the_loop():
    touch = _Probe()
    agent = SimpleNamespace(_touch_activity=touch)
    loop_thread = _arm()
    await GatewayRunner._hmwa_hygiene_stamp(
        agent, "session hygiene compression aborted", "AGENT_COMPRESSION_COOLDOWN", "label")
    assert touch.off_loop(loop_thread)




@pytest.mark.asyncio
async def test_slack_thread_hydration_reads_and_advances_watermark_off_the_loop(monkeypatch):
    from plugins.platforms.slack.adapter import SlackAdapter

    adapter = SlackAdapter(PlatformConfig(enabled=True, token="xoxb-test"))
    active, get_mark, set_mark = _Probe(True), _Probe("100.0"), _Probe()
    monkeypatch.setattr(adapter, "_has_active_session_for_thread", active)
    monkeypatch.setattr(adapter, "_get_thread_watermark", get_mark)
    monkeypatch.setattr(adapter, "_set_thread_watermark", set_mark)
    monkeypatch.setattr(adapter, "_fetch_thread_context", AsyncMock(return_value="missed replies"))
    loop_thread = _arm()
    context, _, _ = await adapter._hydrate_thread_context(
        channel_id="C1", event_thread_ts="100.0", ts="101.0", user_id="U1", team_id="T1",
        is_thread_reply=True, is_mentioned=False, is_dm=False,
    )
    assert context == "missed replies"
    assert active.off_loop(loop_thread)
    assert get_mark.off_loop(loop_thread)
    assert set_mark.off_loop(loop_thread)




@pytest.mark.asyncio
async def test_role_hold_without_attended_routing_reads_route_state_off_the_loop(lane, monkeypatch):
    cfg = {"model": {"default": "base", "provider": "baseline"},
           "model_presets": {"fast": {"model": "first", "provider": "provider-a"}}}
    (lane.runner._resolve_profile_home_for_source(lane.source) / "config.yaml").write_text(yaml.safe_dump(cfg))
    await lane.runner._set_session_model_role(lane.key, "fast")
    route_active = _Probe(True)
    monkeypatch.setattr(lane.runner.session_store, "get_attended_route_active", route_active)
    loop_thread = _arm()
    await lane.runner._resolve_session_agent_runtime_async(
        source=lane.source, session_key=lane.key, user_config=cfg)
    assert route_active.off_loop(loop_thread)


@pytest.mark.asyncio
async def test_turn_dispatch_passes_the_off_loop_reasoning_pick_to_assembly(lane, monkeypatch):
    runner, pick = lane.runner, {"enabled": True, "effort": "high"}
    runner.session_store.set_reasoning_override(lane.key, pick)
    reads = _Probe(pick)
    monkeypatch.setattr(runner.session_store, "get_reasoning_override", reads)
    captured = {}

    class _Assembled(Exception):
        pass

    def build(_disp, _agent_cls, **kwargs):
        captured.update(kwargs)
        raise _Assembled

    monkeypatch.setattr(runner, "_get_proxy_url", lambda: None)
    monkeypatch.setattr(runner, "_run_agent_display_settings",
                        lambda _source: SimpleNamespace(user_config=copy.deepcopy(lane.cfg)))
    monkeypatch.setattr(runner, "_run_agent_build_turn_context", build)
    loop_thread = _arm()
    with pytest.raises(_Assembled):
        await runner._run_agent_inner("hi", "", [], lane.source, "sid", session_key=lane.key)
    assert captured["reasoning_override"] == pick
    assert reads.off_loop(loop_thread)


def _racing_read(runner, key, during):
    """Persisted ``high`` read off the loop, with ``during`` landing on the loop mid-read."""
    loop = asyncio.get_running_loop()

    def read(_key):
        asyncio.run_coroutine_threadsafe(during(), loop).result(timeout=5)
        return {"enabled": True, "effort": "high"}

    runner.session_store.get_reasoning_override = read


@pytest.mark.asyncio
@pytest.mark.parametrize("existing_state", [True, False])
async def test_reasoning_reset_during_rehydration_is_not_undone(lane, existing_state):
    runner = lane.runner
    if existing_state:
        runner._session_state(lane.key)
    else:
        runner._sessions.pop(lane.key, None)

    async def reset():
        runner._session_state(lane.key).conversation.reasoning_override = None

    _racing_read(runner, lane.key, reset)
    assert await runner._session_reasoning_override_async(lane.key) is None
    assert runner._session_state(lane.key).conversation.reasoning_override is None


@pytest.mark.asyncio
async def test_reasoning_pick_set_during_rehydration_wins(lane):
    runner = lane.runner
    runner._session_state(lane.key)
    low = {"enabled": True, "effort": "low"}

    async def pick():
        runner._session_state(lane.key).conversation.reasoning_override = dict(low)

    _racing_read(runner, lane.key, pick)
    assert await runner._session_reasoning_override_async(lane.key) == low
    assert runner._session_state(lane.key).conversation.reasoning_override == low


@pytest.mark.asyncio
async def test_hygiene_gate_waits_for_a_cooldown_scheduled_by_an_unwinding_attempt(lane):
    runner = lane.runner
    started, release, written = threading.Event(), threading.Event(), []

    def write():
        started.set()
        release.wait(5)
        written.append(True)

    runner._hmwa_hygiene_schedule_cooldown("sid", write, failure_label="test")
    await asyncio.to_thread(started.wait, 5)
    threading.Timer(0.2, release.set).start()
    await runner._hmwa_hygiene_await_pending_cooldown("sid")
    assert written == [True]
    await asyncio.sleep(0)
    assert "sid" not in runner._hygiene_pending_cooldowns


def test_failure_streak_does_not_resurrect_an_evicted_session(tmp_path):
    from gateway.run import _hygiene_cooldown_for_failure
    from hermes_state import SessionDB

    runner = object.__new__(GatewayRunner)
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        runner._session_db = db
        assert _hygiene_cooldown_for_failure(runner, "agent:main:telegram:dm:9", 300.0) == 300.0
        assert runner._peek_session_state("agent:main:telegram:dm:9") is None
        assert _hygiene_cooldown_for_failure(runner, "agent:main:telegram:dm:9", 300.0) == 900.0
    finally:
        db.close()
