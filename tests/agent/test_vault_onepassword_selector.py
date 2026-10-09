"""Metadata-only selector regressions; no real password manager is invoked."""
import json
from unittest.mock import Mock, call, patch

import pytest

from agent.vault_backends.onepassword import OnePasswordLoginBackend


def item(item_id="item-a", vault: object = "vault-a"):
    return {"id": item_id, "title": "Example", "category": "LOGIN", "vault": {"id": vault},
            "urls": [{"href": "https://example.com/login"}]}


@pytest.fixture
def later(monkeypatch):
    """Keep a deterministic clock for callers that assert display-cache freshness."""
    from agent.vault_backends import onepassword
    now = [1000.0]
    monkeypatch.setattr(onepassword.time, "monotonic", lambda: now[0])

    def advance():
        now[0] += onepassword._LISTING_TTL_SECONDS + 1
    return advance


@pytest.fixture
def backend():
    with patch("agent.secret_scope.get_secret", return_value="dummy-service-token"):
        backend = OnePasswordLoginBackend()
    backend._run = Mock()
    return backend


@pytest.mark.parametrize("method,flags,value", [
    ("resolve_password", ("--fields", "label=password", "--reveal"), "dummy-password"),
    ("resolve_otp", ("--otp",), "123456"),
])
def test_legacy_handles_resolve_each_items_vault(backend, method, flags, value):
    for item_id, vault_id in [("item-a", "vault-a"), ("item-b", "vault-b")]:
        backend._listing_vault_hint = Mock(return_value=vault_id)
        backend._run.reset_mock()
        backend._run.side_effect = [json.dumps(item(item_id, vault_id)), value + "\r\n"]
        assert getattr(backend, method)("op:" + item_id) == value
        assert backend._run.call_args_list[0].args == (
            "item", "get", item_id, "--vault", vault_id, "--format", "json")
        assert backend._run.call_args_list[1].args == (
            "item", "get", item_id, "--vault", vault_id, *flags)


@pytest.mark.parametrize("metadata", [
    None, item("other"), item(vault=""), item(vault=None),
    dict(item(), vault="not-a-record"),
])
@pytest.mark.parametrize("method", ["resolve_password", "resolve_otp"])
def test_missing_or_invalid_metadata_never_reads_secret(backend, metadata, method):
    backend._listing_vault_hint = Mock(return_value="vault-a")
    backend._run.return_value = json.dumps(metadata) if metadata is not None else "[]"
    if method == "resolve_password":
        with pytest.raises(RuntimeError):
            backend.resolve_password("op:item-a")
    else:
        assert backend.resolve_otp("op:item-a") is None
    assert all("--reveal" not in c.args for c in backend._run.call_args_list)


def test_cold_missing_item_uses_one_fresh_listing(backend):
    from agent.vault_backends import onepassword
    onepassword.invalidate_listing_cache()
    backend._run.return_value = "[]"
    assert backend._listing_vault_hint("missing-item") is None
    assert backend._run.call_count == 1


def test_permission_error_naming_vault_does_not_retry_or_relist(backend):
    backend._listing_vault_hint = Mock(return_value="vault-a")
    backend._run.side_effect = RuntimeError("permission denied reading vault vault-a")
    with pytest.raises(RuntimeError, match="permission denied"):
        backend.get_meta("op:item-a")
    assert backend._listing_vault_hint.call_args_list == [call("item-a")]


def test_moved_item_retries_once_with_a_fresh_listing(backend):
    backend._listing_vault_hint = Mock(side_effect=["vault-old", "vault-new"])
    backend._run.side_effect = [
        RuntimeError("op failed: item not found in vault"),
        json.dumps(item(vault="vault-new")),
        "dummy-password\n",
    ]
    assert backend.resolve_password("op:item-a") == "dummy-password"
    assert backend._listing_vault_hint.call_args_list[0].kwargs == {}
    assert backend._listing_vault_hint.call_args_list[1].kwargs == {"fresh": True}
    assert backend._run.call_args_list[0].args == (
        "item", "get", "item-a", "--vault", "vault-old", "--format", "json")
    assert backend._run.call_args_list[1].args == (
        "item", "get", "item-a", "--vault", "vault-new", "--format", "json")


@pytest.mark.parametrize("method", ["resolve_password", "resolve_otp"])
def test_refresh_failure_does_not_reuse_old_vault(backend, method):
    backend._listing_vault_hint = Mock(return_value="vault-a")
    backend._run.return_value = json.dumps(item())
    assert backend.get_meta("op:item-a") is not None
    backend.discard_fill_metadata("op:item-a")
    backend._run.reset_mock()
    backend._run.side_effect = [RuntimeError("metadata unavailable")]
    if method == "resolve_password":
        with pytest.raises((RuntimeError, ValueError)):
            backend.resolve_password("op:item-a")
    else:
        assert backend.resolve_otp("op:item-a") is None
    assert all("--reveal" not in c.args for c in backend._run.call_args_list)


@pytest.mark.parametrize("handle", ["bw:item-a", "op:", "op:--help", "op:vault-a:item-a"])
def test_invalid_handles_fail_before_cli(backend, handle):
    with pytest.raises(ValueError):
        backend.resolve_password(handle)
    assert backend.resolve_otp(handle) is None
    backend._run.assert_not_called()


def test_fill_metadata_expires_before_secret_read(backend, monkeypatch):
    from agent.vault_backends import onepassword
    now = [1000.0]
    monkeypatch.setattr(onepassword.time, "monotonic", lambda: now[0])
    backend._listing_vault_hint = Mock(return_value="vault-a")
    backend._run.side_effect = [json.dumps(item()), json.dumps(item()), "dummy-password\n"]
    assert backend.get_meta("op:item-a") is not None
    now[0] += onepassword._FILL_METADATA_TTL_SECONDS + 1
    assert backend.resolve_password("op:item-a") == "dummy-password"
    metadata_calls = [call for call in backend._run.call_args_list if "--format" in call.args]
    assert len(metadata_calls) == 2


def test_missing_vault_fails_closed_for_all_cli_accounts(backend):
    backend._listing_vault_hint = Mock(return_value=None)
    backend._run.side_effect = [json.dumps([item(vault="")])]
    with pytest.raises(RuntimeError):
        backend.resolve_password("op:item-a")
    backend._run.assert_not_called()


def test_fresh_backend_uses_one_cold_listing_then_verified_vault(backend):
    backend._listing_vault_hint = Mock(return_value="vault-new")
    backend._run.side_effect = [json.dumps(item(vault="vault-new")), "dummy-password\n"]
    assert backend.resolve_password("op:item-a") == "dummy-password"
    assert backend._run.call_args_list[0].args == (
        "item", "get", "item-a", "--vault", "vault-new", "--format", "json")
