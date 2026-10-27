"""Loop-side state.db steps found by arming the loop guard on every test event loop (2026-10-26).

Conversation boundaries (/new, /resume, auto reset, /branch) persisted the /yolo clear through the
session-store lock on the loop, and codex app-server hygiene wrote its streak/cooldown on the loop.
"""

from __future__ import annotations

import asyncio
import threading
from types import SimpleNamespace

import pytest

import gateway.run as gateway_run
import hermes_loop_guard
from gateway.run import GatewayRunner


@pytest.fixture(autouse=True)
def _guard_reset():
    hermes_loop_guard._reset_for_tests()
    yield
    hermes_loop_guard._reset_for_tests()


class _Probe:
    def __init__(self, value=None):
        self.value = value
        self.threads: list[int] = []

    def __call__(self, *_args, **_kwargs):
        self.threads.append(threading.get_ident())
        return self.value

    def off_loop(self, loop_thread: int) -> bool:
        return bool(self.threads) and loop_thread not in self.threads


def _runner_with_store(yolo):
    runner = object.__new__(GatewayRunner)
    runner.session_store = SimpleNamespace(set_session_yolo=yolo)
    runner._peek_session_state = lambda _key: None
    runner._pending_skills_reload_notes = {}
    return runner


@pytest.mark.asyncio
async def test_boundary_yolo_clear_runs_off_the_loop():
    yolo = _Probe(True)
    runner = _runner_with_store(yolo)
    loop_thread = threading.get_ident()
    hermes_loop_guard.set_loop_thread(loop_thread)

    runner._clear_session_boundary_security_state("k", persist_yolo=False)
    await asyncio.to_thread(runner._persist_boundary_yolo_clear, "k")

    assert yolo.off_loop(loop_thread)


def test_boundary_clear_still_persists_yolo_for_sync_callers():
    yolo = _Probe(True)
    _runner_with_store(yolo)._clear_session_boundary_security_state("k")
    assert len(yolo.threads) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("compacts", [True, False])
async def test_codex_hygiene_streak_and_cooldown_writes_run_off_the_loop(monkeypatch, compacts):
    loop_thread = threading.get_ident()
    reset, cooldown = _Probe(), _Probe()
    monkeypatch.setattr(gateway_run, "_reset_hygiene_failure_streak", reset)
    monkeypatch.setattr(gateway_run, "_record_hygiene_cooldown", cooldown)
    compressor = SimpleNamespace(compression_count=0)
    release = threading.Event()

    def _compress(*_args, **_kwargs):
        if compacts:
            compressor.compression_count += 1
        else:
            release.wait(10)  # a wedged app-server compaction, released at teardown

    agent = SimpleNamespace(_codex_session=object(), context_compressor=compressor, _compress_context=_compress)
    monkeypatch.setattr(gateway_run, "_cached_agent_for_hygiene", lambda _gw, _key: agent)
    hermes_loop_guard.set_loop_thread(loop_thread)

    try:
        outcome = await gateway_run.run_codex_hygiene_compaction(
            SimpleNamespace(), "k", "sid", auto_mode="hermes", history=[], approx_tokens=1,
            timeout_seconds=1.0)
    finally:
        release.set()

    if compacts:
        assert outcome == "compacted" and reset.off_loop(loop_thread)
    else:
        assert outcome == "failed:timeout" and cooldown.off_loop(loop_thread)
