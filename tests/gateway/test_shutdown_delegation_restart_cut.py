"""Real shutdown phases must distinguish recoverable restart cuts from terminal stops."""

import json
import queue
import threading
import time
from unittest.mock import AsyncMock

import pytest

from gateway.run_shutdown import GatewayShutdownMixin
from tests.gateway.restart_test_helpers import make_restart_runner
from tools import async_delegation as ad
from tools.delegate_tool_registry import _handle_control_action
from tools.delegation_resume import is_trusted_restart_cut
from tools.process_registry import process_registry
from tools.process_registry_notifications import _format_async_delegation


@pytest.mark.parametrize("reason", [
    "/stop", "gateway restart", "gateway_restart", "gateway restarting",
    "gateway shutdown (post-interrupt)", "gateway shutdown (final-cleanup)",
])
def test_only_the_shared_producer_restart_token_is_trusted(reason):
    assert is_trusted_restart_cut("gateway shutdown (restart)")
    assert not is_trusted_restart_cut(reason)


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", ["post-interrupt", "final-cleanup"])
@pytest.mark.parametrize("restart", [False, True])
@pytest.mark.parametrize("batch", [False, True])
async def test_real_shutdown_offers_resume_for_every_lifecycle_cut_except_user_stop(monkeypatch, tmp_path, phase, restart, batch):
    monkeypatch.setattr(ad, "_db_path", lambda: tmp_path / "state.db")
    monkeypatch.setattr(process_registry, "completion_queue", queue.Queue())
    monkeypatch.setattr(process_registry, "kill_all", lambda **_kwargs: 0)
    monkeypatch.setattr("tools.environments.base.kill_live_foreground_processes", lambda **_kwargs: 0)
    monkeypatch.setattr("cron.scheduler.mark_running_jobs_interrupted", lambda *_args, **_kwargs: [])
    monkeypatch.setattr("tools.terminal_tool_lifecycle.cleanup_all_environments", lambda: None)
    monkeypatch.setattr("tools.browser_tool_lifecycle.cleanup_all_browsers", lambda: None)
    monkeypatch.setattr("agent.auxiliary_client.shutdown_cached_clients", lambda: None)
    monkeypatch.setattr(GatewayShutdownMixin, "_mark_running_sessions_resume_pending", AsyncMock(return_value=[]))
    runner, _adapter = make_restart_runner()
    runner._restart_requested = restart
    runner._post_interrupt_grace_timeout = lambda: 0.0
    runner._notify_interrupted_cron_jobs = AsyncMock()
    runner._send_interrupted_turn_notes = AsyncMock(return_value=[])
    runner._notify_active_sessions_of_shutdown = AsyncMock()
    ctx = GatewayShutdownMixin._StopContext(deferred_count=lambda: 0, started_at=time.monotonic())
    ad._reset_for_tests()
    started, release = threading.Event(), threading.Event()

    def child():
        started.set()
        assert release.wait(5)
        return ({"results": [{"task_index": 0, "status": "completed", "summary": "done"}]}
                if batch else {"status": "interrupted"})

    kwargs = dict(context="ctx", toolsets=None, role="leaf", model="m",
                  session_key="agent:main:telegram:dm:1", parent_session_id="owner",
                  runner=child, interrupt_fn=lambda reason=None: release.set(), max_async_children=1)
    try:
        handle = (ad.dispatch_async_delegation_batch(goals=["first", "second"], **kwargs) if batch
                  else ad.dispatch_async_delegation(goal="first", **kwargs))
        assert started.wait(5)
        if phase == "post-interrupt":
            await runner._stop_interrupt_remaining_work(ctx)
        else:
            await runner._stop_release_runtime_state(ctx)
        event = process_registry.completion_queue.get(timeout=5)
        assert event["cut_reason"] == ("gateway shutdown (restart)" if restart else f"gateway shutdown ({phase})")
        text = _format_async_delegation(event)
        # Exactly one instruction: a restart cut offers explicit resume; other lifecycle cuts carry
        # the scheduled automatic retry and must not also tell the parent to resume by hand.
        if restart:
            assert "delegate_task(action='resume'" in text
            assert "retry this automatically" not in text
        else:
            assert "retry this automatically" in text and "Do not re-dispatch" in text
            assert "action='resume'" not in text
        parent = type("Parent", (), {"session_id": "owner", "_session_db": None})()
        payload = json.loads(_handle_control_action("resume", handle["delegation_id"], None, parent))
        assert payload["status"] == "retry_claimed"
        if batch:
            assert [task["goal"] for task in payload["tasks"]] == ["second"]
        else:
            assert payload["goal"] == "first"
    finally:
        release.set()
        ad._reset_for_tests()
