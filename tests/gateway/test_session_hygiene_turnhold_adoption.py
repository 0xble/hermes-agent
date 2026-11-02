"""Regression tests for #97963 — hygiene turn-hold must not burn a
watermark-fenced compression attempt.

The 10s ``hygiene_max_turn_hold_seconds`` budget (#92318) releases the
arriving user turn while a thinking summary model is still streaming its
reasoning prefix. Before the fix, that release ALWAYS cancelled the commit
fence, so 100% of the summary attempt (including the full thinking prefix)
was discarded on every turn — auto-compression permanently failed for any
deployment whose summary model thinks longer than the hold.

The fix decouples the turn from the compression: when the worker's commit is
watermark-fenced (rows appended after compression start survive its commit
verbatim as concurrent tail), the detached worker KEEPS its commit admission
and the summary is adopted at its own watermark-fenced commit boundary. The
turn is still released at the same budget — the invariant pinned by
``test_session_hygiene_turn_hold_budget_abandons_streaming_wait`` (#90845)
is untouched (that test's worker is NOT watermark-fenced and still takes the
cancel path).
"""

import asyncio
import importlib
import sys
import threading
import time
import types
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from gateway.config import GatewayConfig, Platform, PlatformConfig
from gateway.platforms.base import BasePlatformAdapter, SendResult
from gateway.platforms.event import MessageEvent
from gateway.session import SessionEntry, SessionSource


def _make_history(n_messages: int, content_size: int = 100) -> list:
    history = []
    content = "x" * content_size
    for i in range(n_messages):
        role = "user" if i % 2 == 0 else "assistant"
        history.append({"role": role, "content": content, "timestamp": f"t{i}"})
    return history


class _CaptureAdapter(BasePlatformAdapter):
    def __init__(self):
        super().__init__(
            PlatformConfig(enabled=True, token="fake-token"), Platform.TELEGRAM
        )
        self.sent = []

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        return True

    async def disconnect(self) -> None:
        return None

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.sent.append({"chat_id": chat_id, "content": content})
        return SendResult(success=True, message_id="x")

    async def get_chat_info(self, chat_id: str):
        return {"id": chat_id}


def _write_turnhold_config(tmp_path):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        "compression:\n"
        "  enabled: true\n"
        "  hygiene_timeout_seconds: 60\n"
        "  hygiene_total_ceiling_seconds: 600\n"
        "  hygiene_max_turn_hold_seconds: 0.3\n"
        "  hygiene_failure_cooldown_seconds: 120\n"
    )


def _build_runner(gateway_run, adapter, fake_db):
    runner = object.__new__(gateway_run.GatewayRunner)
    runner.config = GatewayConfig(
        platforms={
            Platform.TELEGRAM: PlatformConfig(enabled=True, token="fake-token")
        }
    )
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._voice_mode = {}
    runner.hooks = SimpleNamespace(emit=AsyncMock(), loaded_hooks=False)
    runner.session_store = MagicMock()
    runner.session_store.get_or_create_session.return_value = SessionEntry(
        session_key="agent:main:telegram:dm:12345",
        session_id="sess-97963",
        created_at=datetime.now(),
        updated_at=datetime.now(),
        platform=Platform.TELEGRAM,
        chat_type="dm",
    )
    runner.session_store.load_transcript.return_value = _make_history(
        6, content_size=400
    )
    runner.session_store.has_any_sessions.return_value = True
    runner.session_store.rewrite_transcript = MagicMock()
    runner.session_store.append_to_transcript = MagicMock()
    runner._running_agents = {}
    runner._pending_messages = {}
    runner._pending_approvals = {}
    runner._session_db = SimpleNamespace(_db=fake_db)
    runner._is_user_authorized = lambda _source: True
    runner._set_session_env = lambda _context: None
    runner._run_agent = AsyncMock(
        return_value={
            "final_response": "ok",
            "messages": [],
            "tools": [],
            "history_offset": 0,
            "last_prompt_tokens": 0,
        }
    )
    return runner


