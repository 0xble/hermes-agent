#!/usr/bin/env python3
"""Isolated E2E for shared primary cooldown and notice ownership.

Run from the repository root:
    ./.venv/bin/python evals/provider_fallback/probe_shared_primary_cooldown.py

The script creates a temporary HERMES_HOME under ~/.hermes/cache/scratch, serves a
loopback OpenAI-compatible stub, and runs independent AIAgent processes. PASS output
shows one primary request across the outage, one outage notice, zero primary requests
after the simulated restart, and one recovery notice after reset expiry.
"""
from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

ROOT = pathlib.Path(__file__).resolve().parents[2]
SCRATCH = pathlib.Path.home() / ".hermes" / "cache" / "scratch"
SCRATCH.mkdir(parents=True, exist_ok=True)
HOME = pathlib.Path(tempfile.mkdtemp(prefix="shared-primary-cooldown-", dir=SCRATCH))
HERMES_HOME = HOME / ".hermes"
HERMES_HOME.mkdir()
(HERMES_HOME / "config.yaml").write_text(
    "agent:\n  api_max_retries: 0\ncompression:\n  enabled: false\n",
    encoding="utf-8",
)

state = {"primary_available": False, "requests": []}


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
        pass

    def do_GET(self):
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(b'{"data":[]}')

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", "0"))))
        model = body.get("model")
        primary = model == "primary-model"
        status = 200 if (not primary or state["primary_available"]) else 429
        state["requests"].append({"model": model, "status": status})
        payload = (
            {"id": "local", "object": "chat.completion", "choices": [{"index": 0, "message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"}], "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}}
            if status == 200
            else {"error": {"message": "rate_limit_error: retry later", "type": "rate_limit_error"}}
        )
        self.send_response(status)
        if status == 429:
            self.send_header("Retry-After", "7200")
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(json.dumps(payload).encode())


server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
URL = f"http://127.0.0.1:{server.server_port}/v1"

CHILD = r'''
import json, os, sys
from run_agent import AIAgent
from agent.error_classifier import classify_api_error
from agent.shared_primary_cooldown import complete_primary_recovery
role, url = sys.argv[1:]
notices = []
def status(kind, message):
    notices.append(str(message))
fallback = [{"provider": "custom", "model": "fallback-model", "base_url": url, "api_key": "fixture", "api_mode": "chat_completions"}]
agent = AIAgent(api_key="fixture", base_url=url, provider="custom", model="primary-model", api_mode="chat_completions", quiet_mode=True, skip_context_files=True, skip_memory=True, fallback_model=fallback, status_callback=status)
agent.client.max_retries = 0
primary_calls = 0
try:
    if role == "first":
        try:
            agent.client.chat.completions.create(model=agent.model, messages=[{"role":"user", "content":"hello"}])
            raise AssertionError("primary unexpectedly succeeded")
        except Exception as exc:
            primary_calls += 1
            classified = classify_api_error(exc)
            if not agent._try_activate_fallback(classified.reason, reset_at=classified.error_context.get("reset_at")):
                raise
        agent.client.chat.completions.create(model=agent.model, messages=[{"role":"user", "content":"hello"}])
        agent._emit_pending_fallback_notice()
    else:
        agent._restore_primary_runtime()
        agent.client.chat.completions.create(model=agent.model, messages=[{"role":"user", "content":"hello"}])
        if role == "recovery":
            complete_primary_recovery(agent)
finally:
    agent.close()
print(json.dumps({"role": role, "model": agent.model, "primary_calls": primary_calls, "notices": notices}))
'''


def run_child(role: str) -> dict:
    env = os.environ.copy()
    env.update({"HOME": str(HOME), "HERMES_HOME": str(HERMES_HOME), "PYTHONPATH": str(ROOT), "PYTHONDONTWRITEBYTECODE": "1"})
    completed = subprocess.run([sys.executable, "-c", CHILD, role, URL], cwd=ROOT, env=env, check=True, capture_output=True, text=True)
    return json.loads(completed.stdout.strip().splitlines()[-1])


try:
    outage_results = [run_child(role) for role in ("first", "session", "subagent", "cron")]
    restart_result = run_child("restart")
    record_path = HERMES_HOME / "state" / "model_cooldowns.json"
    record = json.loads(record_path.read_text(encoding="utf-8"))
    for entry in record["routes"].values():
        entry["reset_at"] = time.time() - 1
    record_path.write_text(json.dumps(record), encoding="utf-8")
    state["primary_available"] = True
    recovery_result = run_child("recovery")
    primary_requests = [request for request in state["requests"] if request["model"] == "primary-model"]
    outage_notices = [notice for result in outage_results for notice in result["notices"] if "rate-limited" in notice]
    recovery_notices = [notice for notice in recovery_result["notices"] if "restored" in notice]
    summary = {"home": str(HOME), "requests": state["requests"], "outage_results": outage_results, "restart": restart_result, "recovery": recovery_result, "outage_notice_count": len(outage_notices), "recovery_notice_count": len(recovery_notices)}
    print(json.dumps(summary, indent=2))
    assert len(primary_requests) == 2, primary_requests  # one 429 + one successful recovery probe
    assert len(outage_notices) == 1, outage_notices
    assert len(recovery_notices) == 1, recovery_notices
    assert all(result["primary_calls"] == 0 for result in outage_results[1:] + [restart_result])
    print("PROBE_OK: one primary outage call, one outage notice, restart-safe fallback, one recovery notice")
finally:
    server.shutdown()
