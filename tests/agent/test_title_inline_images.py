"""The opt-in title/icon boundary forwards only reconstructed inline image payloads."""
import base64
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from agent.title_generator import choose_topic_icon, generate_title


@pytest.mark.parametrize("consumer", ["title", "icon"])
@pytest.mark.parametrize("kind", ["image", "image_url", "input_image"])
@pytest.mark.parametrize("media", ["image/png", "image/jpeg", "image/gif", "image/webp"])
def test_auxiliary_images_drop_remote_metadata_and_keep_valid_inline(monkeypatch, consumer, kind, media):
    data = base64.b64encode(b"fixture image bytes").decode()
    url = f"data:{media};base64,{data}"
    private = "https://private.invalid/signed?token=SECRET"
    if kind == "image":
        clean = {"type": kind, "source": {"type": "base64", "media_type": media, "data": data}}
        part = {"type": kind, "source": {**clean["source"], "url": private}, "metadata": private}
    elif kind == "image_url":
        clean = {"type": kind, "image_url": {"url": url, "detail": "low"}}
        part = {"type": kind, "image_url": {**clean["image_url"], "metadata": private}, "url": private}
    else:
        clean = {"type": kind, "image_url": url, "detail": "low"}
        part = {**clean, "file_id": private, "metadata": private}
    hostile = [
        {"type": "image", "source": {"type": "url", "url": private, "data": data, "media_type": media}},
        {"type": "image", "source": {"type": "base64", "data": "not base64!", "media_type": media}},
        {"type": "image", "source": {"type": "base64", "data": data, "media_type": "text/html"}},
        {"type": "image_url", "image_url": {"url": private}},
        {"type": "input_image", "image_url": "data:image/png;base64,invalid!"},
        {"type": "image_url", "image_url": {"url": "data:text/html;base64," + data}},
    ]
    response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(
        content='{"title":"Image Review"}' if consumer == "title" else "🎨"), finish_reason="stop")])
    llm = Mock(return_value=response)
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: {
        "auxiliary": {"title_generation": {"include_attachments": True}}})
    monkeypatch.setattr("agent.title_generator.call_llm", llm)
    if consumer == "title":
        assert generate_title("Inspect image", title_context=[*hostile, part]) == "Image Review"
    else:
        assert choose_topic_icon("Image Review", "Inspect image", ["🎨"], title_context=[*hostile, part]) == "🎨"
    content = llm.call_args.kwargs["messages"][1]["content"]
    assert content[1:] == [clean]
    assert "SECRET" not in json.dumps(llm.call_args.kwargs)
    assert part.get("metadata") == private or part.get("url") == private  # Input was not mutated.


@pytest.mark.parametrize("scenario", ["overhead", "aggregate", "count"])
def test_forwarded_image_payload_budget_includes_serialization(monkeypatch, scenario):
    from agent.title_generator import _title_request_content
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: {
        "auxiliary": {"title_generation": {"include_attachments": True}}})
    limit = 2 * 1024 * 1024
    data = "AAAA" * ((limit // 4) if scenario == "overhead" else 200000 if scenario == "aggregate" else 1)
    part = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": data}}
    content = _title_request_content("Inspect", [part] * (5 if scenario == "count" else 3 if scenario == "aggregate" else 1))
    if scenario == "overhead":
        assert content == "Inspect"
    else:
        images = content[1:]
        assert len(images) == (4 if scenario == "count" else 2)
        assert sum(len(json.dumps(image, ensure_ascii=False).encode("utf-8")) for image in images) <= limit
