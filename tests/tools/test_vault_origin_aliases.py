"""Config-driven exact-origin aliases for external vault logins."""

from __future__ import annotations

import json
from unittest.mock import patch

from agent.vault_store import VaultItemMeta
from tools import browser_vault_tool as vault


class _Backend:
    name = "onepassword"
    display_name = "1Password"
    needs_unlock = False
    binds_cards_to_page = True

    def __init__(self, meta):
        self.meta = meta

    def is_unlocked(self):
        return True

    def list_items(self):
        return [self.meta]

    def get_meta(self, handle):
        return self.meta if handle == self.meta.id else None

    def resolve_password(self, handle):
        return "synthetic-password"


def _meta(handle="op:gusto-id", origin="https://gusto.com"):
    return VaultItemMeta(
        id=handle,
        kind="login",
        label="Synthetic login",
        origin=origin,
        created_at="",
        identifier_type="username",
        identifier="user@example.com",
        allowed_origins=(origin,),
    )


def _fill_patches(monkeypatch, backend, page_origin="https://login.gusto.com"):
    monkeypatch.setattr("agent.vault_backends.backend_for_handle", lambda _: backend)
    monkeypatch.setattr(vault, "_focus_bound_origin", lambda *_: None)
    monkeypatch.setattr(vault, "_current_page_origin", lambda _: page_origin)
    monkeypatch.setattr(vault, "_eval_js", lambda *_: {"success": True, "result": json.dumps([
        {"type": "password", "autocomplete": "current-password", "index": 0},
    ])})
    monkeypatch.setattr(vault, "_eval_js_secret", lambda *_: {
        "success": True, "result": json.dumps({"filled": 1}),
    })


def test_saved_origin_refuses_extra_signin_origin_without_alias(monkeypatch):
    """Reproduction: a saved gusto.com login cannot fill login.gusto.com by inference."""
    backend = _Backend(_meta())
    _fill_patches(monkeypatch, backend)
    with patch("hermes_cli.config.load_config_readonly", return_value={"vault": {}}):
        result = json.loads(vault.browser_vault_fill(backend.meta.id, task_id="synthetic"))
    assert result["success"] is False
    assert result["error_type"] == "origin_mismatch"


def test_origin_alias_allows_exact_https_signin_origin(monkeypatch):
    backend = _Backend(_meta())
    _fill_patches(monkeypatch, backend)
    with patch("hermes_cli.config.load_config_readonly", return_value={"vault": {
        "origin_aliases": {"op:gusto-id": ["https://login.gusto.com"]},
    }}):
        result = json.loads(vault.browser_vault_fill(backend.meta.id, task_id="synthetic"))
    assert result["success"] is True
    assert result["origin"] == "https://login.gusto.com"


def test_origin_aliases_are_exact_and_item_scoped(monkeypatch):
    backend = _Backend(_meta(handle="op:gusto-id"))
    _fill_patches(monkeypatch, backend, page_origin="https://login.evil.example")
    with patch("hermes_cli.config.load_config_readonly", return_value={"vault": {
        "origin_aliases": {
            "op:other-id": ["https://login.evil.example"],
            "op:gusto-id": ["https://login.gusto.com"],
        },
    }}):
        result = json.loads(vault.browser_vault_fill(backend.meta.id, task_id="synthetic"))
    assert result["success"] is False
    assert result["error_type"] == "origin_mismatch"


def test_origin_alias_rejects_http_and_wildcards(monkeypatch):
    backend = _Backend(_meta())
    with patch("hermes_cli.config.load_config_readonly", return_value={"vault": {
        "origin_aliases": {"op:gusto-id": [
            "http://login.gusto.com", "https://*.gusto.com", "https://login.gusto.com/path",
            "https://login.gusto.com",
        ]},
    }}):
        from agent.vault_origin_aliases import _meta_with_origin_aliases
        result = _meta_with_origin_aliases(backend.meta)
    assert result.allowed_origins == ("https://gusto.com", "https://login.gusto.com")


def test_raw_onepassword_item_id_alias_does_not_match_another_item(monkeypatch):
    backend = _Backend(_meta(handle="op:other-id"))
    _fill_patches(monkeypatch, backend, page_origin="https://login.gusto.com")
    with patch("hermes_cli.config.load_config_readonly", return_value={"vault": {
        "origin_aliases": {"gusto-id": ["https://login.gusto.com"]},
    }}):
        result = json.loads(vault.browser_vault_fill(backend.meta.id, task_id="synthetic"))
    assert result["success"] is False
    assert result["error_type"] == "origin_mismatch"


def test_browser_vault_list_uses_aliases_for_origin_filter(monkeypatch):
    backend = _Backend(_meta())
    monkeypatch.setattr("agent.vault_backends.enabled_backends", lambda: [backend])
    with patch("hermes_cli.config.load_config_readonly", return_value={"vault": {
        "origin_aliases": {"op:gusto-id": ["https://login.gusto.com"]},
    }}):
        result = json.loads(vault.browser_vault_list(kind="login", origin="https://login.gusto.com"))
    assert result["success"] is True
    assert result["items"][0]["allowed_origins"] == ["https://gusto.com", "https://login.gusto.com"]

