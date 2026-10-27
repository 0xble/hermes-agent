"""Real SDK serialization/validation with a deterministic clock and transport."""

import asyncio
import copy
from itertools import pairwise
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

pytest.importorskip("slack_sdk")
pytest.importorskip("slack_bolt")

from aiohttp import ServerDisconnectedError
from slack_sdk.errors import SlackApiError
from slack_sdk.web.async_client import AsyncWebClient

from gateway.config import PlatformConfig
from plugins.platforms.slack import adapter as slack_adapter
from plugins.platforms.slack import outbound_budget as budget
from plugins.platforms.slack.adapter import SlackAdapter
from plugins.platforms.slack.task_cards import TaskCardCleanup


@pytest.fixture
def transport(monkeypatch):
    clock = SimpleNamespace(now=1000.0, sent=[], responses=[])

    async def sleep(delay):
        clock.now += delay
        await asyncio.sleep(0)

    async def request(self, *, http_verb, api_url, req_args):
        method = api_url.rsplit("/", 1)[-1]
        payload = req_args.get("json") or req_args.get("params") or req_args.get("data") or {}
        clock.sent.append((clock.now, method, payload, self.token))
        if clock.responses:
            return clock.responses.pop(0)
        return {"data": {"ok": True, "ts": "stream-1"}, "headers": {}, "status_code": 200}

    budget._BUDGETS.clear()
    monkeypatch.setattr(budget, "monotonic", lambda: clock.now)
    monkeypatch.setattr(budget, "sleep", sleep)
    monkeypatch.setattr(AsyncWebClient, "_request", request)
    return clock


def client(token="test-workspace-token"):
    return SlackAdapter._new_web_client(token, None)


@pytest.mark.asyncio
async def test_app_and_authenticated_clients_share_channel_across_threads(transport):
    app, authenticated = client(), client()
    await asyncio.gather(
        app.chat_postMessage(channel="C1", thread_ts="thread-1", text="reply"),
        authenticated.chat_postMessage(channel="C1", thread_ts="thread-2", text="reply"),
    )
    assert transport.sent[1][0] - transport.sent[0][0] >= 2
    assert transport.sent[1][0] - transport.sent[0][0] >= 4  # Workspace+method too.


@pytest.mark.asyncio
async def test_lazy_dependency_binding_keeps_shared_admission(transport, monkeypatch):
    from pm import extras

    def bind(extra, import_dependencies, namespace):
        for key, value in import_dependencies().items():
            monkeypatch.setitem(namespace, key, value)
        return True

    monkeypatch.setattr(slack_adapter, "SLACK_AVAILABLE", False)
    monkeypatch.setattr(extras, "ensure_and_bind", bind)
    assert slack_adapter.check_slack_requirements()
    app, authenticated = client(), client()
    await app.chat_postMessage(channel="C1", text="first")
    await authenticated.chat_postMessage(channel="C1", text="second")
    assert transport.sent[-1][0] - transport.sent[0][0] >= 4


@pytest.mark.asyncio
@pytest.mark.parametrize("base_url", ["http://127.0.0.1:8765/api/", "https://slack-compatible.example/api/"])
async def test_alternate_api_does_not_consume_or_inherit_slack_budget(transport, base_url):
    app = client()
    alternate = budget.BudgetedAsyncWebClient(token=app.token, base_url=base_url)
    await app.chat_postMessage(channel="C1", text="Slack reply")
    await alternate.chat_postMessage(channel="C1", text="alternate reply")
    await alternate.assistant_threads_setStatus(channel_id="C1", thread_ts="1", status="Working")
    transport.responses.append({"data": {"ok": False, "error": "ratelimited"},
                                "headers": {"Retry-After": "3600"}, "status_code": 429})
    with pytest.raises(SlackApiError):
        await alternate.chat_postMessage(channel="C1", text="alternate failure")
    await app.chat_postMessage(channel="C2", text="Slack reply after alternate failure")
    assert [entry[0] for entry in transport.sent] == [1000, 1000, 1000, 1000, 1004]


@pytest.mark.asyncio
async def test_independent_channels_workspaces_and_reads(transport):
    app, other = client(), client("other-workspace-token")
    await app.chat_postMessage(channel="C1", text="reply")
    await app.reactions_add(channel="C2", timestamp="1", name="eyes")
    await other.chat_postMessage(channel="C1", text="reply")
    await app.conversations_info(channel="C1")
    assert {entry[0] for entry in transport.sent} == {1000.0}
    # The same method still shares one budget across channels in a workspace.
    await app.chat_postMessage(channel="C2", text="reply")
    assert transport.sent[-1][0] == 1004.0


