"""Named authority is checked against real SDK request authentication, offline."""
import hashlib
import json
from types import SimpleNamespace

import httpx
import pytest

from agent.chat_completion_helpers import enforce_delegation_pin
from tools.custom_subagents import RuntimePin, ResolvedRoute


def fixture(client, *, anthropic=False, overrides=None, fallback=False):
    mode = "anthropic_messages" if anthropic else "chat_completions"
    provider = "anthropic" if anthropic else "custom"
    key = "synthetic-frozen"
    base = "https://fixture.invalid"
    overrides = overrides or {}
    digest = hashlib.sha256(key.encode()).hexdigest()
    route = ResolvedRoute(provider, "m", base, mode, None, key, digest, json.dumps(overrides, sort_keys=True, separators=(",", ":")))
    pin = RuntimePin("worker", "primary" if fallback else provider, "primary" if fallback else "m",
                     base, mode, None, digest, True, None, (route,) if fallback else (),
                     route.request_overrides_json)
    child = SimpleNamespace(provider=provider, model="m", base_url=base, api_mode=mode,
                            api_key=key, request_overrides=overrides, client=client, _delegation_runtime_pin=pin)
    return child


@pytest.mark.parametrize("anthropic", [False, True])
@pytest.mark.parametrize("fallback", [False, True])
@pytest.mark.parametrize("changed", ["request", "default", "missing", "bearer"])
def test_changed_sdk_auth_is_refused_before_transport(anthropic, fallback, changed):
    import openai
    import anthropic as ant
    calls = []
    transport = httpx.MockTransport(lambda request: calls.append(request) or httpx.Response(200, json={}))
    options = {"api_key": "synthetic-frozen", "base_url": "https://fixture.invalid", "http_client": httpx.Client(transport=transport)}
    header = "X-Api-Key" if anthropic else "Authorization"
    value = "foreign" if anthropic else "Bearer foreign"
    if changed == "default":
        options["default_headers"] = {header: value}
    client = ant.Anthropic(**options) if anthropic else openai.OpenAI(**options)
    if changed in {"missing", "bearer"}:
        client.api_key = None
        if changed == "bearer":
            client.auth_token = "foreign"
    child = fixture(client, anthropic=anthropic, fallback=fallback)
    kwargs = {"model": "m", "messages": [{"role": "user", "content": "x"}]}
    if changed == "request":
        kwargs["extra_headers"] = {header: value}
    try:
        with pytest.raises(ValueError):
            enforce_delegation_pin(child, kwargs, client=client)
        assert calls == []
    finally:
        client.close()


@pytest.mark.parametrize("fallback", [False, True])
def test_frozen_anthropic_bearer_and_omit_send_one_authorized_credential(fallback):
    import anthropic
    calls = []
    def response(request):
        calls.append(request)
        return httpx.Response(200, json={"id": "msg", "type": "message", "role": "assistant", "model": "m", "content": [], "stop_reason": "end_turn", "usage": {"input_tokens": 1, "output_tokens": 1}})
    client = anthropic.Anthropic(api_key="ambient-unused", auth_token="synthetic-frozen",
        base_url="https://fixture.invalid", default_headers={"X-Api-Key": anthropic.Omit()},
        http_client=httpx.Client(transport=httpx.MockTransport(response)))
    child = fixture(client, anthropic=True, fallback=fallback)
    kwargs = {"model": "m", "max_tokens": 1, "messages": [{"role": "user", "content": "x"}]}
    try:
        enforce_delegation_pin(child, kwargs, client=client)
        client.messages.create(**kwargs)
        assert calls[0].headers["Authorization"] == "Bearer synthetic-frozen"
        assert "x-api-key" not in calls[0].headers
    finally:
        client.close()


def test_explicit_frozen_header_auth_is_allowed_but_cannot_be_removed():
    import openai
    client = openai.OpenAI(api_key="synthetic-frozen", base_url="https://fixture.invalid",
        http_client=httpx.Client(transport=httpx.MockTransport(lambda _: pytest.fail("unexpected transport"))))
    override = {"extra_headers": {"Authorization": "Bearer explicit-frozen"}}
    child = fixture(client, overrides=override)
    try:
        enforce_delegation_pin(child, {"model": "m", **override}, client=client)
        with pytest.raises(ValueError):
            enforce_delegation_pin(child, {"model": "m"}, client=client)
    finally:
        client.close()


