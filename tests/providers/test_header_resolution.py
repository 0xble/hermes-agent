"""Provider discovery is metadata-only, and headers resolve at their real consumers."""

from __future__ import annotations

import io
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace

import pytest


_PROVIDERS = (
    ("fireworks", "HermesAgent"), ("gmi", "HermesAgent"),
    ("kimi-coding", "HermesAgent"), ("kimi-coding-cn", "HermesAgent"),
    ("opencode-zen", "HermesAgent"), ("opencode-go", "HermesAgent"),
    ("router", "Hermes-Agent"), ("xai", "Hermes-Agent"),
)


@pytest.mark.parametrize("entry", ["providers", "gateway", "request-metadata", "nonvision-metadata", "outbox-spawn"])
def test_metadata_import_and_spawn_do_not_resolve_provenance(tmp_path, entry):
    """Observe canonical identity use before real discovery and spawn imports."""
    repo = Path(__file__).resolve().parents[2]
    observer = tmp_path / "observer"
    observer.mkdir()
    calls = tmp_path / "identity-calls.jsonl"
    (observer / "sitecustomize.py").write_text(
        "import json, os, traceback\n"
        "from types import SimpleNamespace\n"
        "import hermes_cli.version_info as version\n"
        "def identity():\n"
        "    with open(os.environ['HEADER_IDENTITY_CALLS'], 'a') as log:\n"
        "        frames = [{'file': f.filename, 'line': f.lineno} for f in traceback.extract_stack()[-5:-1]]\n"
        "        log.write(json.dumps({'pid': os.getpid(), 'frames': frames}) + '\\n')\n"
        "    return SimpleNamespace(base_version='2099.1.1')\n"
        "version.get_version_info = identity\n"
    )
    probe = """
import importlib, multiprocessing as mp, sys
from pathlib import Path
from providers import list_providers
expected = {'fireworks', 'gmi', 'kimi-coding', 'kimi-coding-cn',
            'opencode-zen', 'opencode-go', 'router', 'xai'}
assert expected <= {profile.name for profile in list_providers()}
if sys.argv[1] == 'gateway':
    import gateway.config
    import agent.auxiliary_client
elif sys.argv[1] == 'request-metadata':
    for name in ('agent.gemini_native_adapter', 'gateway.platforms.yuanbao',
                 'hermes_cli.model_catalog', 'hermes_cli.models',
                 'hermes_cli.doctor_connectivity', 'hermes_cli.models_pricing',
                 'hermes_cli.models_local', 'hermes_cli.models_reasoning_caps',
                 'hermes_cli.web_server_messaging', 'plugins.memory.openviking',
                 'plugins.platforms.slack.adapter', 'plugins.web.perplexity.provider'):
        importlib.import_module(name)
elif sys.argv[1] == 'nonvision-metadata':
    import builtins
    from agent.models_dev import _relay_vision_marker_metadata
    original_import = builtins.__import__
    def import_without_model_catalog(name, *args, **kwargs):
        assert name != 'hermes_cli.models', 'text-only metadata imported the model catalog'
        return original_import(name, *args, **kwargs)
    builtins.__import__ = import_without_model_catalog
    try:
        assert _relay_vision_marker_metadata('opencode-zen', 'plain-text-model') is None
    finally:
        builtins.__import__ = original_import
elif sys.argv[1] == 'outbox-spawn':
    module = importlib.import_module('tests.gateway.test_durable_outbox')
    child = mp.get_context('spawn').Process(
        target=module._die_after_begin, args=(Path(sys.argv[2]) / 'outbox',))
    child.start()
    try:
        child.join(timeout=10)
        assert child.exitcode == 0, child.exitcode
    finally:
        if child.is_alive():
            child.terminate()
            child.join(timeout=5)
assert not Path(sys.argv[3]).exists(), Path(sys.argv[3]).read_text()
"""
    env = os.environ.copy()
    env.update({
        "HERMES_HOME": str(tmp_path / "home"),
        "HERMES_RUNTIME_DIR": str(tmp_path / "runtime"),
        "HEADER_IDENTITY_CALLS": str(calls),
        "PYTHONPATH": os.pathsep.join((str(observer), str(repo))),
    })
    env.pop("HERMES_PROFILE", None)
    result = subprocess.run(
        [sys.executable, "-c", probe, entry, str(tmp_path), str(calls)],
        cwd=repo, env=env, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr


def _capture_sync_requests(monkeypatch, module, payload):
    """Intercept transport while preserving httpx's actual request building."""
    import httpx

    observed = []
    real_client = httpx.Client

    def respond(request):
        observed.append(dict(request.headers))
        return httpx.Response(200, json=payload)

    monkeypatch.setattr(module.httpx, "Client", lambda **kwargs: real_client(
        **kwargs, transport=httpx.MockTransport(respond)))

    def request(method, url, **kwargs):
        # httpx's top-level helpers own a separately imported Client. Replace
        # their transport boundary with a real client, preserving request building.
        with real_client(transport=httpx.MockTransport(respond)) as client:
            return client.request(method, url, **kwargs)

    monkeypatch.setattr(module.httpx, "get", lambda url, **kwargs: request("GET", url, **kwargs))
    monkeypatch.setattr(module.httpx, "post", lambda url, **kwargs: request("POST", url, **kwargs))
    return observed


def _check_native_gemini(monkeypatch, module, expected):
    observed = _capture_sync_requests(monkeypatch, module, {
        "candidates": [{"content": {"parts": [{"text": "ok"}]}, "finishReason": "STOP"}],
    })
    assert module.probe_gemini_tier("probe-key") == "paid"
    assert observed[-1]["x-goog-api-client"] == expected
    with module.GeminiNativeClient(api_key="probe-key") as client:
        client.chat.completions.create(model="header-probe", messages=[{"role": "user", "content": "hi"}])
    assert observed[-1]["user-agent"] == f"{expected} (gemini-native)"
    assert observed[-1]["x-goog-api-client"] == expected
    with module.GeminiNativeClient(api_key="probe-key", default_headers={"User-Agent": "configured"}) as client:
        client.chat.completions.create(model="header-probe", messages=[{"role": "user", "content": "hi"}])
    assert observed[-1]["user-agent"] == "configured"


def _check_yuanbao(monkeypatch, module, expected):
    import asyncio
    import httpx
    from gateway.platforms import yuanbao_proto as proto

    observed, frames = [], []
    real_client = httpx.AsyncClient

    def respond(request):
        observed.append(dict(request.headers))
        return httpx.Response(200, json={"code": 0, "data": {"token": "probe"}})

    monkeypatch.setattr(module.httpx, "AsyncClient", lambda **kwargs: real_client(
        **kwargs, transport=httpx.MockTransport(respond)))
    ack = proto._conn_request(proto.CMD_TYPE["Response"], "auth-bind", "ack", "ConnAccess",
                              proto._encode_parts([(1, "v", 0), (3, "s", "probe-connect")]))

    class Socket:
        async def send(self, frame):
            frames.append(frame)

        async def recv(self):
            return ack

    async def authenticate():
        data = await module.SignManager.fetch("key", "secret", "https://yuanbao.invalid", "probe-route")
        manager = module.ConnectionManager(SimpleNamespace(_bot_id="probe-bot", _route_env="probe-route", name="probe"))
        manager._ws = Socket()
        assert await manager._authenticate(data)

    asyncio.run(authenticate())
    assert observed[0]["x-appversion"] == expected
    assert observed[0]["x-bot-version"] == expected
    assert observed[0]["x-route-env"] == "probe-route"
    decoded = proto.decode_conn_msg(frames[0])
    request = proto._fields_to_dict(proto._parse_fields(decoded["data"]))
    device = proto._fields_to_dict(proto._parse_fields(proto._get_bytes(request, 3)))
    assert proto._get_string(device, 1) == expected
    assert proto._get_string(device, 24) == expected


def _check_manifest(monkeypatch, module, expected):
    payload = {"version": 1, "providers": {}}
    observed = []

    def open_manifest(request, **kwargs):
        observed.append({k.lower(): v for k, v in request.header_items()})
        return io.BytesIO(json.dumps(payload).encode())

    monkeypatch.setattr(module.urllib.request, "urlopen", open_manifest)
    assert module._fetch_manifest("https://catalog.invalid/manifest", timeout=1) == payload
    assert observed[0]["user-agent"] == expected
    assert observed[0]["accept"] == "application/json"


def _check_cli_sibling_consumers(monkeypatch, module, expected):
    from hermes_cli import models_local, models_pricing, models_reasoning_caps

    requests = []
    item = {"id": "header-probe", "type": "language", "tags": ["tool-use"],
            "pricing": {"prompt": "0.01", "completion": "0.02"},
            "input_token_price_per_m": 1, "output_token_price_per_m": 2,
            "supported_parameters": ["reasoning"]}
    payload = {"data": [item], "models": [{"model": "header-probe"}]}

    def open_catalog(request, **kwargs):
        requests.append({k.lower(): v for k, v in request.header_items()})
        return io.BytesIO(json.dumps(payload).encode())

    monkeypatch.setattr(module, "_urlopen_model_catalog_request", open_catalog)
    assert "header-probe" in models_pricing.fetch_models_with_pricing(
        api_key="probe-key", base_url="https://pricing.invalid", force_refresh=True)
    assert requests[-1]["user-agent"] == expected
    assert requests[-1]["authorization"] == "Bearer probe-key"
    monkeypatch.setenv("NOVITA_API_KEY", "probe-key")
    assert "header-probe" in models_pricing._fetch_novita_pricing(force_refresh=True)
    assert requests[-1]["user-agent"] == expected
    assert requests[-1]["authorization"] == "Bearer probe-key"
    assert models_local.probe_ollama_local_models("https://ollama-header.invalid") == ["header-probe"]
    assert requests[-1]["user-agent"] == expected
    assert models_local.probe_ollama_local_models("https://ollama-override.invalid",
                                                headers={"User-Agent": "configured"}) == ["header-probe"]
    assert requests[-1]["user-agent"] == "configured"
    assert models_local._lmstudio_fetch_raw_models("probe-key", "https://lmstudio.invalid") == payload["models"]
    assert requests[-1]["user-agent"] == expected
    assert requests[-1]["authorization"] == "Bearer probe-key"
    assert "header-probe" in models_reasoning_caps._fetch_reasoning_caps_catalog("https://reasoning.invalid/models", timeout=1)
    assert requests[-1]["user-agent"] == expected
    assert requests[-1]["accept"] == "application/json"

    import httpx
    from hermes_cli import config, doctor_connectivity

    observed = _capture_sync_requests(monkeypatch, SimpleNamespace(httpx=httpx), {"data": [item]})
    monkeypatch.setenv("GLM_API_KEY", "probe-key")
    probe = doctor_connectivity._probe_apikey_provider(
        "header-probe", ("GLM_API_KEY",), "https://doctor.invalid/models", None, True)
    assert not probe.issues
    assert observed[-1]["user-agent"] == expected
    assert observed[-1]["authorization"] == "Bearer probe-key"
    assert doctor_connectivity._anthropic_messages_probe("https://anthropic.invalid", "probe-key").status_code == 200
    assert observed[-1]["user-agent"] == expected
    assert observed[-1]["x-api-key"] == "probe-key"
    assert observed[-1]["anthropic-version"] == "2023-06-01"
    monkeypatch.setattr(config, "get_env_value", lambda name: "probe-key" if name == "GITHUB_TOKEN" else "")
    monkeypatch.setattr(config, "load_env", lambda: {})
    assert not doctor_connectivity._probe_github_token().issues
    assert observed[-1]["user-agent"] == expected
    assert observed[-1]["authorization"] == "Bearer probe-key"
    assert observed[-1]["accept"] == "application/vnd.github+json"
    # Existing endpoint-specific override remains ahead of the generic CLI UA.
    monkeypatch.setenv("KIMI_BASE_URL", "https://api.kimi.com/coding/v1")
    assert doctor_connectivity._apikey_request("probe-key", "KIMI_BASE_URL", "unused")[2]["User-Agent"] == "claude-code/0.1.0"


def _check_cli_catalogs(monkeypatch, module, expected):
    observed = []
    payload = {"data": [{"id": "header-probe", "type": "language", "tags": ["tool-use"]}]}

    def open_catalog(request, **kwargs):
        observed.append({k.lower(): v for k, v in request.header_items()})
        return io.BytesIO(json.dumps(payload).encode())

    monkeypatch.setattr(module, "_urlopen_model_catalog_request", open_catalog)
    assert module.probe_api_models("probe-key", "https://generativelanguage.googleapis.com/v1beta")["models"] == ["header-probe"]
    assert observed[-1]["user-agent"] == expected
    assert observed[-1]["x-goog-api-client"] == "hermes-agent/2099.1.1"
    assert observed[-1]["authorization"] == "Bearer probe-key"
    assert module.probe_api_models("probe-key", "https://configured.invalid/v1",
                                   request_headers={"User-Agent": "configured"})["models"] == ["header-probe"]
    assert observed[-1]["user-agent"] == "configured"
    monkeypatch.setenv("DEEPINFRA_API_KEY", "probe-key")
    assert module._fetch_deepinfra_catalog(force_refresh=True) == payload["data"]
    assert observed[-1]["user-agent"] == expected
    assert observed[-1]["authorization"] == "Bearer probe-key"
    monkeypatch.setenv("AI_GATEWAY_API_KEY", "probe-key")
    assert module._fetch_ai_gateway_models() == ["header-probe"]
    assert observed[-1]["user-agent"] == expected
    assert observed[-1]["authorization"] == "Bearer probe-key"
    _check_cli_sibling_consumers(monkeypatch, module, expected)


def _check_telegram_onboarding(monkeypatch, module, expected):
    # This owner imports httpx inside the operation rather than as module metadata.
    import httpx

    observed = _capture_sync_requests(monkeypatch, SimpleNamespace(httpx=httpx), {"status": "ok"})
    monkeypatch.setattr(module, "_telegram_onboarding_base_url", lambda: "https://setup.invalid")
    assert module._telegram_onboarding_request_sync("POST", "/probe", body={"probe": True}, bearer_token="probe-key") == {"status": "ok"}
    assert observed[0]["user-agent"] == expected
    assert observed[0]["accept"] == "application/json"
    assert observed[0]["content-type"] == "application/json"
    assert observed[0]["authorization"] == "Bearer probe-key"


def _check_openviking(monkeypatch, module, expected):
    import httpx

    observed = _capture_sync_requests(monkeypatch, SimpleNamespace(httpx=httpx), {"status": "ok"})
    client = module._VikingClient("https://viking.invalid", api_key="probe-key", account="account", user="user", agent="agent")
    assert client.get("/probe") == {"status": "ok"}
    assert observed[0]["user-agent"] == expected
    assert observed[0]["x-openviking-actor-peer"] == "agent"
    assert observed[0]["authorization"] == "Bearer probe-key"
    assert observed[0]["x-api-key"] == "probe-key"
    assert "x-openviking-account" not in observed[0]
    trusted = module._VikingClient("https://viking.invalid", account="account", user="user", agent="agent")
    assert trusted.get("/probe") == {"status": "ok"}
    assert observed[-1]["user-agent"] == expected
    assert observed[-1]["x-openviking-account"] == "account"
    assert observed[-1]["x-openviking-user"] == "user"


def _check_slack(monkeypatch, module, expected):
    pytest.importorskip("slack_sdk.web.async_client")
    pytest.importorskip("slack_bolt.async_app")
    assert module.SLACK_AVAILABLE
    # The SDK assembles its own suffix. Assert the real SDK-built prefix and
    # the adjacent proxy policy without making a platform network call.
    for proxy in (None, "http://proxy.invalid:8080"):
        client = module.SlackAdapter._new_web_client("probe-key", proxy)
        assert client.headers["User-Agent"].startswith(expected + " ")
        assert client.proxy == proxy


def _check_perplexity(monkeypatch, module, expected):
    from agent import web_search_provider

    observed = _capture_sync_requests(monkeypatch, module, {"results": []})
    monkeypatch.setattr(web_search_provider, "get_provider_env", lambda name: {
        "PERPLEXITY_API_KEY": "probe-key", "PERPLEXITY_BASE_URL": "https://perplexity.invalid",
    }.get(name, ""))
    assert module._perplexity_request("search", {"query": "probe"}) == {"results": []}
    assert observed[0]["user-agent"] == expected
    assert observed[0]["http-referer"] == "https://hermes-agent.nousresearch.com"
    assert observed[0]["x-title"] == "Hermes Agent"
    assert observed[0]["x-pplx-integration"] == "hermes-agent"
    gateway = SimpleNamespace(gateway_origin="https://gateway.invalid", nous_user_token="gateway-probe")
    assert module._perplexity_request("search", {"query": "probe"}, gateway=gateway) == {"results": []}
    assert observed[-1]["user-agent"] == expected
    assert observed[-1]["authorization"] == "Bearer gateway-probe"
    assert not {"http-referer", "x-title", "x-pplx-integration"} & observed[-1].keys()


_REQUEST_CONSUMERS = {
    "native-gemini": ("agent.gemini_native_adapter", "hermes-agent", _check_native_gemini),
    "yuanbao": ("gateway.platforms.yuanbao", "", _check_yuanbao),
    "manifest": ("hermes_cli.model_catalog", "hermes-cli", _check_manifest),
    "cli-catalogs": ("hermes_cli.models", "hermes-cli", _check_cli_catalogs),
    "telegram-onboarding": ("hermes_cli.web_server_messaging", "HermesDashboard", _check_telegram_onboarding),
    "openviking": ("plugins.memory.openviking", "openviking-memory-hermes", _check_openviking),
    "slack": ("plugins.platforms.slack.adapter", "HermesAgent", _check_slack),
    "perplexity": ("plugins.web.perplexity.provider", "HermesAgent", _check_perplexity),
}


@pytest.mark.parametrize("provider,prefix", _PROVIDERS + tuple(
    (name, item[1]) for name, item in _REQUEST_CONSUMERS.items()))
def test_versioned_headers_reach_client_and_catalog_boundaries(monkeypatch, provider, prefix):
    """Use real registry, client policy, SDK request building and catalog requests."""
    if provider in _REQUEST_CONSUMERS:
        import importlib

        module_name, _, check = _REQUEST_CONSUMERS[provider]
        module = importlib.import_module(module_name)
        monkeypatch.setattr(module, "get_version_info", lambda: SimpleNamespace(base_version="2099.1.1"))
        check(monkeypatch, module, f"{prefix}/2099.1.1" if prefix else "2099.1.1")
        return
    import httpx
    from openai import OpenAI
    from agent import auxiliary_client
    from agent.agent_init import _explicit_client_kwargs
    from agent.client_lifecycle import ClientLifecycleMixin
    from hermes_cli import config, urllib_security, version_info
    from hermes_constants import get_hermes_home
    from providers import get_provider_profile

    profile = get_provider_profile(provider)
    assert profile is not None
    monkeypatch.setattr(version_info, "get_version_info", lambda: SimpleNamespace(base_version="2099.1.1"))
    expected = f"{prefix}/2099.1.1"

    def wire_headers(kwargs):
        observed = []

        def respond(request):
            observed.append(dict(request.headers))
            return httpx.Response(200, json={
                "id": "header-probe", "object": "chat.completion", "created": 0,
                "model": "header-probe", "choices": [{"index": 0, "finish_reason": "stop",
                "message": {"role": "assistant", "content": "ok"}}],
            })

        with httpx.Client(transport=httpx.MockTransport(respond)) as http:
            with OpenAI(**kwargs, http_client=http, max_retries=0) as client:
                client.chat.completions.create(model="header-probe", messages=[{"role": "user", "content": "hi"}])
        return observed[0]

    agent = ClientLifecycleMixin()
    agent.provider = provider
    agent.base_url = profile.base_url
    agent.api_mode = "chat_completions"
    agent._client_kwargs = {"api_key": "probe-key", "base_url": profile.base_url}
    main = _explicit_client_kwargs(agent, "probe-key", profile.base_url, None)
    agent._apply_client_headers_for_base_url(profile.base_url)
    auxiliary = {"api_key": "probe-key", "base_url": profile.base_url,
                 "default_headers": auxiliary_client._endpoint_default_headers(profile.base_url, provider)}
    for kwargs in (main, agent._client_kwargs, auxiliary):
        headers = wire_headers(kwargs)
        assert headers["user-agent"] == expected
        for key, value in profile.default_headers.items():
            assert headers[key.lower()] == value

    requests = []

    def open_catalog(request, **kwargs):
        requests.append({k.lower(): v for k, v in request.header_items()})
        return io.BytesIO(json.dumps({"data": [{"id": "header-probe"}]}).encode())

    monkeypatch.setattr(urllib_security, "open_credentialed_url", open_catalog)
    assert profile.fetch_models(api_key="probe-key") == ["header-probe"]
    # Router's existing custom catalog owner deliberately uses the generic CLI UA.
    assert requests[0]["user-agent"] == ("hermes-cli/2099.1.1" if provider == "router" else expected)

    if provider == "xai":
        # The same import chain used to freeze Codex's OAuth client UA in auth metadata.
        from hermes_cli import auth_codex
        oauth_headers = []

        def refresh_response(request):
            oauth_headers.append(dict(request.headers))
            return httpx.Response(200, json={"access_token": "fresh-probe", "refresh_token": "next-probe", "expires_in": 3600})

        monkeypatch.setattr(auth_codex, "_codex_http_client", lambda **kwargs: httpx.Client(
            **kwargs, transport=httpx.MockTransport(refresh_response)))
        auth_codex.refresh_codex_oauth_pure("old-probe", "refresh-probe")
        assert oauth_headers[0]["user-agent"] == "hermes-cli/2099.1.1"

    # Existing user overrides still win after a native client header refresh.
    config.save_config({"model": {"default_headers": {"User-Agent": "configured-user-agent"}}})
    assert (get_hermes_home() / "config.yaml").is_file()
    agent._apply_client_headers_for_base_url(profile.base_url)
    assert wire_headers(agent._client_kwargs)["user-agent"] == "configured-user-agent"
    overridden = auxiliary_client._endpoint_default_headers(profile.base_url, provider)
    assert wire_headers({**main, "default_headers": overridden})["user-agent"] == "configured-user-agent"
