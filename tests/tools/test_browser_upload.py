"""browser_upload: Camofox file-chooser attach, cross-host byte transport, and path safety."""

import base64
import json
from unittest.mock import MagicMock, patch

import pytest
import requests

from tools.browser_camofox import camofox_navigate, camofox_upload


def _resp(data):
    r = MagicMock()
    r.status_code = 200
    r.json.return_value = data
    r.raise_for_status = MagicMock()
    return r


def _open_tab(mock_post, task_id):
    mock_post.return_value = _resp({"tabId": f"tab-{task_id}", "url": "https://example.com"})
    camofox_navigate("https://example.com", task_id=task_id)


@patch("tools.browser_camofox.requests.post")
def test_sends_outside_file_inline_and_posts_ref(mock_post, monkeypatch, tmp_path):
    monkeypatch.setenv("CAMOFOX_URL", "http://localhost:9377")
    root = tmp_path / "uploads"
    root.mkdir()
    monkeypatch.setenv("CAMOFOX_UPLOADS_DIR", str(root))
    src = tmp_path / "logo.png"
    src.write_bytes(b"png-bytes")
    _open_tab(mock_post, "up1")

    mock_post.side_effect = [
        _resp({"result": 0}),
        _resp({"ok": True, "attached": ["x"], "via": "filechooser"}),
    ]
    result = json.loads(camofox_upload([str(src)], ref="@e7", task_id="up1"))

    assert result["success"] is True
    assert result["via"] == "filechooser"
    assert result["attached"] == ["logo.png"]
    url = mock_post.call_args.args[0]
    body = mock_post.call_args.kwargs["json"]
    assert url.endswith("/tabs/tab-up1/upload")
    assert body["ref"] == "e7" and "selector" not in body
    assert "path" not in body
    assert body["files"] == [{"name": "logo.png", "mimeType": "image/png",
                              "dataBase64": base64.b64encode(src.read_bytes()).decode("ascii")}]
    assert not list(root.iterdir())



@patch("tools.browser_camofox.requests.post")
def test_file_already_in_root_is_not_copied(mock_post, monkeypatch, tmp_path):
    monkeypatch.setenv("CAMOFOX_URL", "http://localhost:9377")
    root = tmp_path / "uploads"
    root.mkdir()
    monkeypatch.setenv("CAMOFOX_UPLOADS_DIR", str(root))
    inside = root / "a.pdf"
    inside.write_bytes(b"%PDF")
    _open_tab(mock_post, "up2")

    mock_post.side_effect = [_resp({"result": 0}), _resp({"ok": True, "via": "filechooser"})]
    result = json.loads(camofox_upload(
        [str(inside)], selector="iframe >> internal:control=enter-frame >> button", task_id="up2"))
    assert result["success"] is True

    body = mock_post.call_args.kwargs["json"]
    assert "path" not in body
    assert body["files"] == [{"name": "a.pdf", "mimeType": "application/pdf", "dataBase64": "JVBERg=="}]
    assert body["selector"].startswith("iframe")
    assert not (root / "hermes").exists()


@patch("tools.browser_camofox.requests.post")
def test_rejects_missing_file_without_calling_server(mock_post, monkeypatch, tmp_path):
    monkeypatch.setenv("CAMOFOX_URL", "http://localhost:9377")
    monkeypatch.setenv("CAMOFOX_UPLOADS_DIR", str(tmp_path))
    _open_tab(mock_post, "up3")
    calls = mock_post.call_count

    result = json.loads(camofox_upload([str(tmp_path / "nope.png")], task_id="up3"))

    assert result.get("success") is False
    assert mock_post.call_count == calls


@patch("tools.browser_camofox.requests.post")
def test_rejects_denied_credential_path(mock_post, monkeypatch, tmp_path):
    monkeypatch.setenv("CAMOFOX_URL", "http://localhost:9377")
    monkeypatch.setenv("CAMOFOX_UPLOADS_DIR", str(tmp_path / "uploads"))
    env_file = tmp_path / ".env"
    env_file.write_text("X=1")
    _open_tab(mock_post, "up4")
    calls = mock_post.call_count

    result = json.loads(camofox_upload([str(env_file)], task_id="up4"))

    assert result.get("success") is False
    assert "denied" in json.dumps(result).lower()
    assert mock_post.call_count == calls


@patch("tools.browser_camofox.requests.post")
def test_named_trigger_refused_when_top_page_has_file_input(mock_post, monkeypatch, tmp_path):
    monkeypatch.setenv("CAMOFOX_URL", "http://localhost:9377")
    monkeypatch.setenv("CAMOFOX_UPLOADS_DIR", str(tmp_path / "uploads"))
    src = tmp_path / "logo.png"
    src.write_bytes(b"x")
    _open_tab(mock_post, "up5")

    mock_post.return_value = _resp({"ok": True, "result": 1})
    result = json.loads(camofox_upload([str(src)], selector="iframe >> internal:control=enter-frame >> #up",
                                       task_id="up5"))

    assert result.get("success") is False
    assert "file input" in result["error"]
    assert not mock_post.call_args.args[0].endswith("/upload")
    # The count must pierce open shadow roots, matching Playwright's locator semantics.
    assert "shadowRoot" in mock_post.call_args.kwargs["json"]["expression"]


