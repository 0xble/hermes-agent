"""Verified retention shares the exchange's redaction without widening authored spans."""

import json

import pytest

from agent.memory_manager import MemoryManager
from agent.source_origin import NativeOrigin, ORIGIN_KEY
from plugins.memory.hindsight import HindsightMemoryProvider


@pytest.fixture
def retention(monkeypatch):
    provider, manager, payloads = HindsightMemoryProvider(), MemoryManager(), []
    manager._providers = [provider]
    monkeypatch.setattr(manager, "_owner_allows", lambda hook: True)
    monkeypatch.setattr(manager, "_submit_background", lambda fn: fn())
    monkeypatch.setattr(provider, "_resolve_retain_target", lambda fallback: ("fictional", None))
    monkeypatch.setattr(provider, "_enqueue_retain", lambda job: job())
    monkeypatch.setattr(provider, "_retain_batch", lambda item, **kwargs: payloads.append(item))
    provider._apply_retain_settings({"retain_async": False})
    return manager, payloads


@pytest.mark.parametrize(("user", "assistant", "secret"), [
    ("use Zq8vB2nR5tY9pL3mK7sX4wC6dF1hJk0 for staging.",
     "Set GITHUB_TOKEN=Zq8vB2nR5tY9pL3mK7sX4wC6dF1hJk0 in .env.",
     "Zq8vB2nR5tY9pL3mK7sX4wC6dF1hJk0"),
    ("ok I set it to hunter2zz9 now.", "Your password: hunter2zz9", "hunter2zz9"),
])
@pytest.mark.parametrize("partial", [False, True])
@pytest.mark.parametrize("label_in_messages", [False, True])
def test_verified_hindsight_payload_uses_joint_redaction(retention, user, assistant, secret,
                                                       partial, label_in_messages):
    manager, payloads = retention
    prefix = "Generated routing context.\n" if partial else ""
    text = prefix + user
    receipt = NativeOrigin.person(text, platform="telegram", principal="fictional", event="event",
                                  authored=((len(prefix), len(text)),))
    messages = [{"role": "user", "content": text, "display_metadata": {ORIGIN_KEY: receipt}}]
    if label_in_messages:
        messages.append({"role": "tool", "content": assistant})
        assistant = "The staging setting has been saved."
    manager.sync_all(text, assistant, session_id="fictional", messages=messages)
    assert len(payloads) == 1
    assert secret not in json.dumps(payloads[0])
    retained = json.loads(payloads[0]["content"])[0]
    assert [message["role"] for message in retained] == ["user", "assistant"]
    assert "for staging." in retained[0]["content"] or "now." in retained[0]["content"]
    assert "Generated routing context" not in payloads[0]["content"]
    assert messages[0]["content"] == text
    assert messages[0]["display_metadata"][ORIGIN_KEY] is receipt


def test_verified_skill_instruction_reaches_hindsight_without_scaffold(retention):
    manager, payloads = retention
    instruction = "Keep project progress notices off."
    scaffold = (
        '[IMPORTANT: The user has invoked the "fictional" skill, indicating they want '
        "you to follow its instructions. The full skill content is loaded below.]\n\n"
        "Generated skill instructions.\n\n"
        "The user has provided the following instruction alongside the skill invocation: "
        + instruction
    )
    receipt = NativeOrigin.person(instruction, platform="telegram", principal="fictional", event="event")
    messages = [{"role": "user", "content": instruction, "display_metadata": {ORIGIN_KEY: receipt}}]
    manager.sync_all(scaffold, "I will keep project progress notices off.",
                     session_id="fictional", messages=messages)
    assert len(payloads) == 1
    retained = json.loads(payloads[0]["content"])[0]
    assert instruction in retained[0]["content"]
    assert "Generated skill instructions" not in payloads[0]["content"]
    assert messages[0]["content"] == instruction
    assert messages[0]["display_metadata"][ORIGIN_KEY] is receipt
