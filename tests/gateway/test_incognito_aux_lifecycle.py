"""Auxiliary deferred-work ownership: privacy, pinned aliases and MCP callbacks.

These are in-process boundary tests, not live SDK/network qualification. Keep the
transport doubled, but use the real routing, stream and callback implementations.
"""
import asyncio
import contextvars
import gc
import json
import logging
from types import SimpleNamespace

import pytest

from agent import auxiliary_call_policy as policy
from agent import auxiliary_client as aux
from agent import incognito as privacy
from tools import mcp_tool_handlers as handlers
from tools import mcp_tool_sampling as sampling

SENTINEL = "PRIVATE_AUX_LIFECYCLE_FIXTURE"
ROUTE = {"provider": "custom:vault", "model": "private-model",
         "base_url": "https://vault.invalid/alias/v1", "api_mode": "chat_completions"}


@pytest.fixture
def private_owner():
    sid = "aux-lifecycle-owned-fixture"
    factory = logging.getLogRecordFactory()
    privacy.register_session(sid)
    privacy.set_private_route((sid,), ROUTE)
    try:
        yield sid
    finally:
        privacy.register_session(sid, False)
        logging.setLogRecordFactory(factory)


def test_deferred_stream_retains_privacy_after_off(private_owner, caplog):
    from agent.credential_pool_errors import _withhold_private_error_text
    updates, closed = {}, []

    def source():
        updates.update(_withhold_private_error_text({"last_error_message": SENTINEL}))
        logging.getLogger(__name__).warning("%s", SENTINEL)
        yield "chunk"

    inner = policy._OwnedStream(source(), lambda: closed.append(privacy.is_incognito()))
    with privacy.incognito_scope(True, private_owner):
        stream = policy.scoped_fallback_stream(inner, policy.CallFallbackPolicy("mcp", []), None, None)
    privacy.register_session(private_owner, False)
    assert next(stream) == "chunk"
    assert not privacy.is_incognito()
    stream.close()
    stream.close()
    assert closed == [True]
    assert updates["last_error_message"] is None
    assert SENTINEL not in caplog.text


@pytest.mark.parametrize("cleanup", ["close", "collect"])
def test_unstarted_stream_cleanup_retains_owner(private_owner, cleanup):
    closed = []
    inner = policy._OwnedStream(iter([]), lambda: closed.append(privacy.is_incognito()))
    with privacy.incognito_scope(True, private_owner):
        stream = policy.scoped_fallback_stream(inner, policy.CallFallbackPolicy("mcp", []), None, None)
    privacy.register_session(private_owner, False)
    if cleanup == "close":
        stream.close()
    else:
        del stream
        gc.collect()
    assert closed == [True]
    assert not privacy.is_incognito()


def test_partial_stream_error_never_replays(private_owner):
    recovered = []

    def source():
        yield "role-only-frame"
        raise RuntimeError(SENTINEL)

    with privacy.incognito_scope(True, private_owner):
        raw = policy.stream_with_recovery(source, lambda exc: recovered.append(exc))
        stream = policy.scoped_fallback_stream(raw, policy.CallFallbackPolicy("mcp", []), None, None)
    assert next(stream) == "role-only-frame"
    with pytest.raises(RuntimeError, match=SENTINEL):
        next(stream)
    assert recovered == []
    assert not privacy.is_incognito()


def test_named_alias_keeps_pinned_endpoint_and_rotating_entry_key(private_owner, monkeypatch):
    entry = {"name": "vault", "base_url": "https://vault.invalid/default/v1",
             "api_key": "fixture-key", "extra_headers": {"X-Route": "vault"}}
    monkeypatch.setattr("hermes_cli.runtime_provider._get_named_custom_provider", lambda provider: entry)
    monkeypatch.setattr(aux, "_named_custom_openai_wire_client",
                        lambda base, key, headers: SimpleNamespace(base_url=base, api_key=key, headers=headers))
    monkeypatch.setattr(aux, "_wrap_transport", lambda req, client, *args: client)
    monkeypatch.setattr(aux, "_is_actual_auxiliary_route", lambda *args: False)
    calls = []

    def rotated(entry, provider, base):
        calls.append((provider, base))
        return f"entry-key-{len(calls)}"

    monkeypatch.setattr(aux, "_named_custom_api_key", rotated)
    with privacy.incognito_scope(True, private_owner), policy.private_route_scope((), {}, sync=True) as kwargs:
        req = SimpleNamespace(provider=kwargs["provider"], original_provider=kwargs["provider"],
                              model=kwargs["model"], explicit_base_url=kwargs["base_url"],
                              explicit_api_key=kwargs["api_key"], api_mode=kwargs["api_mode"],
                              main_runtime=None, async_mode=False)
        first, model = aux._resolve_named_custom_branch(req)
        second, _ = aux._resolve_named_custom_branch(req)
    assert first.base_url == second.base_url == ROUTE["base_url"]
    assert model == ROUTE["model"]
    assert [first.api_key, second.api_key] == ["entry-key-1", "entry-key-2"]
    assert first.headers == {"X-Route": "vault"}


