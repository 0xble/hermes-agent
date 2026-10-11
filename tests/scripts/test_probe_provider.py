"""Configured provider probes keep credentials out of argv, output and logs."""

import json
import os
from pathlib import Path
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from socketserver import BaseRequestHandler, ThreadingTCPServer
from threading import Thread

import psutil
import pytest


ROOT = Path(__file__).resolve().parents[2]
BOOTSTRAP = """
import logging, os, runpy, socket, sys
# Hermetic: every hostname resolves to loopback, so host-detected routes reach
# only the test's own listener and never a real provider.
_resolve = socket.getaddrinfo
socket.getaddrinfo = lambda _host, port, *args, **kwargs: _resolve("127.0.0.1", port, *args, **kwargs)
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
            self.send_header("Location", f"http://127.0.0.1:{self.server.server_address[1]}/trap?api_key={key}")
            self.end_headers()
            self.wfile.write(f"{key} sk-reflected-only-1234567890".encode())

        def log_message(self, *_args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    entry = {
        "base_url": f"http://127.0.0.1:{server.server_address[1]}/v1",
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
    {"base_url": "acp://copilot"},
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
            "base_url": f"http://127.0.0.1:{server.server_address[1]}/backup", "api_key": key,
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
    base_url = f"http://127.0.0.1:{server.server_address[1]}/runtime"
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


# Every non-OpenAI wire the runtime can select for a configured custom route.
# Expected runtime api_mode, or a client-level transport keyed off the URL.
_NON_OPENAI_ROUTES = {
    "anthropic-suffix": ({"base_url": "http://127.0.0.1:{port}/anthropic/v1"}, {}, "anthropic_messages"),
    "anthropic-bare-suffix": ({"base_url": "http://127.0.0.1:{port}/anthropic"}, {}, "anthropic_messages"),
    "anthropic-host": ({"base_url": "https://api.anthropic.com:{port}/v1"}, {}, "anthropic_messages"),
    "kimi-coding": ({"base_url": "https://api.kimi.com:{port}/coding/v1"}, {}, "anthropic_messages"),
    "declared-anthropic-alias": ({"base_url": "http://127.0.0.1:{port}/v1", "transport": "anthropic"}, {},
                                 "anthropic_messages"),
    "declared-bedrock": ({"base_url": "http://127.0.0.1:{port}/v1", "api_mode": "bedrock_converse"}, {},
                         "bedrock_converse"),
    "declared-app-server": ({"base_url": "http://127.0.0.1:{port}/v1", "api_mode": "codex_app_server"}, {},
                            "codex_app_server"),
    "openai-runtime-overlay": ({"base_url": "http://127.0.0.1:{port}/v1"}, {"openai_runtime": "codex_app_server"},
                               "codex_app_server"),
    "opencode-family-name": ({"name": "opencode-go-bridge", "base_url": "http://127.0.0.1:{port}/zen/go/v1",
                              "default_model": "minimax-m2.5"}, {}, "anthropic_messages"),
    "opencode-host": ({"name": "zen", "base_url": "https://opencode.ai:{port}/zen/v1",
                       "default_model": "claude-sonnet-4-5"}, {}, "anthropic_messages"),
    "gemini-native": ({"base_url": "https://generativelanguage.googleapis.com:{port}/v1beta"}, {}, "gemini"),
    "vertex-express": ({"base_url": "https://aiplatform.googleapis.com:{port}/v1beta1"}, {}, "gemini"),
    "moa-shortcut": ({"name": "moa", "base_url": "http://127.0.0.1:{port}/v1"}, {}, "moa"),
    # The Vertex rung claims the name and fails before any custom entry is read.
    "vertex-shortcut": ({"name": "vertex-ai", "base_url": "http://127.0.0.1:{port}/v1"}, {}, "not-custom"),
    "bedrock-mantle": ({"base_url": "https://bedrock-mantle.us-east-1.api.aws:{port}/openai/v1"}, {}, "mantle"),
}
# OpenAI-compatible routes the same harness must still reach (non-vacuity).
_OPENAI_ROUTES = {
    "plain": {"base_url": "http://127.0.0.1:{port}/v1"},
    "openai-host-responses": {"base_url": "https://api.openai.com:{port}/v1"},
    "xai-host-responses": {"base_url": "https://api.x.ai:{port}/v1"},
    "gemini-openai-compat": {"base_url": "https://generativelanguage.googleapis.com:{port}/v1beta/openai"},
    "declared-responses": {"base_url": "http://127.0.0.1:{port}/v1", "api_mode": "codex_responses"},
}
_RUNTIME_REFERENCE = """
import json, socket
from unittest.mock import patch
_resolve = socket.getaddrinfo
socket.getaddrinfo = lambda _host, port, *args, **kwargs: _resolve("127.0.0.1", port, *args, **kwargs)
from agent.bedrock_adapter import is_bedrock_openai_base_url
from agent.gemini_native_adapter import is_native_gemini_base_url
from hermes_cli.config import load_config_readonly
from hermes_cli.runtime_provider import resolve_runtime_provider
requested = load_config_readonly()['model']['provider']
with patch('hermes_cli.runtime_provider._try_resolve_from_custom_pool', return_value=None):
    try:
        runtime = resolve_runtime_provider(requested=requested)
    except Exception:
        runtime = {'provider': 'not-custom', 'base_url': ''}
