"""Config-driven exact-origin aliases for external vault logins."""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path
from unittest.mock import patch

from agent.vault_origin_aliases import _configured_aliases
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
    monkeypatch.setattr(vault, "_confirm_alias_fill", lambda *_: "accept")
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
    assert result["error"] == (
        "Refused: current page origin (https://login.gusto.com) does not match the vault item's bound origin(s) "
        "(https://gusto.com). Vault fills only run on the exact origin(s) the credential was saved for. Add the exact "
        "origin with `hermes config set vault.origin_aliases.op:gusto-id '[\"https://login.gusto.com\"]'`, including any "
        "existing aliases because `set` replaces the entire value for that item, then retry the fill. The agent may "
        "write this config entry; the fill-time confirmation names the exact origin and item label. Never edit or rewrite "
        "the existing 1Password item to add a URL: template rewrites can delete passkeys."
    )

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
        result = _meta_with_origin_aliases(backend.meta, _configured_aliases())
    assert result.allowed_origins == ("https://gusto.com", "https://login.gusto.com")


def test_invalid_aliases_are_logged_once(monkeypatch, caplog):
    from agent import vault_origin_aliases

    vault_origin_aliases._invalid_alias_warnings.clear()
    with patch("hermes_cli.config.load_config_readonly", return_value={"vault": {
        "origin_aliases": {"op:gusto-id": [
            "http://login.gusto.com", "https://login.gusto.com/path", "https://*.gusto.com",
        ]},
    }}):
        with caplog.at_level("WARNING", logger="agent.vault_origin_aliases"):
            vault_origin_aliases._configured_aliases()
            vault_origin_aliases._configured_aliases()
    warnings = [record for record in caplog.records if "Ignoring invalid vault.origin_aliases" in record.message]
    assert len(warnings) == 3


def test_raw_onepassword_item_id_alias_does_not_match_another_item(monkeypatch):
    backend = _Backend(_meta(handle="op:other-id"))
    _fill_patches(monkeypatch, backend, page_origin="https://login.gusto.com")
    with patch("hermes_cli.config.load_config_readonly", return_value={"vault": {
        "origin_aliases": {"gusto-id": ["https://login.gusto.com"]},
    }}):
        result = json.loads(vault.browser_vault_fill(backend.meta.id, task_id="synthetic"))
    assert result["success"] is False
    assert result["error_type"] == "origin_mismatch"


def test_alias_only_login_fill_confirms_once_and_saved_origin_does_not(monkeypatch):
    backend = _Backend(_meta(handle="op:confirm-id"))
    _fill_patches(monkeypatch, backend, page_origin="https://login.gusto.com")
    prompts = []
    monkeypatch.setattr(vault, "_confirm_alias_fill", lambda label, origin, saved: prompts.append((label, origin, saved)) or "accept")
    with patch("hermes_cli.config.load_config_readonly", return_value={"vault": {
        "origin_aliases": {"op:confirm-id": ["https://login.gusto.com"]},
    }}):
        first = json.loads(vault.browser_vault_fill(backend.meta.id, task_id="confirm"))
        second = json.loads(vault.browser_vault_fill(backend.meta.id, task_id="confirm"))
    assert first["success"] and second["success"]
    assert prompts == [("Synthetic login", "https://login.gusto.com", ("https://gusto.com",))]

    prompts.clear()
    _fill_patches(monkeypatch, backend, page_origin="https://gusto.com")
    with patch("hermes_cli.config.load_config_readonly", return_value={"vault": {
        "origin_aliases": {"op:confirm-id": ["https://login.gusto.com"]},
    }}):
        saved = json.loads(vault.browser_vault_fill(backend.meta.id, task_id="saved"))
    assert saved["success"]
    assert prompts == []



def test_declined_alias_only_login_fill_refuses_retries(monkeypatch):
    backend = _Backend(_meta(handle="op:decline-id"))
    _fill_patches(monkeypatch, backend)
    prompts = []
    monkeypatch.setattr(vault, "_confirm_alias_fill", lambda *_: prompts.append(True) or "decline")
    with patch("hermes_cli.config.load_config_readonly", return_value={"vault": {
        "origin_aliases": {"op:decline-id": ["https://login.gusto.com"]},
    }}):
        first = json.loads(vault.browser_vault_fill(backend.meta.id, task_id="decline"))
        second = json.loads(vault.browser_vault_fill(backend.meta.id, task_id="decline"))
    assert first["error_type"] == "origin_alias_declined"
    assert second["error_type"] == "origin_alias_retry_refused"
    assert prompts == [True]


