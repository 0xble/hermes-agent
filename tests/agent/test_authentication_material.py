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


def test_only_short_numeric_codes_require_word_boundaries():
    # A short numeric code must match a whole token, not part of an unrelated identifier.
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
    assert "_hermes_source_origin" not in json.dumps(safe_messages)
    assert messages[0]["display_metadata"]["_hermes_source_origin"] is receipt


@pytest.mark.parametrize("secret", ["hunter2zz9", "AbCdEfGhJkLmNpQrStUvWxYz", "7xQ2aZ", "12345678"])
def test_learned_secrets_are_removed_inside_other_tokens(secret):
    safe = _redact_for_provider("Password: " + secret,
                               {"content": f"token_{secret} {secret}abc https://fictional.test/{secret}/end"})
    assert secret not in json.dumps(safe)
    assert safe[1]["content"] == "token_[redacted] [redacted]abc https://fictional.test/[redacted]/end"


def test_native_origin_cannot_silently_serialize_as_empty_metadata():
    receipt = NativeOrigin.person("fictional", platform="fictional", principal="person", event="event")
    assert copy.deepcopy(receipt) == receipt
    with pytest.raises(TypeError):
        json.dumps({"origin": receipt})


@pytest.mark.parametrize(("text", "repeat", "secret"), [
    ("The OTP 482913 arrived, keep notices off.", "Code 482913", "482913"),
    ("My password is P@ssw0rd!", "Use P@ssw0rd! later", "P@ssw0rd!"),
    ("Recovery codes: 482913, 573024.", "Code 573024", "573024"),
    ("Password: P@ss,w0rd!", "Use P@ss,w0rd! later", "P@ss,w0rd!"),
    ('My password is "Abc! xyz".', "Use Abc! xyz later", "xyz"),
])
def test_every_held_value_is_learned_across_payloads(text, repeat, secret):
    assert secret not in json.dumps(_redact_for_provider(text, repeat))


@pytest.mark.parametrize("text", ["My password is wrong.", "The API key is required for login.",
                                   "My password is correct.", "The API key is configured."])
def test_authentication_labels_without_values_pass_unchanged(text):
    assert _redact_for_provider(text) == text
    assert _redact_for_provider({"content": text}, text) == ({"content": text}, text)


def test_missing_origin_retention_skip_is_info_logged_by_surface(monkeypatch, caplog):
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
    with caplog.at_level("INFO", logger="agent.memory_manager"):
        manager.sync_all("Keep notices off.", "Acknowledged.", messages=[], platform="slack")
        manager.sync_all("Keep notices off.", "Acknowledged.", messages=[], platform="slack")
        manager.sync_all("Keep notices off.", "Acknowledged.", messages=[], platform="tui")
    assert "missing verified origin surface=slack count=2" in caplog.text
    assert "missing verified origin surface=tui count=1" in caplog.text
