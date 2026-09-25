"""Several 1Password accounts as vault login sources, exercised through real config loading,
the backend registry and an offline ``op`` executable. Each account must authenticate only
with its own service-account token, so a handle can never resolve under another account."""
import json
import logging
import os
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from agent.vault_backends.base import backend_for_handle, enabled_backends
from agent.vault_backends.onepassword import OnePasswordLoginBackend

_FAKE_OP = r'''
import json
import os
import sys
from pathlib import Path

args = sys.argv[1:]
token = os.environ.get("OP_SERVICE_ACCOUNT_TOKEN")
account = {"dummy-personal-token": "personal", "dummy-business-token": "business"}.get(token)
with (Path(__file__).parent / "audit.jsonl").open("a") as stream:
    stream.write(json.dumps({"argv": args, "account": account,
                             "connect": sorted(k for k in os.environ if k.startswith("OP_CONNECT_"))}) + "\n")
if account is None:
    print("authentication rejected", file=sys.stderr)
    sys.exit(1)
item = {"personal": ("item-p", "vault-p", "https://personal.example/login"),
        "business": ("item-b", "vault-b", (lambda f: f.read_text().strip() if f.exists() else "https://business.example/login")(
            Path(__file__).parent / "business_url"))}[account]
if args == ["item", "list", "--categories", "Login,Credit Card", "--format", "json"]:
    print(json.dumps([{"id": item[0], "title": account, "category": "LOGIN", "vault": {"id": item[1]},
                       "urls": [{"href": item[2]}], "additional_information": account + "@example.com"}]))
elif args == ["item", "get", item[0], "--vault", item[1], "--fields", "label=password", "--reveal"]:
    print("dummy-" + account + "-password")
else:
    print("unknown item", file=sys.stderr)
    sys.exit(2)
'''


def _write_config(home: Path, op: Path, accounts) -> None:
    lines = ["vault:", "  onepassword:", f"    binary_path: {op}", "    accounts:"]
    for entry in accounts:
        lines.append("      - " + "\n        ".join(f"{k}: {v}" for k, v in entry.items()))
    home.joinpath("config.yaml").write_text("\n".join(lines if accounts else lines[:-1] + ["    accounts: []"]) + "\n")


_BUSINESS = {"alias": "business", "account": "business.example.com",
             "service_account_token_env": "OP_SERVICE_ACCOUNT_TOKEN_BUSINESS", "browser_account": "work"}


@pytest.fixture(params=[
    pytest.param(None, marks=pytest.mark.linux_only, id="linux"),
    pytest.param(None, marks=pytest.mark.macos_only, id="macos"),
])
def env(request, tmp_path, monkeypatch):
    for key in list(os.environ):
        if key.startswith("OP_"):
            monkeypatch.delenv(key)
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN", "dummy-personal-token")
    monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN_BUSINESS", "dummy-business-token")
    monkeypatch.setenv("OP_CONNECT_HOST", "https://connect.invalid")
    monkeypatch.setenv("OP_CONNECT_TOKEN", "dummy-connect-token")
    op = tmp_path / "op"
    op.write_text(f"#!{sys.executable}\n" + _FAKE_OP)
    op.chmod(0o700)
    audit = tmp_path / "audit.jsonl"

    def calls():
        return [json.loads(line) for line in audit.read_text().splitlines()] if audit.exists() else []
    return home, op, calls


def test_each_account_lists_and_resolves_only_with_its_own_token(env):
    home, op, calls = env
    _write_config(home, op, [_BUSINESS])
    extra = [b for b in enabled_backends() if b.name.startswith("onepassword@")]
    assert [b.name for b in extra] == ["onepassword@business"]
    business = extra[0]
    assert business.needs_unlock is False and business.browser_account == "work"

    metas = business.list_items()
    assert [m.id for m in metas] == ["op@business:item-b"]
    assert metas[0].identifier == "business@example.com"

    resolved = backend_for_handle("op@business:item-b")
    assert resolved.name == "onepassword@business"
    assert resolved.resolve_password("op@business:item-b") == "dummy-business-password"
    # The personal handle namespace never routes to the business backend, and vice versa.
    assert backend_for_handle("op:item-p").name == "onepassword"
    assert business.owns("op:item-p") is False

    # Every business-account call used the business token and never Connect.
    assert calls() and all(c["account"] == "business" and c["connect"] == [] for c in calls())


