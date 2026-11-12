"""Cross-host Camofox bytes through registered tools and an isolated HTTP peer."""

import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

IDENTITY = "hermes_camofox_" + "f" * 24


@pytest.fixture
def peer(monkeypatch, tmp_path):
    from tools import browser_camofox as camofox
    import tools.browser_tool  # noqa: F401
    from tools.registry import registry

    state = SimpleNamespace(calls=[], downloads=[], status=200, error=None, count=0, tab="transfer-tab")

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def send(self, payload, status=200):
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            if status == 307:
                self.send_header("Location", "/tabs/transfer-tab/upload")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):  # noqa: N802
            parsed = urlsplit(self.path)
            query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
            state.calls.append(("GET", parsed.path, query))
            if parsed.path == "/browser/identities":
                return self.send({"identities": [{"alias": "brianle", "userId": IDENTITY}]})
            if parsed.path == "/tabs":
                return self.send({"tabs": []})
            if parsed.path.endswith("/snapshot"):
                return self.send({"snapshot": "- button Upload [e7]", "refsCount": 1})
            if parsed.path.endswith("/downloads"):
                if state.error is not None:
                    return self.send(state.error, state.status)
                records = state.downloads
                if query.get("includeData") != "true":
                    records = [{k: v for k, v in row.items() if k != "dataBase64"} for row in records]
                return self.send({"tabId": state.tab, "downloads": records}, state.status)
            return self.send({}, 404)

        def do_POST(self):  # noqa: N802
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            state.calls.append(("POST", self.path, body))
            if self.path == "/tabs":
                return self.send({"tabId": state.tab, "url": "https://example.test"})
            if self.path.endswith("/navigate"):
                return self.send({"url": body["url"]})
            if self.path.endswith("/evaluate"):
                return self.send({"result": state.count})
            if self.path.endswith("/back"):
                return self.send({"url": "https://example.test/previous"})
            if self.path.endswith("/upload"):
                if state.status != 200:
                    return self.send({"code": "invalid_upload", "error": "upload rejected"}, state.status)
                if "files" not in body:
                    return self.send({"code": "file_not_found"}, 400)
                return self.send({"ok": True, "attached": [f["name"] for f in body["files"]], "via": "filechooser"})
            return self.send({}, 404)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("CAMOFOX_URL", f"http://127.0.0.1:{server.server_port}")
    monkeypatch.setenv("CAMOFOX_UPLOADS_DIR", str(tmp_path / "not-shared"))
    monkeypatch.setattr(camofox, "_get_camofox_config", lambda: {"accounts": {"brianle": {}}})
    camofox._sessions.clear()
    state.dispatch = lambda name, args: json.loads(registry.dispatch(name, args, task_id="transfer"))
    assert state.dispatch("browser_navigate", {"url": "https://example.test", "account": "brianle"})["success"]
    state.calls.clear()
    try:
        yield state
    finally:
        camofox.release_task_bindings({"transfer"})
        server.shutdown()
        server.server_close()
        thread.join(5)


@pytest.mark.parametrize("mode", ["bytes", "http-error", "redirect", "stale", "unknown-count", "oversize"])
def test_upload_crosses_host_boundary_once_with_original_names(peer, tmp_path, mode):
    raw = bytes(range(256)) + b"private-file-content"
    source = tmp_path / "résumé.png"
    source.write_bytes(raw)
    empty = tmp_path / "empty.txt"
    empty.touch()
    if mode == "http-error":
        peer.status = 500
    if mode == "redirect":
        peer.status = 307
    if mode == "stale":
        peer.status = 410
    if mode == "unknown-count":
        peer.count = None
    if mode == "oversize":
        with source.open("wb") as f:
            f.truncate(20 * 1024 * 1024 + 1)
    result = peer.dispatch("browser_upload", {"paths": [str(source), str(empty)], "ref": "@e7"})
    uploads = [call for call in peer.calls if call[1].endswith("/upload")]
    assert result["success"] is (mode == "bytes")
    assert len(uploads) == (1 if mode in ("bytes", "http-error", "redirect", "stale") else 0)
    assert not any(call[1] == "/tabs" for call in peer.calls)
    assert not (tmp_path / "not-shared").exists()
    assert "private-file-content" not in json.dumps(result)
    if uploads:
        _, route, body = uploads[0]
        assert route == "/tabs/transfer-tab/upload"
        assert body["userId"] == IDENTITY and body["ref"] == "e7"
        assert "path" not in body
        assert body["files"] == [
            {"name": source.name, "mimeType": "image/png", "dataBase64": base64.b64encode(raw).decode()},
            {"name": empty.name, "mimeType": "text/plain", "dataBase64": ""},
        ]
        assert base64.b64encode(raw).decode() not in json.dumps(result)


