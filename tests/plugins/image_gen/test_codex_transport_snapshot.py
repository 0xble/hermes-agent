"""Drive real provider/config/payload handling through HTTPX's transport boundary."""
import base64
import importlib
import json
from pathlib import Path

import httpx
import pytest
import yaml

plugin = importlib.import_module("plugins.image_gen.openai-codex")
PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000d49444154789c6300010000000500010d0a2db40000000049454e44ae426082")
B64 = base64.b64encode(PNG).decode()


@pytest.mark.parametrize("editing", [False, True])
def test_generate_keeps_gateway_transport_snapshot(tmp_path, monkeypatch, editing):
    """Endpoint and credential are snapshotted once per attempt: a config rotation that lands
    after the snapshot must not retarget or re-credential the request already in flight."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("OPENAI_IMAGE_MODEL", raising=False)
    config_path = tmp_path / "config.yaml"

    def configure(base, key):
        config_path.write_text(yaml.safe_dump({"image_gen": {
            "base_url": base, "api_key": key, "api_mode": "images"}}))

    configure("https://old-gateway.invalid/v1/", "old-test-key")

    monkeypatch.setattr(plugin, "_read_codex_access_token", lambda: pytest.fail("Gateway read direct OAuth"))
    monkeypatch.setattr("agent.codex_headers.codex_cloudflare_headers",
                        lambda _: pytest.fail("Gateway added direct account headers"))
    normalize = plugin._normalize_input_images

    def normalize_then_rotate(*args):
        images = normalize(*args)
        configure("https://new-gateway.invalid/v2", "new-test-key")
        return images

    monkeypatch.setattr(plugin, "_normalize_input_images", normalize_then_rotate)
    requests = []

    def handle(request):
        requests.append(request)
        assert request.headers["authorization"] == "Bearer old-test-key"
        assert "chatgpt-account-id" not in request.headers
        suffix = "/images/edits" if editing else "/images/generations"
        assert str(request.url) == "https://old-gateway.invalid/v1" + suffix
        body = request.read()
        if editing:
            assert request.headers["content-type"].startswith("multipart/form-data;")
            assert b'name="image[]"' in body and PNG in body
            assert b'name="prompt"\r\n\r\ncircle' in body
        else:
            payload = json.loads(body)
            assert payload["prompt"] == "circle" and payload["n"] == 1
            assert payload["model"] == plugin.API_MODEL
        return httpx.Response(200, json={"data": [{"b64_json": B64}]})

    native_client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kwargs: native_client(
        **kwargs, transport=httpx.MockTransport(handle), trust_env=False))
    result = plugin.OpenAICodexImageGenProvider().generate(
        "circle", image_url="data:image/png;base64," + B64 if editing else None)
    assert result["success"], result
    assert len(requests) == 1
    assert result["modality"] == ("image" if editing else "text")
    assert Path(result["image"]).read_bytes() == PNG
    assert Path(result["image"]).is_relative_to(tmp_path)


@pytest.mark.parametrize("editing", [False, True])
def test_direct_codex_ignores_gateway_api_mode_and_later_gateway_config(tmp_path, monkeypatch, editing):
    """Without base_url/api_key the provider stays on direct Codex auth: account headers, the
    Codex base URL, and a gateway configured mid-flight never retargets the attempt."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    config_path = tmp_path / "config.yaml"
    config_path.write_text(yaml.safe_dump({"image_gen": {"api_mode": "images"}}))
    monkeypatch.setattr(plugin, "_read_codex_access_token", lambda: "direct-test-token")
    monkeypatch.setattr("agent.codex_headers.codex_cloudflare_headers", lambda token: {"X-Test-Direct": token})
    native_client, requests = httpx.Client, []

    def handle(request):
        requests.append(request)
        suffix = "/images/edits" if editing else "/images/generations"
        assert str(request.url) == plugin._CODEX_BASE_URL + suffix
        assert request.headers["authorization"] == "Bearer direct-test-token"
        assert request.headers["x-test-direct"] == "direct-test-token"
        body = json.loads(request.read())
        assert body["prompt"] == "circle"
        assert ("images" in body) is editing
        config_path.write_text(yaml.safe_dump({"image_gen": {
            "base_url": "https://new-gateway.invalid/v1", "api_key": "new-key", "api_mode": "images"}}))
        return httpx.Response(200, json={"data": [{"b64_json": B64}]})

    monkeypatch.setattr(httpx, "Client", lambda **kwargs: native_client(
        **kwargs, transport=httpx.MockTransport(handle), trust_env=False))
    result = plugin.OpenAICodexImageGenProvider().generate(
        "circle", image_url="data:image/png;base64," + B64 if editing else None)
    assert result["success"], result
    assert len(requests) == 1


@pytest.mark.parametrize("key", [None, "${MISSING_IMAGE_SNAPSHOT_KEY}"])
def test_gateway_invalid_key_never_falls_back_to_direct_auth(tmp_path, monkeypatch, key):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({"image_gen": {
        "base_url": "https://gateway.invalid/v1", "api_key": key, "api_mode": "images"}}))
    monkeypatch.setattr(plugin, "_read_codex_access_token", lambda: pytest.fail("Direct credential fallback"))
    monkeypatch.setattr(httpx, "Client", lambda **kw: pytest.fail("Invalid credentials dispatched"))
    result = plugin.OpenAICodexImageGenProvider().generate("circle")
    assert result["success"] is False and result["error_type"] == "invalid_config"