@pytest.mark.parametrize("trigger", [
    {"ref": "@e7"},
    {"selector": "iframe >> internal:control=enter-frame >> #up"},
])
@pytest.mark.parametrize("evaluate_reply", [
    405, 501, 404, {}, {"result": None}, {"result": "0"}, {"result": False},
    {"result": -1}, {"result": 0.0},
])
@patch("tools.browser_camofox.requests.post")
def test_named_trigger_refused_when_top_level_count_unknown(
        mock_post, monkeypatch, tmp_path, trigger, evaluate_reply):
    from tools.registry import registry
    import tools.browser_tool  # noqa: F401 (registers real upload dispatch)

    monkeypatch.setenv("CAMOFOX_URL", "http://localhost:9377")
    monkeypatch.setenv("CAMOFOX_UPLOADS_DIR", str(tmp_path / "uploads"))
    src = tmp_path / "logo.png"
    src.write_bytes(b"x")
    _open_tab(mock_post, "unknown-count")
    mock_post.reset_mock()

    if isinstance(evaluate_reply, int):
        response = _resp({"error": "evaluate route unavailable"})
        response.status_code = evaluate_reply
        response.raise_for_status.side_effect = requests.HTTPError(response=response)
    else:
        response = _resp(evaluate_reply)
    mock_post.side_effect = [response, _resp({"ok": True, "via": "direct_input"})]
    raw = registry.dispatch(
        "browser_upload", {"paths": [str(src)], **trigger}, task_id="unknown-count")
    result = json.loads(raw) if isinstance(raw, str) else raw

    assert result["success"] is False
    assert "cannot verify" in result["error"].lower()
    assert "top-level" in result["error"] and "named trigger" in result["error"]
    assert "first" in result["error"] and "another upload route" in result["error"]
    assert len(mock_post.call_args_list) == 1
    assert mock_post.call_args.args[0].endswith("/evaluate")


@patch("tools.browser_camofox.requests.post")
def test_upload_without_trigger_does_not_require_count(mock_post, monkeypatch, tmp_path):
    monkeypatch.setenv("CAMOFOX_URL", "http://localhost:9377")
    monkeypatch.setenv("CAMOFOX_UPLOADS_DIR", str(tmp_path / "uploads"))
    src = tmp_path / "logo.png"
    src.write_bytes(b"x")
    _open_tab(mock_post, "default-upload")
    mock_post.reset_mock()
    mock_post.return_value = _resp({"ok": True, "via": "direct_input"})

    result = json.loads(camofox_upload([str(src)], task_id="default-upload"))

    assert result["success"] is True and result["via"] == "direct_input"
    assert len(mock_post.call_args_list) == 1
    assert mock_post.call_args.args[0].endswith("/upload")
    assert "ref" not in mock_post.call_args.kwargs["json"]
    assert "selector" not in mock_post.call_args.kwargs["json"]


@patch("tools.browser_camofox.requests.post")
def test_old_server_rejection_never_replays_and_names_server_first_order(mock_post, monkeypatch, tmp_path):
    import requests

    monkeypatch.setenv("CAMOFOX_URL", "http://localhost:9377")
    root = tmp_path / "hermes-side-uploads"
    monkeypatch.setenv("CAMOFOX_UPLOADS_DIR", str(root))
    src = tmp_path / "logo.png"
    src.write_bytes(b"x")
    _open_tab(mock_post, "up6")

    rejected = MagicMock()
    rejected.status_code = 400
    rejected.json.return_value = {"error": "file not found in upload directory", "code": "file_not_found"}
    rejected.raise_for_status.side_effect = requests.HTTPError("400 Client Error: Bad Request", response=rejected)
    mock_post.return_value = rejected
    result = json.loads(camofox_upload([str(src)], task_id="up6"))

    assert result.get("success") is False
    assert "installed before this client" in result["error"]
    assert "inspect" in result["error"]
    assert not root.exists()
    assert len([c for c in mock_post.call_args_list if c.args[0].endswith("/upload")]) == 1


@patch("tools.browser_camofox.requests.post")
def test_nt_namespace_path_rejected_before_resolve(mock_post, monkeypatch, tmp_path):
    from pathlib import Path
    monkeypatch.setenv("CAMOFOX_URL", "http://localhost:9377")
    monkeypatch.setenv("CAMOFOX_UPLOADS_DIR", str(tmp_path / "uploads"))
    _open_tab(mock_post, "up6")
    calls = mock_post.call_count

    def boom(self, *a, **k):
        raise AssertionError(f"resolve() reached for {self}")
    monkeypatch.setattr(Path, "resolve", boom)
    result = json.loads(camofox_upload(["\\\\?\\UNC\\host\\share\\x.png"], task_id="up6"))

    assert result.get("success") is False
    assert mock_post.call_count == calls


def test_registered_only_for_camofox_and_in_browser_toolset(monkeypatch):
    import toolsets
    from tools.registry import registry
    import tools.browser_tool  # noqa: F401  (registers browser tools)

    assert "browser_upload" in toolsets.TOOLSETS["browser"]["tools"]
    entry = registry.get_entry("browser_upload")
    assert entry is not None and entry.check_fn is not None
    monkeypatch.delenv("CAMOFOX_URL", raising=False)
    assert not entry.check_fn()
    monkeypatch.setenv("CAMOFOX_URL", "http://localhost:9377")
    assert entry.check_fn()
