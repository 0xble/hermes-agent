"""Throwaway process-boundary probe, intentionally not wired into production."""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import socket
import sqlite3
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import Request, urlopen


def db(path):
    con = sqlite3.connect(path, timeout=5)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, owner TEXT, seq INTEGER, text TEXT, state TEXT DEFAULT 'queued')")
    con.execute("CREATE TABLE IF NOT EXISTS outbox (id TEXT PRIMARY KEY, owner TEXT, text TEXT, state TEXT DEFAULT 'pending')")
    con.commit()
    return con


def wire(path, token, payload):
    with socket.socket(socket.AF_UNIX) as sock:
        sock.settimeout(3)
        sock.connect(str(path))
        sock.sendall((json.dumps({**payload, "token": token}) + "\n").encode())
        data = bytearray()
        while b"\n" not in data:
            block = sock.recv(65536)
            if not block:
                break
            data.extend(block)
    return json.loads(data.decode())


def router(home, url, token):
    path = Path("router.sock")
    path.unlink(missing_ok=True)
    server = socket.socket(socket.AF_UNIX)
    server.bind(str(path))
    os.chmod(path, 0o600)
    server.listen(16)
    server.settimeout(.12)
    con = db(home / "probe.db")
    while True:
        try:
            conn, _ = server.accept()
        except socket.timeout:
            pass
        else:
            with conn:
                request = bytearray()
                while b"\n" not in request:
                    request.extend(conn.recv(65536))
                msg = json.loads(request)
                if msg.pop("token", None) != token:
                    response = {"error": "unauthorized"}
                elif msg["op"] == "claim":
                    # A session stays owned by its executor; follow-ups retain FIFO order.
                    row = con.execute("SELECT id, text FROM jobs WHERE owner=? AND state='queued' ORDER BY seq LIMIT 1", (msg["owner"],)).fetchone()
                    if row:
                        con.execute("UPDATE jobs SET state='running' WHERE id=?", (row[0],))
                        con.commit()
                    response = {"job": row}
                elif msg["op"] == "final":
                    con.execute("INSERT OR IGNORE INTO outbox(id,owner,text) VALUES(?,?,?)", (msg["id"], msg["owner"], msg["text"]))
                    con.execute("UPDATE jobs SET state='done' WHERE id=?", (msg["id"],))
                    con.commit()
                    response = {"accepted": True}
                else:
                    response = {"error": "unknown operation"}
                conn.sendall((json.dumps(response) + "\n").encode())
        # Only this process polls and sends. A delivered row is never replayed.
        try:
            with urlopen(url + "/poll", timeout=.5) as result:
                event = json.load(result)
            if event:
                con.execute("INSERT OR IGNORE INTO jobs(id,owner,seq,text) VALUES(?,?,?,?)",
                            (event["id"], event["owner"], event["seq"], event["text"]))
                con.commit()
            rows = con.execute("SELECT id,owner,text FROM outbox WHERE state='pending' ORDER BY rowid").fetchall()
            for ident, owner, text in rows:
                body = json.dumps({"id": ident, "owner": owner, "text": text}).encode()
                with urlopen(Request(url + "/send", data=body, headers={"Content-Type": "application/json"}), timeout=.5):
                    pass
                con.execute("UPDATE outbox SET state='sent' WHERE id=?", (ident,))
                con.commit()
        except (OSError, TimeoutError):
            pass


def executor(home, base_url, token, owner):
    os.environ["HERMES_HOME"] = str(home)
    # Real AIAgent and OpenAI-compatible wire path; model server is local and credential-free.
    from run_agent import AIAgent
    agent = AIAgent(api_key="fake-spike-key", base_url=base_url, provider="test-provider",
                    model="test/fake", quiet_mode=True, skip_context_files=True,
                    skip_memory=True, platform="telegram", session_id=f"spike-{owner}",
                    enabled_toolsets=["terminal"])
    while True:
        try:
            response = wire(Path("router.sock"), token, {"op": "claim", "owner": owner})
        except (OSError, ValueError):
            time.sleep(.15)
            continue
        job = response["job"]
        if job is None:
            time.sleep(.15)
            continue
        ident, text = job
        try:
            answer = agent.chat(text)
        except Exception as exc:
            answer = f"ERROR {type(exc).__name__}: {exc}"
        # Router may be down; preserve the result locally before trying to reconnect.
        pending = home / f"pending-{ident}.json"
        pending.write_text(json.dumps({"op": "final", "id": ident, "owner": owner, "text": answer}))
        while True:
            try:
                ack = wire(Path("router.sock"), token, json.loads(pending.read_text()))
                if ack.get("accepted"):
                    pending.unlink()
                    break
            except (OSError, ValueError):
                pass
            time.sleep(.15)


