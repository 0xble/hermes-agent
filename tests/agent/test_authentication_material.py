"""Authentication egress preserves status prose and complete unrelated tokens."""

import copy
import json

import pytest

from agent.memory_manager import _redact_for_provider
from agent.source_origin import NativeOrigin


@pytest.mark.parametrize(("label", "neighbor"), [
    ("My wifi password: none.", "Nonetheless, keep notices off."),
    ("API key: required", "Approval is required and required fields stay visible."),
    ("api_key=config.api_key in settings.py", "Preserve configuration and reconfiguration."),
])
def test_status_labels_do_not_poison_neighboring_payload(label, neighbor):
    cleaned = _redact_for_provider(label, {"content": neighbor})
    assert cleaned[1]["content"] == neighbor


def test_authentication_values_are_removed_on_token_boundaries():
    safe = _redact_for_provider("Recovery code: 482913.", "482913 and x482913y.",
                                "API key: AbCdEfGhJkLmNpQrStUvWxYz", "AbCdEfGhJkLmNpQrStUvWxYz")
    assert safe[1] == "[redacted] and x482913y."
    assert "482913" not in safe[0]
    assert "AbCdEfGhJkLmNpQrStUvWxYz" not in safe[2] + safe[3]
    text = "Recovery code: 482913. Keep notices off."
    receipt = NativeOrigin.person(text, platform="fictional", principal="person", event="event")
    messages = [{"role": "user", "content": text, "display_metadata": {"_hermes_source_origin": receipt}}]
    safe_messages = _redact_for_provider(messages)
    assert "482913" not in json.dumps(safe_messages)
    assert "Keep notices off." in safe_messages[0]["content"]
    assert messages[0]["display_metadata"]["_hermes_source_origin"] is receipt


def test_native_origin_cannot_silently_serialize_as_empty_metadata():
    receipt = NativeOrigin.person("fictional", platform="fictional", principal="person", event="event")
    assert copy.deepcopy(receipt) == receipt
    with pytest.raises(TypeError):
        json.dumps({"origin": receipt})


def test_missing_origin_retention_skip_is_debug_logged(monkeypatch, caplog):
    from agent.memory_manager import MemoryManager

    class FictionalArchive:
        name = "fictional"
        requires_verified_origin = True

        def sync_turn(self, *args, **kwargs):
            raise AssertionError("unverified turn reached retention")

    manager = MemoryManager()
    manager._providers = [FictionalArchive()]
    monkeypatch.setattr(manager, "_owner_allows", lambda hook: True)
    monkeypatch.setattr(manager, "_submit_background", lambda fn: fn())
    with caplog.at_level("DEBUG", logger="agent.memory_manager"):
        manager.sync_all("Keep notices off.", "Acknowledged.", messages=[])
    assert "Skipping automatic retention for fictional: no verified origin" in caplog.text