@pytest.mark.parametrize("changed", ["dynamic", "hook", "http_auth", "duplicate"])
def test_unverifiable_or_duplicate_auth_is_rejected_without_invoking_it(changed):
    import openai
    options = {"api_key": "synthetic-frozen", "base_url": "https://fixture.invalid"}
    http_options = {"transport": httpx.MockTransport(lambda _: pytest.fail("unexpected transport"))}
    if changed == "dynamic":
        options["api_key"] = lambda: pytest.fail("evaluated dynamic authority")
    elif changed == "hook":
        http_options["event_hooks"] = {"request": [lambda _: pytest.fail("executed auth hook")]}
    elif changed == "http_auth":
        http_options["auth"] = ("user", "foreign")
    elif changed == "duplicate":
        options["default_headers"] = {"authorization": "Bearer foreign"}
    client = openai.OpenAI(**options, http_client=httpx.Client(**http_options))
    child = fixture(client)
    try:
        with pytest.raises(ValueError, match="cannot be verified"):
            enforce_delegation_pin(child, {"model": "m"}, client=client)
    finally:
        client.close()


def test_transition_probe_defers_final_header_requirement_but_final_request_does_not():
    import openai
    client = openai.OpenAI(api_key="synthetic-frozen", base_url="https://fixture.invalid",
        http_client=httpx.Client(transport=httpx.MockTransport(lambda _: pytest.fail("unexpected transport"))))
    child = fixture(client, fallback=True, overrides={"extra_headers": {"Authorization": "Bearer explicit"}})
    try:
        child._delegation_runtime_pin.validate_request(child, {"model": "m"}, client=client, final_request=False)
        with pytest.raises(ValueError):
            enforce_delegation_pin(child, {"model": "m"}, client=client)
        client.base_url = "https://fixture.invalid/other-route"
        with pytest.raises(ValueError, match="route changed"):
            enforce_delegation_pin(child, {"model": "m", "extra_headers": {"Authorization": "Bearer explicit"}}, client=client)
    finally:
        client.close()


@pytest.mark.parametrize("anthropic", [False, True])
@pytest.mark.parametrize("value", ["synthetic-frozen", "explicit-frozen"])
def test_lowercase_frozen_override_sends_exactly_one_physical_auth_header(anthropic, value):
    import openai
    import anthropic as ant
    calls = []
    def response(request):
        calls.append(request)
        payload = {"id": "msg", "type": "message", "role": "assistant", "model": "m", "content": [], "stop_reason": "end_turn", "usage": {"input_tokens": 1, "output_tokens": 1}} if anthropic else {"id": "chat", "choices": [], "model": "m", "object": "chat.completion", "created": 0}
        return httpx.Response(200, json=payload)
    options = {"api_key": "synthetic-frozen", "base_url": "https://fixture.invalid", "http_client": httpx.Client(transport=httpx.MockTransport(response))}
    client = ant.Anthropic(**options) if anthropic else openai.OpenAI(**options)
    name = "x-api-key" if anthropic else "authorization"
    value = value if anthropic else "Bearer " + value
    overrides = {"extra_headers": {name: value}}
    child = fixture(client, anthropic=anthropic, overrides=overrides)
    kwargs = {"model": "m", "messages": [{"role": "user", "content": "x"}], **overrides}
    if anthropic:
        kwargs["max_tokens"] = 1
    try:
        before = dict(client._custom_headers)
        enforce_delegation_pin(child, kwargs, client=client)
        (client.messages.create if anthropic else client.chat.completions.create)(**kwargs)
        assert [v.decode() for k, v in calls[0].headers.raw if k.lower() == name.encode()] == [value]
        assert dict(client._custom_headers) == before
    finally:
        client.close()


@pytest.mark.parametrize("anthropic", [False, True])
def test_primary_sdk_tenant_path_is_frozen(anthropic):
    import openai
    import anthropic as ant
    client = (ant.Anthropic if anthropic else openai.OpenAI)(api_key="synthetic-frozen", base_url="https://fixture.invalid/team-b/v1",
        http_client=httpx.Client(transport=httpx.MockTransport(lambda _: pytest.fail("transport"))))
    child = fixture(client, anthropic=anthropic)
    from dataclasses import replace
    child.base_url = "https://fixture.invalid/team-a/v1"
    child._delegation_runtime_pin = replace(child._delegation_runtime_pin, base_url=child.base_url)
    try:
        with pytest.raises(ValueError, match="route changed"):
            enforce_delegation_pin(child, {"model": "m"}, client=client)
    finally:
        client.close()



