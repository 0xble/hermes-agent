"""Regression coverage for producer-certified display previews, not raw tool data."""

from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import agent.display as display
from agent.codex_runtime import make_codex_app_server_event_bridge


@pytest.mark.parametrize("max_len", [0, 1, 2, 3, 4, 40, 120])
def test_sanitized_preview_retains_provenance_and_redacts_before_clipping(max_len):
    text = "https://example.test/?api_key=opaque-secret-token-123456789"
    preview = display.sanitize_tool_preview(text, max_len)

    # A swallowed producer error must not look like successful safe omission.
    assert isinstance(preview, display.SanitizedToolPreview)
    assert preview
    assert "opaque" not in preview
    if max_len:
        assert len(preview) <= max_len
    else:
        assert "example.test" in preview
    # Serialization intentionally loses the in-process certification marker.
    assert not isinstance(str(preview), display.SanitizedToolPreview)


def test_sanitized_preview_still_fails_closed_on_redaction_errors(monkeypatch):
    def fail(*args, **kwargs):
        raise ValueError("redaction unavailable")

    monkeypatch.setattr(display, "redact_sensitive_text", fail)
    assert display.sanitize_tool_preview("sensitive text", 40) is None


def test_custom_preview_is_not_newly_certified():
    preview = display.build_tool_preview("read_file", {"path": "/tmp/note.txt"})
    assert preview == "note.txt"
    assert not isinstance(preview, display.SanitizedToolPreview)


@pytest.mark.parametrize("item_type, key", [
    ("commandExecution", "command"),
    ("webSearch", "query"),
])
def test_codex_start_keeps_raw_arguments_separate_from_certified_preview(item_type, key):
    value = "Inspect https://example.test/?api_key=opaque-secret-token-123456789"
    item = {"id": "privacy-regression", "type": item_type, key: value}
    original = deepcopy(item)
    agent = SimpleNamespace(tool_progress_callback=Mock())

    make_codex_app_server_event_bridge(agent)({
        "method": "item/started", "params": {"item": item},
    })

    agent.tool_progress_callback.assert_called_once()
    event, name, preview, args = agent.tool_progress_callback.call_args.args
    assert event == "tool.started"
    assert name == ("exec_command" if item_type == "commandExecution" else "web_search")
    assert isinstance(preview, display.SanitizedToolPreview)
    assert preview.startswith("Inspect")
    assert "opaque" not in preview
    assert len(preview) <= 120
    assert args[key] == value
    assert item == original
