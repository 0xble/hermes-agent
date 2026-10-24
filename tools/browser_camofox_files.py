"""Byte transport for Camofox peers that do not share Hermes' filesystem."""

from __future__ import annotations

import base64
import json
import mimetypes
import shutil
import tempfile
import unicodedata
from pathlib import Path

import requests

from tools.registry import tool_error

MAX_FILE_BYTES = 20 * 1024 * 1024
MAX_RESPONSE_BYTES = 28 * 1024 * 1024


def _basename(value: object, *, portable: bool = False) -> str:
    forbidden = '/\\:*?"<>|' if portable else '/\\'
    if (not isinstance(value, str) or not value or value in (".", "..")
            or any(c in value for c in forbidden) or any(unicodedata.category(c) == "Cc" for c in value)):
        raise ValueError("Invalid file name from Camofox")
    return value


def _upload_payloads(paths: list) -> list[dict]:
    from agent.file_safety import get_read_block_error

    if not paths:
        raise ValueError("paths must name at least one local file")
    files = []
    total = 0
    for raw in paths:
        # Check before resolving: NT/device paths can trigger network authentication on resolution.
        blocked = get_read_block_error(str(raw))
        if blocked:
            raise ValueError(blocked)
        source = Path(str(raw)).expanduser().resolve()
        blocked = get_read_block_error(str(source))
        if blocked:
            raise ValueError(blocked)
        if not source.is_file():
            raise ValueError(f"Not a file: {raw}")
        name = _basename(source.name)
        remaining = MAX_FILE_BYTES - total
        if source.stat().st_size > remaining:
            raise ValueError("Uploads exceed the 20 MiB aggregate byte limit")
        with source.open("rb") as handle:
            content = handle.read(remaining + 1)
        total += len(content)
        if total > MAX_FILE_BYTES:
            raise ValueError("Uploads exceed the 20 MiB aggregate byte limit")
        files.append({"name": name, "mimeType": mimetypes.guess_type(name)[0] or "application/octet-stream",
                      "dataBase64": base64.b64encode(content).decode("ascii")})
    return files


def upload(paths: list, ref=None, selector=None, task_id=None) -> str:
    """Send local bytes exactly once, with the existing named-trigger bypass guard."""
    from tools import browser_camofox as browser

    try:
        files = _upload_payloads(paths)
    except (OSError, ValueError) as exc:
        return tool_error(str(exc), success=False)

    def body(session):
        if ref or selector:
            try:
                count = browser._post(browser._tab_path(session, "evaluate"), {
                    "userId": session["user_id"], "expression": browser._COUNT_TOP_FILE_INPUTS_JS}).get("result")
            except requests.HTTPError as exc:
                if browser.classify_camofox_http_error(exc, endpoint="evaluate") != "capability":
                    raise
                count = None
            if not isinstance(count, int) or isinstance(count, bool) or count < 0:
                return tool_error(
                    "Cannot verify the top-level page's file-input count; Camofox may attach to "
                    "the first top-level input instead of the named trigger. No upload was sent. "
                    "Use a server with a working /evaluate route, omit ref/selector only if the "
                    "first top-level input is the intended target, or use another upload route.",
                    success=False)
            if count > 0:
                return tool_error(
                    f"The top-level page has {count} file input(s); Camofox would attach to the first "
                    "one instead of the named trigger. Omit ref/selector if that input is the target; "
                    "otherwise use another upload route.", success=False)
        payload = {"userId": session["user_id"], "files": files}
        if ref:
            payload["ref"] = ref.lstrip("@")
        elif selector:
            payload["selector"] = selector
        if len(json.dumps(payload).encode("utf-8")) > MAX_RESPONSE_BYTES:
            return tool_error("Upload JSON exceeds the 28 MiB transport limit", success=False)
        try:
            # No path fallback or redirect replay: failure after dispatch may already have attached files.
            data = browser._post(browser._tab_path(session, "upload"), payload,
                                 timeout=max(browser._get_command_timeout(), browser._UPLOAD_HTTP_TIMEOUT_S),
                                 allow_redirects=False)
        except (requests.RequestException, ValueError, RuntimeError) as exc:
            if isinstance(exc, requests.HTTPError):
                browser._clear_stale_tab(session, exc)
            return tool_error(
                "Camofox upload was not confirmed. It may already have attached the files; inspect the page "
                "before retrying. Inline uploads require the matching server version, installed before this client.",
                success=False)
        if not isinstance(data, dict) or data.get("ok") is not True:
            return tool_error("Camofox did not confirm the upload; inspect the page before retrying", success=False)
        return json.dumps({"success": True, "attached": [f["name"] for f in files],
                           "via": data.get("via") if data.get("via") in ("direct_input", "panel_input", "filechooser") else None,
                           "note": "Take a fresh snapshot, save, then read back to verify the persisted file."})
    return browser._with_tab(task_id, "attach files", body)


