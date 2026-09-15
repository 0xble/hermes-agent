"""Registered recall controls reach the pinned SDK without widening fallback filters."""

import asyncio
import json
from types import SimpleNamespace

import pytest

from plugins.memory import load_memory_provider


@pytest.mark.parametrize("reject_expansions", [False, True])
def test_registered_recall_controls_reach_sdk_and_local_formatter(monkeypatch, reject_expansions):
    Hindsight = pytest.importorskip("hindsight_client").Hindsight
    from jsonschema import Draft202012Validator

    provider = load_memory_provider("hindsight", register_skills=False)
    provider._apply_recall_settings({"recall_tags": ["private"], "recall_tags_match": "all_strict"})
    schema = next(item["parameters"] for item in provider.get_tool_schemas()
                  if item["name"] == "hindsight_recall")
    requested = {
        "query": "report", "types": ["world", "experience", "observation"],
        "include_entities": True, "max_entity_tokens": 321,
        "include_chunks": True, "max_chunk_tokens": 1234,
        "include_source_facts": True, "max_source_facts_tokens": 765,
        "include_provenance": True, "offset": 1, "limit": 1,
        "tags": ["private", "project"], "tags_match": "all_strict",
    }
    # A schema-conforming caller can request only advertised controls.
    args = {key: value for key, value in requested.items() if key in schema["properties"]}
    Draft202012Validator(schema).validate(args)
    calls = []

    class Transport:
        async def recall_memories(self, bank_id, request, **kwargs):
            wire = request.to_dict()
            calls.append(wire)
            expanded = any(wire.get("include", {}).values())
            if reject_expansions and expanded:
                raise ValueError("422 unknown field in include expansion")
            return SimpleNamespace(
                results=[SimpleNamespace(text=f"memory {index}", id=f"id-{index}", document_id="report")
                         for index in range(3)],
                entities={"person": "Alice"} if expanded else None,
                chunks={"chunk": "original text"} if expanded else None,
                source_facts={"fact": "evidence"} if expanded else None,
            )

    client = object.__new__(Hindsight)
    client._memory_api = Transport()
    client._timeout = 5
    monkeypatch.setattr(provider, "_run_hindsight_operation", lambda op: asyncio.run(op(client)))
    result = json.loads(provider.handle_tool_call("hindsight_recall", args))["result"]
    assert calls[0]["types"] == requested["types"]
    assert calls[0]["include"] == {
        "entities": {"max_tokens": 321}, "chunks": {"max_tokens": 1234},
        "source_facts": {"max_tokens": 765, "max_tokens_per_observation": -1},
    }
    assert len(calls) == (2 if reject_expansions else 1)
    assert all(call["tags"] == requested["tags"] and call["tags_match"] == "all_strict" for call in calls)
    assert all(not {"offset", "limit", "include_provenance"}.intersection(call) for call in calls)
    assert "memory 1" in result and "memory 0" not in result and "memory 2" not in result
    assert '"id": "id-1"' in result
    for label in ("Entities:", "Source chunks:", "Source facts:"):
        assert (label in result) is not reject_expansions

    for key, value in {"types": ["invalid"], "tags_match": "invalid", "max_entity_tokens": 2001,
                       "max_chunk_tokens": 8193, "max_source_facts_tokens": 0,
                       "offset": 501, "limit": 51}.items():
        assert not Draft202012Validator(schema).is_valid({**args, key: value})

    # Auto-recall remains observation-only with configured filters and no expansions.
    provider._compatible_recall("automatic")
    assert calls[-1]["types"] == ["observation"]
    assert calls[-1]["tags"] == ["private"]
    assert not any(calls[-1].get("include", {}).values())
