"""Loopback OpenAI-compatible stub and real-request-path child for shared cooldown tests.

The child drives ``AIAgent.run_conversation`` so requests traverse the production
wrappers (``_interruptible_streaming_api_call`` / ``_interruptible_api_call`` /
``direct_api_call``) instead of calling the OpenAI client directly.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
PRIMARY_MODEL = "primary-model"
FALLBACK_MODEL = "fallback-model"


class StubProvider:
    """Serve the primary (429 with Retry-After unless available) and the fallback (200)."""

    def __init__(self) -> None:
        self.primary_available = False
        self.retry_after: str | None = "7200"
        self.requests: list[dict] = []
        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *args):  # noqa: A002 - BaseHTTPRequestHandler signature
                pass

            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"data":[]}')

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))) or b"{}")
                model = body.get("model")
                stream = bool(body.get("stream"))
                ok = model != PRIMARY_MODEL or stub.primary_available
                stub.requests.append({"model": model, "status": 200 if ok else 429, "stream": stream})
                if not ok:
                    payload = json.dumps({"error": {"message": "rate_limit_error: retry later", "type": "rate_limit_error"}}).encode()
                    self.send_response(429)
                    if stub.retry_after:
                        self.send_header("Retry-After", stub.retry_after)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
                    return
                text = f"OK from {model}"
                if stream:
                    self.send_response(200)
                    self.send_header("Content-Type", "text/event-stream")
                    self.end_headers()
                    base = {"id": "local", "object": "chat.completion.chunk", "created": 0, "model": model}
                    chunks = [
                        {**base, "choices": [{"index": 0, "delta": {"role": "assistant", "content": text}, "finish_reason": None}]},
                        {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                         "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}},
                    ]
                    for chunk in chunks:
                        self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()
                    return
                payload = json.dumps({
                    "id": "local", "object": "chat.completion", "created": 0, "model": model,
                    "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                }).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_port}/v1"

    def primary_requests(self) -> list[dict]:
        return [r for r in self.requests if r["model"] == PRIMARY_MODEL]

    def __enter__(self) -> "StubProvider":
        self.thread.start()
        return self

    def __exit__(self, *_exc) -> None:
        self.server.shutdown()
        self.server.server_close()


# One user turn through AIAgent.run_conversation. argv: url, mode. Modes select the production
# wrapper: "stream" (_interruptible_streaming_api_call), "nonstream" (_interruptible_api_call),
# "direct" (platform=cron, which routes chat_completions through direct_api_call).
# Prints a JSON line with the final model, response and every status/notice line emitted.
CHILD = r'''
import json, sys
from run_agent import AIAgent
url, mode = sys.argv[1:3]
notices = []
fallback = [{"provider": "custom", "model": "fallback-model", "base_url": url, "api_key": "fixture", "api_mode": "chat_completions"}]
agent = AIAgent(
    api_key="fixture", base_url=url, provider="custom", model="primary-model",
    api_mode="chat_completions", quiet_mode=True, skip_context_files=True, skip_memory=True,
    enabled_toolsets=[], fallback_model=fallback, max_iterations=3,
    platform="cron" if mode == "direct" else None,
    status_callback=lambda kind, message: notices.append(str(message)),
)
if mode in ("nonstream", "direct"):
    agent._disable_streaming = True
if mode == "direct":
    from agent.chat_completion_helpers import should_use_direct_api_call
    assert should_use_direct_api_call(agent), "direct mode must exercise direct_api_call"
try:
    result = agent.run_conversation("hello")
finally:
    agent.close()
print(json.dumps({
    "model": agent.model,
    "final_response": result.get("final_response"),
    "failed": bool(result.get("failed")),
    "notices": notices,
}))
'''


def run_turn(hermes_home: Path, url: str, mode: str = "stream") -> dict:
    """Run one real conversation turn in a separate process against the stub."""
    env = os.environ.copy()
    env.update({
        "HERMES_HOME": str(hermes_home),
        "PYTHONPATH": str(ROOT),
        "PYTHONDONTWRITEBYTECODE": "1",
    })
    for key in ("OPENAI_API_KEY", "OPENROUTER_API_KEY", "ANTHROPIC_API_KEY"):
        env.pop(key, None)
    completed = subprocess.run(
        [sys.executable, "-c", CHILD, url, mode],
        cwd=ROOT, env=env, capture_output=True, text=True, timeout=180,
    )
    if completed.returncode != 0:
        raise AssertionError(f"child failed ({completed.returncode}):\n{completed.stdout}\n{completed.stderr}")
    return json.loads(completed.stdout.strip().splitlines()[-1])


def write_home_config(hermes_home: Path) -> None:
    hermes_home.mkdir(parents=True, exist_ok=True)
    (hermes_home / "config.yaml").write_text(
        "agent:\n  api_max_retries: 0\ncompression:\n  enabled: false\n", encoding="utf-8",
    )