def _make_event():
    return MessageEvent(
        text="hello",
        source=SessionSource(
            platform=Platform.TELEGRAM,
            chat_id="12345",
            chat_type="dm",
            user_id="12345",
        ),
        message_id="1",
    )


def _install_fakes(monkeypatch, gateway_run, tmp_path, agent_cls):
    fake_dotenv = types.ModuleType("dotenv")
    fake_dotenv.load_dotenv = lambda *args, **kwargs: None
    monkeypatch.setitem(sys.modules, "dotenv", fake_dotenv)
    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = agent_cls
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)
    monkeypatch.setattr(gateway_run, "_hermes_home", tmp_path)
    monkeypatch.setattr(
        gateway_run, "_resolve_runtime_agent_kwargs", lambda: {"api_key": "fake"}
    )
    monkeypatch.setattr(
        "agent.model_metadata.get_model_context_length",
        lambda *_args, **_kwargs: 100,
    )


async def _drain_deferred(runner, timeout=10.0):
    tasks = getattr(runner, "_deferred_agent_cleanup_tasks", None) or set()
    if tasks:
        await asyncio.wait_for(
            asyncio.gather(*list(tasks), return_exceptions=True), timeout
        )


async def _drain_background(runner, timeout=10.0):
    """Await the adoption callback's retained writes (streak reset, token count). They run in
    worker threads after the agent's cleanup, so asserting on them earlier races the thread."""
    async def drain():
        while tasks := list(getattr(runner, "_background_tasks", None) or ()):
            await asyncio.gather(*tasks, return_exceptions=True)

    await asyncio.wait_for(drain(), timeout)


