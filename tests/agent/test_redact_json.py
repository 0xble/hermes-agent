"""Structural JSON redaction keeps every boundary the text redactor enforces.

All secrets below are synthetic fixtures.
"""

import json

import pytest

from agent import redact
from agent.redact import (
    clear_vault_redaction_values,
    redact_sensitive_json,
    register_vault_date_component,
    register_vault_redaction_value,
    register_vault_card_component,
)

SENTINEL = "«redacted-vault-secret»"


@pytest.fixture(autouse=True)
def _isolated_redaction(monkeypatch):
    monkeypatch.setattr(redact, "_REDACT_ENABLED", True)
    monkeypatch.setattr(redact, "_REDACT_ENABLED_BY_HOME", {})
    clear_vault_redaction_values()
    yield
    clear_vault_redaction_values()


@pytest.mark.parametrize("leaf", [
    "stored account PIN: 1234",  # retained suffix
    "1234 is the stored account PIN",  # retained prefix
    "1234",
    "***1234",  # an already-masked display must still cross the vault boundary
])
@pytest.mark.parametrize("enabled, force", [(True, True), (True, False), (False, True), (False, False)])
def test_registered_secret_in_secret_named_field_never_survives_masking(monkeypatch, leaf, enabled, force):
    monkeypatch.setattr(redact, "_REDACT_ENABLED", enabled)
    register_vault_redaction_value("1234")
    payload = {"password": leaf, "outer": [{"api_key": leaf, "nested": {"token": [leaf]}}], "tuple": (leaf,)}
    serialized = json.dumps(redact_sensitive_json(payload, force=force), ensure_ascii=False)
    assert "1234" not in serialized


@pytest.mark.parametrize("enabled, force", [(True, True), (False, False)])
def test_registered_secret_behind_env_lookup_and_ordinary_text_is_scrubbed(monkeypatch, enabled, force):
    monkeypatch.setattr(redact, "_REDACT_ENABLED", enabled)
    register_vault_redaction_value("s3cr3t-fixture")
    payload = {"password": "os.getenv('PW') or 's3cr3t-fixture'",
               "messages": [{"content": "the vault filled s3cr3t-fixture"}], "s3cr3t-fixture": 1}
    serialized = json.dumps(redact_sensitive_json(payload, force=force), ensure_ascii=False)
    assert "s3cr3t-fixture" not in serialized
    assert SENTINEL in serialized


def test_registered_numeric_leaf_is_scrubbed_like_serialized_text():
    register_vault_redaction_value("4111")
    register_vault_date_component("bday-year", "1987")
    result = redact_sensitive_json({"pin": 4111, "year": 1987, "count": 3, "ok": True})
    assert result == {"pin": SENTINEL, "year": SENTINEL, "count": 3, "ok": True}


def test_registered_numeric_key_is_scrubbed_like_serialized_text():
    register_vault_redaction_value("4111")
    serialized = json.dumps(redact_sensitive_json({4111: "pin", "nested": [{4111: 1}]}), ensure_ascii=False)
    assert "4111" not in serialized


@pytest.mark.parametrize("enabled, force", [(True, True), (False, False)])
def test_formatted_registered_pan_in_secret_field_leaks_no_digits(monkeypatch, enabled, force):
    monkeypatch.setattr(redact, "_REDACT_ENABLED", enabled)
    register_vault_redaction_value("4111111111111111")
    payload = {"card_number": "card 4111 1111 1111 1111 on file",
               "messages": [{"content": "paid with 4111-1111-1111-1111"}]}
    serialized = json.dumps(redact_sensitive_json(payload, force=force), ensure_ascii=False)
    assert "4111" not in serialized and "1111" not in serialized


def test_secret_named_mask_leaks_no_head_or_tail_of_registered_value():
    register_vault_redaction_value("PREFIX-registered-SUFFIX")
    result = redact_sensitive_json({"password": "PREFIX-registered-SUFFIX plus padding text"}, force=True)
    assert "PREFIX" not in result["password"] and "SUFFIX" not in result["password"]


@pytest.mark.parametrize("flags", [{}, {"force": True}, {"code_file": True},
                                   {"file_read": True}, {"file_read": True, "secret_file": True}])
def test_structural_redaction_preserves_vault_tab_and_origin_scope(flags):
    register_vault_card_component("billing_postal_code", "98765", tab="checkout", origin="https://pay.example")
    register_vault_date_component("bday-year", "1987", tab="checkout", origin="https://pay.example")
    payload = {"ordinary": [{"postal": "98765", "year": 1987}]}
    matched = redact_sensitive_json(payload, vault_tab="checkout", vault_origin="https://pay.example", **flags)
    other_origin = redact_sensitive_json(payload, vault_tab="checkout", vault_origin="https://other.example", **flags)
    other_tab = redact_sensitive_json(payload, vault_tab="other", vault_origin="https://pay.example", **flags)
    assert matched == {"ordinary": [{"postal": SENTINEL, "year": SENTINEL}]}
    assert other_origin == {"ordinary": [{"postal": "98765", "year": SENTINEL}]}
    assert other_tab == payload


def test_env_lookup_expression_without_registered_secret_is_preserved():
    expression = "os.getenv('PASSWORD')"
    assert redact_sensitive_json({"password": expression}, force=True) == {"password": expression}


def test_unregistered_secret_field_is_still_masked_and_ordinary_text_untouched():
    result = redact_sensitive_json({"password": "opaque-fixture-value-123456789", "model": "gpt", "n": 2})
    assert "opaque-fixture-value-123456789" not in result["password"]
    assert result["model"] == "gpt" and result["n"] == 2


@pytest.mark.parametrize("flags, masked", [
    ({"code_file": True}, False),
    ({"file_read": True}, False),
    ({"file_read": True, "secret_file": True}, True),
    ({"code_file": True, "secret_file": True}, True),
])
def test_code_and_file_read_flags_keep_text_redactor_semantics(flags, masked):
    leaf = "opaque-fixture-value-123456789"
    result = redact_sensitive_json({"apiKey": leaf}, **flags)
    assert (leaf not in result["apiKey"]) is masked
    if masked and flags.get("file_read"):
        assert result["apiKey"] == "«redacted-secret»"


def test_default_api_follows_routed_profile_policy(tmp_path, monkeypatch):
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    monkeypatch.setattr(redact, "_REDACT_ENABLED", False)  # launch profile opted out
    routed = tmp_path / "routed-profile"
    routed.mkdir()
    token = set_hermes_home_override(routed)
    try:
        result = redact_sensitive_json({"password": "opaque-fixture-value-123456789"})
    finally:
        reset_hermes_home_override(token)
    assert "opaque-fixture-value-123456789" not in result["password"]


def test_default_api_honors_routed_profile_opt_out(tmp_path, monkeypatch):
    from hermes_constants import reset_hermes_home_override, set_hermes_home_override

    routed = tmp_path / "routed-opt-out"
    routed.mkdir()
    (routed / ".env").write_text("HERMES_REDACT_SECRETS=false\n", encoding="utf-8")
    token = set_hermes_home_override(routed)
    try:
        register_vault_redaction_value("9876")
        result = redact_sensitive_json({"password": "opaque-fixture-value-123456789", "pin": "PIN 9876"})
    finally:
        clear_vault_redaction_values()
        reset_hermes_home_override(token)
    assert result["password"] == "opaque-fixture-value-123456789"
    assert result["pin"] == f"PIN {SENTINEL}"
