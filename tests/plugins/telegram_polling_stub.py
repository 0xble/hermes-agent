"""Local Bot API with Telegram offset confirmation for polling-transfer tests."""
import json
import select
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs


class BotAPI:
    def __init__(self):
        self.lock = threading.Lock()
        self.updates = []
        self.confirmed = 0
        self.inflight = 0
        self.maximum = 0
        self.offsets = []
        self.errors = []
        self.sent = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self):
        return f"http://127.0.0.1:{self.server.server_port}/bot"

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)

    def add(self, first, last, *, text="/status", chat_id=1):
        with self.lock:
            self.updates.extend({
                "update_id": uid,
                "message": {
                    "message_id": uid, "date": 1800000000,
                    "chat": {"id": chat_id, "type": "private"},
                    "from": {"id": chat_id, "is_bot": False, "first_name": "Test"},
                    "text": text,
                    "entities": ([{"type": "bot_command", "offset": 0, "length": len(text.split()[0])}]
                                 if text.startswith("/") else []),
                },
            } for uid in range(first, last + 1))

    def add_callback(self, uid, data, *, chat_id=1):
        with self.lock:
            self.updates.append({"update_id": uid, "callback_query": {
                "id": f"callback-{uid}", "from": {"id": chat_id, "is_bot": False, "first_name": "Test"},
                "chat_instance": "test-instance", "data": data,
                "message": {"message_id": 1, "date": 1800000000,
                            "chat": {"id": chat_id, "type": "private"}, "text": "approval"}}})

    def _handler(self):
        state = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def do_POST(self):
                raw = self.rfile.read(int(self.headers.get("Content-Length", 0))).decode()
                try:
                    form = parse_qs(raw)
                    if not form and raw.startswith("{"):
                        form = json.loads(raw)
                    method = self.path.rsplit("/", 1)[-1].lower()
                    if method == "getupdates":
                        offset = int(form.get("offset", [0])[0])
                        timeout = float(form.get("timeout", [0])[0])
                        with state.lock:
                            state.inflight += 1
                            state.maximum = max(state.maximum, state.inflight)
                            state.offsets.append(offset)
                            state.confirmed = max(state.confirmed, offset - 1)
                        closed_early = False
                        try:
                            deadline = time.monotonic() + min(timeout, 0.35)
                            while True:
                                with state.lock:
                                    result = [item for item in state.updates if item["update_id"] >= offset][:100]
                                if result or time.monotonic() >= deadline:
                                    break
                                # A cancelled client request no longer owns a long poll.
                                readable, _, _ = select.select([self.connection], [], [], 0)
                                if readable and not self.connection.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT):
                                    closed_early = True
                                    break
                                time.sleep(0.005)
                        finally:
                            if closed_early:
                                # Client cancellation finishes before server-side unwinding.
                                time.sleep(0.02)
                            with state.lock:
                                state.inflight -= 1
                    elif method == "getme":
                        result = {"id": 123456, "is_bot": True, "first_name": "Stub", "username": "stub_bot"}
                    elif method == "deletewebhook":
                        assert form.get("drop_pending_updates", ["false"])[0].lower() != "true"
                        result = True
                    elif method in {"sendmessage", "sendchataction", "setmycommands", "setmyshortdescription", "answercallbackquery", "editmessagereplymarkup", "editmessagetext"}:
                        if method == "sendmessage":
                            with state.lock:
                                state.sent.append({"chat_id": int(form.get("chat_id", [1])[0]),
                                                   "text": form.get("text", [""])[0],
                                                   "reply_markup": form.get("reply_markup", [""])[0]})
                        result = ({"message_id": 1, "date": 1800000000,
                                   "chat": {"id": 1, "type": "private"}, "text": "ok"}
                                  if method in {"sendmessage", "editmessagetext"} else True)
                    else:
                        raise ValueError(method)
                    body = json.dumps({"ok": True, "result": result}).encode()
                    code = 200
                except Exception as exc:
                    with state.lock:
                        state.errors.append(repr(exc))
                    body = json.dumps({"ok": False, "error_code": 400, "description": repr(exc)}).encode()
                    code = 400
                try:
                    self.send_response(code)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        return Handler
