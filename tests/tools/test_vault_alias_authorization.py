"""Alias consent and revocation survive prompt preferences and in-flight work."""

import json
from unittest.mock import Mock

import pytest

from agent.vault_store import VaultItemMeta
from tools import browser_vault_tool as vault


@pytest.fixture
def alias_fill(monkeypatch):
    origin = "https://signin.example.test"
    meta = VaultItemMeta(id="op:synthetic-alias", kind="login", label="Synthetic login",
                         origin="https://example.test", allowed_origins=("https://example.test",),
                         created_at="", has_otp=True)
    backend = Mock(browser_account="", needs_unlock=False)
    backend.name = "onepassword"
    backend.get_meta.return_value = meta
    backend.resolve_password.return_value = "synthetic-password"
    backend.resolve_otp.return_value = "246810"
    config = {"vault": {"origin_aliases": {meta.id: [origin]}}}
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: config)
    monkeypatch.setattr("agent.vault_backends.backend_for_handle", lambda _: backend)
    monkeypatch.setattr(vault, "_focus_bound_origin", lambda *_: None)
    monkeypatch.setattr(vault, "_current_page_origin", lambda _: origin)
    monkeypatch.setattr(vault, "_confirm_alias_fill", lambda *_: "accept")
    monkeypatch.setattr(vault, "_eval_js", lambda *_: {"success": True, "result": [
        {"type": "password", "autocomplete": "current-password", "index": 0},
        {"type": "text", "autocomplete": "one-time-code", "index": 1},
    ]})
    write = Mock(return_value={"success": True, "result": {"filled": 1}})
    monkeypatch.setattr(vault, "_eval_js_secret", write)
    monkeypatch.setattr(vault, "_alias_fill_decisions", {})
    monkeypatch.setattr(vault, "_alias_fill_refused", set())
    return backend, config, write, origin


@pytest.mark.parametrize("action", ["fill", "enter_code"])
@pytest.mark.parametrize("confirm", [True, False])
@pytest.mark.parametrize("decision", ["decline", "timeout"])
def test_alias_refusal_survives_confirmation_preference_change(alias_fill, monkeypatch, action, confirm, decision):
    backend, config, write, origin = alias_fill
    monkeypatch.setattr(vault, "_confirm_alias_fill", lambda *_: decision)
    args = {"action": action, "handle": backend.get_meta.return_value.id}
    declined = json.loads(vault._handle_vault(args, task_id="alias-refusal"))
    assert declined["error_type"] == ("origin_alias_declined" if decision == "decline" else "origin_alias_prompt_unanswered")
    config["vault"]["confirm_alias_fills"] = confirm
    # A changed prompt policy must not get another chance to auto-accept a refusal.
    prompt = Mock(return_value="accept")
    monkeypatch.setattr(vault, "_confirm_alias_fill", prompt)
    result = json.loads(vault._handle_vault(args, task_id="alias-refusal"))
    assert result["error_type"] == "origin_alias_retry_refused"
    prompt.assert_not_called()
    backend.resolve_password.assert_not_called()
    backend.resolve_otp.assert_not_called()
    write.assert_not_called()


@pytest.mark.parametrize("action", ["fill", "enter_code"])
@pytest.mark.parametrize("confirm", [True, False])
@pytest.mark.parametrize("stage", ["inspection", "resolution"])
def test_alias_revocation_stops_in_flight_fill(alias_fill, monkeypatch, action, confirm, stage):
    backend, config, write, _ = alias_fill
    config["vault"]["confirm_alias_fills"] = confirm
    handle = backend.get_meta.return_value.id
    resolver = backend.resolve_password if action == "fill" else backend.resolve_otp

    def revoke(value):
        config["vault"]["origin_aliases"].clear()
        return value

    if stage == "inspection":
        monkeypatch.setattr(vault, "_eval_js", lambda *_: revoke({"success": True, "result": [
            {"type": "password", "autocomplete": "current-password", "index": 0},
            {"type": "text", "autocomplete": "one-time-code", "index": 1},
        ]}))
    else:
        resolver.side_effect = lambda _: revoke(resolver.return_value)
    result = json.loads(vault._handle_vault({"action": action, "handle": handle}, task_id="alias-revoke"))
    assert result["success"] is False
    assert result["error_type"] == "origin_alias_revoked"
    assert resolver.call_count == (stage == "resolution")
    write.assert_not_called()
