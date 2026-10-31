"""Native progress survives transient Slack failures without a raw-text substitute."""

import asyncio
import queue
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from gateway.config import PlatformConfig
from gateway.run_turn_runner import TurnRunner
from plugins.platforms.slack.adapter import SlackAdapter


class SlackError(Exception):
    def __init__(self, code, *, headers=None):
        super().__init__(code)
        self.response = Response(error=code)
        self.response.headers = headers or {}


class Response(dict):
    headers: dict[str, str]


def make_runner(monkeypatch, *, failure_method, error):
    adapter = SlackAdapter(PlatformConfig(extra={"native_task_cards": True}))
    failed = False

    async def api_call(method, *, json):
        nonlocal failed
        if method == failure_method and not failed:
            failed = True
            raise error
        return {"ts": "stream-1", "ok": True}

    client = SimpleNamespace(api_call=AsyncMock(side_effect=api_call))
    adapter._app = object()
    monkeypatch.setattr(adapter, "_get_client", Mock(return_value=client))
    adapter.send = AsyncMock()
    adapter.edit_message = AsyncMock()
    ctx = SimpleNamespace(
        source=SimpleNamespace(chat_id="D1"), _progress_reply_to="100.1",
        _progress_metadata={"thread_id": "100.1"}, _cleanup_progress=False,
        tool_progress_enabled=False,
    )
    runner = TurnRunner(None, ctx)
    state = runner._TaskCardState(adapter=adapter)
    state.apply_event({"type": "tool.started", "tool_call_id": "call-1",
                       "tool_name": "terminal", "preview": "sed -n '1,400p' findings.md"})
    return runner, state, client


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_method", ["chat.appendStream"])
async def test_timeout_preserves_native_lane_and_recovers(monkeypatch, caplog, failure_method):
    runner, state, client = make_runner(
        monkeypatch, failure_method=failure_method, error=TimeoutError(),
    )
    await runner._task_card_publish(state)
    assert not state.native_failed
    state.adapter.send.assert_not_awaited()
    assert "TimeoutError" in caplog.text
    calls_after_failure = client.api_call.await_count
    state.apply_event({"type": "tool.completed", "tool_call_id": "call-1",
                       "tool_name": "terminal", "is_error": False})
    await runner._task_card_publish(state)
    assert client.api_call.await_count == calls_after_failure  # Cooldown coalesces events.
    state.native_retry_at = 0
    await runner._task_card_publish(state)
    calls = client.api_call.await_args_list
    starts = [c for c in calls if c.args[0] == "chat.startStream"]
    assert len(starts) == (2 if failure_method == "chat.startStream" else 1)
    assert calls[-1].args == ("chat.appendStream",)
    assert calls[-1].kwargs["json"]["ts"] == "stream-1"
    task = calls[-1].kwargs["json"]["chunks"][-1]
    assert task["id"] == "call-1"
    assert task["status"] == "complete"
    assert "sed" not in task["title"]
    state.adapter.send.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_method", ["chat.appendStream", "chat.startStream"])
async def test_rate_limit_honors_retry_after(monkeypatch, failure_method):
    runner, state, client = make_runner(
        monkeypatch, failure_method=failure_method,
        error=SlackError("ratelimited", headers={"Retry-After": "120"}),
    )
    before = asyncio.get_running_loop().time()
    await runner._task_card_publish(state)
    assert state.native_retry_at >= before + 120
    calls_after_failure = client.api_call.await_count
    await runner._task_card_publish(state)
    assert client.api_call.await_count == calls_after_failure
    assert not state.native_failed
    state.adapter.send.assert_not_awaited()
    state.native_retry_at = 0
    await runner._task_card_publish(state)
    assert client.api_call.await_args.args == ("chat.appendStream",)
    assert not state.native_failed


@pytest.mark.asyncio
@pytest.mark.parametrize("error", [TimeoutError(), RuntimeError("Slack startStream returned no stream timestamp")])
async def test_ambiguous_start_is_not_retried_or_replaced(monkeypatch, caplog, error):
    runner, state, client = make_runner(
        monkeypatch, failure_method="chat.startStream", error=error,
    )
    await runner._task_card_publish(state)
    await runner._task_card_publish(state)
    client.api_call.assert_awaited_once()
    state.adapter.send.assert_not_awaited()
    assert state.publication_suppressed
    assert "ambiguous" in caplog.text


