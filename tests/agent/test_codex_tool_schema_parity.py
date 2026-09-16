"""Codex Responses tool-schema conversion contracts shared by direct and auxiliary paths."""

from __future__ import annotations

import copy
from types import SimpleNamespace
from typing import Any

import pytest

from agent.auxiliary_client import _CodexCompletionsAdapter
from agent.transports.codex import ResponsesApiTransport


def _chat_tool(*, strict: bool | None = None, parameters: dict[str, Any] | None = None) -> dict[str, Any]:
    function: dict[str, Any] = {
        "name": "search_records",
        "description": "Search records.",
        "parameters": parameters or {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "minimum": 1},
            },
            "required": ["query"],
            "additionalProperties": False,
        },
    }
    if strict is not None:
        function["strict"] = strict
    return {"type": "function", "function": function}


def _auxiliary_tools(tools: list[dict[str, Any]], *, base_url: str = "https://chatgpt.com/backend-api/codex") -> list[dict[str, Any]]:
    adapter = _CodexCompletionsAdapter(SimpleNamespace(base_url=base_url), "gpt-5.6-terra")
    response_kwargs, _, _ = adapter._build_responses_kwargs({
        "messages": [{"role": "user", "content": "find it"}],
        "tools": tools,
    })
    return response_kwargs["tools"]


@pytest.mark.parametrize("strict", [True, False, None])
def test_direct_and_auxiliary_codex_schema_conversion_match_and_downgrade_incompatible_strict_schema(strict):
    """Both callers preserve optional fields but never forward them as strict Responses schemas."""
    tools = [_chat_tool(strict=strict)]

    direct = ResponsesApiTransport().convert_tools(tools)
    auxiliary = _auxiliary_tools(tools)

    assert direct is not None
    assert direct == auxiliary
    assert direct == [{
        "type": "function", "name": "search_records", "description": "Search records.",
        "strict": False,
        "parameters": tools[0]["function"]["parameters"],
    }]
    assert direct[0]["parameters"]["required"] == ["query"]
    assert "limit" not in direct[0]["parameters"]["required"]


def test_direct_and_auxiliary_codex_schema_conversion_preserve_compatible_strict_schema():
    """A strict-compatible schema retains its explicitly requested strict contract on both paths."""
    tools = [_chat_tool(strict=True, parameters={
        "type": "object",
        "properties": {"query": {"type": "string"}, "limit": {"type": "integer", "minimum": 1}},
        "required": ["query", "limit"],
        "additionalProperties": False,
    })]

    direct = ResponsesApiTransport().convert_tools(tools)

    assert direct == _auxiliary_tools(tools)
    assert direct[0]["strict"] is True


@pytest.mark.parametrize("parameters", [
    {"type": "object", "properties": {"metadata": {"type": "object"}}, "required": ["metadata"], "additionalProperties": False},
    {"type": "object", "properties": {"metadata": {"$ref": "#/$defs/metadata"}}, "required": ["metadata"], "additionalProperties": False, "$defs": {"metadata": {"type": "object", "properties": {}, "required": [], "additionalProperties": False}}},
])
def test_direct_and_auxiliary_codex_schema_conversion_reject_invalid_nested_strict_objects(parameters):
    """Nested object schemas and references must be fully strict-compatible before strict is forwarded."""
    tools = [_chat_tool(strict=True, parameters=parameters)]

    direct = ResponsesApiTransport().convert_tools(tools)

    assert direct == _auxiliary_tools(tools)
    assert direct[0]["strict"] is False


@pytest.mark.parametrize("strict", [True, False, None])
@pytest.mark.parametrize("base_url", ["https://api.x.ai/v1", "https://chatgpt.com/backend-api/codex"])
def test_auxiliary_codex_schema_sanitizes_provider_rejections_without_mutating_caller_schema(strict, base_url):
    """The xAI sanitizer remains active before shared conversion and owns a private copy."""
    tools = [_chat_tool(
        strict=strict,
        parameters={
            "type": "object",
            "properties": {
                "query": {"type": "string", "pattern": "^[a-z]+$", "format": "email"},
            },
            "required": [],
        },
    )]
    original = copy.deepcopy(tools)

    converted = _auxiliary_tools(tools, base_url=base_url)

    assert converted[0]["strict"] is False
    assert "pattern" not in converted[0]["parameters"]["properties"]["query"]
    assert "format" not in converted[0]["parameters"]["properties"]["query"]
    assert tools == original

