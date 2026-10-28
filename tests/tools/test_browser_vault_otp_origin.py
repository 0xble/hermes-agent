"""OTP credentials obey the saved login's exact-origin and alias-consent boundary."""

import json
from unittest.mock import Mock

import pytest

from agent.vault_backends.local import LocalLoginBackend
from agent.vault_store import VaultItemMeta
from tools import browser_vault_tool as vault
from tools.registry import registry


@pytest.fixture
def otp(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    meta = VaultItemMeta(id="op:otp-origin", kind="login", label="Synthetic login",
                         origin="https://login.test", allowed_origins=("https://login.test",),
                         created_at="", has_otp=True)
    backend = LocalLoginBackend()
    backend.name = "onepassword"
    backend.display_name = "1Password"
    backend.get_meta = Mock(return_value=meta)
    backend.resolve_otp = Mock(return_value="246810")
    monkeypatch.setattr("agent.vault_backends.backend_for_handle", lambda _: backend)
    monkeypatch.setattr(vault, "_browser_key", lambda t: t)
    monkeypatch.setattr(vault, "_focus_bound_origin", lambda *_: None)
    monkeypatch.setattr(vault, "_current_page_origin", lambda _: "https://evil.test")
    monkeypatch.setattr(vault, "_eval_js", lambda *_: {"success": True, "result": json.dumps([
        {"index": 0, "type": "text", "autocomplete": "one-time-code"},
    ])})
    write = Mock(return_value={"success": True, "result": {"filled": 1}})
    monkeypatch.setattr(vault, "_eval_js_secret", write)
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: {"vault": {}})
    return backend, write


def _enter(handle="op:otp-origin"):
    # Exercise the actual registered handler, including the page-operation fence.
    return json.loads(registry.dispatch("browser_vault", {"action": "enter_code", "handle": handle}, task_id="otp"))


@pytest.mark.parametrize("origin", ["https://evil.test", "http://login.test", "https://login.test:8443", "https://login.test.evil.test"])
def test_otp_wrong_origin_refuses_before_resolving_code(otp, monkeypatch, origin):
    backend, write = otp
    monkeypatch.setattr(vault, "_current_page_origin", lambda _: origin)
    result = _enter()
    assert result["success"] is False
    assert result["error_type"] == "origin_mismatch"
    backend.resolve_otp.assert_not_called()
    write.assert_not_called()


def test_otp_saved_origin_fills_without_alias_prompt(otp, monkeypatch):
    backend, write = otp
    monkeypatch.setattr(vault, "_current_page_origin", lambda _: "https://login.test")
    consent = Mock(return_value="accept")
    monkeypatch.setattr(vault, "_confirm_alias_fill", consent)
    result = _enter()
    assert result["success"] is True
    assert result["origin"] == "https://login.test"
    assert "246810" not in json.dumps(result)
    consent.assert_not_called()
    backend.resolve_otp.assert_called_once()
    assert '"https://login.test"' in write.call_args.args[1]


def test_otp_alias_origin_requires_consent_once(otp, monkeypatch):
    backend, write = otp
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: {
        "vault": {"origin_aliases": {"op:otp-origin": ["https://evil.test"]}},
    })
    consent = Mock(return_value="accept")
    monkeypatch.setattr(vault, "_confirm_alias_fill", consent)
    assert _enter()["success"]
    assert _enter()["success"]
    consent.assert_called_once_with("Synthetic login", "https://evil.test", ("https://login.test",))
    assert backend.resolve_otp.call_count == 2
    assert write.call_count == 2


def test_otp_alias_origin_skips_consent_when_confirmation_disabled(otp, monkeypatch):
    backend, write = otp
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: {
        "vault": {"confirm_alias_fills": False, "origin_aliases": {"op:otp-origin": ["https://evil.test"]}},
    })
    consent = Mock(return_value="unanswered")
    monkeypatch.setattr(vault, "_confirm_alias_fill", consent)
    assert _enter()["success"]
    consent.assert_not_called()
    backend.resolve_otp.assert_called_once()
    assert write.call_count == 1


def test_otp_disabled_confirmation_logs_cross_domain_warning(otp, monkeypatch, caplog):
    _backend, _write = otp
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: {
        "vault": {"confirm_alias_fills": False, "origin_aliases": {"op:otp-origin": ["https://evil.test"]}},
    })
    with caplog.at_level("INFO", logger="tools.browser_vault_tool"):
        assert _enter()["success"]
    assert "registrable domain differs" in caplog.text


def test_otp_disabled_confirmation_still_refuses_unlisted_origin(otp, monkeypatch):
    backend, write = otp
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: {
        "vault": {"confirm_alias_fills": False, "origin_aliases": {"op:otp-origin": ["https://other.test"]}},
    })
    assert _enter()["error_type"] == "origin_mismatch"
    backend.resolve_otp.assert_not_called()
    write.assert_not_called()


