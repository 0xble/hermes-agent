"""Configured provider probes keep credentials out of argv, output and logs."""

import json
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


def test_probe_refuses_last_known_good_backup_without_contacting_provider(tmp_path):
    key = "backup-fixture-credential"
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append(self.path)
            self.send_response(200)
            self.end_headers()

        def log_message(self, format, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    config_path = tmp_path / "config.yaml"
    config_path.write_text(json.dumps({
        "model": {"provider": "fixture"}, "providers": {"fixture": {
            "base_url": f"http://127.0.0.1:{server.server_port}/backup", "api_key": key,
        }},
    }))
    env = os.environ.copy()
    env["HERMES_HOME"] = str(tmp_path)
    try:
        # A successful real load leaves the good backup used by a fresh process.
        seed = subprocess.run([sys.executable, "-c", """
from hermes_cli.config import load_config_readonly
assert load_config_readonly()['model']['provider'] == 'fixture'
"""], cwd=ROOT, env=env, capture_output=True, text=True, timeout=30)
        assert seed.returncode == 0, "valid config did not load"
        assert list((tmp_path / "backups" / "config").glob("config.yaml.good.*"))
        config_path.write_text(f"model: [{key}\n")
        result = _run(tmp_path, key, "config")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    assert requests == [], "probe contacted the stale backup endpoint"
    assert result.returncode == 2, result.stdout + result.stderr
    assert "configuration" in result.stdout


@pytest.mark.parametrize("conflict", ["endpoint", "credential", "duplicate"])
def test_probe_matches_runtime_custom_resolution(tmp_path, conflict):
    """The diagnostic and the real runtime must use the same selected route."""
    key = "fresh-fixture-credential"
    stale_key = "stale-fixture-credential"
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            requests.append((self.path, self.headers.get("Authorization")))
            self.send_response(200)
            self.end_headers()

        def log_message(self, format, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_port}/runtime"
    entry = {"base_url": base_url, "api_key": key}
    config = {"model": {"provider": "fixture"}, "providers": {"fixture": entry}}
    if conflict == "endpoint":
        config["custom_providers"] = [{"name": "fixture", "base_url": base_url + "/legacy", "api_key": key}]
    elif conflict == "duplicate":
        config["custom_providers"] = [{"name": "fixture", "base_url": base_url, "api_key": stale_key}]
    else:
        entry.update(api_key=stale_key, key_env="PROBE_TEST_KEY")
    (tmp_path / "config.yaml").write_text(json.dumps(config))
    try:
        result = _run(tmp_path, key, "env")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    # Resolve AFTER the probe: the runtime may seed/persist its pool, whereas
    # the status-only diagnostic must not create credential state.
    assert not (tmp_path / "auth.json").exists()
    env = os.environ.copy()
    env.update(HERMES_HOME=str(tmp_path), PROBE_TEST_KEY=key,
               PROBE_TEST_BASE_URL=base_url)
    runtime = subprocess.run([sys.executable, "-c", """
import os
from unittest.mock import patch
from hermes_cli.runtime_provider import resolve_runtime_provider
# The probe's supported boundary is static config, not pool rotation/seeding.
# Keep all real provider/credential lookup in the runtime, disabling only that
# unsupported stateful rung in this reference resolution.
with patch('hermes_cli.runtime_provider._try_resolve_from_custom_pool', return_value=None):
    runtime = resolve_runtime_provider(requested='fixture')
assert runtime['base_url'] == os.environ['PROBE_TEST_BASE_URL']
assert runtime['api_key'] == os.environ['PROBE_TEST_KEY']
"""], cwd=ROOT, env=env, capture_output=True, text=True, timeout=30)
    assert runtime.returncode == 0, "runtime fixture did not resolve the expected route"
    assert stale_key not in result.stdout + result.stderr + (tmp_path / "probe.log").read_text()
    assert result.returncode == 0, result.stdout + result.stderr
    assert requests == [("/runtime/models", f"Bearer {key}")]


def test_probe_refuses_persisted_credential_pools_without_mutating_them(tmp_path):
    key = "configured-fixture-credential"
    config = {"model": {"provider": "fixture"}, "providers": {"fixture": {
        "base_url": "http://127.0.0.1:1/v1", "api_key": key,
    }}}
    (tmp_path / "config.yaml").write_text(json.dumps(config))
    auth_path = tmp_path / "auth.json"
    auth_text = json.dumps({"credential_pool": {"fixture": [{
        "id": "fixture-pool", "access_token": "unsupported-pool-credential",
    }]}})
    auth_path.write_text(auth_text)
    result = _run(tmp_path, key, "config")
    assert result.returncode == 2, result.stdout + result.stderr
    assert "configuration" in result.stdout
    assert auth_path.read_text() == auth_text


@pytest.mark.parametrize("shape", ["providers", "custom_providers"])
@pytest.mark.parametrize("unsupported", [
    {"key_cmd": "echo unsafe"},
    {"extra_headers": {"X-Fixture-Key": "unsupported-header-credential"}},
    {"api_mode": "unknown_transport"},
])
def test_runtime_lookup_does_not_bypass_probe_safety_guards(tmp_path, shape, unsupported):
    key = "configured-fixture-credential"
    entry = {"base_url": "http://127.0.0.1:1/v1", "api_key": key, **unsupported}
    config: dict = {"model": {"provider": "fixture"}}
    config[shape] = {"fixture": entry} if shape == "providers" else [{"name": "fixture", **entry}]
    (tmp_path / "config.yaml").write_text(json.dumps(config))
    result = _run(tmp_path, key, "config")
    assert result.returncode == 2, result.stdout + result.stderr
    assert "configuration" in result.stdout