@pytest.mark.asyncio
async def test_all_current_write_families_share_channel(transport):
    app = client()
    await app.chat_postMessage(channel="C1", text="reply")
    await app.api_call("chat.startStream", json={"channel": "C1", "thread_ts": "thread-2"})
    await app.chat_appendStream(channel="C1", ts="stream-1", markdown_text="delta")
    await app.chat_stopStream(channel="C1", ts="stream-1")
    await app.reactions_add(channel="C1", timestamp="1", name="eyes")
    await app.assistant_threads_setStatus(channel_id="C1", thread_ts="thread-3", status="")
    await app.assistant_threads_setTitle(channel_id="C1", thread_ts="thread-4", title="Task")
    await app.files_completeUploadExternal(files=[{"id": "F1"}], channel_id="C1")
    times = [entry[0] for entry in transport.sent]
    assert all(b - a >= 2 for a, b in pairwise(times))


@pytest.mark.asyncio
@pytest.mark.parametrize("method, gap, floor", [("chat.startStream", 4, 20), ("chat.appendStream", .8, 100)])
async def test_workspace_method_budget_stays_at_or_below_three_quarters(transport, method, gap, floor):
    app, authenticated = client(), client()
    for index in range(100):
        await (app if index % 2 else authenticated).api_call(
            method, json={"channel": f"C{index}", "markdown_text": "reply"})
    times = [entry[0] for entry in transport.sent]
    assert all(b - a >= gap - 1e-9 for a, b in pairwise(times))
    count = sum(timestamp < times[0] + 60 - 1e-9 for timestamp in times)
    assert count <= floor * .75


@pytest.mark.asyncio
async def test_aggregate_channel_budget_counts_all_threads_and_producers(transport):
    app, authenticated = client(), client()
    methods = ["chat.postMessage", "chat.startStream", "chat.appendStream", "chat.stopStream",
               "reactions.add", "assistant.threads.setTitle", "files.completeUploadExternal"]
    for index in range(100):
        await (app if index % 2 else authenticated).api_call(
            methods[index % len(methods)],
            json={"channel": "C1", "thread_ts": str(index), "markdown_text": "reply"})
    times = [entry[0] for entry in transport.sent]
    assert all(b - a >= 2 for a, b in pairwise(times))
    count = sum(timestamp < times[0] + 60 for timestamp in times)
    assert count <= 60 * .75



@pytest.mark.asyncio
async def test_retry_after_is_workspace_method_scoped_and_widening_expires(transport):
    app, authenticated = client(), client()
    transport.responses.append({"data": {"ok": False, "error": "ratelimited"},
                                "headers": {"Retry-After": "10"}, "status_code": 429})
    with pytest.raises(SlackApiError):
        await app.chat_postMessage(channel="C1", text="reply")
    await app.reactions_add(channel="C2", timestamp="1", name="eyes")
    await client("other-workspace-token").chat_postMessage(channel="C1", text="reply")
    assert transport.now == 1000
    await authenticated.chat_postMessage(channel="C2", text="reply")
    assert transport.sent[-1][0] == 1010
    await app.chat_postMessage(channel="C3", text="reply")
    assert transport.sent[-1][0] == 1018  # Temporarily doubled method spacing.
    transport.now = 1100
    await app.chat_postMessage(channel="C1", text="reply")
    await app.chat_postMessage(channel="C2", text="reply")
    assert transport.sent[-1][0] == 1104  # Original rate returns.


@pytest.mark.asyncio
async def test_cosmetic_ticks_are_shed_but_status_clear_is_admitted(transport):
    app = client()
    await app.chat_postMessage(channel="C1", text="reply")
    for method, payload in [
        ("chat.appendStream", {"channel": "C1", "ts": "1", "chunks": [{"type": "task_update", "id": "1"}]}),
        ("assistant.threads.setStatus", {"channel_id": "C1", "thread_ts": "1", "status": "Working"}),
    ]:
        with pytest.raises(SlackApiError) as error:
            await app.api_call(method, json=payload)
        assert error.value.response.get("error") == "ratelimited"
        assert float(error.value.response.headers["Retry-After"]) == 2
    assert len(transport.sent) == 1
    await app.assistant_threads_setStatus(channel_id="C1", thread_ts="1", status="")
    assert transport.sent[-1][0] == 1002


