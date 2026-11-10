"""Native turn evidence must never enter a JSON history or process frame."""

import asyncio
import io
import json
import logging
import queue
import threading
from functools import partial
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


def _stdio_egress(event, *, fanout=False):
    output = io.StringIO()
    transport = StdioTransport(lambda: output, threading.Lock())
    if fanout:
        transport = FanoutTransport(transport)
    assert transport.write(event)
    if fanout:
        assert transport.flush(timeout=5)
    return json.loads(output.getvalue())


def _websocket_egress(event, *, batch=False):
    sent = []
    async def send_text(line):
        sent.append(json.loads(line))
    async def send():
        transport = WSTransport(SimpleNamespace(send_text=send_text), asyncio.get_running_loop())
        if batch:
            assert await transport.write_many_async([event])
        else:
            assert await transport.write_async(event)
    asyncio.run(send())
    assert len(sent) == 1
    return sent[0]


def _publisher_egress(event):
    publisher = WsPublisherTransport.__new__(WsPublisherTransport)
    publisher._dead, publisher._ws, publisher._worker = False, object(), object()
    publisher._q = queue.Queue()
    assert publisher.write(event)
    return json.loads(publisher._q.get_nowait())


def _compute_egress(event):
    host = ComputeHost.__new__(ComputeHost)
    host._write_lock, host._stdout = threading.Lock(), io.StringIO()
    host.emit({"type": "rpc", "message": event})
    return json.loads(host._stdout.getvalue())["message"]


def _worker_egress(event):
    from agent.runtime_session_store import _worker_json_default
    return json.loads(json.dumps(event, allow_nan=False, default=_worker_json_default))


def _durable_egress(event):
    from gateway.session_results import _redacted
    return json.loads(json.dumps(_redacted(event)))


_HISTORY_EGRESS = {
    "raw-history": lambda event: json.loads(json.dumps(event)),
    "rpc-stdio": _stdio_egress,
    "rpc-fanout": partial(_stdio_egress, fanout=True),
    "websocket": _websocket_egress,
    "websocket-batch": partial(_websocket_egress, batch=True),
    "event-replay": lambda event: json.loads(_freeze_params(event)),
    "event-publisher": _publisher_egress,
    "compute-host-output": _compute_egress,
    "worker-journal": _worker_egress,
    "durable-result": _durable_egress,
}


@pytest.mark.parametrize("boundary", _HISTORY_EGRESS)
def test_native_human_and_generated_turns_survive_every_history_json_egress(boundary):
    """Exercise each JSON sink found across live history, transport, replay and persistence.

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
    decoded = _HISTORY_EGRESS[boundary](event)
    assert "error" not in decoded
    if boundary in {"raw-history", "worker-journal"}:
        assert decoded["messages"] == history
    else:
        from agent.source_origin import public_origin_projection
        assert decoded["messages"] == public_origin_projection(history)
        assert ORIGIN_KEY not in json.dumps(decoded)
    assert retention_user_text(text, human["history"]) == text
    assert retention_user_text(text, json.loads(json.dumps(human["history"]))) is None
    assert retention_user_text(text, generated["history"]) is None
