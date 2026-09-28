"""Disposable native-outbox crash-window probe (NOT a router/executor implementation)."""
from __future__ import annotations

import asyncio
import contextlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import random
import signal
import subprocess
import sys
import threading
import time

from gateway.outbox import Outbox, bind_turn, clear_turn, recover
from gateway.platforms.base import SendResult
from plugins.platforms.telegram.adapter import TelegramAdapter


def _adapter(base_url: str, enabled: bool = True):
    from telegram import Bot
    from types import SimpleNamespace
    adapter = TelegramAdapter.__new__(TelegramAdapter)
    adapter.gateway_runner = SimpleNamespace(config=SimpleNamespace(durable_outbox_enabled=enabled))
    adapter._bot = Bot(token="123456:DISPOSABLE_TEST_TOKEN", base_url=base_url + "/bot")
    @contextlib.asynccontextmanager
    async def chat_lock(chat_id):
        yield
    adapter._chat_send_lock = chat_lock

    async def telegram_send(chat_id, content, reply_to, metadata):
        result = await adapter._bot.send_message(chat_id=chat_id, text=content)
        return SendResult(success=True, message_id=str(result.message_id))

    adapter._send_text_locked = telegram_send
    return adapter


class _BotStub(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        self.server.log.append(format % args)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        route = self.path.rsplit("/", 1)[-1]
        self.server.calls.append({"route": route, "body": body.decode(errors="replace")})
        self.server.accepted.set()
        if route.lower() == "sendmessage":
            self.server.release.wait(timeout=10)
        reply = {"ok": True, "result": {"message_id": len(self.server.calls), "date": 0,
                                       "chat": {"id": 111, "type": "private"}, "text": "answer"}}
        data = json.dumps(reply).encode()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass


def _server():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _BotStub)
    server.calls = []
    server.log = []
    server.accepted = threading.Event()
    server.release = threading.Event()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


async def _worker_send(home: Path, base_url: str):
    from gateway.config import load_gateway_config
    config = load_gateway_config()
    if not config.durable_outbox_enabled:
        raise RuntimeError("disposable profile did not enable durable outbox")
    adapter = _adapter(base_url, config.durable_outbox_enabled)
    bind_turn(home, "native-telegram-turn")
    try:
        await adapter.send("111", "answer")
    finally:
        clear_turn()


def _env(home: Path):
    env = dict(os.environ)
    env["HERMES_HOME"] = str(home)
    env["HERMES_GATEWAY_LOCK_DIR"] = str(home / "locks")
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])
    env.pop("TELEGRAM_BOT_TOKEN", None)
    return env


def run_crash_windows(home: Path, evidence: Path, count: int = 20):
    """SIGKILL an actual PTB send *after* stub acceptance, before outbox receipt."""
    home, evidence = Path(home), Path(evidence)
    evidence.mkdir(parents=True, exist_ok=True)
    rng = random.SystemRandom()
    receipts = []
    for index in range(count):
        run_home = home / f"crash-{index:02d}"
        run_home.mkdir(parents=True, exist_ok=True)
        (run_home / "config.yaml").write_text("gateway:\n  durable_outbox:\n    enabled: true\n")
        server, thread = _server()
        base_url = f"http://127.0.0.1:{server.server_port}"
        log_path = evidence / f"crash-{index:02d}.log"
        with log_path.open("w") as log:
            child = subprocess.Popen([sys.executable, "-m", "spikes.s4_rerun.probe", "send",
                                      str(run_home), base_url], env=_env(run_home),
                                     stdout=log, stderr=subprocess.STDOUT)
            try:
                accepted = server.accepted.wait(timeout=15)
                if not accepted:
                    raise RuntimeError(f"worker did not reach Telegram stub (exit={child.poll()})")
                log.write(f"stub accepted {server.calls[-1]['route']} for worker pid {child.pid}\n")
                log.flush()
                jitter_ms = rng.randrange(2, 90)
                time.sleep(jitter_ms / 1000)
                os.kill(child.pid, signal.SIGKILL)
                child.wait(timeout=10)
                log.write(f"SIGKILL worker after accepted send; delay_ms={jitter_ms}; status={child.returncode}\n")
                log.flush()
                server.release.set()
                adapter = _adapter(base_url)
                recovered = asyncio.run(recover(Outbox(run_home), adapter))
                log.write(f"native outbox recovery sent={recovered[0]} ambiguous={recovered[1]}\n")
                log.flush()
                calls = list(server.calls)
                sends = [call for call in calls if call["route"].lower() == "sendmessage"]
                receipt = {"run": index, "kill_delay_ms": jitter_ms, "worker_pid": child.pid,
                           "worker_returncode": child.returncode, "bot_api_calls": calls,
                           "accepted_sends": len(sends), "recovery_sends": recovered[0],
                           "ambiguous_rows": recovered[1], "duplicate_sends": max(0, len(sends) - 1),
                           "outbox_states": [row.state for row in Outbox(run_home).all_rows()],
                           "log": str(log_path), "scope": "outbox egress worker, not router"}
                (evidence / f"crash-{index:02d}.json").write_text(json.dumps(receipt, indent=2))
                receipts.append(receipt)
            finally:
                server.release.set()
                if child.poll() is None:
                    child.kill()
                child.wait(timeout=10)
                server.shutdown()
                server.server_close()
                thread.join(timeout=10)
    return receipts


def run_scoped_lock_conflict(home: Path):
    """Use gateway.status's cross-process token lock against an actual contender."""
    home = Path(home)
    home.mkdir(parents=True, exist_ok=True)
    env = _env(home)
    token = "123456:DISPOSABLE_TEST_TOKEN"
    holder = subprocess.Popen([sys.executable, "-m", "spikes.s4_rerun.probe", "lock-holder", token,
                               "gateway/run.py"],
                              env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                              bufsize=1)
    try:
        line = holder.stdout.readline().strip()
        held = json.loads(line)
        contender = subprocess.run([sys.executable, "-m", "spikes.s4_rerun.probe", "lock-check", token],
                                   env=env, capture_output=True, text=True, timeout=15, check=True)
        rejected = json.loads(contender.stdout)
    finally:
        holder.terminate()
        holder.wait(timeout=10)
    after = subprocess.run([sys.executable, "-m", "spikes.s4_rerun.probe", "lock-check", token],
                           env=env, capture_output=True, text=True, timeout=15, check=True)
    return {"holder_pid": holder.pid, "holder_acquired": held["acquired"],
            "contender_acquired": rejected["acquired"],
            "contender_existing_pid": rejected["existing_pid"],
            "after_holder_exit_acquired": json.loads(after.stdout)["acquired"],
            "scope": "telegram-bot-token", "holder_identity": "probe argv with gateway/run.py marker (not real router)"}


def _lock_command(command: str, token: str):
    from gateway.status import acquire_scoped_lock, release_scoped_lock
    scope = "telegram-bot-token"
    acquired, existing = acquire_scoped_lock(scope, token)
    print(json.dumps({"acquired": acquired, "existing_pid": (existing or {}).get("pid")}), flush=True)
    if command == "lock-holder":
        time.sleep(90)
    if acquired:
        release_scoped_lock(scope, token)


if __name__ == "__main__":
    if sys.argv[1] == "send":
        asyncio.run(_worker_send(Path(sys.argv[2]), sys.argv[3]))
    else:
        _lock_command(sys.argv[1], sys.argv[2])