@pytest.mark.asyncio
@pytest.mark.parametrize("hold_bookkeeping", [False, True])
async def test_turn_hold_keeps_admission_and_adopts_watermark_fenced_summary(
    monkeypatch, tmp_path, hold_bookkeeping
):
    """A watermark-fenced worker keeps its commit admission at turn-hold
    expiry; its late summary is ADOPTED (committed), not discarded — while
    the turn itself is still released at the budget (#90845 invariant).
    """
    worker_started = threading.Event()
    release_worker = threading.Event()
    committed = threading.Event()
    cleanup_done = threading.Event()
    worker_gave_up = threading.Event()
    reset_started = threading.Event()
    release_reset = threading.Event()
    reset_finished = threading.Event()
    fake_db = MagicMock()
    fake_db.get_compression_failure_cooldown.return_value = None

    def reset_streak(*args):
        reset_started.set()
        if hold_bookkeeping:
            assert release_reset.wait(5), "test never released the durable streak reset"
        reset_finished.set()

    fake_db.reset_hygiene_failure_streak.side_effect = reset_streak

    class FencedStreamingAgent:
        last_instance = None

        def __init__(self, **kwargs):
            self.session_id = kwargs.get("session_id", "sess-97963")
            self._session_db = kwargs.get("session_db")
            self._last_compaction_in_place = False
            self.context_compressor = SimpleNamespace(
                bind_session_state=MagicMock(),
                _last_compress_aborted=False,
                _last_aux_model_failure_model=None,
            )
            self.shutdown_memory_provider = MagicMock()
            self.close = MagicMock(side_effect=cleanup_done.set)
            type(self).last_instance = self

        def _compress_context(
            self, messages, *_args, commit_fence=None, **_kwargs
        ):
            # Real compress_context marks the fence right after capturing
            # the active-row watermark under the durable compression lock.
            if commit_fence is not None:
                commit_fence.mark_commit_watermark_fenced()
            worker_started.set()
            # Thinking-model shape: continuous progress, no commit yet —
            # only the turn-hold budget can release the waiting turn.
            # Bounded spin: a failing assertion before release_worker.set()
            # must not leave this executor thread alive forever (pytest
            # would hang at interpreter exit joining executor threads).
            _spin_started = time.monotonic()
            while not release_worker.is_set():
                if time.monotonic() - _spin_started > 120:
                    worker_gave_up.set()
                    return (messages, None)
                if commit_fence is not None:
                    commit_fence.touch_progress()
                time.sleep(0.01)
            if commit_fence is not None and not commit_fence.begin_commit():
                return (messages, None)
            try:
                self._session_db.archive_and_compact(
                    self.session_id,
                    [{"role": "assistant", "content": "summary"}],
                    watermark=6,
                )
                self._last_compaction_in_place = True
                committed.set()
                return ([{"role": "assistant", "content": "summary"}], None)
            finally:
                if commit_fence is not None:
                    commit_fence.finish_commit()

    gateway_run = importlib.import_module("gateway.run")
    _write_turnhold_config(tmp_path)
    _install_fakes(monkeypatch, gateway_run, tmp_path, FencedStreamingAgent)

    adapter = _CaptureAdapter()
    runner = _build_runner(gateway_run, adapter, fake_db)
    cleanup_futures = []
    defer_cleanup = runner._defer_agent_cleanup_until_future_done

    def capture_cleanup(future, agent, **kwargs):
        cleanup_futures.append(future)
        defer_cleanup(future, agent, **kwargs)

    monkeypatch.setattr(runner, "_defer_agent_cleanup_until_future_done", capture_cleanup)

    # Generous outer bound only, kept under the 60s hygiene timeout so that path cannot pass for
    # this one: the release is proven by ordering below, not by wall-clock speed.
    result = await asyncio.wait_for(runner._handle_message(_make_event()), timeout=45)

    # #90845/#92318 invariant intact: the turn is released at the budget, i.e. it completed
    # while the worker was still streaming. The worker only ends once the test releases it,
    # so a turn that waited for it could not have returned with these events unset.
    assert result == "ok"
    assert worker_started.is_set()
    assert not release_worker.is_set() and not committed.is_set() and not worker_gave_up.is_set(), (
        "turn returned only after the worker finished despite the turn-hold budget"
    )
    assert runner._run_agent.await_count == 1

    # (b) NO retry-after was armed while the attempt is still running —
    # arming it would block the agent-side preflight from adopting the
    # finished summary ("same-session cooldown active", #97963).
    assert not fake_db.record_compression_failure_cooldown.called, (
        "keep-admission path must not arm the retry-after while the "
        "detached attempt is still running"
    )

    # The detached worker finishes late; its commit is ADMITTED (adoption),
    # not refused — the summary attempt is no longer burned.
    release_worker.set()
    await asyncio.wait_for(asyncio.to_thread(committed.wait, 5), timeout=6)
    assert committed.is_set(), (
        "watermark-fenced worker must keep its commit admission after "
        "turn-hold expiry (fence was cancelled — attempt burned)"
    )
    fake_db.archive_and_compact.assert_called_once()
    # The commit went through the watermark-fenced path (concurrent tail
    # rows above the watermark survive the compaction).
    assert fake_db.archive_and_compact.call_args.kwargs.get("watermark") == 6

    if hold_bookkeeping:
        try:
            assert await asyncio.to_thread(reset_started.wait, 5)
            assert cleanup_futures and not cleanup_futures[-1].done(), (
                "deferred cleanup must wait for durable adoption, not just the compressor"
            )
            assert not cleanup_done.is_set()
        finally:
            release_reset.set()
    await _drain_deferred(runner)
    await asyncio.wait_for(asyncio.to_thread(cleanup_done.wait, 5), timeout=6)
    assert reset_finished.is_set()
    FencedStreamingAgent.last_instance.close.assert_called_once()
    await _drain_background(runner)

    # Successful adoption resets the hygiene failure streak and still never
    # advances it (the deferral is not a failure).
    assert not fake_db.increment_hygiene_failure_streak.called
    assert fake_db.reset_hygiene_failure_streak.called
    # The summary was adopted: the turn-hold is internal lifecycle, so the user sees nothing.
    sent = [m["content"] for m in adapter.sent]
    assert sent == [], f"adopted turn-hold must not notify the user, got: {sent}"
    # The pre-compaction prompt count is dropped so the next turn does not re-fire hygiene.
    assert runner.session_store.get_or_create_session.return_value.last_prompt_tokens == 0
    assert runner._hygiene_deferred_adoption_epoch("agent:main:telegram:dm:12345") == 1


