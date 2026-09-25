"""A crash-left final reply that carries attachments must resume, not be redelivered as bare text."""

from __future__ import annotations

import time
from types import SimpleNamespace

from gateway.config import Platform
from gateway.run import GatewayRunner


def _history(content: str) -> list:
    return [
        {"role": "user", "content": "send the photo", "timestamp": time.time()},
        {"role": "assistant", "content": content, "timestamp": time.time() + 1},
    ]


def _reply(content: str):
    runner = object.__new__(GatewayRunner)
    origin = SimpleNamespace(platform=Platform.TELEGRAM, chat_id="1", thread_id=None)
    return runner._crash_left_reply(_history(content), time.time() - 5, origin)


def test_reply_with_attachment_resumes_instead_of_losing_it():
    assert _reply("Done. MEDIA:/tmp/hermes-photo.png") is None


def test_media_only_reply_resumes():
    assert _reply("MEDIA:/tmp/hermes-photo.png") is None


def test_markdown_image_reply_resumes():
    assert _reply("Done. ![chart](https://example.invalid/chart.png)") is None


def test_bare_local_file_reply_resumes(tmp_path):
    image = tmp_path / "chart.png"
    image.write_bytes(b"png")
    assert _reply(f"Done. Saved to {image}") is None


def test_text_reply_is_still_redelivered():
    assert _reply("Done, no attachment.") == "Done, no attachment."