class Transport(BaseHTTPRequestHandler):
    def log_message(self, *_):
        pass

    def do_GET(self):
        with self.server.lock:
            if self.path == "/poll":
                self.server.polls.append(time.monotonic())
                event = self.server.events.pop(0) if self.server.events else None
            else:
                event = None
        body = json.dumps(event).encode()
        self.send_response(200)
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        value = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        with self.server.lock:
            self.server.sends.append(value)
        self.send_response(200)
        self.end_headers()


def run_probe(home: Path, base_url: str, tool_wait=3):
    home.mkdir(parents=True, exist_ok=True)
    token = "isolated-spike-token"
    # Neither live Telegram nor live Hermes state is read or inherited.
    http = ThreadingHTTPServer(("127.0.0.1", 0), Transport)
    http.lock = threading.Lock()
    http.polls, http.sends, http.events = [], [], []
    thread = threading.Thread(target=http.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{http.server_port}"
    env = {k: v for k, v in os.environ.items() if not any(s in k for s in ("TOKEN", "API_KEY", "SECRET"))}
    env["HERMES_HOME"] = str(home)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2])
    processes = []

    def start(role, *args):
        log = (home / f"{role}-{'-'.join(args[-1:]) if args else 'main'}-{len(processes)}.log").open("w")
        proc = subprocess.Popen([sys.executable, "-m", "spikes.s4_router_executor.probe", role,
                                 str(home), *(str(x) for x in args)],
                                env=env, stdout=log, stderr=subprocess.STDOUT, cwd=home)
        processes.append((proc, log))
        return proc

    def until(predicate, seconds=35):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if predicate():
                return True
            time.sleep(.1)
        return False

    try:
        first = start("router", url, token)
        assert until(lambda: (home / "router.sock").exists()), "router socket absent"
        a = start("executor", base_url, token, "A")
        with http.lock:
            http.events.append({"id": "a1", "owner": "A", "seq": 1, "text": "run long tool"})
            http.events.append({"id": "a2", "owner": "A", "seq": 2, "text": "follow-up"})
        assert until(lambda: db(home / "probe.db").execute("SELECT state FROM jobs WHERE id='a1'").fetchone() == ("running",)), "A not running"
        b = start("executor", base_url, token, "B")
        with http.lock:
            http.events.append({"id": "b1", "owner": "B", "seq": 1, "text": "new B session"})
        assert until(lambda: db(home / "probe.db").execute("SELECT state FROM jobs WHERE id='b1'").fetchone() == ("done",)), "B not done"
        assert until(lambda: db(home / "probe.db").execute("SELECT state FROM outbox WHERE id='b1'").fetchone() == ("sent",)), "B send not receipted"
        b_while_a = db(home / "probe.db").execute("SELECT state FROM jobs WHERE id='a1'").fetchone() != ("done",)
        first.terminate()
        first.wait(timeout=5)
        second = start("router", url, token)
        assert until(lambda: db(home / "probe.db").execute("SELECT count(*) FROM jobs WHERE state='done'").fetchone()[0] == 3, max(35, tool_wait + 25)), "turns unfinished"
        assert until(lambda: db(home / "probe.db").execute("SELECT count(*) FROM outbox WHERE state='sent'").fetchone()[0] == 3), "sends unfinished"
        with http.lock:
            sends = list(http.sends)
            polls = len(http.polls)
        receipt = {"a_survived_router_replacement": any(x["id"] == "a1" and "A done" in x["text"] for x in sends),
                   "b_served_during_a": b_while_a,
                   "a_followup_ordered": [x[0] for x in db(home / "probe.db").execute("SELECT id FROM jobs WHERE owner='A' AND state='done' ORDER BY seq")] == ["a1", "a2"],
                   "poller_max": 1 if first.poll() is not None and second.poll() is None else -1,
                   "poll_calls": polls, "send_calls": len(sends),
                   "duplicate_sends": len(sends) - len({x["id"] for x in sends}),
                   "sends": sends, "executors": {"A": a.pid, "B": b.pid}, "routers": [first.pid, second.pid]}
        (home / "receipt.json").write_text(json.dumps(receipt, indent=2))
        return receipt
    finally:
        for proc, log in processes:
            if proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait()
            log.close()
        http.shutdown()
        http.server_close()


if __name__ == "__main__":
    role, home, *args = sys.argv[1:]
    if role == "router":
        router(Path(home), *args)
    elif role == "executor":
        executor(Path(home), *args)