url = runtime['base_url']
print(json.dumps(runtime['provider'] if runtime['provider'] in {'moa', 'not-custom'}
                 else 'gemini' if is_native_gemini_base_url(url) else 'mantle' if is_bedrock_openai_base_url(url)
                 else runtime['api_mode']))
"""


def _write_route(home, port, entry, model_extra, shape):
    key = "transport-fixture-credential"
    entry = {k: v.format(port=port) for k, v in entry.items()}
    name = entry.pop("name", "fixture")
    model = entry.pop("default_model", "fixture-model")
    entry["api_key"] = key
    if shape != "providers" and "transport" in entry:
        entry["api_mode"] = entry.pop("transport")  # ``transport`` is a keyed-block spelling.
    # A model id avoids the runtime's local single-model autodetect GET.
    config = {"model": {"provider": name, "default": model, **model_extra}}
    if shape == "model":
        config["model"].update(entry, provider="custom")
    elif shape == "providers":
        config["providers"] = {name: {**entry, "default_model": model}}
    else:
        config["custom_providers"] = [{"name": name, **entry, "model": model}]
    (home / "config.yaml").write_text(json.dumps(config))
    return key


def _connection_counter():
    connections = []

    class Handler(BaseRequestHandler):
        def handle(self):
            connections.append(self.client_address)

    server = ThreadingTCPServer(("127.0.0.1", 0), Handler)
    server.daemon_threads = True
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread, connections


def _stop(server, thread):
    server.shutdown()
    server.server_close()
    thread.join(timeout=5)


@pytest.mark.parametrize("shape", ["providers", "custom_providers", "model"])
@pytest.mark.parametrize("route", sorted(_NON_OPENAI_ROUTES))
def test_probe_refuses_every_runtime_selected_non_openai_transport(tmp_path, route, shape):
    """Undeclared modes are inferred like the runtime does; no wire but OpenAI /models + Bearer."""
    entry, model_extra, expected = _NON_OPENAI_ROUTES[route]
    if shape == "model" and ("name" in entry or model_extra):
        pytest.skip("route is defined by a named provider")
    server, thread, connections = _connection_counter()
    try:
        key = _write_route(tmp_path, server.server_address[1], entry, model_extra, shape)
        env = os.environ.copy()
        env["HERMES_HOME"] = str(tmp_path)
        reference = subprocess.run([sys.executable, "-c", _RUNTIME_REFERENCE], cwd=ROOT, env=env,
                                   capture_output=True, text=True, timeout=30)
        assert reference.returncode == 0, "runtime reference did not resolve"
        # The fixture really is a route the runtime sends over a non-OpenAI wire.
        assert json.loads(reference.stdout.strip().splitlines()[-1]) == expected
        assert connections == [], "runtime reference contacted the endpoint"
        result = _run(tmp_path, key, "config")
    finally:
        _stop(server, thread)
    assert connections == [], "probe contacted a non-OpenAI-compatible endpoint"
    assert result.returncode == 2, result.stdout + result.stderr
    assert "configuration" in result.stdout


@pytest.mark.parametrize("route", sorted(_OPENAI_ROUTES))
def test_probe_still_reaches_openai_compatible_transports(tmp_path, route):
    server, thread, connections = _connection_counter()
    try:
        key = _write_route(tmp_path, server.server_address[1], _OPENAI_ROUTES[route], {}, "providers")
        result = _run(tmp_path, key, "config")
    finally:
        _stop(server, thread)
    # The listener closes without HTTP, so reaching it proves the route was allowed.
    assert connections, result.stdout + result.stderr
    assert result.returncode == 1, result.stdout + result.stderr