@pytest.mark.asyncio
async def test_turn_hold_kept_admission_arms_flat_retry_only_when_nothing_commits(
    monkeypatch, tmp_path
):
    """If the kept-admission worker ends WITHOUT committing (summary failed
    / attempt superseded), the flat non-escalating retry-after is restored so
    sustained traffic does not spawn-and-abandon a compressor every turn —
    but only AFTER the attempt truly ended, and without touching the streak.
    """
    worker_started = threading.Event()
    release_worker = threading.Event()
    fake_db = MagicMock()
    fake_db.get_compression_failure_cooldown.return_value = None

    class FencedNoCommitAgent:
        def __init__(self, **kwargs):
            self.session_id = kwargs.get("session_id", "sess-97963")
            self._session_db = kwargs.get("session_db")
            self._last_compaction_in_place = False
            self.context_compressor = SimpleNamespace(
                bind_session_state=MagicMock(),
                _last_compress_aborted=False,
                _last_aux_model_failure_model=None,
            )
            self.shutdown_memory_provider = MagicMock()
            self.close = MagicMock()

        def _compress_context(
            self, messages, *_args, commit_fence=None, **_kwargs
        ):
            if commit_fence is not None:
                commit_fence.mark_commit_watermark_fenced()
            worker_started.set()
            _spin_started = time.monotonic()
            while not release_worker.is_set():
                if time.monotonic() - _spin_started > 20:
                    return (messages, None)
                if commit_fence is not None:
                    commit_fence.touch_progress()
                time.sleep(0.01)
            # Summary failed — return unchanged, no commit.
            return (messages, None)

    gateway_run = importlib.import_module("gateway.run")
    _write_turnhold_config(tmp_path)
    _install_fakes(monkeypatch, gateway_run, tmp_path, FencedNoCommitAgent)

    adapter = _CaptureAdapter()
    runner = _build_runner(gateway_run, adapter, fake_db)

    result = await asyncio.wait_for(runner._handle_message(_make_event()), timeout=15)
    assert result == "ok"
    assert worker_started.is_set()
    # While the attempt still runs: no cooldown, so preflight adoption
    # stays possible.
    assert not fake_db.record_compression_failure_cooldown.called

    release_worker.set()
    await _drain_deferred(runner)
    # Let the done-callback fire.
    for _ in range(100):
        if fake_db.record_compression_failure_cooldown.called:
            break
        await asyncio.sleep(0.05)

    # Nothing committed → flat retry-after restored (spacing), streak intact.
    assert fake_db.record_compression_failure_cooldown.called, (
        "a kept-admission attempt that ends without committing must restore "
        "the flat turn-hold retry-after spacing"
    )
    args = fake_db.record_compression_failure_cooldown.call_args[0]
    retry = args[1] - time.time()
    assert retry <= 120, (
        f"retry-after must stay flat (~60s), got {retry:.0f}s"
    )
    assert "turn-hold" in (args[2] or "")
    assert not fake_db.increment_hygiene_failure_streak.called, (
        "turn-hold deferral must never advance the failure streak"
    )
    # Nothing was adopted, but nothing failed either: a superseded/no-op attempt stays silent.
    assert [m["content"] for m in adapter.sent] == []


