"""An overloaded auxiliary primary walks the configured fallback chain over real HTTP."""

import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading

import pytest
import hermes_yaml as yaml


OVERLOAD_MESSAGE = "Our servers are currently overloaded. Please try again later."


@pytest.fixture
def lanes(monkeypatch):
    """One local server hosting three OpenAI-compatible lanes; each lane overloads or answers by name."""
    from agent.auxiliary_client import _reset_aux_unhealthy_cache, shutdown_cached_clients

    shutdown_cached_clients()
    _reset_aux_unhealthy_cache()
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    overloaded = set()
    attempts = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            lane = self.path.split("/")[1]
            attempts.append((lane, payload["model"]))
            if lane in overloaded:
                # A streaming proxy relays an upstream overload as an in-stream error event, so
                # the client sees no HTTP status: the wording is the only signal.
                status, content_type = 200, "text/event-stream"
                error = {"error": {"message": OVERLOAD_MESSAGE, "type": "overloaded_error"}}
                body = f"data: {json.dumps(error)}\n\n".encode()
            else:
                response = {
                    "id": "chatcmpl-lane",
                    "created": 1,
                    "model": payload["model"],
                    "object": "chat.completion",
                    "choices": [{
                        "index": 0,
                        "message": {"role": "assistant", "content": f"answered by {lane}"},
                        "finish_reason": "stop",
                    }],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                }
                status = 200
                if payload.get("stream"):
                    response["object"] = "chat.completion.chunk"
                    response["choices"][0]["delta"] = response["choices"][0].pop("message")
                    body = f"data: {json.dumps(response)}\n\ndata: [DONE]\n\n".encode()
                    content_type = "text/event-stream"
                else:
                    body = json.dumps(response).encode()
                    content_type = "application/json"
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", overloaded, attempts
    finally:
        shutdown_cached_clients()
        _reset_aux_unhealthy_cache()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.parametrize("async_mode", [False, True])
def test_overloaded_primary_walks_configured_chain_in_order(tmp_path, monkeypatch, lanes, async_mode):
    """Stops an overloaded auxiliary primary from failing the task instead of trying each configured
    fallback in order and returning the first one that answers."""
    from agent.auxiliary_client import async_call_llm, call_llm

    base, overloaded, attempts = lanes
    overloaded.update({"primary", "first"})
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    config = {
        "model": {"provider": "custom", "default": "primary-model", "base_url": f"{base}/primary/v1"},
        "auxiliary": {
            # The lanes are stream-only; same-provider retries are off so each lane is reached once.
            "stream_only_base_urls": ["127.0.0.1"],
            "transient_retries": 0,
            "session_search": {
                "provider": "custom",
                "model": "primary-model",
                "base_url": f"{base}/primary/v1",
                "api_key": "primary-key",
                "fallback_chain": [
                    {"provider": "custom", "model": "first-model", "base_url": f"{base}/first/v1",
                     "api_key": "first-key"},
                    {"provider": "custom", "model": "second-model", "base_url": f"{base}/second/v1",
                     "api_key": "second-key"},
                    {"provider": "custom", "model": "third-model", "base_url": f"{base}/third/v1",
                     "api_key": "third-key"},
                ],
            }
        },
    }
    (tmp_path / "config.yaml").write_text(yaml.safe_dump(config), encoding="utf-8")
    kwargs = {
        "task": "session_search",
        "messages": [{"role": "user", "content": "Summarize the results."}],
        "timeout": 5,
    }

    response = asyncio.run(async_call_llm(**kwargs)) if async_mode else call_llm(**kwargs)

    assert response.choices[0].message.content == "answered by second"
    # The healthy third candidate is never reached: the walk stops at the first one that answers.
    assert attempts == [
        ("primary", "primary-model"), ("first", "first-model"), ("second", "second-model"),
    ]
