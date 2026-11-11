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


def test_learned_credentials_require_token_boundaries():
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


@pytest.mark.parametrize("secret", ["hunter2zz9", "AbCdEfGhJkLmNpQrStUvWxYz", "7xQ2aZ9q", "12345678"])
def test_learned_secrets_preserve_other_tokens(secret):
    safe = _redact_for_provider("Password: " + secret,
                               {"content": f"token_{secret} {secret}abc https://fictional.test/{secret}/end {secret}"})
    assert secret not in safe[0]
    assert safe[1]["content"] == f"token_{secret} {secret}abc https://fictional.test/[redacted]/end [redacted]"


@pytest.mark.parametrize("text", [
    "I know; nothing is known yet. Snow is falling now.",
    "The OTP is 6 digits.",
    "The password is too short.",
    "Your login code is valid for 10 minutes.",
    "The OTP is delivered by SMS.",
    "The password is surprisingly complicated.",
    "The OTP is delivered!",
])
def test_benign_prose_neither_holds_nor_teaches_values(text):
    from agent.authentication_material import authentication_values, exclude_authentication_material

    assert authentication_values(text) == ()
    assert exclude_authentication_material(text) == text
    assert _redact_for_provider(text, {"content": text}) == (text, {"content": text})


def test_credential_shape_boundary_fails_closed_for_non_text_startup_values():
    from agent.authentication_material import credential_shaped

    assert credential_shaped(None) is False
    assert credential_shaped(12345678) is False
    assert credential_shaped("482913", label=None) is False


@pytest.mark.parametrize(("text", "repeat"), [
    ("password: str", "Use str(x) and str(y)."),
    ("Password: is stored", "It is stored and is available."),
    ("API key: see the docs", "Please see the docs and see examples."),
    ("OTP: 6 digits", "Use 6 digits, with 6 places."),
    ("Reset password: done", "Work done, all done."),
    ('def login(password: str, otp: int)', "Use str(x) and int(x)."),
    ('class Cfg: password: str = ""', "Use str(x)."),
    *[(f"password: {value}", f"Use {value} here.") for value in (
        "str", "int", "bool", "float", "bytes", "None", "Optional[str]", "Any", "dict", "list",
        "Optional[bytes]", "dict[str, int]", "(", ")", ",",
        "(Hunter2!x9)", ")Hunter2!x9", ",Hunter2!x9",
    )],
    ("Password=7xQ2aZ", "Use 7xQ2aZ here."),
    *[(f"OTP{separator}{value}", f"Use {value} here.")
      for separator in (": ", " = ", " is ", " are ") for value in ("6", "12", "123")],
])
def test_unshaped_assigned_values_neither_hold_nor_teach(text, repeat):
    from agent.authentication_material import authentication_values, exclude_authentication_material

    assert authentication_values(text) == ()
    assert exclude_authentication_material(text) == text
    cleaned, neighbor = _redact_for_provider(text, {"content": repeat})
    assert "[authentication span held]" not in cleaned
    assert neighbor == {"content": repeat}


def test_held_sentence_does_not_teach_unrelated_tokens():
    safe = _redact_for_provider("Password: hunter2zz9 NeighboringLongIdentifier.",
                               "hunter2zz9 NeighboringLongIdentifier")
    assert "hunter2zz9" not in json.dumps(safe)
    assert safe[1] == "[redacted] NeighboringLongIdentifier"


@pytest.mark.parametrize(("text", "value"), [
    ('Password is "fictional phrase".', "fictional phrase"),
    ("Password: hunter2zz9", "hunter2zz9"),
    ('Password="7xQ2aZ"', "7xQ2aZ"),
    ("My password is P@ssw0rd!", "P@ssw0rd!"),
    ("Password is AbCdEfGhJkLmNpQrStUvWxYz", "AbCdEfGhJkLmNpQrStUvWxYz"),
    ("Recovery code is 12345678", "12345678"),
    ("password: Hunter2!x9", "Hunter2!x9"),
    ("api_key=sk-fictionalAbC123", "sk-fictionalAbC123"),
    ("The OTP is 482913.", "482913"),
    *[(f"{label}{separator}{value}", value)
      for label in ("OTP", "Recovery code") for separator in (": ", "=", " is ", " are ", " ")
      for value in ("482913", "4829137", "48291375")],
    ('Password: "str"', "str"),
])
def test_credential_shapes_still_hide_standalone_repeats(text, value):
    from agent.authentication_material import authentication_values, exclude_authentication_material

    assert value in authentication_values(text)
    assert value not in exclude_authentication_material(text)
    assert value not in json.dumps(_redact_for_provider(text, "Repeat " + value))


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
                                   "My password is correct.", "The API key is configured.",
                                   "My password is wrong!", "The API key is required!"])
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


@pytest.mark.parametrize('text', ['\nLine one.\nLine two.\n\n', 'Para one!\n\nPara two?',
    'My password is the same as before.', 'My password is not working.', 'Password:\nI forgot it',
    'OTP is in my inbox'])
def test_prose_and_paragraph_boundaries_survive_authentication_scrubbing(text):
    assert _redact_for_provider(text, 'Other things in the inbox.') == (text, 'Other things in the inbox.')
