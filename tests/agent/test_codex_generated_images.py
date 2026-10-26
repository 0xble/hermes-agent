"""Completed provider-hosted images are answers, not empty Responses turns."""

import base64
import io
from pathlib import Path
from types import SimpleNamespace as NS

import pytest
from PIL import Image

from agent.codex_responses_adapter import _normalize_codex_response


@pytest.fixture(autouse=True)
def isolated_media_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))


def _image_item(**overrides):
    image = io.BytesIO()
    Image.new("RGB", (1, 1), (25, 50, 75)).save(image, format="PNG")
    # Recorded live shape: completed image_generation_call followed by an empty message.
    fields = dict(type="image_generation_call", id="ig_fixture", status="completed",
                  output_format="png", result=base64.b64encode(image.getvalue()).decode("ascii"))
    fields.update(overrides)
    return NS(**fields)


def _response(*items, status="completed"):
    return NS(status=status, output=list(items), output_text="")


@pytest.mark.parametrize("issuer", ["codex_backend", "other:http://localhost/v1"])
def test_completed_image_only_response_delivers_media(tmp_path, monkeypatch, issuer):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    item = _image_item()
    message, finish = _normalize_codex_response(
        _response(item, NS(type="message", status="completed", role="assistant", content=[])),
        issuer_kind=issuer,
    )
    assert finish == "stop"
    assert message.tool_calls == []
    assert message.content.startswith("MEDIA:")
    image_path = Path(message.content.removeprefix("MEDIA:").strip())
    assert image_path.is_relative_to(tmp_path / "cache" / "generated" / "images")
    assert image_path.read_bytes() == base64.b64decode(item.result)
    assert item.result not in str(vars(message))
    assert message.codex_message_items[-1]["content"][0]["text"] == message.content


@pytest.mark.parametrize("result", [None, "", "not-base64", base64.b64encode(b"not an image").decode()])
def test_unusable_completed_image_is_truthful_nonempty_result(result):
    message, finish = _normalize_codex_response(_response(_image_item(result=result)))
    assert finish == "stop"
    assert message.content
    assert "could not attach" in message.content
    assert "MEDIA:" not in message.content


@pytest.mark.parametrize("status", ["in_progress", "incomplete"])
def test_partial_images_are_not_delivered(status):
    message, finish = _normalize_codex_response(_response(_image_item(status=status), status="incomplete"))
    assert not message.content
    assert finish == "incomplete"


def test_completed_image_does_not_hide_text_or_function_calls():
    text = NS(type="message", status="completed", phase="final_answer", role="assistant",
              content=[NS(type="output_text", text="Priya leads Tess.")])
    function = NS(type="function_call", status="completed", id="fc_fixture", call_id="call_fixture",
                  name="test_tool", arguments="{}")
    message, finish = _normalize_codex_response(_response(_image_item(), text, function))
    assert "MEDIA:" in message.content
    assert "Priya leads Tess." in message.content
    assert finish == "tool_calls"
    assert message.tool_calls[0].function.name == "test_tool"


def test_corrupt_png_checksum_does_not_hide_usable_text():
    item = _image_item()
    raw = bytearray(base64.b64decode(item.result))
    data_index = raw.index(b"IDAT")
    chunk_size = int.from_bytes(raw[data_index - 4:data_index], "big")
    raw[data_index + 4 + chunk_size] ^= 1
    item.result = base64.b64encode(raw).decode("ascii")
    text = NS(type="message", status="completed", phase="final_answer", role="assistant",
              content=[NS(type="output_text", text="Priya leads the company.")])
    message, finish = _normalize_codex_response(_response(item, text))
    assert finish == "stop"
    assert "could not attach" in message.content
    assert "Priya leads the company." in message.content
    assert "MEDIA:" not in message.content


def test_image_cache_failure_is_truthful_not_empty(monkeypatch):
    from agent import provider_media

    def cannot_write(*args, **kwargs):
        raise OSError("fixture disk unavailable")

    monkeypatch.setattr(provider_media, "save_bytes", cannot_write)
    message, finish = _normalize_codex_response(_response(_image_item()))
    assert finish == "stop"
    assert "could not attach" in message.content
    assert "MEDIA:" not in message.content
