"""Boot auto-resume is a parent notice, not child reconstruction."""

from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.run import _drain_gateway_watch_events
from gateway.run import GatewayRunner


@pytest.mark.asyncio
async def test_boot_notice_delivery_claims_only_trigger_and_injects_parent_turn(monkeypatch):
    runner = object.__new__(GatewayRunner)
    runner._completion_event_scope = lambda _evt: nullcontext()
    runner._classify_completion_target = AsyncMock(return_value="deliver")
    runner._completion_delivery_ready = AsyncMock(return_value=True)
    runner._inject_watch_notification = AsyncMock(return_value=True)

    claim = {"delegation_id": "deleg-1", "auto_resume_claim": "boot:claim"}
    claim_fn = lambda _delegation_id: (claim, None)
    complete = lambda delegation_id, claim_id: seen.append(("complete", delegation_id, claim_id))
    release = lambda delegation_id, claim_id: seen.append(("release", delegation_id, claim_id))
    seen = []
    monkeypatch.setattr("tools.delegation_resume.claim_auto_resume_trigger", claim_fn)
    monkeypatch.setattr("tools.delegation_resume.complete_auto_resume_trigger", complete)
    monkeypatch.setattr("tools.delegation_resume.release_auto_resume_trigger", release)

    evt = {
        "type": "delegation_auto_resume",
        "delegation_id": "deleg-1",
        "parent_session_id": "parent-1",
        "text": "call delegate_task(action='resume', subagent_id='deleg-1')",
    }
    assert await runner._deliver_auto_resume_notice(evt) is True
    runner._inject_watch_notification.assert_awaited_once_with(evt["text"], evt, raise_not_accepted=True)
    assert seen == [("complete", "deleg-1", "boot:claim")]


@pytest.mark.asyncio
async def test_boot_notice_is_requeued_when_parent_route_is_not_ready(monkeypatch):
    runner = object.__new__(GatewayRunner)
    runner._completion_event_scope = lambda _evt: nullcontext()
    runner._classify_completion_target = AsyncMock(return_value="retry")
    runner._completion_delivery_ready = AsyncMock(return_value=False)
    runner._inject_watch_notification = AsyncMock()

    evt = {"type": "delegation_auto_resume", "delegation_id": "deleg-2", "parent_session_id": "parent-2"}
    assert await runner._deliver_auto_resume_notice(evt) is False
    runner._inject_watch_notification.assert_not_awaited()


def test_gateway_watch_drain_preserves_boot_notice_for_async_watcher():
    import queue

    events = queue.Queue()
    boot = {"type": "delegation_auto_resume", "delegation_id": "deleg-3"}
    events.put(boot)
    assert _drain_gateway_watch_events(events) == []
    assert events.get_nowait() == boot


def test_startup_queues_parent_notice_without_reconstructing_child(monkeypatch):
    import queue

    from gateway.run_startup import GatewayStartupMixin

    class _Registry:
        completion_queue = queue.Queue()

    record = {
        "delegation_id": "deleg-4",
        "session_key": "agent:default:telegram:dm:123",
        "origin_ui_session_id": "ui-4",
        "origin_session_id": "",
        "parent_session_id": "parent-4",
        "task": {"goal": "sensitive goal", "scope_id": "scope-4"},
    }
    monkeypatch.setattr("tools.process_registry.process_registry", _Registry())
    monkeypatch.setattr("tools.delegation_resume.list_boot_candidates", lambda: [record])
    monkeypatch.setattr("hermes_cli.profiles.get_active_profile_name", lambda: "default")
    monkeypatch.setattr("gateway.run._multiplex_profile_homes", lambda _config: [])
    runner = object.__new__(GatewayStartupMixin)
    runner.config = object()

    assert runner._schedule_auto_resume_delegations() == 1
    event = _Registry.completion_queue.get_nowait()
    assert event["type"] == "delegation_auto_resume"
    assert event["delegation_id"] == "deleg-4"
    assert "sensitive goal" not in event["text"]
