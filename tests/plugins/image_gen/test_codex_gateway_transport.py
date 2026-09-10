"""Gateway authentication must not read direct OAuth credentials."""
import importlib

import pytest

plugin = importlib.import_module("plugins.image_gen.openai-codex")


def test_gateway_credentials_and_missing_key_fail_closed(monkeypatch):
    config = {"base_url": "http://127.0.0.1:8317/v1/", "api_key": "test-key"}
    monkeypatch.setattr(plugin, "load_image_gen_config", lambda: config)
    monkeypatch.setattr(plugin, "_read_codex_access_token", lambda: pytest.fail("OAuth read"))
    assert plugin._resolve_transport() == ("http://127.0.0.1:8317/v1", "test-key", True)
    config.pop("api_key")
    with pytest.raises(ValueError, match="api_key"):
        plugin._resolve_transport()
    assert not plugin.OpenAICodexImageGenProvider().is_available()


def test_gateway_stream_uses_only_gateway_auth(monkeypatch):
    import httpx
    monkeypatch.setattr("agent.codex_headers.codex_cloudflare_headers", lambda token: pytest.fail("Direct headers"))
    class Response:
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def raise_for_status(self): pass
        def iter_lines(self): return iter([])
    class Client:
        def __init__(self, **kwargs):
            assert kwargs["headers"]["Authorization"] == "Bearer gateway-key"
            assert "ChatGPT-Account-ID" not in kwargs["headers"]
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def stream(self, method, url, **kwargs):
            assert url == "http://127.0.0.1:8317/v1/responses"
            assert method == "POST"
            return Response()
    monkeypatch.setattr(httpx, "Client", Client)
    assert plugin._collect_image_b64("gateway-key", prompt="circle", size="1024x1024", quality="medium", base_url="http://127.0.0.1:8317/v1", gateway=True) is None