def test_raw_item_id_alias_matches_multi_account_onepassword_handles(monkeypatch):
    for handle in ("op@business:gusto-id", "op:connect:vault:gusto-id"):
        backend = _Backend(_meta(handle=handle))
        _fill_patches(monkeypatch, backend, page_origin="https://login.gusto.com")
        with patch("hermes_cli.config.load_config_readonly", return_value={"vault": {
            "origin_aliases": {"gusto-id": ["https://login.gusto.com"]},
        }}):
            result = json.loads(vault.browser_vault_fill(handle, task_id="synthetic"))
        assert result["success"] is True, (handle, result)


def test_alias_fill_cache_never_evicts_refusals(monkeypatch):
    vault._alias_fill_decisions.clear()
    vault._alias_fill_refused.clear()
    monkeypatch.setattr(vault, "_ALIAS_FILL_CACHE_CAP", 1)
    refused = ("home", "session", "refused", "https://refused.example")
    first_approval = ("home", "session", "approved-1", "https://one.example")
    second_approval = ("home", "session", "approved-2", "https://two.example")
    vault._record_alias_fill_decision(refused, "refused")
    vault._record_alias_fill_decision(first_approval, "accept")
    vault._record_alias_fill_decision(second_approval, "accept")
    assert vault._alias_fill_decision(refused) == "refused"
    assert vault._alias_fill_decision(second_approval) == "accept"
    assert vault._alias_fill_decision(first_approval) is None


def test_browser_vault_list_uses_aliases_for_origin_filter(monkeypatch):
    backend = _Backend(_meta())
    monkeypatch.setattr("agent.vault_backends.enabled_backends", lambda: [backend])
    with patch("hermes_cli.config.load_config_readonly", return_value={"vault": {
        "origin_aliases": {"op:gusto-id": ["https://login.gusto.com"]},
    }}):
        result = json.loads(vault.browser_vault_list(kind="login", origin="https://login.gusto.com"))
    assert result["success"] is True
    assert result["items"][0]["allowed_origins"] == ["https://gusto.com", "https://login.gusto.com"]


def test_alias_confirmation_prompt_names_full_origin_and_cross_domain_warning(monkeypatch):
    prompts = []
    monkeypatch.setattr(
        "tools.approval_prompt.request_elicitation_consent",
        lambda *args, **kwargs: prompts.append((args[0], args[1], kwargs)) or "accept",
    )
    assert vault._confirm_alias_fill("Synthetic login", "https://login.example.com:8443", ("https://gusto.com",)) == "accept"
    title, detail, kwargs = prompts[-1]
    assert "Synthetic login" in title
    assert "https://login.example.com:8443" in title
    assert "registrable domain differs" in detail
    assert "login.example.com" in detail and "gusto.com" in detail
    assert kwargs["surface"] == "vault-origin-alias"


def test_alias_confirmation_prompt_omits_warning_for_matching_registrable_domain(monkeypatch):
    details = []
    monkeypatch.setattr(
        "tools.approval_prompt.request_elicitation_consent",
        lambda *args, **kwargs: details.append((args[0], args[1])) or "accept",
    )
    vault._confirm_alias_fill("Synthetic login", "https://app.gusto.com", ("https://login.gusto.com",))
    assert "registrable domain differs" not in details[-1][1]


def test_alias_confirmation_handles_multipart_public_suffix():
    from agent.vault_origin_aliases import alias_domain_warning, registrable_domain
    assert registrable_domain("https://login.example.co.uk") == "example.co.uk"
    assert registrable_domain("https://app.example.co.uk") == "example.co.uk"
    assert alias_domain_warning("https://login.example.co.uk", ("https://app.example.co.uk",)) is None
    assert alias_domain_warning("https://login.other.co.uk", ("https://app.example.co.uk",)) is not None


