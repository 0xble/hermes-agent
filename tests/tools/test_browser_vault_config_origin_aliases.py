"""``vault.origin_aliases``: a saved login fills on an extra exact origin the owner named, after the user
approves that origin.

The page is a stand-in for the CDP supervisor (current URL + inspected controls; it records every
secret-bearing evaluation). Secrets are dummy values in a temporary local vault; the config is a dict the
tests edit in place.
"""

from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from agent.vault_backends.local import LocalLoginBackend
from agent.vault_store import VaultStore, normalize_origin

_PASSWORD = "dummy-password-7"
_OTP_SEED = "JBSWY3DPEHPK3PXP"  # base32 test seed, not a real account
_SAVED = "https://bank.example"
_ALIAS = "https://signin.bank-auth.example"
_LOGIN_CONTROLS = [
    {"autocomplete": "username", "formIndex": 0, "index": 0, "label": "", "name": "user", "type": "text"},
    {"autocomplete": "current-password", "formIndex": 0, "index": 1, "label": "", "name": "pw", "type": "password"},
]
_OTP_CONTROLS = [{"autocomplete": "one-time-code", "formIndex": 0, "index": 0, "label": "Authentication code",
                  "name": "otp", "type": "text"}]


class _FakePage:
    def __init__(self, href: str, controls: list[dict]):
        self.href, self.controls, self.writes = href, controls, []

    def focus_page(self, origin: str, accept=None) -> dict:
        return {"ok": bool(origin) and normalize_origin(self.href) == origin, "url": self.href}

    def evaluate_runtime(self, expression: str) -> dict:
        if expression.strip() == "window.location.href":
            return {"ok": True, "result": self.href}
        if "expectedOrigin" in expression:
            self.writes.append(expression)
            return {"ok": True, "result": json.dumps({"filled": 1})}
        return {"ok": True, "result": json.dumps(self.controls)}


class _RecordingBackend(LocalLoginBackend):
    def __init__(self):
        self.resolved: list[str] = []

    def resolve_password(self, handle):
        self.resolved.append("password")
        return super().resolve_password(handle)

    def resolve_otp(self, handle):
        self.resolved.append("otp")
        return super().resolve_otp(handle)

    def resolve_secret(self, handle):
        self.resolved.append("secret")
        return super().resolve_secret(handle)


@pytest.fixture()
def vault(tmp_path):
    from agent import redact

    store = VaultStore(base_dir=tmp_path / "vault")
    backend = _RecordingBackend()
    config: dict = {"vault": {"origin_aliases": {}}}
    try:
        with patch("agent.vault_store.get_vault_store", return_value=store), \
             patch("agent.vault_backends.backend_for_handle", return_value=backend), \
             patch("agent.vault_backends.enabled_backends", return_value=[backend]), \
             patch("hermes_cli.config.load_config_readonly", return_value=config), \
             patch("tools.approval.is_approved", return_value=False):
            yield store, backend, config["vault"]["origin_aliases"]
    finally:
        redact.clear_vault_redaction_values()


def _login(store):
    return store.add_item("login", "Bank", {"identifier_type": "username", "identifier": "tester",
                                            "password": _PASSWORD, "otp_secret": _OTP_SEED}, origin=_SAVED)


def _run(fn, handle, page, *, answer="accept", during_prompt=None):
    from tools import browser_vault_tool

    asked: list[tuple] = []

    def consent(message, description, **kwargs):
        asked.append((message, description, kwargs.get("surface"), kwargs.get("remember_key")))
        if during_prompt is not None:
            during_prompt()
        return answer

    with patch.object(browser_vault_tool, "_ensure_supervisor", return_value=page), \
         patch("tools.approval_prompt.request_elicitation_consent", side_effect=consent):
        return json.loads(getattr(browser_vault_tool, fn)(handle, task_id="t")), asked


def test_an_approved_alias_origin_fills_the_password_pinned_to_that_origin(vault):
    store, backend, aliases = vault
    meta = _login(store)
    aliases[meta.id] = [_ALIAS + "/"]
    page = _FakePage(_ALIAS + "/login", _LOGIN_CONTROLS)
    out, asked = _run("browser_vault_fill", meta.id, page)
    assert out["success"] is True and out["origin"] == _ALIAS
    assert backend.resolved == ["password"]
    [(message, description, surface, remember_key)] = asked
    assert message == f"Fill login 'Bank' on {_ALIAS}" and _SAVED in description
    assert surface == "vault-alias" and remember_key == f"vault-alias:{meta.id}@{_ALIAS}"
    [write] = page.writes
    assert json.dumps(_ALIAS) in write