def _bounded_response_content(response: requests.Response) -> bytes:
    content = bytearray()
    for chunk in response.iter_content(chunk_size=64 * 1024):
        if len(content) + len(chunk) > MAX_RESPONSE_BYTES:
            raise ValueError("Camofox download response exceeds the 28 MiB transport limit")
        content.extend(chunk)
    return bytes(content)


def _download_response(session: dict, include_data: bool) -> list[dict]:
    from tools import browser_camofox as browser

    params = {**browser._user_params(session), "includeData": "true" if include_data else "false",
              "maxBytes": MAX_FILE_BYTES}
    try:
        response = browser._request("get", browser._tab_path(session, "downloads"), params=params, stream=True,
                                    allow_redirects=False)
    except requests.HTTPError as exc:
        if exc.response is not None:
            content = b""
            try:
                content = _bounded_response_content(exc.response)
            finally:
                # Keep only bounded bytes readable by stale-tab recovery after cleanup.
                exc.response._content = content
                browser._clear_stale_tab(session, exc)
                exc.response.close()
        raise
    try:
        if response.status_code != 200:
            raise ValueError("Camofox did not return downloads")
        content = _bounded_response_content(response)
        try:
            data = json.loads(content)
        except (ValueError, UnicodeError):
            raise ValueError("Invalid Camofox download response") from None
        if (not isinstance(data, dict) or data.get("tabId") != session["tab_id"]
                or not isinstance(data.get("downloads"), list) or len(data["downloads"]) > 20):
            raise ValueError("Invalid Camofox download response")
        return data["downloads"]
    finally:
        response.close()


def _download_metadata(row: object) -> dict:
    if not isinstance(row, dict):
        raise ValueError("Invalid Camofox download metadata")
    identifier, size, mime = row.get("id"), row.get("bytes"), row.get("mimeType")
    if (not isinstance(identifier, str) or not identifier or len(identifier) > 200
            or (size is not None and (type(size) is not int or size < 0))
            or not isinstance(mime, str) or len(mime) > 200):
        raise ValueError("Invalid Camofox download metadata")
    return {"id": identifier, "name": _basename(row.get("suggestedFilename"), portable=True), "bytes": size,
            "mime_type": mime, "failed": row.get("failure") is not None}


def _download_content(row: dict, item: dict) -> bytes:
    encoded = row.get("dataBase64")
    if (item["failed"] or type(item["bytes"]) is not int or item["bytes"] > MAX_FILE_BYTES
            or not isinstance(encoded, str) or row.get("dataSkipped") is not None
            or row.get("readError") is not None):
        raise ValueError("Download content is unavailable, failed, or exceeds the 20 MiB byte limit")
    if len(encoded) > 4 * ((MAX_FILE_BYTES + 2) // 3):
        raise ValueError("Download content exceeds the 20 MiB byte limit")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, UnicodeError):
        raise ValueError("Invalid Camofox download encoding") from None
    if len(raw) != item["bytes"] or base64.b64encode(raw).decode("ascii") != encoded:
        raise ValueError("Camofox download content does not match its size or canonical encoding")
    return raw


def _save_download(name: str, raw: bytes) -> Path:
    from hermes_constants import get_hermes_home

    root = get_hermes_home() / "browser_downloads"
    root.mkdir(parents=True, exist_ok=True)
    directory = Path(tempfile.mkdtemp(prefix="download-", dir=root))
    try:
        target = directory / name
        with target.open("xb") as handle:
            handle.write(raw)
    except OSError:
        shutil.rmtree(directory)
        raise
    return target


def downloads(download_id=None, task_id=None) -> str:
    """List captured files, or import one into a unique profile-local artifact directory."""
    from tools import browser_camofox as browser

    if download_id is not None and (not isinstance(download_id, str) or not download_id):
        return tool_error("download_id must be a nonempty string", success=False)

    def body(session):
        try:
            rows = _download_response(session, include_data=download_id is not None)
            metadata = [_download_metadata(row) for row in rows]
            ids = [item["id"] for item in metadata]
            if len(ids) != len(set(ids)):
                raise ValueError("Duplicate Camofox download IDs")
            if download_id is None:
                return json.dumps({"success": True, "downloads": metadata, "count": len(metadata)})
            if download_id not in ids:
                raise ValueError("Download ID not found in the current tab")
            index = ids.index(download_id)
            item, row = metadata[index], rows[index]
            raw = _download_content(row, item)
            target = _save_download(item["name"], raw)
            return json.dumps({"success": True, "download_id": download_id, "output_path": str(target),
                               "name": item["name"], "mime_type": item["mime_type"], "bytes": len(raw)})
        except ValueError as exc:
            return tool_error(str(exc), success=False)
        except requests.RequestException:
            raise
        except OSError:
            return tool_error("Could not save the download on the Python host", success=False)
    return browser._with_tab(task_id, "retrieve downloads", body)