def test_alias_warning_is_advisory_for_unlisted_public_suffixes(monkeypatch):
    from agent.vault_origin_aliases import alias_domain_warning
    pairs = [
        ("https://evil.co.kr", "https://bank.co.kr"),
        ("https://evil.github.io", "https://brian.github.io"),
        ("https://evil.com.de", "https://bank.com.de"),
    ]
    for alias, saved in pairs:
        assert alias_domain_warning(alias, (saved,)) is None
        prompts = []
        monkeypatch.setattr(
            "tools.approval_prompt.request_elicitation_consent",
            lambda *args, **kwargs: prompts.append((args[0], args[1])) or "accept",
        )
        vault._confirm_alias_fill("Synthetic login", alias, (saved,))
        assert alias in prompts[-1][0]
        assert "registrable domain differs" not in prompts[-1][1]




def test_multi_account_handle_round_trips_and_unions_with_raw_item_id(tmp_path, monkeypatch):
    handle = "op@hostandhome:zys56ajg4voda76332p3mfjwr4"
    raw_id = "zys56ajg4voda76332p3mfjwr4"
    home = tmp_path / "hermes-home"
    env = os.environ.copy()
    env["HERMES_HOME"] = str(home)
    repo = Path(__file__).resolve().parents[2]
    set_command = [
        "uv", "run", "python", "hermes", "config", "set",
        f"vault.origin_aliases.{handle}", '["https://login.planetfitness.com"]',
    ]
    get_command = [
        "uv", "run", "python", "hermes", "config", "get",
        f"vault.origin_aliases.{handle}", "--json",
    ]
    subprocess.run(set_command, cwd=repo, env=env, capture_output=True, text=True, check=True)
    readback = subprocess.run(get_command, cwd=repo, env=env, capture_output=True, text=True, check=True)
    assert json.loads(readback.stdout) == ["https://login.planetfitness.com"]

    from agent.vault_origin_aliases import apply_origin_aliases
    meta = _meta(handle=handle, origin="https://gusto.com")
    with patch("hermes_cli.config.load_config_readonly", return_value={"vault": {
        "origin_aliases": {
            handle: ["https://login.planetfitness.com"],
            raw_id: ["https://accounts.planetfitness.com"],
        },
    }}):
        augmented = apply_origin_aliases([meta])[0]
    assert augmented.allowed_origins == (
        "https://gusto.com",
        "https://login.planetfitness.com",
        "https://accounts.planetfitness.com",
    )


def test_agent_written_config_set_form_is_honored_without_restart(tmp_path, monkeypatch):
    home = tmp_path / "hermes-home"
    env = os.environ.copy()
    env["HERMES_HOME"] = str(home)
    repo = Path(__file__).resolve().parents[2]
    command = [
        "uv", "run", "python", "hermes", "config", "set",
        "vault.origin_aliases.example-item", '["https://signin.example.com"]',
    ]
    monkeypatch.setenv("HERMES_HOME", str(home))
    assert _configured_aliases() == {}
    completed = subprocess.run(command, cwd=repo, env=env, capture_output=True, text=True, check=True)
    assert "Set vault.origin_aliases.example-item" in completed.stdout
    assert _configured_aliases()["example-item"] == ("https://signin.example.com",)
    backend = _Backend(_meta(handle="example-item"))
    _fill_patches(monkeypatch, backend, page_origin="https://signin.example.com")
    result = json.loads(vault.browser_vault_fill("example-item", task_id="config-set"))
    assert result["success"] is True