@pytest.mark.asyncio
async def test_turn_hold_kept_admission_summary_failure_warns_user(
    monkeypatch, tmp_path
):
    """A kept-admission worker whose summary really fails after the turn-hold
    posts the ordinary hygiene-failure warning (and never the deferral notice):
    nothing will be adopted, so the user is told, exactly once."""
    worker_started = threading.Event()
    release_worker = threading.Event()
    fake_db = MagicMock()
    fake_db.get_compression_failure_cooldown.return_value = None

    class FencedFailingAgent:
        def __init__(self, **kwargs):
            self.session_id = kwargs.get("session_id", "sess-97963")
            self._session_db = kwargs.get("session_db")
            self._last_compaction_in_place = False
            self.context_compressor = SimpleNamespace(
                bind_session_state=MagicMock(),
                _last_compress_aborted=False,
                _last_summary_error=None,
                _last_aux_model_failure_model=None,
            )
            self.shutdown_memory_provider = MagicMock()
            self.close = MagicMock()

        def _compress_context(self, messages, *_args, commit_fence=None, **_kwargs):
            if commit_fence is not None:
                commit_fence.mark_commit_watermark_fenced()
            worker_started.set()
            _spin_started = time.monotonic()
            while not release_worker.is_set():
                if time.monotonic() - _spin_started > 20:
                    break
                if commit_fence is not None:
                    commit_fence.touch_progress()
                time.sleep(0.01)
            self.context_compressor._last_compress_aborted = True
            self.context_compressor._last_summary_error = "summary model returned 500"
            return (messages, None)

    gateway_run = importlib.import_module("gateway.run")
    _write_turnhold_config(tmp_path)
    _install_fakes(monkeypatch, gateway_run, tmp_path, FencedFailingAgent)

    adapter = _CaptureAdapter()
    runner = _build_runner(gateway_run, adapter, fake_db)

    result = await asyncio.wait_for(runner._handle_message(_make_event()), timeout=15)
    assert result == "ok"
    assert worker_started.is_set()
    # The turn-hold itself is silent while the worker may still be adopted.
    assert [m["content"] for m in adapter.sent] == []

    release_worker.set()
    await _drain_deferred(runner)
    for _ in range(100):
        if adapter.sent:
            break
        await asyncio.sleep(0.05)

    sent = [m["content"] for m in adapter.sent]
    from agent.i18n import t
    assert sent == [t("gateway.compress.hygiene_failed")], sent
    # A real failure escalates like the inline abort path.
    assert fake_db.increment_hygiene_failure_streak.called
    assert fake_db.record_compression_failure_cooldown.called


