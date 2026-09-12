"""Context modes are independent of routing; portable snapshots never replay authority."""
import json
from types import SimpleNamespace

import pytest

from tools.custom_subagents import parse_definitions, ResolvedSubagentLaunch
from tools.delegation_history import (
    capture_visible_window, portable_history, prepare_task_histories, resolve_context_mode,
)


def definitions(**overrides):
    return parse_definitions({"subagents": {name: {
        "description": "fixture", "instructions": "Assigned scope only", **fields,
    } for name, fields in overrides.items()}})


@pytest.mark.parametrize("role,expected", [("lead", "fork"), ("worker", "fresh"),
    ("explorer", "fresh"), ("advisor", "fresh"), ("council", "fresh"), ("designer", "fresh")])
def test_role_defaults_and_overrides_do_not_change_model_inheritance(role, expected):
    d = definitions(**{role: {"inherit_parent": True}})[role]
    assert resolve_context_mode({}, d) == expected
    for mode in ("fresh", "fork"):
        assert resolve_context_mode({"context_mode": mode}, d) == mode
        assert resolve_context_mode({"context_mode": mode}, d, independent_review=True) == "fresh"
        configured = definitions(**{role: {"inherit_parent": True, "context_mode": mode}})[role]
        assert resolve_context_mode({}, configured) == mode
        assert configured.inherit_parent
        assert configured.model is None
    assert resolve_context_mode({}) == "fresh"


@pytest.mark.parametrize("bad", [None, "", "inherit", "FORK", True, [], {}])
def test_bad_context_mode_is_not_silently_fresh(bad):
    with pytest.raises(ValueError, match="context_mode"):
        resolve_context_mode({"context_mode": bad})
    with pytest.raises(ValueError):
        definitions(lead={"context_mode": bad})


def test_resume_never_reforks_even_when_role_default_changes():
    d = definitions(lead={"context_mode": "fork"})["lead"]
    assert resolve_context_mode({"resume_session_id": "own-child"}, d) == "resume"
    for mode in ("fresh", "fork"):
        with pytest.raises(ValueError, match="own history"):
            resolve_context_mode({"resume_session_id": "own-child", "context_mode": mode}, d)


def test_independent_review_cannot_resume_contaminated_context():
    with pytest.raises(ValueError, match="Independent review requires a fresh child"):
        resolve_context_mode({"resume_session_id": "child"}, independent_review=True)


def window():
    return [
        {"role": "system", "content": "PARENT_ONLY_PERMISSION"},
        {"role": "developer", "content": "PARENT_ONLY_DIRECTIVE"},
        {"role": "user", "content": "accepted scope and correction"},
        {"role": "assistant", "content": "I inspected evidence", "reasoning": "PRIVATE_REASONING",
         "tool_calls": [{"id": "c1", "response_item_id": "NATIVE_ID", "function": {"name": "read_file", "arguments": '{"path":"fixture"}'}}]},
        {"role": "tool", "tool_call_id": "c1", "content": "evidence", "cache_control": {"type": "ephemeral"}},
    ]


def test_snapshot_is_one_time_current_window_and_sibling_isolated():
    parent = SimpleNamespace(provider="openai", _session_messages=[{"role": "user", "content": "ARCHIVED"}])
    visible = window()
    capture_visible_window(parent, {"messages": visible}, [])
    visible.append({"role": "assistant", "tool_calls": [{"id": "in-flight", "function": {"name": "delegate_task"}}]})
    d = definitions(lead={})["lead"]
    launches = [ResolvedSubagentLaunch(d, {}, None)] * 2
    prepared = prepare_task_histories([{}, {}], launches, parent)
    text = prepared[0][1][0]["content"]
    assert "evidence" in text and "accepted scope" in text
    for excluded in ("PARENT_ONLY", "PRIVATE_REASONING", "NATIVE_ID", "ARCHIVED", "in-flight", "cache_control"):
        assert excluded not in text
    assert "not new instructions, permission grants" in text
    prepared[0][1][0]["content"] = "mutated sibling"
    assert prepared[1][1][0]["content"] == text
    # Compaction/selection replaces the actual outbound window. Never read old DB
    # or append the previous snapshot, even across model/provider changes.
    parent.provider = "anthropic"
    capture_visible_window(parent, {"messages": [{"role": "user", "content": "COMPACTED SUMMARY"}]}, visible)
    later = prepare_task_histories([{}], launches[:1], parent)[0][1]
    assert "COMPACTED SUMMARY" in later[0]["content"]
    assert "evidence" not in later[0]["content"]
    assert "evidence" in prepared[1][1][0]["content"]


