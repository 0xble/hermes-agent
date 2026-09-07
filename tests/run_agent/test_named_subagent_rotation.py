"""Account recovery must preserve a named child's route, not freeze its account."""
from dataclasses import replace

import httpx
import openai
import pytest

from agent.credential_pool import CredentialPool, PooledCredential
from tests.run_agent.test_custom_subagent_runtime import make_child
from tests.run_agent.test_run_agent_codex_responses import _codex_message_response
from tools.custom_subagents import parse_definitions
from tools.delegate_tool import _build_child_agent


def build_child(make_child, *, pooled=True):
    parent = make_child("high", "gpt-6-astra")
    del parent._delegation_runtime_pin
    entries = [PooledCredential.from_dict("openai-codex", {
        "id": name, "access_token": token, "priority": priority,
    }) for priority, (name, token) in enumerate([
        ("first", parent.api_key), ("reserve", "fixture-reserve-token"),
    ])]
    pool = CredentialPool("openai-codex", entries)
    parent._credential_pool = pool if pooled else None
    definition = parse_definitions({"subagents": {"fixture": {
        "description": "Test worker", "instructions": "Complete the test.",
        "model": "gpt-5.6-terra", "provider": "openai-codex", "reasoning_effort": "medium",
    }}})["fixture"]
    child = _build_child_agent(
        task_index=0, goal="Complete test", context=None, toolsets=None,
        model=definition.model, max_iterations=4, task_count=1, parent_agent=parent,
        subagent_definition=definition, resolved_reasoning={"enabled": True, "effort": "medium"},
    )
    return parent, child, pool, entries


def test_named_worker_recovers_usage_limit_without_changing_route(make_child, monkeypatch):
    parent, child, pool, entries = build_child(make_child)
    requests = []
    original = (parent.model, parent.provider, parent.base_url, parent.api_key)

    def api(kwargs):
        # Exercise the real final-request pin as well as the conversation retry path.
        child._delegation_runtime_pin.validate_request(child, kwargs, client=child.client)
        requests.append((child.api_key, kwargs))
        if child.api_key == entries[0].runtime_api_key:
            response = httpx.Response(429, request=httpx.Request("POST", child.base_url))
            raise openai.RateLimitError("usage limit reached", response=response,
                                       body={"error": {"type": "usage_limit_reached"}})
        return _codex_message_response("Recovered")

    monkeypatch.setattr(child, "_interruptible_api_call", api)
    try:
        result = child.run_conversation("Complete the fixture request")
        assert result["completed"] and result["final_response"] == "Recovered"
        assert [key for key, _ in requests] == [e.runtime_api_key for e in entries]
        assert all(kwargs["model"] == "gpt-5.6-terra" and
                   kwargs["reasoning"]["effort"] == "medium" for _, kwargs in requests)
        assert requests[0][1]["instructions"] == requests[1][1]["instructions"]
        assert (parent.model, parent.provider, parent.base_url, parent.api_key) == original
        assert child._credential_pool is pool
    finally:
        child.close()


@pytest.mark.parametrize("case", ["explicit_key", "unpooled_key", "endpoint", "provider", "unbound"])
def test_pool_inheritance_never_widens_fixed_authority(make_child, case):
    from tools.custom_subagents import inherited_credential_pool
    parent, child, pool, _ = build_child(make_child)
    defaults = {}
    try:
        if case == "explicit_key":
            defaults["api_key"] = child.api_key
        elif case == "unpooled_key":
            child.api_key = "fixture-fixed-key"
        elif case == "endpoint":
            child.base_url = "https://unrelated.example/v1"
        elif case == "provider":
            child.provider = "openai"
        elif case == "unbound":
            parent._credential_pool = None
        assert inherited_credential_pool(child, parent, defaults) is None
    finally:
        child.close()


def test_named_worker_lease_advances_pin_and_rejects_old_client(make_child):
    from tools.delegate_tool_child_run import _lease_child_credential
    _, child, pool, entries = build_child(make_child)
    old_client = child.client
    pool.mark_exhausted_and_rotate(status_code=429, api_key_hint=entries[0].runtime_api_key,
                                   credential_id=entries[0].id,
                                   error_context={"error": {"type": "usage_limit_reached"}})
    try:
        leased_pool, lease_id = _lease_child_credential(child)
        assert leased_pool is pool and lease_id == entries[1].id
        assert child.api_key == entries[1].runtime_api_key
        kwargs = child._build_api_kwargs([{"role": "user", "content": "fixture"}])
        child._delegation_runtime_pin.validate_request(child, kwargs, client=child.client)
        with pytest.raises(ValueError, match="credential"):
            child._delegation_runtime_pin.validate_request(child, kwargs, client=old_client)
        pool.release_lease(lease_id)
    finally:
        child.close()


@pytest.mark.parametrize("case", ["fixed", "foreign", "endpoint", "provider"])
def test_named_worker_rejects_unauthorized_pool_swap(make_child, case):
    _, child, pool, entries = build_child(make_child, pooled=case != "fixed")
    entry = entries[1]
    if case == "foreign":
        entry = replace(entry, id="outside", access_token="fixture-outside-token")
    elif case == "endpoint":
        entry.base_url = "https://unrelated.example/v1"
    elif case == "provider":
        entry.provider = "openai"
    before = (child.api_key, child.base_url, child.client)
    try:
        with pytest.raises(ValueError):
            child._swap_credential(entry)
        assert (child.api_key, child.base_url, child.client) == before
    finally:
        child.close()
