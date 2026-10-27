"""Lazy OTP availability uses synthetic metadata only, never a live manager."""

import json
import os
from unittest.mock import Mock

import pytest

from agent.vault_backends import onepassword
from tools.browser_vault_tool import browser_vault_list

ORIGIN = "https://login.example"


@pytest.fixture
def manager(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    for key in list(os.environ):
        if key.startswith("OP_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("OP_SERVICE_ACCOUNT_TOKEN", "synthetic-token")
    onepassword.invalidate_listing_cache()
    backend = onepassword.OnePasswordLoginBackend()
    listing = [{"id": "item-a", "title": "Synthetic login", "category": "LOGIN",
                "vault": {"id": "vault-a"}, "urls": [{"href": ORIGIN}]}]
    detail = {**listing[0], "fields": [{"id": "totp", "type": "OTP", "value": "not-for-output"}]}
    reads = []

    def run(*args):
        reads.append(args)
        if args[:2] == ("item", "list"):
            return json.dumps(listing)
        assert args == ("item", "get", "item-a", "--vault", "vault-a", "--format", "json")
        if isinstance(detail.get("error"), Exception):
            raise detail["error"]
        return json.dumps(detail)

    monkeypatch.setattr(backend, "_run", run)
    monkeypatch.setattr("agent.vault_backends.enabled_backends", lambda: [backend])
    monkeypatch.setattr(backend, "resolve_otp", Mock(side_effect=AssertionError("must not mint OTP")))
    monkeypatch.setattr(backend, "resolve_password", Mock(side_effect=AssertionError("must not read password")))
    yield backend, listing, detail, reads
    onepassword.invalidate_listing_cache()


def listed(**kwargs):
    result = json.loads(browser_vault_list(**kwargs))
    assert result["success"]
    assert "not-for-output" not in json.dumps(result)
    return result["items"]


def test_origin_scoped_available_then_reuses_listing_cache(manager):
    backend, _, _, reads = manager
    assert listed()[0]["otp_available"] is None
    assert len(reads) == 1
    item = listed(origin=ORIGIN)[0]
    assert item["otp_available"] is True
    assert item["two_factor"] == "automatic"
    assert len(reads) == 2
    assert listed(origin=ORIGIN)[0]["otp_available"] is True
    assert listed()[0]["otp_available"] is True
    assert len(reads) == 2
    assert "not-for-output" not in repr(onepassword._LISTING_CACHE)
    assert not hasattr(backend._fill_metadata, "item")


@pytest.mark.parametrize("needs_unlock", [True, False])
def test_known_absent_is_not_unknown_and_is_cached(manager, needs_unlock):
    backend, _, detail, reads = manager
    backend.needs_unlock = needs_unlock
    detail["fields"] = [{"id": "password", "type": "CONCEALED", "value": "not-for-output"}]
    item = listed(origin=ORIGIN)[0]
    assert item["otp_available"] is False
    assert "automatic" not in item["two_factor"]
    assert listed(origin=ORIGIN)[0]["otp_available"] is False
    assert len(reads) == 2


@pytest.mark.parametrize("change", [
    {"error": RuntimeError("not-for-output")}, {"fields": None},
    {"fields": [None]}, {"id": "wrong-id"}, {"vault": {"id": "wrong-vault"}},
    {"state": "ARCHIVED"}, {"category": "CREDIT_CARD"},
])
def test_failure_or_incomplete_detail_stays_unknown(manager, change):
    _, _, detail, _ = manager
    detail.update(change)
    item = listed(origin=ORIGIN)[0]
    assert item["otp_available"] is None
    assert item["two_factor"] != "automatic"


@pytest.mark.parametrize("origin", [
    None, "https://elsewhere.example", "http://login.example",
    "https://child.login.example", "https://login.example:8443",
])
def test_no_details_for_unfiltered_or_wrong_origins(manager, origin):
    _, _, _, reads = manager
    listed(origin=origin)
    assert all(args[:2] == ("item", "list") for args in reads)


def test_no_bulk_detail_reads_for_ambiguous_origin(manager):
    _, listing, _, reads = manager
    listing.append({**listing[0], "id": "item-b"})
    assert all(item["otp_available"] is None for item in listed(origin=ORIGIN))
    assert len(reads) == 1


def test_cache_expiry_and_invalidation_refresh_availability(manager, monkeypatch):
    _, _, detail, reads = manager
    now = [100.0]
    monkeypatch.setattr(onepassword.time, "monotonic", lambda: now[0])
    assert listed(origin=ORIGIN)[0]["otp_available"] is True
    detail["fields"] = []
    now[0] += onepassword._LISTING_TTL_SECONDS + 1
    assert listed(origin=ORIGIN)[0]["otp_available"] is False
    assert len(reads) == 4
    detail["fields"] = [{"type": "OTP"}]
    onepassword.invalidate_listing_cache()
    assert listed(origin=ORIGIN)[0]["otp_available"] is True
    assert len(reads) == 6


def test_credential_scope_does_not_reuse_another_tokens_availability(manager):
    backend, _, detail, reads = manager
    assert listed(origin=ORIGIN)[0]["otp_available"] is True
    backend._service_token = "another-synthetic-token"
    detail["fields"] = []
    assert listed(origin=ORIGIN)[0]["otp_available"] is False
    assert len(reads) == 4


def test_inflight_invalidation_does_not_resurrect_availability(manager, monkeypatch):
    backend, _, _, _ = manager
    run = backend._run

    def invalidating_run(*args):
        result = run(*args)
        if args[:2] == ("item", "get"):
            onepassword.invalidate_listing_cache()
        return result

    monkeypatch.setattr(backend, "_run", invalidating_run)
    assert listed(origin=ORIGIN)[0]["otp_available"] is True
    assert not onepassword._LISTING_CACHE


def test_nonlogin_filter_does_not_read_details(manager):
    _, _, _, reads = manager
    assert listed(kind="payment", origin=ORIGIN) == []
    assert len(reads) == 1


def test_locked_backend_never_prompts_or_reads(manager, monkeypatch):
    backend, _, _, reads = manager
    monkeypatch.setattr(backend, "is_unlocked", lambda: False)
    monkeypatch.setattr(backend, "unlock", Mock(side_effect=AssertionError("must not prompt")))
    result = json.loads(browser_vault_list(origin=ORIGIN))
    assert result["items"] == [] and result["locked"]
    assert reads == []