@pytest.mark.parametrize("extra", [
    {"allOf": [{"type": "string"}]}, {"not": {"type": "null"}},
    {"if": {"type": "string"}}, {"then": {"type": "string"}},
    {"else": {"type": "string"}}, {"dependentSchemas": {}},
    {"dependentRequired": {}}, {"patternProperties": {}},
    {"unevaluatedProperties": False}, {"unevaluatedItems": False},
    {"oneOf": [{"type": "string"}]}, {"prefixItems": [{"type": "string"}]},
    {"type": "array", "items": [{"type": "object"}]},
])
def test_unsupported_strict_shapes_downgrade_on_both_paths(extra):
    schema = {"type": "object", "properties": {"value": {"type": "string", **extra}},
              "required": ["value"], "additionalProperties": False}
    tools = [_chat_tool(strict=True, parameters=schema)]
    original = copy.deepcopy(tools)
    direct = ResponsesApiTransport().convert_tools(tools)
    assert direct[0]["strict"] is False
    assert _auxiliary_tools(tools)[0]["strict"] is False
    assert direct[0]["parameters"] == schema
    assert tools == original


@pytest.mark.parametrize("strict", ["false", "true", 1, [], {}])
def test_only_literal_true_requests_strict(strict):
    tools = [_chat_tool(strict=strict, parameters={"type": "object", "properties": {},
                                                "required": [], "additionalProperties": False})]
    assert ResponsesApiTransport().convert_tools(tools)[0]["strict"] is False
    assert _auxiliary_tools(tools)[0]["strict"] is False


def test_deep_schema_downgrades_on_both_paths():
    schema = {"type": "string"}
    for _ in range(30):
        schema = {"type": "object", "properties": {"value": schema},
                  "required": ["value"], "additionalProperties": False}
    tools = [_chat_tool(strict=True, parameters=schema)]
    assert ResponsesApiTransport().convert_tools(tools)[0]["strict"] is False
    assert _auxiliary_tools(tools)[0]["strict"] is False


def test_compatibility_predicate_bounds_python_cycles_and_extreme_depth():
    from agent.codex_responses_adapter import _strict_schema_compatible
    schema: dict[str, Any] = {"type": "array"}
    schema["items"] = schema
    assert _strict_schema_compatible(schema) is False
    schema = {"type": "string"}
    for _ in range(2000):
        schema = {"type": "array", "items": schema}
    assert _strict_schema_compatible(schema) is False


def test_valid_strict_survives_existing_auxiliary_sanitizer():
    schema = {"type": "object", "properties": {"value": {"type": "string", "pattern": "x", "format": "email"}},
              "required": ["value"], "additionalProperties": False}
    tools = [_chat_tool(strict=True, parameters=schema)]
    direct = ResponsesApiTransport().convert_tools(tools)
    auxiliary = _auxiliary_tools(tools)
    assert direct[0]["strict"] is auxiliary[0]["strict"] is True
    assert direct[0]["parameters"] == schema
    assert "pattern" not in auxiliary[0]["parameters"]["properties"]["value"]
    assert "format" not in auxiliary[0]["parameters"]["properties"]["value"]


@pytest.mark.parametrize("value,expected", [
    ({"anyOf": [{"type": "string"}, {"type": "null"}]}, True),
    ({"type": None, "anyOf": [{"type": "string"}, {"type": "null"}]}, False),
    ({"type": ["string", "null"]}, True),
    ({"type": ["array", "null"], "items": {"type": "string"}}, True),
    ({"type": ["object", "null"], "properties": {}, "required": [], "additionalProperties": False}, True),
    ({"type": ["array", "null"]}, False),
    ({"type": ["object", "null"]}, False),
])
def test_nullable_and_anyof_type_presence_on_both_paths(value, expected):
    schema = {"type": "object", "properties": {"value": value},
              "required": ["value"], "additionalProperties": False}
    tools = [_chat_tool(strict=True, parameters=schema)]
    original = copy.deepcopy(tools)
    direct = ResponsesApiTransport().convert_tools(tools)
    auxiliary = _auxiliary_tools(tools)
    assert direct == auxiliary
    assert direct[0]["strict"] is expected
    assert direct[0]["parameters"] == schema
    assert tools == original