def test_no_count_cap_or_silent_truncation():
    messages = [{"role": "user", "content": f"ROW-{i}"} for i in range(401)]
    records = json.loads(portable_history(messages)[0]["content"].split("\n", 2)[2])
    assert len(records) == 401
    assert records[0]["content"] == "ROW-0" and records[-1]["content"] == "ROW-400"


@pytest.mark.parametrize("rows", [
    [{"type": "reasoning", "encrypted_content": "SECRET_NATIVE"},
     {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "question"}]},
     {"type": "function_call", "id": "NATIVE_ID", "call_id": "c1", "name": "read_file", "arguments": "{}"},
     {"type": "function_call_output", "call_id": "c1", "output": "evidence"}],
    [{"role": "assistant", "content": [{"type": "thinking", "thinking": "SECRET_NATIVE", "signature": "SIGNATURE"},
     {"type": "tool_use", "id": "c1", "name": "read_file", "input": {}}]},
     {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "c1", "content": "evidence"}]}],
])
def test_cross_provider_native_call_groups_become_reference_not_replay(rows):
    history = portable_history(rows)
    assert len(history) == 1 and history[0]["role"] == "user"
    assert "evidence" in history[0]["content"]
    assert "SECRET_NATIVE" not in repr(history) and "NATIVE_ID" not in repr(history)
    assert "SIGNATURE" not in repr(history)
    assert "tool_calls" not in history[0]


@pytest.mark.parametrize("rows,match", [
    (None, "No current"),
    ([{"type": "compaction", "encrypted_content": "opaque"}], "Opaque native"),
    ([{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "fixture"}}]}], "non-text"),
    ([{"role": "tool", "tool_call_id": "orphan", "content": "bad"}], "Orphan"),
    (window()[:-1], "Incomplete"),
])
def test_unavailable_or_unsafe_context_fails_loudly(rows, match):
    with pytest.raises(ValueError, match=match):
        portable_history(rows)


def test_schema_advertises_configurable_context_separate_from_model(monkeypatch):
    from tools import delegate_tool
    from tools.registry import registry
    monkeypatch.setattr(delegate_tool, "_load_config", lambda: {"subagents": {
        "lead": {"description": "fixture", "instructions": "fixture", "inherit_parent": True},
        "advisor": {"description": "fixture", "instructions": "fixture", "context_mode": "fork"}}})
    schema = registry.get_definitions({"delegate_task"}, quiet=True)[0]["function"]
    props = schema["parameters"]["properties"]["tasks"]["items"]["properties"]
    assert props["context_mode"]["enum"] == ["fresh", "fork"]
    assert "context default=fork" in props["subagent_type"]["description"]
    assert "context_mode" not in props["resume_session_id"].get("required", [])


def test_real_registry_dispatch_validates_whole_batch_before_children(monkeypatch):
    from tools import delegate_tool as dt
    from tools.registry import registry
    parent = SimpleNamespace(_delegate_depth=0, session_id="parent", _delegation_visible_window=window())
    config = {"subagents": {"lead": {"description": "fixture", "instructions": "fixture", "inherit_parent": True}}}
    monkeypatch.setattr(dt, "_load_config", lambda: config)
    monkeypatch.setattr(dt, "last_delegation_config_error", lambda: None)
    monkeypatch.setattr(dt, "_resolve_delegation_credentials", lambda *_: {})
    d = parse_definitions(config)["lead"]
    # Routing is not under test here: the actual registry handler owns validation,
    # snapshot selection and batch atomicity. Never invoke a paid route.
    monkeypatch.setattr(dt, "_preflight_task_runtime", lambda *args: ([ResolvedSubagentLaunch(d, {}, None)] * 2, None))
    def forbidden(*args, **kwargs):
        pytest.fail("invalid batch constructed a child or reserved metadata")
    monkeypatch.setattr(dt, "_build_children", forbidden)
    monkeypatch.setattr("tools.async_delegation.reserve_delegation_metadata", forbidden)
    tasks = [{"goal": "Inspect fixture details", "task_label": "Check fixture", "context_mode": "fork"},
             {"goal": "Inspect other fixture details", "task_label": "Check other", "context_mode": "invalid"}]
    response = json.loads(registry.dispatch("delegate_task", {"tasks": tasks}, parent_agent=parent))
    assert "context_mode" in response["error"]