@pytest.mark.parametrize("answer", ["decline", "cancel"])
def test_an_unapproved_alias_origin_resolves_and_writes_nothing(vault, answer):
    store, backend, aliases = vault
    meta = _login(store)
    aliases[meta.id] = [_ALIAS]
    page = _FakePage(_ALIAS + "/login", _LOGIN_CONTROLS)
    out, _ = _run("browser_vault_fill", meta.id, page, answer=answer)
    assert out["success"] is False and out["error_type"] == "origin_alias_declined"
    assert backend.resolved == [] and page.writes == []


def test_a_remembered_approval_is_not_asked_again(vault):
    store, _, aliases = vault
    meta = _login(store)
    aliases[meta.id] = [_ALIAS]
    page = _FakePage(_ALIAS + "/login", _LOGIN_CONTROLS)
    with patch("tools.approval.is_approved", return_value=True):
        out, asked = _run("browser_vault_fill", meta.id, page)
    assert out["success"] is True and asked == []


def test_the_saved_origin_never_asks(vault):
    store, _, aliases = vault
    meta = _login(store)
    aliases[meta.id] = [_ALIAS]
    page = _FakePage(_SAVED + "/login", _LOGIN_CONTROLS)
    out, asked = _run("browser_vault_fill", meta.id, page)
    assert out["success"] is True and out["origin"] == _SAVED and asked == []


def test_an_alias_removed_while_the_prompt_was_open_refuses(vault):
    store, backend, aliases = vault
    meta = _login(store)
    aliases[meta.id] = [_ALIAS]
    page = _FakePage(_ALIAS + "/login", _LOGIN_CONTROLS)
    out, asked = _run("browser_vault_fill", meta.id, page, during_prompt=lambda: aliases.pop(meta.id))
    assert len(asked) == 1
    assert out["success"] is False and out["error_type"] == "origin_mismatch"
    assert backend.resolved == [] and page.writes == []


@pytest.mark.parametrize("entry,page_url", [
    ("http://signin.bank-auth.example", "http://signin.bank-auth.example/login"),
    ("https://signin.bank-auth.example/login", "https://signin.bank-auth.example/login"),
    ("https://*.bank-auth.example", "https://signin.bank-auth.example/login"),
    ("https://user@signin.bank-auth.example", "https://signin.bank-auth.example/login"),
    ("https://bank-auth.example", "https://signin.bank-auth.example/login"),
    ("https://signin.bank-auth.example", "https://signin.bank-auth.example:8443/login"),
], ids=["http", "path", "wildcard", "userinfo", "parent-domain", "other-port"])
def test_an_alias_is_one_exact_https_origin(vault, entry, page_url):
    store, backend, aliases = vault
    meta = _login(store)
    aliases[meta.id] = entry  # a single string is accepted as a one-item list
    page = _FakePage(page_url, _LOGIN_CONTROLS)
    out, asked = _run("browser_vault_fill", meta.id, page)
    assert out["error_type"] == "origin_mismatch" and asked == []
    assert backend.resolved == [] and page.writes == []


def test_an_alias_names_one_login_only(vault):
    store, backend, aliases = vault
    meta, other = _login(store), _login(store)
    aliases[other.id] = [_ALIAS]
    page = _FakePage(_ALIAS + "/login", _LOGIN_CONTROLS)
    out, asked = _run("browser_vault_fill", meta.id, page)
    assert out["error_type"] == "origin_mismatch" and asked == [] and backend.resolved == []


def test_an_approved_alias_origin_mints_the_saved_code(vault):
    store, backend, aliases = vault
    meta = _login(store)
    aliases[meta.id] = [_ALIAS]
    page = _FakePage(_ALIAS + "/2fa", _OTP_CONTROLS)
    out, asked = _run("browser_vault_enter_code", meta.id, page)
    assert out["success"] is True and out["origin"] == _ALIAS and out["source"] == "local"
    assert backend.resolved == ["otp"] and len(asked) == 1
    [write] = page.writes
    assert json.dumps(_ALIAS) in write


def test_aliases_do_not_apply_to_cards(vault):
    store, backend, aliases = vault
    card = store.add_item(kind="payment", label="Card", origin=_SAVED, secret={
        "card_number": "4111111111111111", "cardholder_name": "Test User", "exp_month": "07",
        "exp_year": "2029", "cvc": "123", "billing_postal_code": "94107"})
    aliases[card.id] = [_ALIAS]
    page = _FakePage(_ALIAS + "/checkout", [{"autocomplete": "cc-number", "index": 0, "type": "text"}])
    out, asked = _run("browser_vault_fill", card.id, page)
    assert out["error_type"] == "origin_mismatch" and asked == [] and backend.resolved == []


def test_list_shows_a_logins_aliases(vault):
    from tools import browser_vault_tool

    store, _, aliases = vault
    meta = _login(store)
    aliases[meta.id] = [_ALIAS, "http://ignored.example"]
    [entry] = json.loads(browser_vault_tool.browser_vault_list())["items"]
    assert entry["origin"] == _SAVED and entry["origin_aliases"] == [_ALIAS]
