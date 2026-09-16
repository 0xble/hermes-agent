"""Explicit gateway Images API behavior."""
import importlib
import pytest

plugin = importlib.import_module("plugins.image_gen.openai-codex")


@pytest.mark.parametrize("editing", [False, True])
def test_images_gateway(monkeypatch, editing):
    import httpx
    monkeypatch.setattr("plugins.image_gen.openai._load_image_bytes", lambda ref: (b"image", "test.png"))

    class Response:
        status_code = 200
        headers = {"x-codex-imagegen-request-id": "req_gateway"}

        def json(self): return {"data": [{"b64_json": "result"}]}

    class Client:
        def __init__(self, **kwargs): pass
        def __enter__(self): return self
        def __exit__(self, *args): pass

        def post(self, url, **kwargs):
            assert kwargs["headers"] == {"Authorization": "Bearer test-key"}
            payload = kwargs["data" if editing else "json"]
            assert payload["model"] == plugin.API_MODEL
            assert payload["quality"] == "medium"
            assert url.endswith("/images/edits" if editing else "/images/generations")
            if editing:
                assert kwargs["files"] == [("image[]", ("test.png", b"image"))]
            return Response()

    monkeypatch.setattr(httpx, "Client", Client)
    body = plugin._post_image_request(
        "test-key", prompt="circle", size="1024x1024", quality="medium",
        input_images=[{"image_url": "fixture"}] if editing else None,
        base_url="http://localhost/v1", gateway=True, api_mode="images")
    assert body == {"data": [{"b64_json": "result"}], "imagegen_request_id": "req_gateway"}