@pytest.mark.asyncio
async def test_start_disables_sdk_retries_without_mutating_shared_client(monkeypatch):
    runner, state, _ = make_runner(monkeypatch, failure_method="unused", error=TimeoutError())
    attempts = []

    class Client:
        retry_handlers = [object()]

        async def api_call(self, method, *, json):
            attempts.append((method, self.retry_handlers))
            raise ConnectionError("response lost")

    client = Client()
    monkeypatch.setattr(state.adapter, "_get_client", lambda *a, **kw: client)
    await runner._task_card_publish(state)
    assert attempts == [("chat.startStream", [])]
    assert client.retry_handlers
    assert state.publication_suppressed
    state.adapter.send.assert_not_awaited()


@pytest.mark.asyncio
async def test_permanent_rejection_uses_one_clean_fallback(monkeypatch, caplog):
    runner, state, client = make_runner(
        monkeypatch, failure_method="chat.appendStream", error=SlackError("invalid_blocks"),
    )
    state.adapter.send.return_value = SimpleNamespace(success=True, message_id="fallback-1")
    state.adapter.edit_message.return_value = SimpleNamespace(success=True)
    await runner._task_card_publish(state)
    assert state.native_failed
    assert "invalid_blocks" in caplog.text
    state.apply_event({"type": "tool.completed", "tool_call_id": "call-1", "tool_name": "terminal"})
    await runner._task_card_publish(state)
    state.adapter.send.assert_awaited_once()
    state.adapter.edit_message.assert_awaited_once()
    text = state.adapter.edit_message.await_args.kwargs["content"]
    assert "sed" not in text and "terminal -" not in text
    assert "complete" in text
    assert client.api_call.await_count == 2


@pytest.mark.parametrize("tool_name,preview", [
    ("terminal", "sed -n '1,400p' findings.md"),
    ("execute_code", "from hermes_tools import web_search\nimport re"),
])
@pytest.mark.parametrize("event_type", ["tool.started", "tool.completed"])
def test_executable_previews_never_become_progress_titles(monkeypatch, tool_name, preview, event_type):
    runner, state, _ = make_runner(monkeypatch, failure_method="unused", error=TimeoutError())
    state.apply_event({"type": event_type, "tool_call_id": "script", "tool_name": tool_name,
                       "preview": preview})
    title = state.tasks["script"]["title"]
    assert preview.splitlines()[0] not in title
    assert tool_name not in title
    assert title


@pytest.mark.asyncio
async def test_idle_consumer_retries_latest_projection_without_new_events(monkeypatch):
    runner, state, client = make_runner(
        monkeypatch, failure_method="chat.appendStream", error=TimeoutError(),
    )
    # A shorter test clock interval, not a different retry path.
    original_publish = runner._task_card_publish

    async def publish(st):
        await original_publish(st)
        if st.native_retry_at:
            st.native_retry_at = asyncio.get_running_loop().time() + 0.01

    monkeypatch.setattr(runner, "_task_card_publish", publish)
    runner._ctx.progress_queue = queue.Queue()
    runner._ctx.progress_queue.put({"type": "tool.started", "tool_call_id": "call-1",
                                    "tool_name": "terminal", "preview": "pwd"})
    runner._ctx._run_still_current = lambda: client.api_call.await_count < 3
    runner._ctx.agent_holder = [None]
    task = asyncio.create_task(runner._send_native_task_card_progress(state.adapter))
    await asyncio.wait_for(task, timeout=2)
    # Turn end seals and then deletes the recovered card (ephemeral task cards, the default).
    assert [c.args[0] for c in client.api_call.await_args_list] == [
        "chat.startStream", "chat.appendStream", "chat.appendStream", "chat.stopStream", "chat.delete",
    ]
    assert client.api_call.await_args.kwargs["json"] == {"channel": "D1", "ts": "stream-1"}
    state.adapter.send.assert_not_awaited()