@pytest.mark.asyncio
async def test_adapter_progress_retries_latest_projection_without_raw_fallback(transport, monkeypatch):
    app = client()
    adapter = SlackAdapter(PlatformConfig(extra={"native_task_cards": True}))
    adapter._app = object()
    monkeypatch.setattr(adapter, "_get_client", Mock(return_value=app))
    task = {"id": "call-1", "title": "Checking", "status": "in_progress"}
    first = await adapter.send_native_task_card_progress("C1", [task], reply_to="thread-1")
    assert first.retryable and not first.held
    assert first.retry_after == 2
    transport.now += first.retry_after
    task["status"] = "complete"
    second = await adapter.send_native_task_card_progress("C1", [task], reply_to="thread-1")
    assert second.success
    assert [entry[1] for entry in transport.sent] == ["chat.startStream", "chat.appendStream"]
    assert transport.sent[-1][2]["chunks"][-1]["status"] == "complete"


@pytest.mark.asyncio
async def test_adapter_reply_reaction_status_and_progress_share_transport(transport, monkeypatch):
    app = client()
    adapter = SlackAdapter(PlatformConfig(extra={"native_task_cards": True}))
    adapter._app = object()
    monkeypatch.setattr(adapter, "_get_client", Mock(return_value=app))
    assert (await adapter.send("C1", "Answer")).success
    await adapter.send_typing("C1", {"thread_id": "thread-2"})
    assert len(transport.sent) == 1  # Status tick is shed, not queued.
    assert await adapter._add_reaction("C1", "1", "eyes")
    task = {"id": "call-1", "title": "Checking", "status": "in_progress"}
    result = await adapter.send_native_task_card_progress("C1", [task], reply_to="thread-3")
    assert result.retryable  # Start was admitted, cosmetic append remains recoverable.
    await adapter.stop_typing("C1", {"thread_id": "thread-2"})
    assert [entry[1] for entry in transport.sent] == [
        "chat.postMessage", "reactions.add", "chat.startStream", "assistant.threads.setStatus"]
    assert all(b[0] - a[0] >= 2 for a, b in pairwise(transport.sent))
    assert transport.sent[-1][2]["status"] == ""


@pytest.mark.asyncio
async def test_copied_start_has_no_hidden_sdk_retries_and_keeps_budget(transport, monkeypatch):
    app = client()
    attempts = []

    class BrokenSession:
        closed = False

        def request(self, *args, **kwargs):
            attempts.append(kwargs)
            raise ServerDisconnectedError("response lost after creation")

    app.session = BrokenSession()
    # Undo only the fake transport, exercising the SDK's real retry loop.
    monkeypatch.undo()
    with pytest.raises(ServerDisconnectedError):
        # The ledger-owned start path the adapter uses; nothing is recorded for a failed start.
        await TaskCardCleanup(SimpleNamespace(_team_clients={})).start_stream(
            app, SimpleNamespace(channel="C1", thread_ts="1", stream_ts="", message_timestamps=[], team_id=""), {})
    assert len(attempts) == 1
    assert app.retry_handlers == []
    assert copy.copy(app)._outbound_budget is app._outbound_budget


@pytest.mark.asyncio
async def test_cancelled_wait_does_not_reserve_slot_or_block_other_channels(transport, monkeypatch):
    app = client()
    await app.chat_postMessage(channel="C1", text="first")
    waiting, release = asyncio.Event(), asyncio.Event()

    async def sleep(delay):
        waiting.set()
        await release.wait()

    monkeypatch.setattr(budget, "sleep", sleep)
    queued = asyncio.create_task(app.chat_postMessage(channel="C1", text="cancel"))
    await asyncio.wait_for(waiting.wait(), timeout=2)
    await app.reactions_add(channel="C2", timestamp="1", name="eyes")
    queued.cancel()
    with pytest.raises(asyncio.CancelledError):
        await queued
    transport.now += 4
    await app.chat_postMessage(channel="C1", text="after cancellation")
    assert [entry[2].get("text") for entry in transport.sent] == ["first", None, "after cancellation"]


@pytest.mark.asyncio
async def test_long_server_cooldown_returns_retry_after_without_parking_output(transport):
    app = client()
    transport.responses.append({"data": {"ok": False, "error": "ratelimited"},
                                "headers": {"Retry-After": "3600"}, "status_code": 429})
    with pytest.raises(SlackApiError):
        await app.chat_postMessage(channel="C1", text="first")
    with pytest.raises(SlackApiError) as error:
        await app.chat_postMessage(channel="C2", text="next")
    assert float(error.value.response.headers["Retry-After"]) == 3600
    assert transport.now == 1000
    assert len(transport.sent) == 1
    await app.reactions_add(channel="C2", timestamp="1", name="eyes")
    assert transport.sent[-1][0] == 1000
    assert transport.sent[-1][1] == "reactions.add"
