"""Config edits reach the next gateway agent without editing its predecessor.

Real profile config loaders, tool-definition cache and turn-agent resolution.
The entire AIAgent is a minimal SchemaAgent stub: session state and frozen prompt
are synthetic; cache-cap and fallback-chain hooks are disabled. These tests do
not exercise real agent construction, inference, clients or a live gateway.
"""

from copy import deepcopy
from collections import OrderedDict
from types import SimpleNamespace
import threading

import pytest
import yaml

from gateway.config import Platform
from gateway.run import GatewayRunner, _load_gateway_config
from gateway.run_turn_runner import TurnRunner
from gateway.session import SessionSource
from gateway.turn_context import TurnContext
from hermes_constants import set_hermes_home_override, reset_hermes_home_override


def _role(**kwargs):
    return {"description": "A bounded visual pass.", "instructions": "Keep scope bounded.",
            "model": "test-model", "reasoning_effort": "high", **kwargs}


@pytest.fixture
def harness(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_IGNORE_USER_CONFIG", raising=False)
    from model_tools import get_tool_definitions

    class SchemaAgent:
        def __init__(self, **kwargs):
            self.session_id = kwargs["session_id"]
            self.tools = get_tool_definitions(enabled_toolsets=["delegation"], quiet_mode=True)
            self._session_messages = []
            self._cached_system_prompt = "frozen prompt"

    runner = GatewayRunner.__new__(GatewayRunner)
    runner._agent_cache = OrderedDict()
    runner._agent_cache_lock = threading.Lock()
    runner._session_db = None
    runner._prefill_messages = []
    runner._service_tier = None
    monkeypatch.setattr(runner, "_enforce_agent_cache_cap", lambda: None)
    monkeypatch.setattr(runner, "_fallback_chain_for_route", lambda _: [])
    monkeypatch.setattr(runner, "_apply_fallback_chain_to_agent", lambda *_: None)
    ctx = TurnContext(source=SessionSource(platform=Platform.LOCAL, chat_id="chat"),
                      session_key="route", session_id="conversation", enabled_toolsets=["delegation"],
                      AIAgent=SchemaAgent, history=[])
    route = {"model": "test-parent", "runtime": {}}

    def write(config, home=tmp_path):
        home.mkdir(parents=True, exist_ok=True)
        (home / "config.yaml").write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")

    def turn():
        ctx.user_config = _load_gateway_config()
        turn_runner = TurnRunner(runner, ctx)
        agent, reused = turn_runner._resolve_turn_agent(route, "cli", "", 10, None, {})
        history, _, _ = turn_runner._load_turn_history(agent, reused)
        return agent, reused, history

    token = set_hermes_home_override(tmp_path)
    try:
        yield SimpleNamespace(write=write, turn=turn, runner=runner, ctx=ctx, home=tmp_path)
    finally:
        reset_hermes_home_override(token)


def _schema(agent):
    return next(t["function"] for t in agent.tools if t["function"]["name"] == "delegate_task")


def _selector(agent):
    return _schema(agent)["parameters"]["properties"]["tasks"]["items"]["properties"].get("subagent_type")


def test_add_edit_remove_roles_rebuilds_only_at_next_turn(harness):
    h = harness
    h.write({})
    old, reused, _ = h.turn()
    assert not reused
    assert _selector(old) is None
    frozen = deepcopy(old.tools)
    config = {"delegation": {"subagents": {"designer": _role()}}}
    h.write(config)
    # An edit does not reach into an agent already executing a turn.
    assert old.tools == frozen
    assert old._cached_system_prompt == "frozen prompt"
    added, reused, _ = h.turn()
    assert not reused and added is not old
    assert _selector(added)["enum"] == ["designer"]
    assert old.tools == frozen
    assert h.turn()[:2] == (added, True)

    config["delegation"]["subagents"]["designer"].update(
        description="Verify the rendered result.", model="test-next", reasoning_effort="medium")
    h.write(config)
    edited, reused, _ = h.turn()
    assert not reused and edited is not added
    assert "Verify the rendered result." in _selector(edited)["description"]
    assert "test-next / medium" in _selector(edited)["description"]
    assert "test-next" not in _selector(added)["description"]

    config["delegation"]["subagents"]["illustrator"] = config["delegation"]["subagents"].pop("designer")
    h.write(config)
    renamed, reused, _ = h.turn()
    assert not reused and renamed is not edited
    assert _selector(renamed)["enum"] == ["illustrator"]

    h.write({})
    removed, reused, _ = h.turn()
    assert not reused and removed is not edited
    assert _selector(removed) is None
    assert h.turn()[:2] == (removed, True)


@pytest.mark.parametrize("key,value", [
    ("instructions", "Use the revised trusted instructions."),
    ("provider", "openai"),
    ("fallbacks", [{"provider": "openai", "model": "test-fallback", "reasoning_effort": "low"}]),
    ("moa_presets", ["balanced", "careful"]),
])
def test_nested_role_settings_and_preset_allowlist_invalidate(harness, key, value):
    h = harness
    role = _role()
    if key == "moa_presets":
        role.pop("reasoning_effort")
        role.update(provider="moa", model="balanced", moa_presets=["balanced"])
    config = {"delegation": {"subagents": {"specialist": role}}}
    h.write(config)
    before, _, _ = h.turn()
    frozen = deepcopy(before.tools)
    role[key] = value
    h.write(config)
    after, reused, _ = h.turn()
    assert not reused and after is not before
    assert _selector(after)["enum"] == ["specialist"]
    assert before.tools == frozen
    assert h.turn()[:2] == (after, True)


@pytest.mark.parametrize("key,value", [
    ("model", "test-new-default"), ("provider", "openai"), ("reasoning_effort", "low"),
    ("max_concurrent_children", 7), ("max_spawn_depth", 1),
    ("orchestrator_enabled", False), ("independent_completions", True),
])
def test_schema_driving_defaults_invalidate(harness, key, value):
    h = harness
    config = {"delegation": {"subagents": {"specialist": {
        "description": "Use inherited defaults.", "instructions": "Stay bounded."}}}}
    h.write(config)
    before, _, _ = h.turn()
    config["delegation"][key] = value
    h.write(config)
    after, reused, _ = h.turn()
    assert not reused and after is not before
    assert h.turn()[:2] == (after, True)


def test_reordered_and_unrelated_config_reuses_frozen_agent(harness):
    h = harness
    role = _role()
    h.write({"delegation": {"subagents": {"designer": role}}})
    before, _, _ = h.turn()
    frozen = deepcopy(before.tools)
    h.write({"display": {"tool_progress": "off"}, "delegation": {
        "subagents": {"designer": dict(reversed(list(role.items())))}, "child_timeout_seconds": 123}})
    assert h.turn()[:2] == (before, True)
    assert before.tools == frozen


@pytest.mark.parametrize("same_session,external_write", [(True, False), (False, False), (True, True)])
def test_rebuild_preserves_only_same_session_unpersisted_history(harness, monkeypatch, same_session, external_write):
    h = harness
    h.write({})
    old, _, _ = h.turn()
    live = [{"role": "user", "content": "Remember this."},
            {"role": "assistant", "content": "I will."}]
    old._session_messages = deepcopy(live)
    copied = []

    def copy_history_off_lock(rows):
        # Exercise the real resolution path: copying a long transcript must not
        # block unrelated sessions or the idle sweeper on the global cache lock.
        assert h.runner._agent_cache_lock.acquire(blocking=False)
        h.runner._agent_cache_lock.release()
        copied.append(rows)
        return deepcopy(rows)

    monkeypatch.setattr("gateway.run_turn_runner.deepcopy", copy_history_off_lock)
    if not same_session:
        h.ctx.session_id = "different-conversation"
    if external_write:
        agent, sig, _, sid = h.runner._agent_cache[h.ctx.session_key]
        h.runner._agent_cache[h.ctx.session_key] = (agent, sig, 2, sid)
        h.runner._session_db = SimpleNamespace(_db=SimpleNamespace(get_session=lambda _: {"message_count": 3}))
    h.write({"delegation": {"subagents": {"designer": _role()}}})
    fresh, reused, history = h.turn()
    assert not reused and fresh is not old
    assert history == (live if same_session and not external_write else [])
    assert copied == ([old._session_messages] if same_session and not external_write else [])
    if history:
        history[0]["content"] = "Changed in the new turn."
    assert old._session_messages == live


@pytest.mark.parametrize("tool_name,expected_effect", [("web_search", None), ("terminal", "unknown")])
def test_rebuild_canonicalizes_unpersisted_tool_tail_without_mutating_predecessor(harness, tool_name, expected_effect):
    """A rebuilt agent must apply current replay safety to the recovered snapshot."""
    h = harness
    h.write({})
    old, _, _ = h.turn()
    live = [
        {"role": "user", "content": "Check the result."},
        {"role": "assistant", "content": "", "tool_calls": [
            {"id": "call-rebuild", "type": "function",
             "function": {"name": tool_name, "arguments": "{}"}},
        ]},
    ]
    old._session_messages = deepcopy(live)
    h.write({"delegation": {"subagents": {"designer": _role()}}})

    fresh, reused, history = h.turn()

    assert not reused and fresh is not old
    assert old._session_messages == live
    assert history[0] == live[0]
    if expected_effect is None:
        assert history == live[:1]
    else:
        assert history[1] == live[1]
        assert history[2]["tool_call_id"] == "call-rebuild"
        assert history[2]["effect_disposition"] == expected_effect
        assert "UNKNOWN" in history[2]["content"]


def test_profile_scoped_loaders_keep_other_cached_session_untouched(harness):
    h = harness
    h.write({"delegation": {"subagents": {"designer": _role()}}})
    first, _, _ = h.turn()
    first_tools = deepcopy(first.tools)
    other_home = h.home / "other-profile"
    h.write({"delegation": {"subagents": {"analyst": _role(description="Analyze only.")}}}, other_home)
    token = set_hermes_home_override(other_home)
    h.ctx.session_key = "other-profile:route"
    h.ctx.session_id = "other-conversation"
    try:
        other, reused, _ = h.turn()
    finally:
        reset_hermes_home_override(token)
    assert not reused
    assert _selector(other)["enum"] == ["analyst"]
    assert first.tools == first_tools

    h.ctx.session_key, h.ctx.session_id = "route", "conversation"
    assert h.turn()[:2] == (first, True)
    assert _selector(first)["enum"] == ["designer"]

@pytest.mark.parametrize("failure", [RuntimeError("uncopyable history"), RecursionError("nested history")])
def test_snapshot_copy_failure_falls_back_to_persisted_history(harness, monkeypatch, failure):
    h = harness
    h.write({})
    old, _, _ = h.turn()
    persisted = [{"role": "user", "content": "Persisted context."}]
    h.ctx.history = deepcopy(persisted)
    live = persisted + [{"role": "assistant", "content": "Live only."}]
    old._session_messages = deepcopy(live)

    def fail_copy(rows):
        assert rows is old._session_messages
        assert h.runner._agent_cache_lock.acquire(blocking=False)
        h.runner._agent_cache_lock.release()
        raise failure

    monkeypatch.setattr("gateway.run_turn_runner.deepcopy", fail_copy)
    h.write({"delegation": {"subagents": {"designer": _role()}}})
    fresh, reused, history = h.turn()
    assert fresh is not old and not reused
    assert history == persisted
    assert old._session_messages == live
    assert h.turn()[:2] == (fresh, True)


def test_rebuild_prefers_longer_persisted_history(harness):
    h = harness
    h.write({})
    old, _, _ = h.turn()
    live = [{"role": "user", "content": "Earlier context."}]
    old._session_messages = deepcopy(live)
    persisted = live + [{"role": "assistant", "content": "Already persisted."}]
    h.ctx.history = deepcopy(persisted)
    h.write({"delegation": {"subagents": {"designer": _role()}}})
    fresh, reused, history = h.turn()
    assert fresh is not old and not reused
    assert history == persisted
    assert old._session_messages == live


def test_non_delegation_signature_change_preserves_detached_history(harness):
    h = harness
    h.write({})
    old, _, _ = h.turn()
    live = [{"role": "user", "content": "Keep this context."},
            {"role": "assistant", "content": "Preserved."}]
    old._session_messages = deepcopy(live)
    h.write({"compression": {"threshold": 0.75}})
    fresh, reused, history = h.turn()
    assert fresh is not old and not reused
    assert history == live
    history[0]["content"] = "Detached mutation."
    assert old._session_messages == live



def test_invalid_roles_refresh_diagnostic_then_recover(harness):
    h = harness
    h.write({"delegation": {"subagents": {"designer": _role()}}})
    valid, _, _ = h.turn()
    h.write({"delegation": {"subagents": ["invalid"]}})
    invalid, reused, _ = h.turn()
    assert not reused and invalid is not valid
    assert _selector(invalid) is None
    assert "delegation.subagents must be a mapping" in _schema(invalid)["description"]
    h.write({"delegation": {"subagents": {"designer": _role()}}})
    recovered, reused, _ = h.turn()
    assert not reused and recovered is not invalid
    assert _selector(recovered)["enum"] == ["designer"]


@pytest.mark.parametrize("rows", [
    [{"role": "assistant", "content": "Already durable.", "_db_persisted": True}],
    [{"role": "assistant", "content": "Ephemeral scaffolding.", "_thinking_prefill": True}],
])
def test_rebuild_does_not_resurrect_filtered_history(harness, rows):
    h = harness
    h.write({})
    old, _, _ = h.turn()
    old._session_messages = rows
    h.write({"delegation": {"subagents": {"designer": _role()}}})
    fresh, reused, history = h.turn()
    assert not reused and fresh is not old
    assert history == []
