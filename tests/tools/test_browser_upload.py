"""browser_upload: Camofox file-chooser attach, staging into the uploads root, and path safety."""

import json
from unittest.mock import MagicMock, patch

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
def test_stages_outside_file_into_uploads_root_and_posts_ref(mock_post, monkeypatch, tmp_path):
    monkeypatch.setenv("CAMOFOX_URL", "http://localhost:9377")
    root = tmp_path / "uploads"
    root.mkdir()
    monkeypatch.setenv("CAMOFOX_UPLOADS_DIR", str(root))
    src = tmp_path / "logo.png"
    src.write_bytes(b"png-bytes")
    _open_tab(mock_post, "up1")

    mock_post.return_value = _resp({"ok": True, "attached": ["x"], "via": "filechooser"})
    result = json.loads(camofox_upload([str(src)], ref="@e7", task_id="up1"))

    assert result["success"] is True
    assert result["via"] == "filechooser"
    assert result["attached"] == ["logo.png"]
    url = mock_post.call_args.args[0]
    body = mock_post.call_args.kwargs["json"]
    assert url.endswith("/tabs/tab-up1/upload")
    assert body["ref"] == "e7" and "selector" not in body
    (staged,) = body["path"]
    assert staged.startswith(str(root.resolve()))
    # The page sees the staged file's basename as File.name, so it must be the original name.
    assert staged.rsplit("/", 1)[-1] == "logo.png"
    with open(staged, "rb") as fh:
        assert fh.read() == src.read_bytes()


@patch("tools.browser_camofox.requests.post")
def test_file_already_in_root_is_not_copied(mock_post, monkeypatch, tmp_path):
    monkeypatch.setenv("CAMOFOX_URL", "http://localhost:9377")
    root = tmp_path / "uploads"
    root.mkdir()
    monkeypatch.setenv("CAMOFOX_UPLOADS_DIR", str(root))
    inside = root / "a.pdf"
    inside.write_bytes(b"%PDF")
    _open_tab(mock_post, "up2")

    mock_post.return_value = _resp({"ok": True, "via": "direct_input"})
    json.loads(camofox_upload([str(inside)], selector="iframe >> internal:control=enter-frame >> button",
                              task_id="up2"))

    body = mock_post.call_args.kwargs["json"]
    assert body["path"] == [str(inside.resolve())]
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


@patch("tools.browser_camofox.requests.post")
def test_server_root_mismatch_names_staging_dir_and_setting(mock_post, monkeypatch, tmp_path):
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
    assert str(root) in result["error"]
    assert "browser.camofox.uploads_dir" in result["error"]


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


def test_failed_staging_copy_leaves_nothing_reusable(monkeypatch, tmp_path):
    import shutil
    from tools import browser_camofox

    root = tmp_path / "uploads"
    root.mkdir()
    src = tmp_path / "logo.png"
    src.write_bytes(b"complete-bytes" * 100)
    real_copy = shutil.copyfile

    def partial_then_fail(a, b, *args, **kwargs):
        with open(b, "wb") as fh:
            fh.write(b"partial")
        raise OSError("disk full")

    monkeypatch.setattr(shutil, "copyfile", partial_then_fail)
    try:
        browser_camofox._stage_upload_file(str(src), str(root))
    except OSError:
        pass
    else:
        raise AssertionError("expected the failed copy to raise")
    assert [p for p in (root / "hermes").rglob("*") if p.is_file()] == []

    monkeypatch.setattr(shutil, "copyfile", real_copy)
    staged = browser_camofox._stage_upload_file(str(src), str(root))
    with open(staged, "rb") as fh:
        assert fh.read() == src.read_bytes()
    assert staged.rsplit("/", 1)[-1] == "logo.png"


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
