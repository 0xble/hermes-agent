"""Native lifecycle transitions stay best effort and fenced to the owning surface."""

import asyncio
from unittest.mock import AsyncMock, call

import pytest

from tests.gateway.test_slack import _slack_mod, adapter as slack_fixture


@pytest.fixture
def adapter(monkeypatch):
    monkeypatch.setattr(_slack_mod, "_AGENT_SESSIONS_SUPPORTED", True)
    return slack_fixture.__wrapped__()


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_status", ["processing", "active"])
async def test_native_failure_preserves_legacy_status_and_clear_retry(adapter, caplog, failed_status):
    md = {"thread_id": "1700.0001", "slack_team_id": "T1"}
    native = adapter._app.client.agents_sessions_setStatus = AsyncMock()
    legacy = adapter._app.client.assistant_threads_setStatus = AsyncMock()

    async def fail(**kwargs):
        if kwargs["status"] == failed_status:
            raise RuntimeError("native unavailable")

    native.side_effect = fail
    caplog.set_level("INFO")
    await adapter.send_typing("C1", md)
    await adapter.send_typing("C1", md)
    await adapter.stop_typing("C1", md)
    assert [c.kwargs["status"] for c in native.await_args_list] == ["processing", "active"]
    assert [c.kwargs["status"] for c in legacy.await_args_list] == ["is thinking...", "is thinking...", ""]
    assert any("C1" in message and "1700.0001" in message and failed_status in message
               and "native unavailable" in message for message in caplog.messages)
    if failed_status == "active":
        native.side_effect = None
        await adapter.stop_typing("C1")  # Target survives the failed native clear.
        assert native.await_args == call(channel_id="C1", thread_ts="1700.0001", status="active")
        assert not adapter._active_status_threads


