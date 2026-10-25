"""Compression must defer execution, not consume inputs as retry notices."""

import asyncio
import threading
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from hermes_state import SessionDB
from tests.gateway.test_session_hygiene import _make_cooldown_runner


@pytest.mark.asyncio
async def test_hygiene_turnhold_retries_inputs_after_commit(monkeypatch, tmp_path):
    sid = "hygiene-deferred"
    started = threading.Event()
    release = threading.Event()
    committed = threading.Event()
    done = asyncio.Event()
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session(sid, "telegram")
    summary = [{"role": "assistant", "content": "committed summary"}]
    executions = []

    class Compressor:
        def __init__(self, **kwargs):
            self.session_id = sid
            self.context_compressor = SimpleNamespace(
                bind_session_state=MagicMock(), _last_compress_aborted=False,
                _last_aux_model_failure_model=None,
            )
            self.shutdown_memory_provider = MagicMock()
            self.close = MagicMock()

        def _compress_context(self, messages, *_args, commit_fence=None, **kwargs):
            assert db.try_acquire_compression_lock(sid, "hygiene-worker")
            commit_fence.mark_commit_watermark_fenced()
            started.set()
            try:
                assert release.wait(30), "test did not release hygiene"
                assert commit_fence.begin_commit()
                try:
                    runner.session_store.load_transcript.return_value = summary
                    self._last_compaction_in_place = True
                    committed.set()
                    return summary, None
                finally:
                    commit_fence.finish_commit()
            finally:
                db.release_compression_lock(sid, "hygiene-worker")

    runner, adapter, first = _make_cooldown_runner(monkeypatch, tmp_path, Compressor, db, sid)
    key = runner.session_store.get_or_create_session.return_value.session_key
    monkeypatch.setattr(runner, "_lookup_session_id_under_store_lock", lambda *_: sid)
    runner._run_post_turn_hooks = AsyncMock()
    adapter._message_handler = runner._handle_message
    # Avoid outbound presentation and unrelated durable delivery fixtures. The real
    # adapter processing/drain and real gateway admission/preparation remain in use.
    adapter._start_typing_refresh = MagicMock(return_value=None)
    runner.session_store.get_model_override.return_value = None
    adapter._stop_typing_refresh = AsyncMock()
    adapter._fire_post_delivery_callback = AsyncMock()
    (tmp_path / "config.yaml").write_text(
        "compression:\n  enabled: true\n  hygiene_max_turn_hold_seconds: 0.01\n"
        "  hygiene_timeout_seconds: 60\n  hygiene_total_ceiling_seconds: 600\n",
        encoding="utf-8",
    )

    # This case requires an already-watermarked worker. Thread-pool startup can
    # exceed the tiny turn-hold budget, so establish that precondition before
    # entering the real waiter without changing its clock or budget.
    wait_for_summary = runner._hmwa_hygiene_wait_for_summary

    async def wait_for_watermarked_summary(attempt, hs, session_entry):
        assert await asyncio.to_thread(started.wait, 5), "hygiene worker did not start"
        assert attempt.commit_fence.commit_watermark_fenced
        assert not attempt.commit_fence.is_cancelled
        return await wait_for_summary(attempt, hs, session_entry)

    monkeypatch.setattr(runner, "_hmwa_hygiene_wait_for_summary", wait_for_watermarked_summary)

    async def run_agent(**kwargs):
        assert committed.is_set(), "model ran against history still owned by hygiene"
        assert kwargs["history"] == summary
        executions.append(kwargs["message"])
        # The model runner normally promotes FIFO followups before returning.
        # Keep that production promotion boundary while replacing model calls.
        if key not in adapter._pending_messages:
            pending = runner._promote_queued_event(key, adapter, None)
            if pending is not None:
                adapter._pending_messages[key] = pending
        if len(executions) == 3:
            done.set()
        return {"final_response": "", "messages": [], "tools": [],
                "history_offset": 0, "last_prompt_tokens": 0}

    runner._run_agent = AsyncMock(side_effect=run_agent)
    first.text = "initiating input"
    second = replace(first, text="process finished", message_id="2", internal=True)
    third = replace(first, text="resume pending", message_id="3", internal=True)
    try:
        # A direct internal dispatch has no adapter guard. Its automatic retry must
        # still acquire an owner and drain without another inbound message.
        assert await asyncio.wait_for(runner._handle_message(first), 10) is None
        assert started.is_set() and not committed.is_set()
        assert await runner._handle_message(second) is None
        assert await runner._handle_message(third) is None
        assert runner._run_agent.await_count == 0
        assert adapter.sent == []
        runner._run_post_turn_hooks.assert_not_awaited()
        release.set()
        await asyncio.wait_for(done.wait(), 15)
        assert executions == ["initiating input", "process finished", "resume pending"]
        assert not any("compression is already running" in m["content"] for m in adapter.sent)
        assert not adapter._pending_messages
        assert not runner._overflow_queue(key)
    finally:
        release.set()
        await adapter.disconnect()
        tasks = list(getattr(adapter, "_background_tasks", set()))
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        tasks = getattr(runner, "_deferred_agent_cleanup_tasks", None) or set()
        if tasks:
            await asyncio.wait_for(asyncio.gather(*list(tasks), return_exceptions=True), 10)
        db.close()


@pytest.mark.asyncio
async def test_compression_gate_preserves_event_identity_and_controls(monkeypatch, tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    sid = "already-compressing"
    db.create_session(sid, "telegram")
    runner, adapter, event = _make_cooldown_runner(monkeypatch, tmp_path, MagicMock, db, sid)
    key = runner.session_store.get_or_create_session.return_value.session_key
    monkeypatch.setattr(runner, "_lookup_session_id_under_store_lock", lambda *_: sid)
    # A direct dispatch needs one fallback drain, but a live adapter task drains
    # its own slot. Keep this dispatch owned so the test can inspect exact inputs.
    adapter._active_sessions[key] = asyncio.Event()
    db.try_acquire_compression_lock(sid, "compression-worker")
    monkeypatch.setattr("hermes_cli.session_controls.internal_wake_is_current", lambda *_a, **_k: True)
    wake = replace(event, text="goal wake", message_id="wake", internal=True,
                   metadata={"goal_continuation": True, "goal_continuation_instance": "goal-a"})
    try:
        assert await runner._handle_message(event) is None
        assert await runner._handle_message(wake) is None
        assert adapter._pending_messages[key] is event
        assert runner._overflow_queue(key) == [wake]
        assert wake.internal and wake.metadata["goal_continuation_instance"] == "goal-a"
        runner._run_agent.assert_not_awaited()
        assert adapter.sent == []
        # Control commands remain available while compression defers model turns.
        event = replace(event, text="/status", message_id="status")
        runner._hm_dispatch_idle_commands = AsyncMock(return_value=(True, "status reply"))
        assert await runner._handle_message(event) == "status reply"
    finally:
        db.release_compression_lock(sid, "compression-worker")
        db.close()
