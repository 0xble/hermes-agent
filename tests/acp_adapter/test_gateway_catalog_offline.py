"""Real ACP attachment must not wait for remote model-catalog discovery."""
import asyncio
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socket
import threading

import pytest

from tests.acp_adapter.test_gateway_sessions import editor, viewer


@contextmanager
def _held_catalog():
    """A loopback models.dev mirror that accepts each request and answers only on release."""
    release, arrived = threading.Event(), threading.Event()
    held = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            held.append(self.connection)
            arrived.set()
            release.wait(60)
            body = b"{}"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    def still_waiting():
        """True while every catalog client is still connected, waiting for its held reply."""
        def open_(conn):
            try:
                return conn.recv(1, socket.MSG_PEEK | socket.MSG_DONTWAIT) != b""
            except BlockingIOError:
                return True
            except OSError:
                return False
        return bool(held) and all(open_(conn) for conn in list(held))

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}/api.json", arrived, still_waiting
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.platforms("linux")
@pytest.mark.parametrize("method", ["session/new", "session/load"])
@pytest.mark.asyncio
async def test_cold_attach_reads_catalogs_without_foreground_dns(daemon, tmp_path, monkeypatch, method):
    # The parent process's offline-inventory fixture cannot patch the ACP subprocess.
    # Fail remote resolution immediately there, and distinguish a harmless daemon
    # cache refresh from network work the session/load reply actually waits for.
    bootstrap = '''import runpy, socket, sys, threading
original = socket.getaddrinfo
def resolve(host, *args, **kwargs):
    if host not in ("127.0.0.1", "::1", "localhost", None):
        if not threading.current_thread().daemon:
            print("FOREGROUND_EXTERNAL_DNS", host, file=sys.stderr, flush=True)
            import traceback
            traceback.print_stack(file=sys.stderr)
        raise OSError("remote catalog unavailable")
    return original(host, *args, **kwargs)
socket.getaddrinfo = resolve
runpy.run_module("acp_adapter.entry", run_name="__main__")
'''
    spawn = asyncio.create_subprocess_exec

    async def guarded_spawn(executable, *args, **kwargs):
        assert args == ("-m", "acp_adapter.entry")
        return await spawn(executable, "-c", bootstrap, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", guarded_spawn)
    home = daemon[0]
    async with viewer(daemon) as ws:
        created = await ws.rpc("session.create", request_id="offline-acp", source="cli", cwd=str(tmp_path))
        # The DNS guard cannot see the cold models.dev fetch: it resolves in a daemon worker
        # while its caller waits, and a loopback mirror never reaches the guard. Hold that
        # mirror's reply instead; the attachment must answer while the request is unanswered.
        with _held_catalog() as (url, arrived, still_waiting):
            # Only the ACP process spawned below reads this; the daemon already loaded its config.
            config = json.loads((home / "config.yaml").read_text())
            config["models_dev"]["url"] = url
            (home / "config.yaml").write_text(json.dumps(config))
            async with editor(daemon, tmp_path) as acp:
                assert "result" in await acp.rpc("initialize", protocolVersion=1, clientCapabilities={})
                params = {"cwd": str(tmp_path), "mcpServers": []}
                if method == "session/load":
                    params["sessionId"] = created["session_id"]
                loaded = await acp.rpc(method, **params)
                assert "result" in loaded, loaded
                models = loaded["result"]["models"]
                assert models["currentModelId"] in {row["modelId"] for row in models["availableModels"]}
                assert await asyncio.to_thread(arrived.wait, 10), (
                    "the background warmer never requested the catalog, so the hold proved nothing")
                # A reply that waited on the catalog returns only after its client timed out
                # and dropped the held connection.
                assert still_waiting(), f"{method} waited on the held models.dev request"
    assert "FOREGROUND_EXTERNAL_DNS" not in (tmp_path / "acp.log").read_text(encoding="utf-8-sig")
