"""Configured provider probes keep credentials out of argv, output and logs."""

import json
import logging
import os
from pathlib import Path
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread

import psutil
import pytest


ROOT = Path(__file__).resolve().parents[2]
BOOTSTRAP = """
import logging, os, runpy, sys
logging.basicConfig(filename=os.environ['PROBE_TEST_LOG'], level=logging.DEBUG)
sys.argv = ['provider-probe']
runpy.run_path('scripts/probe_provider.py', run_name='__main__')
"""


def _run(home, key, source):
    env = os.environ.copy()
    env.update(HERMES_HOME=str(home), PROBE_TEST_LOG=str(home / "probe.log"),
               HERMES_REDACT_SECRETS="false")
    env.pop("PROBE_TEST_KEY", None)
    if source == "env":
        env["PROBE_TEST_KEY"] = key
    command = [sys.executable, "-c", BOOTSTRAP]
    result = subprocess.run(command, cwd=ROOT, env=env, capture_output=True,
                            text=True, timeout=30)
    assert key not in " ".join(command)
    output = result.stdout + result.stderr + (home / "probe.log").read_text()
    assert key not in output
    assert "sk-reflected-only-1234567890" not in output
    return result


@pytest.mark.parametrize("status,source,shape", [
    (200, "env", "providers"), (401, "config", "custom_providers"),
    (500, "dotenv", "model"), (302, "env", "providers"),
    (0, "config", "model"),
])
def test_probe_uses_configured_endpoint_without_disclosing_credentials(tmp_path, status, source, shape):
    key = "opaque/probe+credential=" if source != "dotenv" else "sk-probe-fixture-1234567890"
    requests = []
    process_ids = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append((self.path, self.headers.get("Authorization")))
            for process in psutil.Process().children():
                if BOOTSTRAP in process.cmdline():
                    process_ids.append(process.pid)
                    assert key not in " ".join(process.cmdline())
            if status == 0:
                self.connection.close()
                return
            self.send_response(status, message=f"{key} sk-reflected-only-1234567890")
            self.send_header("Location", f"http://127.0.0.1:{self.server.server_port}/trap?api_key={key}")
            self.end_headers()
            self.wfile.write(f"{key} sk-reflected-only-1234567890".encode())

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    entry = {
        "base_url": f"http://127.0.0.1:{server.server_port}/v1",
        "sk-reflected-only-1234567890": "unrecognized config key",
    }
    entry.update({"api_key": key} if source == "config" else {"key_env": "PROBE_TEST_KEY"})
    config = {"model": {"provider": "fixture"}, "security": {"redact_secrets": False}}
    if shape == "model":
        config["model"].update(entry, provider="custom")
    elif shape == "providers":
        config["providers"] = {"fixture": entry}
    else:
        config["custom_providers"] = [{"name": "fixture", **entry}]
    (tmp_path / "config.yaml").write_text(json.dumps(config))
    if source == "dotenv":
        (tmp_path / ".env").write_text(f"PROBE_TEST_KEY={key}\n")
    try:
        result = _run(tmp_path, key, source)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert result.returncode == (0 if status == 200 else 1), result.stdout + result.stderr
    assert (f"HTTP {status}" if status else "network error") in result.stdout
    assert requests == [("/v1/models", f"Bearer {key}")]
    assert process_ids, "probe process argv was not observed during the HTTP request"


@pytest.mark.parametrize("entry", [
    {"base_url": "http://127.0.0.1:1/v1?api_key=sk-reflected-only-1234567890"},
    {"base_url": "http://user:password@127.0.0.1:1/v1"},
    {"base_url": "http://127.0.0.1:1/v1", "key_cmd": "echo unsafe"},
    {"base_url": "http://127.0.0.1:1/v1", "enabled": False},
    {"base_url": "http://127.0.0.1:1/v1", "api_mode": "anthropic_messages"},
    {"base_url": "http://127.0.0.1:1/v1", "api_key": ""},
    {"base_url": "http://example.invalid/v1"},
    {"base_url": "ftp://127.0.0.1/v1"},
    {"base_url": "http://127.0.0.1:1/opaque-probe-credential"},
    {},
    None,  # YAML parsing can reflect arbitrary secret text in its warning.
])
def test_probe_refuses_unsafe_or_unsupported_configuration(tmp_path, entry):
    key = "opaque-probe-credential"
    config_text = f"model: [{key}\n" if entry is None else json.dumps({
        "model": {"provider": "fixture"},
        "providers": {"fixture": {"api_key": key, **entry}},
    })
    (tmp_path / "config.yaml").write_text(config_text)
    result = _run(tmp_path, key, "config")
    assert result.returncode == 2, result.stdout + result.stderr
    assert "configuration" in result.stdout
