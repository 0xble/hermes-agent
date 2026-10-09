#!/usr/bin/env python3
"""Isolated E2E for shared primary cooldown and notice ownership.

Run from the repository root:
    ./.venv/bin/python evals/provider_fallback/probe_shared_primary_cooldown.py

The script creates a temporary HERMES_HOME under ~/.hermes/cache/scratch (removed on exit;
set PROBE_KEEP_HOME=1 to keep it for debugging), serves a
loopback OpenAI-compatible stub (primary 429 with Retry-After 7200 until released,
fallback 200), and runs every turn as a separate process through the production request
path, ``AIAgent.run_conversation``. Roles exercise all three request wrappers: streaming
(``first``, ``session``, ``restart``, ``recovery``), non-streaming (``subagent``) and
``direct_api_call`` (``cron``, platform=cron).

A pass prints two lines:
    PROBE_OK: one primary outage call, one outage notice, restart-safe fallback, one recovery notice
    PROBE_OK: no-reset shared backoff 60s -> 120s -> 240s; cooling sessions never wait on the primary

Phase 1 also proves that fallback replies never clear the outage: the shared record must
still exist with its 2h reset after every outage-phase turn, and no recovery notice may
appear before the primary actually answers.
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
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
RECORD_PATH = HERMES_HOME / "state" / "model_cooldowns.json"
PRIMARY_MODEL = "primary-model"

state = {"primary_available": False, "send_reset": True, "requests": []}
lock = threading.Lock()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *_args):
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
        ok = model != PRIMARY_MODEL or state["primary_available"]
        with lock:
            state["requests"].append({"model": model, "status": 200 if ok else 429, "stream": stream})
        if not ok:
            payload = json.dumps({"error": {"message": "rate_limit_error: retry later", "type": "rate_limit_error"}}).encode()
            self.send_response(429)
            if state["send_reset"]:
                self.send_header("Retry-After", "7200")
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
            for chunk in (
                {**base, "choices": [{"index": 0, "delta": {"role": "assistant", "content": text}, "finish_reason": None}]},
                {**base, "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
                 "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}},
            ):
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


server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
URL = f"http://127.0.0.1:{server.server_port}/v1"

# One real user turn. Roles choose the request wrapper; nothing here calls the client, the
# fallback switch or the recovery helper directly.
CHILD = r'''
import json, sys
from run_agent import AIAgent
role, url = sys.argv[1:3]
notices = []
fallback = [{"provider": "custom", "model": "fallback-model", "base_url": url, "api_key": "fixture", "api_mode": "chat_completions"}]
agent = AIAgent(
    api_key="fixture", base_url=url, provider="custom", model="primary-model",
    api_mode="chat_completions", quiet_mode=True, skip_context_files=True, skip_memory=True,
    enabled_toolsets=[], fallback_model=fallback, max_iterations=3,
    platform="cron" if role == "cron" else None,
    status_callback=lambda kind, message: notices.append(str(message)),
)
if role in ("subagent", "cron"):
    agent._disable_streaming = True
if role == "cron":
    from agent.chat_completion_helpers import should_use_direct_api_call
    assert should_use_direct_api_call(agent), "cron role must exercise direct_api_call"
try:
    result = agent.run_conversation("hello")
finally:
    agent.close()
print(json.dumps({"role": role, "model": agent.model, "final_response": result.get("final_response"), "notices": notices}))
'''


def primary_count() -> int:
    with lock:
        return len([r for r in state["requests"] if r["model"] == PRIMARY_MODEL])


def run_child(role: str) -> dict:
    env = os.environ.copy()
    env.update({"HOME": str(HOME), "HERMES_HOME": str(HERMES_HOME), "PYTHONPATH": str(ROOT), "PYTHONDONTWRITEBYTECODE": "1"})
    for key in ("OPENAI_API_KEY", "OPENROUTER_API_KEY", "ANTHROPIC_API_KEY"):
        env.pop(key, None)
    before = primary_count()
    completed = subprocess.run(
        [sys.executable, "-c", CHILD, role, URL], cwd=ROOT, env=env,
        capture_output=True, text=True, timeout=300,
    )
    if completed.returncode != 0:
        raise AssertionError(f"{role} child failed ({completed.returncode}):\n{completed.stdout}\n{completed.stderr}")
    result = json.loads(completed.stdout.strip().splitlines()[-1])
    result["primary_calls"] = primary_count() - before
    return result


def read_record() -> dict:
    return next(iter(json.loads(RECORD_PATH.read_text(encoding="utf-8-sig"))["routes"].values()))


def expire_record() -> None:
    record = json.loads(RECORD_PATH.read_text(encoding="utf-8-sig"))
    for entry in record["routes"].values():
        entry["reset_at"] = time.time() - 1
    RECORD_PATH.write_text(json.dumps(record), encoding="utf-8")


def notices_matching(results, needle: str) -> list[str]:
    return [n for r in results for n in r["notices"] if needle in n]


try:
    # Phase 1: provider reset (Retry-After 7200).
    outage_results = []
    for role in ("first", "session", "subagent", "cron"):
        result = run_child(role)
        outage_results.append(result)
        assert result["final_response"] == "OK from fallback-model", result
        # A fallback reply must never clear the outage (P1-A regression guard).
        assert RECORD_PATH.exists(), f"{role}: fallback reply cleared the shared outage"
        entry = read_record()
        assert entry["reset_at"] - time.time() > 7000, (role, entry)
    restart_result = run_child("restart")
    assert restart_result["final_response"] == "OK from fallback-model", restart_result
    assert RECORD_PATH.exists(), "restart: fallback reply cleared the shared outage"
    expire_record()
    state["primary_available"] = True
    recovery_result = run_child("recovery")
    primary_requests = [r for r in state["requests"] if r["model"] == PRIMARY_MODEL]
    outage_notices = notices_matching(outage_results + [restart_result], "rate-limited until")
    early_recovery = notices_matching(outage_results + [restart_result], "restored")
    recovery_notices = notices_matching([recovery_result], "restored")
    summary = {
        "home": str(HOME), "requests": state["requests"], "outage_results": outage_results,
        "restart": restart_result, "recovery": recovery_result,
        "outage_notice_count": len(outage_notices), "recovery_notice_count": len(recovery_notices),
    }
    print(json.dumps(summary, indent=2))
    assert [r["status"] for r in primary_requests] == [429, 200], primary_requests
    assert outage_results[0]["primary_calls"] == 1, outage_results[0]
    assert all(r["primary_calls"] == 0 for r in outage_results[1:] + [restart_result])
    assert len(outage_notices) == 1, outage_notices
    assert early_recovery == [], early_recovery
    assert recovery_result["final_response"] == "OK from primary-model", recovery_result
    assert recovery_result["model"] == PRIMARY_MODEL, recovery_result
    assert len(recovery_notices) == 1, recovery_notices
    assert not RECORD_PATH.exists(), "primary success must clear the shared outage"
    print("PROBE_OK: one primary outage call, one outage notice, restart-safe fallback, one recovery notice")

    # Phase 2 (no provider reset): each outage-arming turn escalates the SHARED level
    # (60s -> 120s -> 240s), and a turn started while cooling never calls the primary.
    state["primary_available"] = False
    state["send_reset"] = False
    windows = []
    phase2 = []
    for _ in range(3):
        armed = run_child("first")
        assert armed["primary_calls"] == 1, armed
        assert armed["final_response"] == "OK from fallback-model", armed
        entry = read_record()
        windows.append(round(entry["reset_at"] - entry["recorded_at"]))
        blocked = run_child("session")
        assert blocked["primary_calls"] == 0 and blocked["final_response"] == "OK from fallback-model", blocked
        phase2 += [armed, blocked]
        expire_record()  # the next turn probes the primary (still 429) and escalates
    phase2_outage = notices_matching(phase2, "rate-limited until")
    print(json.dumps({"no_reset_backoff_windows_s": windows, "phase2_outage_notice_count": len(phase2_outage)}))
    assert windows == [60, 120, 240], windows
    assert len(phase2_outage) == 1, phase2_outage
    assert notices_matching(phase2, "restored") == []
    print("PROBE_OK: no-reset shared backoff 60s -> 120s -> 240s; cooling sessions never wait on the primary")
finally:
    server.shutdown()
    server.server_close()
    if os.environ.get("PROBE_KEEP_HOME") == "1":
        print(f"probe HERMES_HOME kept at {HOME}")
    else:
        shutil.rmtree(HOME, ignore_errors=True)
