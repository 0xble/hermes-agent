"""Named route identity and durable/legacy pool storage share one credential owner."""
from contextlib import nullcontext
import json
from types import SimpleNamespace

import httpx
import pytest
from openai import APIStatusError

from agent import auxiliary_client as aux
import hermes_yaml

URL = "https://named-proxy.invalid/v1"
MODEL = "owned-model"


def _row(identifier, token, *, source="manual", priority=0):
    return {"id": identifier, "label": identifier, "auth_type": "api_key", "source": source,
            "priority": priority, "access_token": token}


def _home(tmp_path, monkeypatch, providers, pools):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(hermes_yaml.safe_dump({"providers": providers}))
    (tmp_path / "auth.json").write_text(json.dumps({"version": 1, "providers": {}, "credential_pool": pools}))
    monkeypatch.setattr(aux, "_client_cache", {})


@pytest.mark.parametrize("status", [401, 402])
@pytest.mark.parametrize("storage", ["durable", "legacy", "both"])
@pytest.mark.parametrize("private", [False, True])
def test_named_call_recovers_owned_pool_and_rebuilds_cached_client(tmp_path, monkeypatch, status, storage, private):
    from agent.incognito import incognito_scope, register_session, set_private_route

    owned_key = "proxy" if storage != "legacy" else "custom:proxy-display"
    pools = {owned_key: [_row("first", "owned-a"), _row("second", "owned-b", priority=1)]}
    if storage == "both":
        pools["custom:proxy-display"] = [_row("legacy", "legacy-key")]
    _home(tmp_path, monkeypatch, {"proxy": {"name": "Proxy Display", "base_url": URL,
                                           "default_model": MODEL}}, pools)
    attempts = []
    clients = []
    response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))])

    def client_factory(**kwargs):
        client = SimpleNamespace(**kwargs)
        clients.append(client)

        def create(**request):
            attempts.append((client.api_key, str(client.base_url).rstrip("/"), request["model"]))
            assert len(attempts) <= 2, "recovery must be bounded to the failed key and its successor"
            if client.api_key == "owned-a":
                wire = httpx.Response(status, request=httpx.Request("POST", URL + "/chat/completions"))
                raise APIStatusError("authentication failed" if status == 401 else "billing quota exceeded",
                                     response=wire, body={"error": {"message": "rejected"}})
            assert client.api_key == "owned-b"
            return response

        client.chat = SimpleNamespace(completions=SimpleNamespace(create=create))
        return client

    monkeypatch.setattr(aux, "OpenAI", client_factory)
    route = "custom:proxy"
    assert aux._normalize_aux_provider(route) == route
    initial, model = aux._get_cached_client(route, MODEL, task="compression")
    assert (initial.api_key, model) == ("owned-a", MODEL)
    before = json.loads((tmp_path / "auth.json").read_text())["credential_pool"]
    session = "synthetic-pool-recovery"
    if private:
        register_session(session)
        set_private_route([session], {"provider": route, "model": MODEL})
    try:
        with incognito_scope(True, session) if private else nullcontext():
            result = aux.call_llm(task="compression", provider=route, model=MODEL,
                                  messages=[{"role": "user", "content": "synthetic input"}], fallback_chain=[])
    finally:
        if private:
            register_session(session, False)
    assert result is response
    assert attempts == [("owned-a", URL, MODEL), ("owned-b", URL, MODEL)]
    rebuilt, model = aux._get_cached_client(route, MODEL, task="compression")
    assert rebuilt is not initial and rebuilt.api_key == "owned-b"
    assert aux._get_cached_client(route, MODEL, task="compression")[0] is rebuilt
    after = json.loads((tmp_path / "auth.json").read_text())["credential_pool"]
    rows = {row["id"]: row for row in after[owned_key]}
    assert rows["first"]["last_status"] == "exhausted"
    assert rows["first"]["last_error_code"] == status
    assert rows["second"].get("last_status") is None
    if storage == "both":
        assert after["custom:proxy-display"] == before["custom:proxy-display"]


@pytest.mark.parametrize("owner_present", [False, True])
def test_named_pool_filters_builtin_rows_and_missing_owner_fails_closed(tmp_path, monkeypatch, owner_present):
    from agent.custom_entry_pool import pool_for_aux_provider
    from agent.credential_pool import load_pool

    monkeypatch.setenv("DEEPSEEK_API_KEY", "foreign-builtin")
    entry = {"base_url": URL, "api_key": "entry-inline", "default_model": MODEL}
    pools = {"deepseek": [_row("foreign", "foreign-builtin", source="env:DEEPSEEK_API_KEY"),
                           _row("first", "owned-a", priority=1), _row("second", "owned-b", priority=2)],
             "custom:deepseek": [_row("orphan", "orphan-key")] if not owner_present else []}
    _home(tmp_path, monkeypatch, {"deepseek": entry} if owner_present else {}, pools)
    # Built-ins retain their ordinary, unfiltered pool; naming the entry never borrows that identity.
    builtin = pool_for_aux_provider("deepseek", "deepseek", load_pool)
    assert "foreign-builtin" in {row.access_token for row in builtin.entries()}
    named = pool_for_aux_provider("custom:deepseek", "custom:deepseek", load_pool)
    if not owner_present:
        assert named is None, "a removed named owner must not load orphan or built-in credentials"
        return
    assert {row.access_token for row in named.entries()} == {"owned-a", "owned-b"}
    assert named.select().access_token == "owned-a"
    before = json.loads((tmp_path / "auth.json").read_text())["credential_pool"]["deepseek"]
    error = type("AuthError", (Exception,), {"status_code": 401})("authentication failed")
    assert aux._recover_provider_pool("custom:deepseek", error, failed_api_key="owned-a") is True
    after = json.loads((tmp_path / "auth.json").read_text())["credential_pool"]["deepseek"]
    assert next(row for row in after if row["id"] == "foreign") == next(row for row in before if row["id"] == "foreign")
    assert pool_for_aux_provider("custom:deepseek", "custom:deepseek", load_pool).peek().access_token == "owned-b"


@pytest.mark.parametrize("error_type", [OSError, ValueError, TypeError, AttributeError])
def test_named_pool_load_failure_tries_only_owned_legacy_candidate(tmp_path, monkeypatch, error_type):
    from agent.custom_entry_pool import pool_for_aux_provider
    from agent.credential_pool import load_pool

    _home(tmp_path, monkeypatch, {"proxy": {"name": "Proxy Display", "base_url": URL}},
          {"custom:proxy-display": [_row("legacy", "owned-legacy")]})
    attempted = []

    def load(key):
        attempted.append(key)
        if key == "proxy":
            raise error_type("unreadable or malformed durable pool")
        return load_pool(key)

    pool = pool_for_aux_provider("custom:proxy", "custom:proxy", load)
    assert attempted == ["proxy", "custom:proxy-display"]
    assert pool.peek().access_token == "owned-legacy"
    attempted.clear()
    assert pool_for_aux_provider("custom:removed", "custom:removed", load) is None
    assert attempted == []
