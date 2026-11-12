"""browser_exec's schema rewrite is a cache-only, session-scoped metadata read."""
from pathlib import Path
import subprocess
import sys

import pytest

from tests.e2e.core.providers._native_helpers import make_home


_COLD_SCHEMA = '''import os, socket, threading, time
original = socket.getaddrinfo
entered = threading.Event()
def resolve(host, *args, **kwargs):
    if host == "models.dev":
        entered.set()
        threading.Event().wait()
        raise OSError("catalog DNS unavailable")
    return original(host, *args, **kwargs)
socket.getaddrinfo = resolve
from agent import models_dev
from model_tools import _rewrite_browser_exec
models_dev._start_background_refresh_models_dev()
assert entered.wait(10), "catalog warmer did not enter DNS"
def stuck():
    print("BROWSER_EXEC_WAITED_FOR_CATALOG_DNS", flush=True)
    os._exit(86)
watchdog = threading.Timer(3, stuck)
watchdog.daemon = True
watchdog.start()
try:
    started = time.monotonic()
    td = {"type": "function", "function": {"name": "browser_exec", "description": "Browser"}}
    result = _rewrite_browser_exec(td, {"terminal", "vision_analyze", "browser_exec"})
    elapsed = time.monotonic() - started
    assert elapsed < 2, f"schema rewrite waited {elapsed:.2f}s"
    assert "vision_analyze" in result["function"]["description"], result
    print("BROWSER_EXEC_SCHEMA_READY", elapsed, flush=True)
finally:
    watchdog.cancel()
'''


def test_browser_exec_schema_returns_while_catalog_dns_is_blocked(tmp_path):
    home = make_home(tmp_path, {
        "provider": "openai", "default": "offline-schema-fixture", "base_url": "http://127.0.0.1:1/v1",
    }, env_file={"OPENAI_API_KEY": "loopback-only"})
    result = subprocess.run(
        [sys.executable, "-c", _COLD_SCHEMA], cwd=Path(__file__).resolve().parents[2],
        env=home.env(), capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "BROWSER_EXEC_SCHEMA_READY" in result.stdout


@pytest.mark.parametrize(
    "available,native,expected",
    [
        ({"browser_exec", "vision_analyze"}, False, None),
        ({"browser_exec", "terminal"}, False, False),
        ({"browser_exec", "terminal", "vision_analyze"}, False, True),
        ({"browser_exec", "terminal", "vision_analyze"}, True, False),
    ],
)
def test_browser_exec_names_only_available_non_native_vision(monkeypatch, available, native, expected):
    from model_tools import _rewrite_browser_exec
    from tools import vision_tools

    policies = []

    def native_vision(*, allow_network=True):
        policies.append(allow_network)
        return native

    monkeypatch.setattr(vision_tools, "_should_use_native_vision_fast_path", native_vision)
    td = {"type": "function", "function": {"name": "browser_exec", "description": "Browser"}}
    result = _rewrite_browser_exec(td, available)
    if expected is None:
        assert result is None
    else:
        assert result is not None
        assert ("vision_analyze" in result["function"]["description"]) is expected
    assert policies == ([False] if {"terminal", "vision_analyze"} <= available else [])