def test_alias_removed_during_confirmation_refuses_fill(monkeypatch):
    """An alias deleted while the (possibly hours-long) prompt waits must not authorize the write."""
    vault._alias_fill_decisions.clear()
    vault._alias_fill_refused.clear()
    backend = _Backend(_meta(handle="op:revoke-id"))
    _fill_patches(monkeypatch, backend)
    config = {"vault": {"origin_aliases": {"op:revoke-id": ["https://login.gusto.com"]}}}
    writes, prompts = [], []
    monkeypatch.setattr(vault, "_eval_js_secret", lambda *args: writes.append(args) or {
        "success": True, "result": json.dumps({"filled": 1}),
    })

    def _confirm(*args):
        prompts.append(args)
        config["vault"]["origin_aliases"].pop("op:revoke-id")
        return "accept"

    monkeypatch.setattr(vault, "_confirm_alias_fill", _confirm)
    with patch("hermes_cli.config.load_config_readonly", side_effect=lambda: config):
        result = json.loads(vault.browser_vault_fill(backend.meta.id, task_id="revoke"))
        assert result["success"] is False
        assert result["error_type"] == "origin_alias_revoked"
        assert writes == []
        # The acceptance was not cached: re-adding the alias asks again rather than reusing it.
        config["vault"]["origin_aliases"]["op:revoke-id"] = ["https://login.gusto.com"]
        monkeypatch.setattr(vault, "_confirm_alias_fill", lambda *args: prompts.append(args) or "accept")
        again = json.loads(vault.browser_vault_fill(backend.meta.id, task_id="revoke"))
    assert again["success"] is True
    assert len(prompts) == 2 and len(writes) == 1


def test_alias_removed_from_config_file_during_confirmation_is_not_served_stale(tmp_path, monkeypatch):
    """The re-check goes through the real config loader, whose cache must observe the rewrite."""
    vault._alias_fill_decisions.clear()
    vault._alias_fill_refused.clear()
    home = tmp_path / "hermes-home"
    home.mkdir()
    config_path = home / "config.yaml"
    # JSON is valid YAML; the test runner's environment does not ship PyYAML.
    config_path.write_text(json.dumps({"vault": {"origin_aliases": {
        "op:file-revoke-id": ["https://login.gusto.com"],
    }}}), encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(home))
    assert _configured_aliases()["op:file-revoke-id"] == ("https://login.gusto.com",)  # warm the cache
    backend = _Backend(_meta(handle="op:file-revoke-id"))
    _fill_patches(monkeypatch, backend)
    writes = []
    monkeypatch.setattr(vault, "_eval_js_secret", lambda *args: writes.append(args) or {
        "success": True, "result": json.dumps({"filled": 1}),
    })

    def _confirm(*_):
        config_path.write_text(json.dumps({"vault": {"origin_aliases": {}}}) + "\n", encoding="utf-8")
        return "accept"

    monkeypatch.setattr(vault, "_confirm_alias_fill", _confirm)
    result = json.loads(vault.browser_vault_fill(backend.meta.id, task_id="file-revoke"))
    assert result["success"] is False
    assert result["error_type"] == "origin_alias_revoked"
    assert writes == []


def test_alias_still_present_after_confirmation_fills(monkeypatch):
    vault._alias_fill_decisions.clear()
    vault._alias_fill_refused.clear()
    backend = _Backend(_meta(handle="op:kept-id"))
    _fill_patches(monkeypatch, backend)
    writes, loads = [], []
    monkeypatch.setattr(vault, "_eval_js_secret", lambda *args: writes.append(args) or {
        "success": True, "result": json.dumps({"filled": 1}),
    })
    config = {"vault": {"origin_aliases": {"op:kept-id": ["https://login.gusto.com"]}}}
    with patch("hermes_cli.config.load_config_readonly", side_effect=lambda: loads.append(1) or config):
        result = json.loads(vault.browser_vault_fill(backend.meta.id, task_id="kept"))
    assert result["success"] is True
    assert result["origin"] == "https://login.gusto.com"
    assert len(writes) == 1
    assert len(loads) == 2  # once before the prompt, once after it


def test_saved_origin_fill_skips_prompt_and_alias_recheck(monkeypatch):
    backend = _Backend(_meta(handle="op:saved-id"))
    _fill_patches(monkeypatch, backend, page_origin="https://gusto.com")
    prompts, loads = [], []
    monkeypatch.setattr(vault, "_confirm_alias_fill", lambda *args: prompts.append(args) or "accept")
    with patch("hermes_cli.config.load_config_readonly", side_effect=lambda: loads.append(1) or {"vault": {}}):
        result = json.loads(vault.browser_vault_fill(backend.meta.id, task_id="saved-only"))
    assert result["success"] is True
    assert prompts == []
    assert len(loads) == 1
