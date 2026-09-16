"""A configured gateway authenticates with its own key only."""
import importlib

import pytest

plugin = importlib.import_module("plugins.image_gen.openai-codex")


def test_gateway_request_uses_only_gateway_auth(monkeypatch):
    import httpx
    monkeypatch.setattr("agent.codex_headers.codex_cloudflare_headers",
                        lambda token: pytest.fail("Direct headers"))
    monkeypatch.setattr(plugin, "_read_codex_access_token", lambda: pytest.fail("Direct OAuth read"))

    class Response:
        status_code = 200
        headers: dict = {}

        def json(self): return {"data": [{"b64_json": "result"}]}

    class Client:
        def __init__(self, **kwargs):
            # The bearer is applied per-request here, so the client itself carries no auth.
            assert "headers" not in kwargs or "Authorization" not in kwargs.get("headers", {})

        def __enter__(self): return self
        def __exit__(self, *args): pass

        def post(self, url, **kwargs):
            assert url == "http://127.0.0.1:8317/v1/images/generations"
            assert kwargs["headers"] == {"Authorization": "Bearer gateway-key"}
            assert "ChatGPT-Account-ID" not in kwargs["headers"]
            return Response()

    monkeypatch.setattr(httpx, "Client", Client)
    body = plugin._post_image_request(
        "gateway-key", prompt="circle", size="1024x1024", quality="medium",
        base_url="http://127.0.0.1:8317/v1", gateway=True)
    assert body["data"][0]["b64_json"] == "result"