def test_business_handle_cannot_resolve_personal_item(env):
    home, op, calls = env
    _write_config(home, op, [_BUSINESS])
    business = backend_for_handle("op@business:item-p")
    with pytest.raises(RuntimeError):
        business.resolve_password("op@business:item-p")
    assert all(c["argv"][:2] != ["item", "get"] for c in calls())


def test_missing_token_is_reported_and_never_falls_back(env, monkeypatch):
    home, op, calls = env
    monkeypatch.delenv("OP_SERVICE_ACCOUNT_TOKEN_BUSINESS")
    _write_config(home, op, [_BUSINESS])
    business = backend_for_handle("op@business:item-b")
    with pytest.raises(RuntimeError, match="OP_SERVICE_ACCOUNT_TOKEN_BUSINESS"):
        business.list_items()
    with pytest.raises(RuntimeError):
        business.resolve_password("op@business:item-b")
    assert calls() == []


@pytest.mark.parametrize("entry", [
    {**_BUSINESS, "service_account_token_env": "OP_SERVICE_ACCOUNT_TOKEN"},  # primary's token
    {**_BUSINESS, "alias": "Bad Alias"},
    {**_BUSINESS, "account": ""},
    {k: v for k, v in _BUSINESS.items() if k != "service_account_token_env"},
])
def test_invalid_entries_are_ignored(entry, caplog):
    caplog.set_level(logging.WARNING)
    assert OnePasswordLoginBackend.additional_accounts({"accounts": [entry]}) == []
    assert "Ignoring vault.onepassword.accounts entry" in caplog.text


def test_duplicate_alias_and_shared_token_keep_only_the_first():
    second = {**_BUSINESS, "account": "other.example.com", "service_account_token_env": "OP_OTHER"}
    shared = {**_BUSINESS, "alias": "shared"}
    backends = OnePasswordLoginBackend.additional_accounts({"accounts": [_BUSINESS, second, shared]})
    assert [b.alias for b in backends] == ["business"]


def test_browser_account_pin_refuses_other_browser_before_resolving(env):
    from tools import browser_vault_tool  # registers the vault tools
    from tools.registry import registry

    home, op, calls = env
    _write_config(home, op, [_BUSINESS])
    with patch("tools.browser_camofox.get_session_account", return_value="personal"):
        fill = json.loads(registry.dispatch("browser_vault_fill", {"handle": "op@business:item-b"}, task_id="t1"))
        code = json.loads(registry.dispatch("browser_vault_enter_code", {"handle": "op@business:item-b"},
                                            task_id="t1"))
    assert fill["error_type"] == code["error_type"] == "browser_account_mismatch"
    assert "dummy-business-password" not in json.dumps([fill, code])
    assert calls() == []

    listed = json.loads(registry.dispatch("browser_vault_list", {}))
    business = [i for i in listed["items"] if i["backend"] == "onepassword@business"]
    assert [(i["handle"], i["browser_account"]) for i in business] == [("op@business:item-b", "work")]

    with patch("tools.browser_camofox.get_session_account", return_value="work"), \
            patch("tools.browser_camofox.is_camofox_mode", return_value=True):
        assert browser_vault_tool._browser_account_refusal(backend_for_handle("op@business:item-b"), "t1") is None


def test_stale_camofox_binding_does_not_authorize_another_browser(env):
    """A task once bound to the pinned Camofox account must not fill once a CDP/local browser takes over."""
    from tools import browser_vault_tool

    home, op, _calls = env
    _write_config(home, op, [_BUSINESS])
    backend = backend_for_handle("op@business:item-b")
    with patch("tools.browser_camofox.get_session_account", return_value="work"), \
            patch("tools.browser_camofox.is_camofox_mode", return_value=False):
        refusal = browser_vault_tool._browser_account_refusal(backend, "t1")
    assert refusal is not None and json.loads(refusal)["error_type"] == "browser_account_mismatch"