def test_anthropic_primary_uses_exact_constructor_tenant_path_and_sends_one_bearer():
    from dataclasses import replace
    import anthropic
    from agent.anthropic_adapter import _base_client_kwargs
    calls = []
    def response(request):
        calls.append(request)
        return httpx.Response(200, json={"id": "msg", "type": "message", "role": "assistant", "model": "m", "content": [], "stop_reason": "end_turn", "usage": {"input_tokens": 1, "output_tokens": 1}})
    frozen_base = "https://fixture.invalid/team-a/v1"
    _, options = _base_client_kwargs(frozen_base, None)
    client = anthropic.Anthropic(**options, api_key="ambient-unused", auth_token="synthetic-frozen",
        default_headers={"X-Api-Key": anthropic.Omit()}, http_client=httpx.Client(transport=httpx.MockTransport(response)))
    child = fixture(client, anthropic=True)
    child.base_url = frozen_base
    child._delegation_runtime_pin = replace(child._delegation_runtime_pin, base_url=frozen_base)
    kwargs = {"model": "m", "max_tokens": 1, "messages": [{"role": "user", "content": "x"}]}
    try:
        enforce_delegation_pin(child, kwargs, client=client)
        client.messages.create(**kwargs)
        assert calls[0].url.path == "/team-a/v1/messages"
        assert [v for k, v in calls[0].headers.raw if k.lower() == b"authorization"] == [b"Bearer synthetic-frozen"]
        assert "x-api-key" not in calls[0].headers
    finally:
        client.close()


def test_frozen_request_override_cannot_hide_conflicting_sdk_defaults():
    import openai
    client = openai.OpenAI(api_key="synthetic-frozen", base_url="https://fixture.invalid",
        default_headers={"authorization": "Bearer foreign"},
        http_client=httpx.Client(transport=httpx.MockTransport(lambda _: pytest.fail("transport"))))
    override = {"extra_headers": {"authorization": "Bearer explicit-frozen"}}
    child = fixture(client, overrides=override)
    try:
        with pytest.raises(ValueError):
            enforce_delegation_pin(child, {"model": "m", **override}, client=client)
    finally:
        client.close()


@pytest.mark.parametrize("spelling", ["authorization", "Authorization"])
@pytest.mark.parametrize("value", ["synthetic-frozen", "explicit-frozen"])
def test_fallback_factory_preserves_frozen_request_auth(monkeypatch, spelling, value):
    from tools.custom_subagent_fallbacks import frozen_fallback_client
    calls = []
    def response(request):
        calls.append(request)
        return httpx.Response(200, json={"id": "chat", "choices": [], "model": "m", "object": "chat.completion", "created": 0})
    transport = httpx.Client(transport=httpx.MockTransport(response))
    monkeypatch.setattr("agent.auxiliary_client._openai_http_client_kwargs", lambda _: {"http_client": transport})
    overrides = {"extra_headers": {spelling: "Bearer " + value}}
    child = fixture(None, fallback=True, overrides=overrides)
    pin = child._delegation_runtime_pin
    client = child.client = frozen_fallback_client(pin, pin.fallback_routes[0].native_entry())
    def request():
        return {"model": "m", "messages": [{"role": "user", "content": "x"}], **overrides}
    try:
        kwargs = request()
        enforce_delegation_pin(child, kwargs, client=client)
        client.chat.completions.create(**kwargs)
        assert len(calls) == 1
        assert [v.decode() for k, v in calls[0].headers.raw if k.lower() == b"authorization"] == ["Bearer " + value]
        with pytest.raises(ValueError):
            enforce_delegation_pin(child, {"model": "m"}, client=client)
        for defaults in ({"Authorization": "Bearer foreign"}, {"authorization": "Bearer foreign"}):
            client._custom_headers = defaults
            with pytest.raises(ValueError):
                enforce_delegation_pin(child, request(), client=client)
        assert len(calls) == 1
    finally:
        client.close()