@pytest.mark.asyncio
async def test_turn_hold_without_watermark_fence_still_cancels(
    monkeypatch, tmp_path
):
    """A worker whose commit is NOT watermark-fenced (no session_db /
    watermark capture failed) must still be cancelled at turn-hold expiry —
    a late unfenced commit could clobber newer turns. Never worse than the
    status quo. (Complements the pinned #90845 test, which exercises the
    same path through the public surface.)
    """
    worker_started = threading.Event()
    release_worker = threading.Event()
    fake_db = MagicMock()
    fake_db.get_compression_failure_cooldown.return_value = None

    class UnfencedStreamingAgent:
        def __init__(self, **kwargs):
            self.session_id = kwargs.get("session_id", "sess-97963")
            self._session_db = kwargs.get("session_db")
            self._last_compaction_in_place = False
            self.context_compressor = SimpleNamespace(
                bind_session_state=MagicMock(),
                _last_compress_aborted=False,
                _last_aux_model_failure_model=None,
            )
            self.shutdown_memory_provider = MagicMock()
            self.close = MagicMock()

        def _compress_context(
            self, messages, *_args, commit_fence=None, **_kwargs
        ):
            # Deliberately NO mark_commit_watermark_fenced().
            worker_started.set()
            _spin_started = time.monotonic()
            while not release_worker.is_set():
                if time.monotonic() - _spin_started > 20:
                    return (messages, None)
                if commit_fence is not None:
                    commit_fence.touch_progress()
                time.sleep(0.01)
            if commit_fence is not None and not commit_fence.begin_commit():
                return (messages, None)
            try:
                self._session_db.archive_and_compact(
                    self.session_id,
                    [{"role": "assistant", "content": "too late"}],
                )
                return ([{"role": "assistant", "content": "too late"}], None)
            finally:
                if commit_fence is not None:
                    commit_fence.finish_commit()

    gateway_run = importlib.import_module("gateway.run")
    _write_turnhold_config(tmp_path)
    _install_fakes(monkeypatch, gateway_run, tmp_path, UnfencedStreamingAgent)

    adapter = _CaptureAdapter()
    runner = _build_runner(gateway_run, adapter, fake_db)

    result = await asyncio.wait_for(runner._handle_message(_make_event()), timeout=15)
    assert result == "ok"
    assert worker_started.is_set()

    release_worker.set()
    await _drain_deferred(runner)
    await asyncio.sleep(0.2)
    # The unfenced late commit was refused — discard as before the fix.
    fake_db.archive_and_compact.assert_not_called()
    # Legacy path still records the flat retry-after immediately.
    assert fake_db.record_compression_failure_cooldown.called
    assert not fake_db.increment_hygiene_failure_streak.called
    # Cancelled: nothing will be adopted, so the deferral notice is the user's only signal.
    from agent.i18n import t
    assert [m["content"] for m in adapter.sent] == [t("gateway.compress.turnhold_deferred")]


class _OrderedStore:
    """``update_session`` in completion order, like the off-loop facade. *during_first_write* runs while the
    turn's own write is in flight, before it lands."""

    def __init__(self, during_first_write=None):
        self.last_prompt_tokens = None
        self.writes = []
        self._during_first_write = during_first_write

    async def update_session(self, _key, last_prompt_tokens=None, touch_activity=True):
        hook, self._during_first_write = self._during_first_write, None
        if hook is not None:
            await hook(self)
        self.writes.append(last_prompt_tokens)
        self.last_prompt_tokens = last_prompt_tokens


def _bare_runner():
    gateway_run = importlib.import_module("gateway.run")
    return object.__new__(gateway_run.GatewayRunner)


@pytest.mark.asyncio
async def test_adoption_landing_while_the_turn_count_is_written_still_ends_at_zero():
    """The turn checks the adoption epoch, then writes its count off-loop. An adoption that lands in that
    window bumps the epoch and its own ``last_prompt_tokens=0`` write can finish FIRST; the turn's stale
    pre-compaction count then lands on top and re-fires hygiene next turn. The re-check after the write
    must leave 0."""
    runner = _bare_runner()
    key = "agent:main:telegram:dm:12345"

    async def _adoption_lands(store):
        runner._note_hygiene_deferred_adoption(key)
        store.writes.append(0)
        store.last_prompt_tokens = 0

    store = _OrderedStore(during_first_write=_adoption_lands)
    await runner._hmwa_store_turn_prompt_tokens(store, key, key, 91_000, 0, touch_activity=True)
    assert store.writes == [0, 91_000, 0]
    assert store.last_prompt_tokens == 0


@pytest.mark.asyncio
async def test_turn_count_is_kept_when_no_adoption_lands():
    runner = _bare_runner()
    store = _OrderedStore()
    await runner._hmwa_store_turn_prompt_tokens(store, "k", "k", 91_000, 0, touch_activity=True)
    assert store.writes == [91_000]


@pytest.mark.asyncio
async def test_adoption_before_the_write_stores_zero_once():
    runner = _bare_runner()
    runner._note_hygiene_deferred_adoption("k")
    store = _OrderedStore()
    await runner._hmwa_store_turn_prompt_tokens(store, "k", "k", 91_000, 0, touch_activity=True)
    assert store.writes == [0]
