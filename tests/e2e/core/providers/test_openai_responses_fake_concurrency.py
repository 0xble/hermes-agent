"""Concurrency contracts for the shared, SDK-backed Responses fake."""

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from urllib.request import Request, urlopen

import pytest
from pydantic import ConfigDict

from tests.fakes.providers import openai_responses as fake


@pytest.mark.parametrize("renderers", [("turn", "turn"), ("turn", "soft_fail"), ("soft_fail", "soft_fail")])
def test_cold_sdk_schema_rebuild_is_serialized(monkeypatch, renderers):
    turn = fake.Turn([
        fake.Reasoning("opaque", "summary"), fake.Message("answer"),
        fake.FunctionCall("noop", {"value": 1}),
    ])
    # Isolate the forced interleaving to the fresh event class below.
    fake.turn_events(turn)
    fake.soft_fail_events(fake.SoftFail())
    # Fresh deferred models keep this test cold even after other tests warmed the SDK.
    # Rendezvous at the real model_rebuild check/delete window. If both renderers
    # enter it, hold the first deletion until the second has tried the same deletion.
    # With serialization only one arrives, so the rendezvous expires harmlessly. Both renderers
    # are released together by ``start``, so an unserialized second contender reaches the window
    # within milliseconds; 1 s keeps the serialized (passing) case from idling the full wait.
    contenders = threading.Barrier(2)
    deletions = threading.Barrier(2)

    class RacingMeta(type(fake.ResponseCreatedEvent)):
        def __delattr__(cls, name):
            if name != "__pydantic_core_schema__":
                return super().__delattr__(name)
            try:
                contenders.wait(timeout=1)
            except threading.BrokenBarrierError:
                return super().__delattr__(name)
            try:
                return super().__delattr__(name)
            finally:
                deletions.wait(timeout=5)

    class ColdCreatedEvent(fake.ResponseCreatedEvent, metaclass=RacingMeta):
        model_config = ConfigDict(defer_build=True)

    assert not ColdCreatedEvent.__pydantic_complete__
    monkeypatch.setattr(fake, "ResponseCreatedEvent", ColdCreatedEvent)
    start = threading.Barrier(2)

    def render(kind):
        start.wait(timeout=10)
        if kind == "turn":
            return fake.turn_events(turn)
        return fake.soft_fail_events(fake.SoftFail())

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(render, kind) for kind in renderers]
        results = [future.result(timeout=20) for future in futures]

    for kind, events in zip(renderers, results):
        assert events[0]["type"] == "response.created"
        assert [event["sequence_number"] for event in events] == list(range(1, len(events) + 1))
        if kind == "turn":
            completed = events[-1]
            assert completed["type"] == "response.completed"
            assert completed["response"]["output"][1]["content"][0]["text"] == "answer"
            assert completed["response"]["usage"]["total_tokens"] == 120
        else:
            assert events[-1]["type"] == "response.failed"
            assert events[-1]["response"]["error"]["code"] == "invalid_encrypted_content"


def test_stalled_stream_does_not_block_other_requests_or_servers(monkeypatch):
    streaming = threading.Event()
    release = threading.Event()
    handler_for = fake._handler_for

    def paused_handler_for(server):
        class Handler(handler_for(server)):
            def end_headers(self):
                super().end_headers()
                if self.headers.get("X-Pause-Stream"):
                    streaming.set()
                    assert release.wait(timeout=20), "stalled stream was not released"

        return Handler

    monkeypatch.setattr(fake, "_handler_for", paused_handler_for)

    def request(server, *, main=True, pause=False):
        body = {"model": fake.MODEL_ID, "input": "hi", "stream": True}
        if main:
            body["tools"] = [{
                "type": "function", "name": "noop", "strict": False, "parameters": {"type": "object"},
            }]
        headers = {"Content-Type": "application/json"}
        if pause:
            headers["X-Pause-Stream"] = "1"
        req = Request(server.base_url + "/responses", data=json.dumps(body).encode(), headers=headers)
        with urlopen(req, timeout=10) as response:
            events = [json.loads(line[6:]) for line in response if line.startswith(b"data: ")]
        assert events[-1]["type"] == "response.completed"
        return events[-1]["response"]["output"][0]["content"][0]["text"]

    with fake.FakeResponsesServer([
        fake.Turn([fake.Message("A1")]), fake.Turn([fake.Message("A2")]),
    ]) as first, fake.FakeResponsesServer([fake.Turn([fake.Message("B1")])]) as second:
        with ThreadPoolExecutor(max_workers=1) as pool:
            stalled = pool.submit(request, first, pause=True)
            try:
                assert streaming.wait(timeout=10), "first request never reached streaming"
                assert request(first, main=False) == "Fake title"
                assert request(second) == "B1"
            finally:
                release.set()
            assert stalled.result(timeout=10) == "A1"
        assert request(first) == "A2"
        assert first.invalid_requests() == second.invalid_requests() == []
