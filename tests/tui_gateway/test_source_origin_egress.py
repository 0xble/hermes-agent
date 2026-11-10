"""Native turn evidence must never enter a JSON history or process frame."""

import asyncio
import io
import json
import logging
import queue
import threading
from types import SimpleNamespace

import pytest

from agent.turn_context import _stage_turn_user_message
from tui_gateway import server
from tui_gateway.compute_host import ComputeHost
from tui_gateway.event_publisher import WsPublisherTransport
from tui_gateway.event_replay import _freeze_params
from tui_gateway.host_supervisor import HostSupervisor
from tui_gateway.transport import FanoutTransport, StdioTransport, serialize_frame
from tui_gateway.ws import WSTransport


def _stamped_history(platform="tui", kind="auto_continue", metadata=None):
    message, _ = _stage_turn_user_message(
        SimpleNamespace(platform=platform), "Continue the fictional task.", None, None,
        None, kind, metadata or {"other": "preserved"})
    session = {"history": [], "history_version": 0, "history_lock": threading.Lock(),
               "cwd": "/fictional", "source": "tui"}
    assert server._commit_turn_history(session, {"messages": [message]}, [], 0) is None
    return session


@pytest.mark.parametrize(("platform", "kind"), [
    ("tui", "auto_continue"), ("tui", "async_delegation_complete"),
    ("tui", "supervisor_checkin"), ("tui", "internal_notification"),
    *[(producer, None) for producer in ("oneshot", "subagent", "relay", "goal")],
])
@pytest.mark.parametrize("boundary", ["history", "turn.start"])
def test_stamped_turn_survives_history_and_next_compute_turn(platform, kind, boundary):
    session = _stamped_history(platform, kind)
    if boundary == "history":
        messages = server._history_to_messages(session["history"])
        wire = serialize_frame({"jsonrpc": "2.0", "id": "history", "result": {"messages": messages}},
                               "fictional", logging.getLogger(__name__))
        decoded = json.loads(wire)
        assert "error" not in decoded
        assert decoded["result"]["messages"][0]["display_metadata"]["other"] == "preserved"
    else:
        frame = server._compute_host_turn_frame("next", "fictional", session, "Next instruction")
        stream = io.StringIO()
        supervisor = HostSupervisor.__new__(HostSupervisor)
        supervisor._lock = threading.RLock()
        supervisor._proc = SimpleNamespace(poll=lambda: None, stdin=stream)
        supervisor._send_frame(frame)
        decoded = json.loads(stream.getvalue())
        assert decoded["type"] == "turn.start"
        assert decoded["history"][0]["content"] == "Continue the fictional task."


@pytest.mark.parametrize("boundary", [
    "raw-history", "rpc-stdio", "rpc-fanout", "websocket", "websocket-batch",
    "event-replay", "event-publisher", "compute-host-output", "worker-journal", "durable-result",
])
def test_native_human_and_generated_turns_survive_every_history_json_egress(boundary):
    """Exercise the JSON sinks found across live history, transport, replay and export.

    WebSocket single/batch, stdio and fanout share serialize_frame. Compute-host output,
    event replay, the publisher and worker persistence each have their own encoder.
    """
    from agent.source_origin import NativeOrigin, ORIGIN_KEY, retention_user_text

    text = "Continue the fictional task."
    receipt = NativeOrigin.person(text, platform="telegram", principal="fictional", event="event")
    human = _stamped_history("telegram", None, {ORIGIN_KEY: receipt, "other": "preserved"})
    generated = _stamped_history()
    history = human["history"] + generated["history"]
    event = {"type": "history", "session_id": "fictional", "messages": history}
    frame = {"jsonrpc": "2.0", "id": "fixture", "result": event}
    if boundary in {"rpc-stdio", "rpc-fanout"}:
        output = io.StringIO()
        transport = StdioTransport(lambda: output, threading.Lock())
        if boundary == "rpc-fanout":
            transport = FanoutTransport(transport)
        assert transport.write(frame)
        if boundary == "rpc-fanout":
            assert transport.flush(timeout=5)
        decoded = json.loads(output.getvalue())
        assert "error" not in decoded and decoded["result"]["messages"] == history
    elif boundary in {"websocket", "websocket-batch"}:
        sent = []
        async def send_text(line):
            sent.append(json.loads(line))
        async def send():
            transport = WSTransport(SimpleNamespace(send_text=send_text), asyncio.get_running_loop())
            if boundary == "websocket-batch":
                assert await transport.write_many_async([frame])
            else:
                assert await transport.write_async(frame)
        asyncio.run(send())
        assert len(sent) == 1 and "error" not in sent[0]
        assert sent[0]["result"]["messages"] == history
    elif boundary == "event-replay":
        assert json.loads(_freeze_params(event))["messages"] == history
    elif boundary == "event-publisher":
        publisher = WsPublisherTransport.__new__(WsPublisherTransport)
        publisher._dead, publisher._ws, publisher._worker = False, object(), object()
        publisher._q = queue.Queue()
        assert publisher.write(event)
        assert json.loads(publisher._q.get_nowait())["messages"] == history
    elif boundary == "compute-host-output":
        host = ComputeHost.__new__(ComputeHost)
        host._write_lock, host._stdout = threading.Lock(), io.StringIO()
        host.emit({"type": "rpc", "message": event})
        assert json.loads(host._stdout.getvalue())["message"]["messages"] == history
    elif boundary == "worker-journal":
        from agent.runtime_session_store import _worker_json_default
        assert json.loads(json.dumps(event, allow_nan=False, default=_worker_json_default))["messages"] == history
    elif boundary == "durable-result":
        from gateway.session_results import _redacted
        assert json.loads(json.dumps(_redacted(event)))["messages"] == history
    else:
        assert json.loads(json.dumps(history)) == history
    assert retention_user_text(text, human["history"]) == text
    assert retention_user_text(text, json.loads(json.dumps(human["history"]))) is None
    assert retention_user_text(text, generated["history"]) is None
