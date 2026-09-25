"""Boot auto-resume is a parent notice, not child reconstruction."""

from contextlib import nullcontext
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from gateway.config import Platform
from gateway.run import _drain_gateway_watch_events
from gateway.run import GatewayRunner
from gateway.session import SessionSource


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
async def test_boot_notice_suppresses_unauthorized_target_before_claimed_injection(monkeypatch):
    runner = object.__new__(GatewayRunner)
    runner._completion_event_scope = lambda _evt: nullcontext()
    runner._build_process_event_source = Mock(return_value=SessionSource(
        platform=Platform.TELEGRAM, chat_id="chat-1", chat_type="dm", user_id="foreign",
    ))
    runner._resolve_injection_adapter = Mock(return_value=object())
    runner._completion_delivery_ready = AsyncMock(return_value=True)
    runner._is_user_authorized_for_source = Mock(return_value=False)
    runner._classify_completion_target = AsyncMock(return_value="deliver")
    runner._inject_watch_notification = AsyncMock(side_effect=AssertionError("unauthorized target was injected"))

    seen = []
    monkeypatch.setattr(
        "tools.delegation_resume.claim_auto_resume_trigger",
        lambda _delegation_id: ({"auto_resume_claim": "boot:unauthorized"}, None),
    )
    monkeypatch.setattr(
        "tools.delegation_resume.complete_auto_resume_trigger",
        lambda delegation_id, claim_id: seen.append((delegation_id, claim_id)),
    )

    evt = {
        "type": "delegation_auto_resume",
        "delegation_id": "deleg-unauthorized",
        "parent_session_id": "parent-unauthorized",
    }
    assert await runner._deliver_auto_resume_notice(evt) is True
    assert seen == [("deleg-unauthorized", "boot:unauthorized")]
    runner._is_user_authorized_for_source.assert_called_once()


@pytest.mark.asyncio
async def test_boot_notice_defers_disconnected_owner_then_delivers_after_reconnect(monkeypatch):
    runner = object.__new__(GatewayRunner)
    runner._completion_event_scope = lambda _evt: nullcontext()
    source = SessionSource(platform=Platform.TELEGRAM, chat_id="chat-1", chat_type="dm", user_id="owner")
    runner._build_process_event_source = Mock(return_value=source)
    adapter = object()
    runner._resolve_injection_adapter = Mock(return_value=None)
    runner._is_user_authorized_for_source = Mock(return_value=False)
    runner._classify_completion_target = AsyncMock(return_value="deliver")
    runner._completion_delivery_ready = AsyncMock(return_value=True)
    runner._inject_watch_notification = AsyncMock(return_value=True)
    seen = []
    monkeypatch.setattr("tools.delegation_resume.claim_auto_resume_trigger",
                        lambda _id: (seen.append("claim") or {"auto_resume_claim": "boot:claim"}, None))
    monkeypatch.setattr("tools.delegation_resume.complete_auto_resume_trigger",
                        lambda *_args: seen.append("complete"))
    monkeypatch.setattr("tools.delegation_resume.release_auto_resume_trigger",
                        lambda *_args: seen.append("release"))
    evt = {"type": "delegation_auto_resume", "delegation_id": "deleg-reconnect",
           "parent_session_id": "parent-reconnect", "text": "resume"}
    assert await runner._deliver_auto_resume_notice(evt) is False
    assert seen == []
    runner._is_user_authorized_for_source.assert_not_called()
    runner._resolve_injection_adapter.return_value = adapter
    runner._is_user_authorized_for_source.return_value = True
    assert await runner._deliver_auto_resume_notice(evt) is True
    assert seen == ["claim", "complete"]
    runner._inject_watch_notification.assert_awaited_once()


@pytest.mark.asyncio
async def test_api_server_injection_uses_event_profile_adapter(monkeypatch):
    runner = object.__new__(GatewayRunner)
    runner._build_process_event_source = Mock(return_value=None)
    secondary = SimpleNamespace(supports_async_delivery=False)
    runner._adapters_for_profile = Mock(return_value={Platform.API_SERVER: secondary})
    runner._self_post_api_server = AsyncMock(return_value=True)

    evt = {
        "type": "delegation_auto_resume",
        "profile": "secondary",
        "origin_session_id": "api-session-secondary",
        "platform": "api_server",
    }
    assert await runner._inject_watch_notification("resume", evt) is True
    runner._adapters_for_profile.assert_called_once_with("secondary")
    runner._self_post_api_server.assert_awaited_once_with(secondary, "resume", "api-session-secondary", evt)


@pytest.mark.asyncio
async def test_completion_scope_uses_stamped_profile_for_raw_api_event(monkeypatch):
    runner = object.__new__(GatewayRunner)
    runner._build_process_event_source = Mock(return_value=None)
    entered = []

    class _Scope:
        def __enter__(self):
            entered.append("enter")
        def __exit__(self, *_args):
            entered.append("exit")

    monkeypatch.setattr("hermes_cli.profiles.profile_exists", lambda name: name == "secondary")
    monkeypatch.setattr("hermes_cli.profiles.get_profile_dir", lambda name: "/profiles/" + name)
    monkeypatch.setattr("gateway.run._async_profile_runtime_scope", lambda home: _Scope())
    monkeypatch.setattr("hermes_constants.get_hermes_home_override", lambda: "/profiles/default")

    with runner._completion_event_scope({"profile": "secondary", "platform": "api_server"}):
        pass
    assert entered == ["enter", "exit"]


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
    assert event["profile"] == "default"
    assert "sensitive goal" not in event["text"]


def test_startup_auto_resume_can_be_disabled_by_gateway_config(monkeypatch):
    import queue

    from gateway.run_startup import GatewayStartupMixin

    class _Registry:
        completion_queue = queue.Queue()

    monkeypatch.setattr("tools.process_registry.process_registry", _Registry())
    monkeypatch.setattr("tools.delegation_resume.list_boot_candidates", lambda: [{"delegation_id": "must-not-queue"}])
    runner = object.__new__(GatewayStartupMixin)
    runner.config = SimpleNamespace(auto_resume_on_boot=False)

    assert runner._schedule_auto_resume_delegations() == 0
    assert _Registry.completion_queue.empty()
