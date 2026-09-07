"""Minimal loopback CDP client for managed-browser window visibility."""
from __future__ import annotations

import json
import os
import socket
import time
from contextlib import AbstractContextManager
from urllib.parse import urlparse
from urllib.request import urlopen


class CdpHandoffError(RuntimeError):
    pass


class HandoffCDP(AbstractContextManager):
    def __init__(self, endpoint: str, *, ws_factory=None):
        parsed = urlparse(endpoint)
        if parsed.scheme == "http":
            if parsed.hostname not in {"127.0.0.1", "::1", "localhost"}:
                raise CdpHandoffError("CDP endpoint must be loopback")
            with urlopen(endpoint.rstrip("/") + "/json/version", timeout=3) as response:
                endpoint = json.loads(response.read())["webSocketDebuggerUrl"]
            parsed = urlparse(endpoint)
        if parsed.scheme != "ws" or parsed.hostname not in {"127.0.0.1", "::1", "localhost"}:
            raise CdpHandoffError("CDP endpoint must be loopback")
        self.endpoint = endpoint
        if ws_factory is None:
            try:
                from websockets.sync.client import connect
            except ImportError as exc:
                raise CdpHandoffError("websockets is required for browser visibility") from exc
            ws_factory = lambda url, timeout: connect(url, open_timeout=timeout, close_timeout=timeout)
        self._socket = ws_factory(endpoint, timeout=3)
        self._next_id = 1

    def __exit__(self, *args):
        self.close()

    def close(self) -> None:
        self._socket.close()

    def call(self, method: str, params: dict | None = None, *, session_id: str | None = None) -> dict:
        request_id = self._next_id
        self._next_id += 1
        request = {"id": request_id, "method": method, "params": params or {}}
        if session_id is not None:
            request["sessionId"] = session_id
        self._socket.send(json.dumps(request))
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            try:
                raw = self._socket.recv(timeout=max(0.0, deadline - time.monotonic()))
            except (socket.timeout, TimeoutError) as exc:
                raise CdpHandoffError(f"CDP call timed out: {method}") from exc
            message = json.loads(raw)
            if message.get("id") != request_id:
                continue
            if "error" in message:
                raise CdpHandoffError(f"CDP call failed: {method}")
            return message.get("result", {})
        raise CdpHandoffError(f"CDP call timed out: {method}")

    def set_visibility(self, action: str) -> dict:
        if action not in {"reveal", "minimize"}:
            raise CdpHandoffError("Unknown visibility action")
        target_infos = self.call("Target.getTargets").get("targetInfos", [])
        pages = [target for target in target_infos if target.get("type") == "page"]
        if not pages:
            raise CdpHandoffError("Managed browser has no page window to change")
        window_ids = set()
        for page in pages:
            target_id = page.get("targetId")
            if not target_id:
                raise CdpHandoffError("Managed browser page target is invalid")
            window_id = self.call("Browser.getWindowForTarget", {"targetId": target_id}).get("windowId")
            if not isinstance(window_id, int):
                raise CdpHandoffError("Managed browser window is unavailable")
            window_ids.add(window_id)
        if len(window_ids) != 1:
            raise CdpHandoffError("Managed browser has multiple page windows; visibility was not changed")
        window_id = window_ids.pop()
        state = "normal" if action == "reveal" else "minimized"
        self.call("Browser.setWindowBounds", {"windowId": window_id, "bounds": {"windowState": state}})
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            observed = self.call("Browser.getWindowBounds", {"windowId": window_id}).get("bounds", {})
            if observed.get("windowState") == state:
                return {"window_id": window_id, "window_state": state}
            time.sleep(0.05)
        raise CdpHandoffError("Managed browser did not confirm requested window visibility")

    def assert_owned_headed_command_line(self, copy_dir: str) -> None:
        """Bind this CDP listener to the expected owned process, not just its port file."""
        import psutil

        processes = self.call("SystemInfo.getProcessInfo").get("processInfo", [])
        browsers = [item for item in processes if item.get("type") == "browser"]
        if len(browsers) != 1 or not isinstance(browsers[0].get("id"), int):
            raise CdpHandoffError("CDP cannot prove managed browser process ownership")
        try:
            arguments = psutil.Process(browsers[0]["id"]).cmdline()
        except (psutil.Error, OSError) as exc:
            raise CdpHandoffError("Cannot inspect managed browser process ownership") from exc
        if not isinstance(arguments, list) or not all(isinstance(arg, str) for arg in arguments):
            raise CdpHandoffError("CDP cannot prove managed browser command-line ownership")
        expected = os.path.realpath(copy_dir)
        user_data_dirs = [os.path.realpath(arg.split("=", 1)[1]) for arg in arguments if arg.startswith("--user-data-dir=")]
        if user_data_dirs != [expected]:
            raise CdpHandoffError("CDP listener is not the expected managed browser")
        if any(arg == "--headless" or arg.startswith("--headless=") for arg in arguments):
            raise CdpHandoffError("CDP listener is headless; visibility was not changed")