def test_missing_token_surfaces_through_otp_instead_of_prompting(env, monkeypatch):
    from agent.vault_backends.base import MissingCredential
    from tools import browser_vault_tool

    home, op, calls = env
    monkeypatch.delenv("OP_SERVICE_ACCOUNT_TOKEN_BUSINESS")
    _write_config(home, op, [_BUSINESS])
    business = backend_for_handle("op@business:item-b")
    assert business is not None
    with pytest.raises(MissingCredential):
        business.resolve_otp("op@business:item-b")

    prompted = []
    otp_control = {"tag": "input", "type": "text", "autocomplete": "one-time-code", "name": "code", "id": "code"}
    with patch.object(browser_vault_tool, "_browser_account_refusal", return_value=None), \
            patch.object(browser_vault_tool, "_focus_bound_origin"), \
            patch.object(browser_vault_tool, "_current_page_origin", return_value="https://example.com"), \
            patch.object(browser_vault_tool, "_eval_js", return_value={"success": True,
                                                                      "result": json.dumps([otp_control])}), \
            patch("agent.vault_backends.unlock.get_code_prompt_callback",
                  return_value=lambda *a: prompted.append(a) or "123456"), \
            patch("agent.vault_backends.unlock.can_prompt_here", return_value=True):
        out = json.loads(browser_vault_tool.browser_vault_enter_code("op@business:item-b", task_id="t1"))
    assert out["error_type"] == "credential_missing" and "OP_SERVICE_ACCOUNT_TOKEN_BUSINESS" in out["error"]
    assert prompted == [] and calls() == []


def _listings(calls):
    return [c for c in calls() if c["argv"][:2] == ["item", "list"]]


def test_display_listing_is_reused_but_never_across_tokens(env, monkeypatch):
    """browser_vault_list is called repeatedly against a per-account request quota."""
    home, op, calls = env
    _write_config(home, op, [_BUSINESS])
    business = backend_for_handle("op@business:item-b")
    business.list_items()
    business.list_items()
    assert len(_listings(calls)) == 1

    # A rotated token is a new identity: it must list for itself, not reuse the old answer.
    monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN_BUSINESS", "dummy-personal-token")
    assert [m.id for m in backend_for_handle("op@business:item-b").list_items()] == ["op@business:item-p"]
    assert len(_listings(calls)) == 2


def test_fill_authorization_sees_a_website_change_the_display_cache_has_not(env):
    """A password must never be authorized for a site the item no longer names."""
    home, op, _calls = env
    _write_config(home, op, [_BUSINESS])
    business = backend_for_handle("op@business:item-b")
    assert business.list_items()[0].origin == "https://business.example"
    op.with_name("business_url").write_text("https://moved.example/login")
    assert business.get_meta("op@business:item-b").origin == "https://moved.example"


def test_failed_listing_is_not_reused(env, monkeypatch):
    home, op, calls = env
    _write_config(home, op, [_BUSINESS])
    monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN_BUSINESS", "rejected-token")
    with pytest.raises(RuntimeError):
        backend_for_handle("op@business:item-b").list_items()
    monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN_BUSINESS", "dummy-business-token")
    assert [m.id for m in backend_for_handle("op@business:item-b").list_items()] == ["op@business:item-b"]


@pytest.mark.parametrize("saved,expected", [
    ("business.example", ["https://business.example"]),
    ("business.example:8443/login", ["https://business.example:8443"]),
    ("mailto:someone@evil.example", []),
    ("user@evil.example", []),
    ("javascript:alert(1)", []),
])
def test_only_bare_hostnames_gain_an_https_scheme(saved, expected):
    from agent.vault_backends.onepassword import _all_origins
    assert _all_origins([saved]) == expected