@pytest.mark.parametrize("setting,prompted", [("false", False), (None, True), ('"no"', True)])
def test_confirm_alias_fills_from_real_profile_config(otp, monkeypatch, tmp_path, setting, prompted):
    """Invariant: vault.confirm_alias_fills is read from the profile config.yaml by the real loader."""
    backend, write = otp
    monkeypatch.undo()  # drop the patched loader; re-apply only the page/backend fakes below
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("agent.vault_backends.backend_for_handle", lambda _: backend)
    monkeypatch.setattr(vault, "_browser_key", lambda t: t)
    monkeypatch.setattr(vault, "_focus_bound_origin", lambda *_: None)
    monkeypatch.setattr(vault, "_current_page_origin", lambda _: "https://evil.test")
    monkeypatch.setattr(vault, "_eval_js", lambda *_: {"success": True, "result": json.dumps([
        {"index": 0, "type": "text", "autocomplete": "one-time-code"},
    ])})
    monkeypatch.setattr(vault, "_eval_js_secret", write)
    lines = ["vault:", "  origin_aliases:", "    op:otp-origin:", "      - https://evil.test"]
    if setting is not None:
        lines.insert(1, f"  confirm_alias_fills: {setting}")
    (tmp_path / "config.yaml").write_text("\n".join(lines) + "\n")
    consent = Mock(return_value="unanswered")
    monkeypatch.setattr(vault, "_confirm_alias_fill", consent)
    vault._alias_fill_decisions.clear()
    vault._alias_fill_refused.clear()
    result = _enter()
    assert consent.called is prompted
    assert result["success"] is (not prompted)
    assert write.called is (not prompted)
    monkeypatch.setattr(vault, "_current_page_origin", lambda _: "https://unlisted.test")
    assert _enter()["error_type"] == "origin_mismatch"


@pytest.mark.parametrize("decision,error", [("decline", "origin_alias_declined"),
                                           ("unanswered", "origin_alias_prompt_unanswered")])
def test_otp_alias_refusal_blocks_retry(otp, monkeypatch, decision, error):
    backend, write = otp
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: {
        "vault": {"origin_aliases": {"op:otp-origin": ["https://evil.test"]}},
    })
    consent = Mock(return_value=decision)
    monkeypatch.setattr(vault, "_confirm_alias_fill", consent)
    assert _enter()["error_type"] == error
    assert _enter()["error_type"] == "origin_alias_retry_refused"
    consent.assert_called_once()
    backend.resolve_otp.assert_not_called()
    write.assert_not_called()


def test_otp_alias_revoked_during_confirmation_never_resolves(otp, monkeypatch):
    backend, write = otp
    config = {"vault": {"origin_aliases": {"op:otp-origin": ["https://evil.test"]}}}
    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: config)

    def revoke(*_):
        config["vault"]["origin_aliases"].clear()
        return "accept"

    monkeypatch.setattr(vault, "_confirm_alias_fill", revoke)
    assert _enter()["error_type"] == "origin_alias_revoked"
    backend.resolve_otp.assert_not_called()
    write.assert_not_called()


@pytest.mark.parametrize("kind,error", [(None, "credential_missing"), ("payment", "credential_missing"),
                                       ("unbound", "no_origin")])
def test_otp_invalid_handle_metadata_never_resolves(otp, kind, error):
    backend, write = otp
    if kind is None:
        backend.get_meta.return_value = None
    elif kind == "unbound":
        backend.get_meta.return_value = VaultItemMeta(id="op:otp-origin", kind="login", label="Unbound",
                                                     origin=None, allowed_origins=(), created_at="", has_otp=True)
    else:
        backend.get_meta.return_value = VaultItemMeta(id="op:otp-origin", kind=kind, label="Not a login",
                                                     origin=None, allowed_origins=(), created_at="")
    assert _enter()["error_type"] == error
    backend.resolve_otp.assert_not_called()
    write.assert_not_called()


def test_otp_origin_fallback_accepts_bound_login_without_origin_tuple(otp, monkeypatch):
    backend, _ = otp
    backend.get_meta.return_value = VaultItemMeta(id="op:otp-origin", kind="login", label="Legacy",
                                                 origin="https://login.test", allowed_origins=(), created_at="", has_otp=True)
    monkeypatch.setattr(vault, "_current_page_origin", lambda _: "https://login.test")
    assert _enter()["success"]


def test_otp_navigation_during_inspection_never_resolves(otp, monkeypatch):
    backend, write = otp
    origins = iter(["https://login.test", "https://evil.test"])
    monkeypatch.setattr(vault, "_current_page_origin", lambda _: next(origins))
    assert _enter()["error_type"] == "origin_changed"
    backend.resolve_otp.assert_not_called()
    write.assert_not_called()


def test_focus_does_not_authorize_requested_origin_after_navigation(monkeypatch):
    monkeypatch.setattr("tools.browser_camofox.is_camofox_mode", lambda: False)
    supervisor = Mock()
    supervisor.focus_page.return_value = {"ok": True, "url": "https://login.test/otp"}
    monkeypatch.setattr(vault, "_ensure_supervisor", lambda _: supervisor)
    monkeypatch.setattr(vault, "_current_page_origin", lambda _: "https://evil.test")
    assert vault._focus_bound_origin("otp", "https://login.test", "otp") == "https://evil.test"