@pytest.mark.asyncio
@pytest.mark.parametrize("retiring", ["send", "stop", "native_active"])
async def test_successor_keeps_processing_when_retired_request_finishes(adapter, retiring):
    """Both endpoints mutate server state, including a legacy empty-string clear."""
    md = {"thread_id": "1700.0001", "slack_team_id": "T1"}
    entered, release, registered = (asyncio.Event() for _ in range(3))
    completions, server_status = [], ["active"]
    delayed = [False]

    async def apply(protocol, value):
        if delayed[0] and (
                (protocol == "legacy" and retiring != "native_active")
                or (protocol == "native" and value == "active")):
            delayed[0] = False
            entered.set()
            await release.wait()
        completions.append((protocol, value))
        # https://docs.slack.dev/ai/migrating-to-agent-messaging/#compatibility-bridge
        server_status[0] = value if protocol == "native" else "processing" if value else "active"

    async def native(**kwargs):
        await apply("native", kwargs["status"])

    async def legacy(**kwargs):
        await apply("legacy", kwargs["status"])

    adapter._app.client.agents_sessions_setStatus = AsyncMock(side_effect=native)
    adapter._app.client.assistant_threads_setStatus = AsyncMock(side_effect=legacy)
    old_owner = asyncio.create_task(asyncio.Event().wait())
    scope = adapter._typing_scope_key("C1", md)
    owners = adapter.__dict__.setdefault("_typing_refresh_owners", {})
    owners[scope] = old_owner
    await adapter.send_typing("C1", md)
    delayed[0] = True
    method = adapter.send_typing if retiring == "send" else adapter.stop_typing
    pending = asyncio.create_task(method("C1", md))
    successor = successor_send = None
    try:
        await asyncio.wait_for(entered.wait(), 5)
        successor = asyncio.create_task(asyncio.Event().wait())
        owners[scope] = successor
        status_entry = adapter._status_entry

        def register(*args):
            result = status_entry(*args)
            registered.set()
            return result

        adapter._status_entry = register
        successor_send = asyncio.create_task(adapter.send_typing("C1", md))
        await asyncio.wait_for(registered.wait(), 5)
        release.set()
        await asyncio.wait_for(asyncio.gather(pending, successor_send), 5)
        assert server_status[0] == "processing", completions
        assert completions[-2:] == [("native", "processing"), ("legacy", "is thinking...")]
        entry = adapter._active_status_threads[("T1", "C1", "1700.0001")]
        assert entry["owner"] is successor
        assert owners[scope] is successor
        before = list(completions)
        await adapter._stop_typing_refresh("C1", old_owner, metadata=md)
        assert completions == before, "retired cleanup wrote to the successor's session"
        assert owners[scope] is successor
        assert adapter._active_status_threads[("T1", "C1", "1700.0001")] is entry
    finally:
        release.set()
        tasks = [task for task in (pending, old_owner, successor, successor_send) if task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("inflight_protocol", ["native", "legacy"])
async def test_retired_clear_waiting_for_status_lock_writes_neither_protocol(adapter, inflight_protocol):
    """A clear queued before successor admission must recheck before either API write."""
    md = {"thread_id": "1700.0001", "slack_team_id": "T1"}
    entered, clearing, registered, release = (asyncio.Event() for _ in range(4))
    completions, server_status = [], ["active"]
    delayed = [False]

    async def apply(protocol, value):
        if protocol == inflight_protocol and not delayed[0]:
            delayed[0] = True
            entered.set()
            await release.wait()
        completions.append((protocol, value))
        server_status[0] = value if protocol == "native" else "processing" if value else "active"

    async def native(**kwargs):
        await apply("native", kwargs["status"])

    async def legacy(**kwargs):
        await apply("legacy", kwargs["status"])

    adapter._app.client.agents_sessions_setStatus = AsyncMock(side_effect=native)
    adapter._app.client.assistant_threads_setStatus = AsyncMock(side_effect=legacy)
    owners = adapter.__dict__.setdefault("_typing_refresh_owners", {})
    scope = adapter._typing_scope_key("C1", md)
    old_owner = owners[scope] = asyncio.create_task(asyncio.Event().wait())
    old_send = asyncio.create_task(adapter.send_typing("C1", md))
    pending_clear = successor = successor_send = None
    try:
        await asyncio.wait_for(entered.wait(), 5)
        clear_target = adapter._status_clear_target

        def target(*args):
            result = clear_target(*args)
            clearing.set()
            return result

        adapter._status_clear_target = target
        pending_clear = asyncio.create_task(adapter.stop_typing("C1", md))
        await asyncio.wait_for(clearing.wait(), 5)
        successor = owners[scope] = asyncio.create_task(asyncio.Event().wait())
        status_entry = adapter._status_entry

        def register(*args):
            result = status_entry(*args)
            registered.set()
            return result

        adapter._status_entry = register
        successor_send = asyncio.create_task(adapter.send_typing("C1", md))
        await asyncio.wait_for(registered.wait(), 5)
        release.set()
        await asyncio.wait_for(asyncio.gather(old_send, pending_clear, successor_send), 5)
        assert server_status[0] == "processing", completions
        assert ("legacy", "") not in completions
        assert ("native", "active") not in completions
        assert completions[-2:] == [("native", "processing"), ("legacy", "is thinking...")]
        if inflight_protocol == "native":
            assert completions.count(("legacy", "is thinking...")) == 1
        assert owners[scope] is successor
        assert adapter._active_status_threads[("T1", "C1", "1700.0001")]["owner"] is successor
    finally:
        release.set()
        tasks = [task for task in (old_owner, old_send, pending_clear, successor, successor_send)
                 if task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@pytest.mark.asyncio
async def test_successor_processing_follows_inflight_native_active(adapter):
    md = {"thread_id": "1700.0001", "slack_team_id": "T1"}
    entered, release = asyncio.Event(), asyncio.Event()
    statuses = []

    async def delayed_active(**kwargs):
        if kwargs["status"] == "active":
            entered.set()
            await release.wait()
        statuses.append(kwargs["status"])

    adapter._app.client.agents_sessions_setStatus = AsyncMock(side_effect=delayed_active)
    await adapter.send_typing("C1", md)
    clearing = asyncio.create_task(adapter.stop_typing("C1", md))
    successor = None
    try:
        await asyncio.wait_for(entered.wait(), 5)
        successor = asyncio.create_task(adapter.send_typing("C1", md))
        await asyncio.sleep(0)
        release.set()
        await asyncio.wait_for(asyncio.gather(clearing, successor), 5)
        assert statuses == ["processing", "active", "processing"]
        assert adapter._active_status_threads
    finally:
        release.set()
        await asyncio.gather(*(task for task in (clearing, successor) if task), return_exceptions=True)


@pytest.mark.asyncio
async def test_native_stop_does_not_clear_a_successor(adapter):
    from plugins.platforms.slack.adapter_native_stop import handle_agent_session_stopped

    md = {"thread_id": "1700.0001", "slack_team_id": "T1"}
    entered, release = asyncio.Event(), asyncio.Event()

    async def dispatch(*_args, **_kwargs):
        entered.set()
        await release.wait()

    native = adapter._app.client.agents_sessions_setStatus = AsyncMock()
    adapter._slash_channel_gated = lambda _: False
    adapter._handle_slash_command = AsyncMock(side_effect=dispatch)
    adapter._authorization_check = lambda *_args, **_kwargs: True
    adapter.send = AsyncMock()
    await adapter.send_typing("C1", md)
    stopping = asyncio.create_task(handle_agent_session_stopped(
        adapter, {"channel": "C1", "thread_ts": "1700.0001", "user": "U1"}, {"team_id": "T1"}))
    successor = None
    try:
        await asyncio.wait_for(entered.wait(), 5)
        successor = asyncio.create_task(asyncio.Event().wait())
        adapter._typing_refresh_owners = {adapter._typing_scope_key("C1", md): successor}
        await adapter.send_typing("C1", md)
        release.set()
        await asyncio.wait_for(stopping, 5)
        assert all(c.kwargs["status"] == "processing" for c in native.await_args_list)
        assert adapter._active_status_threads
    finally:
        release.set()
        if successor:
            successor.cancel()
        await asyncio.gather(*(task for task in (stopping, successor) if task), return_exceptions=True)


@pytest.mark.asyncio
async def test_workspace_roots_and_next_turn_have_independent_transitions(adapter):
    clients = {team: type(adapter._app.client)() for team in ("T1", "T2")}
    for client in clients.values():
        client.agents_sessions_setStatus = AsyncMock()
        client.assistant_threads_setStatus = AsyncMock()
    adapter._get_client = lambda _channel, team_id="": clients[team_id]
    for team in clients:
        md = {"thread_id": "1700.0001", "slack_team_id": team}
        await adapter.send_typing("C1", md)
        await adapter.send_typing("C1", md)
    await adapter.stop_typing("C1")  # Ambiguous across teams, never guess.
    assert all(client.agents_sessions_setStatus.await_count == 1 for client in clients.values())
    md = {"thread_id": "1700.0001", "slack_team_id": "T1"}
    await adapter.stop_typing("C1", md)
    await adapter.send_typing("C1", md)
    assert [c.kwargs["status"] for c in clients["T1"].agents_sessions_setStatus.await_args_list] == [
        "processing", "active", "processing"]
    assert [c.kwargs["status"] for c in clients["T2"].agents_sessions_setStatus.await_args_list] == ["processing"]


@pytest.mark.asyncio
@pytest.mark.parametrize("stopping_team", ["T1", "T2"])
async def test_rootless_clear_respects_explicit_workspace(adapter, stopping_team):
    native = adapter._app.client.agents_sessions_setStatus = AsyncMock()
    await adapter.send_typing("C1", {"thread_id": "1700.0001", "slack_team_id": "T1"})
    await adapter.stop_typing("C1", {"slack_team_id": stopping_team})
    assert [c.kwargs["status"] for c in native.await_args_list] == (
        ["processing", "active"] if stopping_team == "T1" else ["processing"])
    assert bool(adapter._active_status_threads) is (stopping_team != "T1")


@pytest.mark.asyncio
async def test_sdk_without_native_method_keeps_legacy_only(adapter, monkeypatch):
    from types import SimpleNamespace

    monkeypatch.setattr(_slack_mod, "_AGENT_SESSIONS_SUPPORTED", False)
    legacy = AsyncMock()
    adapter._get_client = lambda *_args, **_kwargs: SimpleNamespace(assistant_threads_setStatus=legacy)
    md = {"thread_id": "1700.0001", "slack_team_id": "T1"}
    await adapter.send_typing("C1", md)
    await adapter.send_typing("C1", md)
    await adapter.stop_typing("C1", md)
    assert [c.kwargs["status"] for c in legacy.await_args_list] == ["is thinking...", "is thinking...", ""]
    assert not adapter._active_status_threads