@pytest.mark.parametrize("mode", ["bytes", "empty", "traversal", "base64", "size", "missing", "failed", "oversize", "stale", "wrong-tab", "duplicate", "invalid-metadata"])
def test_download_imports_validated_bytes_without_overwrite_or_content_output(peer, monkeypatch, tmp_path, mode):
    from hermes_constants import get_hermes_home

    raw = b"private-file-content\x00\xff"
    record = {"id": "download-1", "suggestedFilename": "résumé.pdf", "mimeType": "application/pdf",
              "bytes": len(raw), "failure": None, "dataBase64": base64.b64encode(raw).decode(),
              "filePath": "/inaccessible-mac/private.pdf"}
    if mode == "empty":
        record.update(bytes=0, dataBase64="")
        raw = b""
    elif mode == "traversal":
        record["suggestedFilename"] = "../escape.pdf"
    elif mode == "base64":
        record["dataBase64"] = "Zh=="  # valid alphabet, noncanonical padding bits
    elif mode == "size":
        record["bytes"] += 1
    elif mode == "missing":
        record.pop("dataBase64")
        record["dataSkipped"] = "max_bytes_exceeded"
    elif mode == "failed":
        record["failure"] = "capture_failed"
    elif mode == "oversize":
        record["bytes"] = 20 * 1024 * 1024 + 1
    elif mode == "stale":
        peer.status = 410
    if mode == "wrong-tab":
        peer.tab = "unowned-tab"
    if mode == "invalid-metadata":
        record["mimeType"] = {"untrusted": "metadata"}
    peer.downloads = [record, record] if mode == "duplicate" else [record]
    listing = peer.dispatch("browser_downloads", {})
    if mode not in ("stale", "traversal", "wrong-tab", "duplicate", "invalid-metadata"):
        assert listing.get("success"), listing
        assert listing["downloads"][0]["id"] == "download-1"
        assert "dataBase64" not in json.dumps(listing) and "filePath" not in json.dumps(listing)
    result = peer.dispatch("browser_downloads", {"download_id": "download-1"})
    assert result.get("success") is (mode in ("bytes", "empty")), result
    assert "private-file-content" not in json.dumps(result)
    assert "dataBase64" not in json.dumps(result) and "/inaccessible-mac" not in json.dumps(result)
    root = get_hermes_home() / "browser_downloads"
    if result["success"]:
        target = Path(result["output_path"])
        assert target.is_relative_to(root) and target.name == "résumé.pdf"
        assert target.read_bytes() == raw
        again = peer.dispatch("browser_downloads", {"download_id": "download-1"})
        assert again["success"] and again["output_path"] != str(target)
        assert target.read_bytes() == raw and Path(again["output_path"]).read_bytes() == raw
        gets = [call for call in peer.calls if call[1].endswith("/downloads")]
        assert all(call[2]["userId"] == IDENTITY for call in gets)
        assert gets[-1][2]["includeData"] == "true"
    else:
        assert not root.exists() or not list(root.rglob("*"))


@pytest.mark.parametrize("arguments", [{}, {"download_id": "lost"}], ids=["list", "import"])
def test_download_missing_tab_invalidates_binding_without_replay(peer, arguments):
    peer.status = 404
    peer.error = {"error": "Tab not found"}

    result = peer.dispatch("browser_downloads", arguments)

    assert result["success"] is False
    assert "tab is gone" in result["error"] and "browser_navigate" in result["error"]
    assert peer.dispatch("browser_snapshot", {})["success"] is False
    assert len([call for call in peer.calls if call[1].endswith("/downloads")]) == 1
    assert not any(call[1] == "/tabs" or call[1].endswith("/snapshot") for call in peer.calls)


def test_download_error_body_obeys_transport_limit(peer, monkeypatch):
    from tools import browser_camofox_files

    monkeypatch.setattr(browser_camofox_files, "MAX_RESPONSE_BYTES", 512)
    peer.status = 404
    peer.error = {"error": "Tab not found", "padding": "x" * 2048}

    result = peer.dispatch("browser_downloads", {})

    assert result["success"] is False and "transport limit" in result["error"]
    assert peer.dispatch("browser_snapshot", {})["success"] is True
    assert len([call for call in peer.calls if call[1].endswith("/downloads")]) == 1


def test_download_stream_limit_does_not_return_partial_file(peer, monkeypatch):
    from hermes_constants import get_hermes_home
    from tools import browser_camofox_files

    raw = b"x" * 2048
    peer.downloads = [{"id": "large", "suggestedFilename": "bounded.bin", "mimeType": "application/octet-stream",
                       "bytes": len(raw), "failure": None, "dataBase64": base64.b64encode(raw).decode()}]
    monkeypatch.setattr(browser_camofox_files, "MAX_RESPONSE_BYTES", 512)
    assert peer.dispatch("browser_downloads", {})["success"]
    result = peer.dispatch("browser_downloads", {"download_id": "large"})
    assert result["success"] is False and "transport limit" in result["error"]
    assert not (get_hermes_home() / "browser_downloads").exists()


def test_file_transfer_keeps_existing_history_navigation(peer):
    result = peer.dispatch("browser_back", {})
    assert result["success"] is True and result["url"] == "https://example.test/previous"
    assert [call[1] for call in peer.calls if call[0] == "POST"] == ["/tabs/transfer-tab/back"]
