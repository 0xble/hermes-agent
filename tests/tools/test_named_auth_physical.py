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