@pytest.mark.parametrize("alias_key", ["host-scoped-key", ""])
def test_foreign_alias_never_borrows_named_entry_secrets(private_owner, monkeypatch, alias_key):
    entry = {"name": "vault", "base_url": "https://vault.invalid/v1", "api_key": "named-secret",
             "key_env": "NAMED_KEY", "key_cmd": "must-not-run", "extra_headers": {"Authorization": "named-secret"}}
    req = SimpleNamespace(provider="custom:vault", explicit_api_key=None, main_runtime=None)
    monkeypatch.setattr(aux, "_read_main_api_key_if_same_origin", lambda base: alias_key)
    monkeypatch.setattr(aux, "_named_custom_api_key", lambda *args: pytest.fail("foreign entry credential lookup"))
    with privacy.incognito_scope(True, private_owner), policy.private_route_scope((), {}, sync=True):
        key, headers = policy.named_route_credentials(req, entry, "https://alias.invalid/v1")
    assert key == (alias_key or "no-key-required")
    assert headers is None


@pytest.mark.parametrize("op", ["resources/list", "resources/read", "prompts/list", "prompts/get"])
def test_utility_rpc_captures_and_clears_callback_owner(private_owner, monkeypatch, op):
    from tools import mcp_tool_discovery
    server = SimpleNamespace(session=object(), _rpc_lock=asyncio.Lock(), _inflight_tasks=set(),
                             _pending_call_context=None)
    monkeypatch.setattr(mcp_tool_discovery, "_get_connected_server_for_call", lambda name: server)
    monkeypatch.setattr(handlers._loop, "_run_on_mcp_loop",
                        lambda call, timeout: contextvars.Context().run(asyncio.run, call()))
    observed = []

    async def rpc(session, args, name):
        captured = server._pending_call_context
        observed.append(captured is not None and captured.copy().run(privacy.is_incognito))
        return "ok"

    handler = handlers._make_utility_handler(op, op, rpc, lambda result, name: {"result": result})("fixture", 5)
    with privacy.incognito_scope(True, private_owner):
        assert json.loads(handler({})) == {"result": "ok"}
    assert observed == [True]
    assert server._pending_call_context is None
    assert not server._inflight_tasks
    assert not privacy.is_incognito()
    handler({})
    assert observed == [True, False]


@pytest.mark.asyncio
async def test_sampling_argument_parse_logs_use_owner_scope(private_owner, monkeypatch, caplog):
    observed = []
    function = SimpleNamespace(name="fixture", arguments=SENTINEL)
    response = SimpleNamespace(model="private-model", usage=SimpleNamespace(total_tokens=1), choices=[
        SimpleNamespace(finish_reason="tool_calls", message=SimpleNamespace(
            content=None, tool_calls=[SimpleNamespace(id="fixture", function=function)]))])

    def llm(**kwargs):
        observed.append(privacy.is_incognito())
        return response

    monkeypatch.setattr(aux, "call_llm", llm)
    with privacy.incognito_scope(True, private_owner):
        captured = contextvars.copy_context()
    handler = sampling.SamplingHandler("fixture", {}, call_context=lambda: captured)
    params = SimpleNamespace(messages=[SimpleNamespace(role="user", content=SimpleNamespace(type="text", text=SENTINEL))],
                             max_tokens=32, temperature=None, tools=None)
    result = await handler(None, params)
    assert result.content[0].input == {"_raw": SENTINEL}
    assert observed == [True]
    assert SENTINEL not in caplog.text
    assert not privacy.is_incognito()


@pytest.mark.asyncio
async def test_sampling_admission_runs_in_owner_scope(private_owner, monkeypatch):
    with privacy.incognito_scope(True, private_owner):
        captured = contextvars.copy_context()
    handler = sampling.SamplingHandler("fixture", {}, call_context=lambda: captured)
    observed = []

    def admit(params):
        observed.append(privacy.is_incognito())
        return None, "fixture-admission-result"

    monkeypatch.setattr(handler, "_admit", admit)
    assert await handler(None, SimpleNamespace()) == "fixture-admission-result"
    assert observed == [True]
    assert not privacy.is_incognito()
