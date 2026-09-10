"""Negative and integration coverage for the subagent audit recommendations.

Each test names the audit item it defends. These are deliberately *not* direct
calls to the Python delegation helpers where a real path exists: the bypasses
being closed here were all reachable through ordinary tools and an ordinary
tool-schema rebuild, so that is where they are exercised.
"""
from __future__ import annotations

import json

import pytest


@pytest.fixture
def knowledge_home(monkeypatch, tmp_path):
    """A disposable Hermes home with real memory and skill files in it."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "memories").mkdir()
    memory = tmp_path / "memories/MEMORY.md"
    memory.write_text("ORIGINAL MEMORY\n", encoding="utf-8")
    skill = tmp_path / "skills/fixture-skill/SKILL.md"
    skill.parent.mkdir(parents=True)
    skill.write_text("---\nname: fixture-skill\n---\nORIGINAL SKILL\n", encoding="utf-8")
    ordinary = tmp_path / "workspace/notes.md"
    ordinary.parent.mkdir()
    ordinary.write_text("ORDINARY\n", encoding="utf-8")
    return {"home": tmp_path, "memory": memory, "skill": skill, "ordinary": ordinary}


# ── item 1: shared-knowledge write bypasses ─────────────────────────────────

def test_write_file_bypass_is_denied_and_file_is_untouched(knowledge_home):
    from agent.delegation_context import delegated_child_context
    from tools.file_tools import write_file_tool

    memory = knowledge_home["memory"]
    with delegated_child_context("child", read_only_knowledge=True):
        result = write_file_tool(str(memory), "BYPASSED\n")
    assert "shared knowledge" in result.lower()
    # The reproduction that started this: the guard must deny, not merely warn.
    assert memory.read_text(encoding="utf-8") == "ORIGINAL MEMORY\n"


def _patch_call(mode, skill):
    """The same edit expressed in both patch shapes, so both are covered."""
    if mode == "replace":
        return {"mode": "replace", "path": str(skill),
                "old_string": "ORIGINAL SKILL", "new_string": "BYPASSED"}
    return {"mode": "patch", "patch": (
        f"*** Begin Patch\n*** Update File: {skill}\n"
        "-ORIGINAL SKILL\n+BYPASSED\n*** End Patch"
    )}


@pytest.mark.parametrize("mode", ["replace", "patch"])
def test_patch_bypass_is_denied(knowledge_home, mode):
    from agent.delegation_context import delegated_child_context
    from tools.file_tools import patch_tool

    skill = knowledge_home["skill"]
    with delegated_child_context("child", read_only_knowledge=True):
        result = patch_tool(**_patch_call(mode, skill))
    assert "shared knowledge" in result.lower()
    assert "ORIGINAL SKILL" in skill.read_text(encoding="utf-8")


@pytest.mark.parametrize("mode", ["replace", "patch"])
def test_patch_still_works_for_the_parent(knowledge_home, mode):
    """Proof the denial above came from the guard, not an unwritable file."""
    from tools.file_tools import patch_tool

    skill = knowledge_home["skill"]
    result = patch_tool(**_patch_call(mode, skill))
    assert "shared knowledge" not in result.lower()
    assert "BYPASSED" in skill.read_text(encoding="utf-8")


def test_v4a_move_out_of_protected_root_is_denied(knowledge_home):
    """Both endpoints of a Move are checked — moving knowledge OUT is a write."""
    from agent.delegation_context import delegated_child_context
    from tools.file_tools import patch_tool

    memory = knowledge_home["memory"]
    destination = knowledge_home["home"] / "workspace/stolen.md"
    patch = (
        f"*** Begin Patch\n*** Move File: {memory} -> {destination}\n*** End Patch"
    )
    with delegated_child_context("child", read_only_knowledge=True):
        result = patch_tool(mode="patch", patch=patch)
    assert "shared knowledge" in result.lower()
    assert memory.exists()


def test_shell_and_interpreter_bypasses_are_denied(knowledge_home):
    from agent.delegation_context import delegated_child_context
    from tools.code_execution_tool import execute_code
    from tools.terminal_tool import terminal_tool

    memory = knowledge_home["memory"]
    with delegated_child_context("child", read_only_knowledge=True):
        shell = terminal_tool(command=f"echo BYPASSED >> {memory}")
        interpreted = execute_code(code=f"open({str(memory)!r}, 'a').write('X')")
    assert "shared knowledge" in shell.lower()
    assert "shared knowledge" in interpreted.lower()
    assert memory.read_text(encoding="utf-8") == "ORIGINAL MEMORY\n"


def test_guard_does_not_touch_ordinary_paths_or_the_parent(knowledge_home):
    """The boundary is narrow on purpose: children still do real work."""
    from agent.delegation_context import delegated_child_context
    from tools.file_tools import write_file_tool

    ordinary = knowledge_home["ordinary"]
    with delegated_child_context("child", read_only_knowledge=True):
        result = write_file_tool(str(ordinary), "CHILD WROTE THIS\n")
    assert "shared knowledge" not in result.lower()
    assert ordinary.read_text(encoding="utf-8") == "CHILD WROTE THIS\n"

    # Outside a read-only child context nothing is guarded at all.
    memory = knowledge_home["memory"]
    assert "shared knowledge" not in write_file_tool(str(memory), "PARENT\n").lower()
    assert memory.read_text(encoding="utf-8") == "PARENT\n"


def test_boundary_report_never_claims_a_sandbox():
    """An overstated boundary is worse than a documented partial one."""
    from tools.knowledge_boundary import boundary_report

    report = boundary_report()
    assert report["shell_enforcement"] == "command_scan"
    assert report["not_enforced"], "the honest limits must travel with the claim"


# ── item 2: delegation config load failures ─────────────────────────────────

def test_loader_failure_is_recorded_and_refuses_to_spawn(monkeypatch):
    from types import SimpleNamespace

    from tools import delegate_tool

    def broken_loader():
        raise OSError("config.yaml is unreadable")

    monkeypatch.setattr("hermes_cli.config.load_config_readonly", broken_loader)
    monkeypatch.delenv("HERMES_IGNORE_USER_CONFIG", raising=False)

    with pytest.raises(delegate_tool.DelegationConfigError):
        delegate_tool.load_delegation_config()

    # Tolerant callers still work, but the failure is no longer invisible.
    delegate_tool._load_config()
    assert "unreadable" in (delegate_tool.last_delegation_config_error() or "")

    parent = SimpleNamespace(
        provider="openai-codex", model="gpt-6-astra", api_mode="codex_responses",
        base_url="https://chatgpt.com/backend-api/codex", api_key="fixture",
        request_overrides={},
    )
    monkeypatch.setattr(
        delegate_tool, "_build_child_preserving_parent_tools",
        lambda **kw: pytest.fail("spawned on an unreadable configuration"),
    )
    result = json.loads(delegate_tool.delegate_task(
        tasks=[{"goal": "Do some scoped fixture work", "task_label": "Do some scoped"}], parent_agent=parent,
    ))
    assert "could not be loaded" in result["error"]


def test_absent_delegation_block_is_not_a_failure(monkeypatch):
    from tools import delegate_tool

    monkeypatch.setattr("hermes_cli.config.load_config_readonly", lambda: {})
    monkeypatch.delenv("HERMES_IGNORE_USER_CONFIG", raising=False)
    assert delegate_tool.load_delegation_config() == {}
    assert delegate_tool.last_delegation_config_error() is None


def test_non_mapping_delegation_block_fails_loudly(monkeypatch):
    from tools import delegate_tool

    monkeypatch.setattr(
        "hermes_cli.config.load_config_readonly", lambda: {"delegation": ["oops"]},
    )
    monkeypatch.delenv("HERMES_IGNORE_USER_CONFIG", raising=False)
    with pytest.raises(delegate_tool.DelegationConfigError, match="must be a mapping"):
        delegate_tool.load_delegation_config()


# ── items 3, 7, 8: what the parent is actually told ─────────────────────────

def _definition(**overrides):
    base = {
        "description": "Read and attribute evidence.",
        "instructions": "Read only. Cite every source.",
        "provider": "openai-codex", "model": "gpt-5.6-luna",
        "reasoning_effort": "medium",
    }
    base.update(overrides)
    return base


def test_invalid_definition_is_named_not_silently_dropped(monkeypatch):
    from tools import delegate_tool

    monkeypatch.setattr(delegate_tool, "_load_config", lambda: {
        "subagents": {"explorer": _definition(reasoning_effort="turbo")},
    })
    overrides = delegate_tool._build_dynamic_schema_overrides()
    description = overrides["description"]
    assert "CONFIG WARNING" in description
    assert "reasoning_effort" in description
    # The selector is gone (nothing valid to select) but the tool still works.
    properties = overrides["parameters"]["properties"]["tasks"]["items"]["properties"]
    assert "subagent_type" not in properties
    assert "action" in overrides["parameters"]["properties"]


def test_invalid_definition_keeps_control_actions_working(monkeypatch):
    """list/steer/stop must not depend on a valid role registry."""
    from types import SimpleNamespace

    from tools import delegate_tool

    monkeypatch.setattr(delegate_tool, "_load_config", lambda: {
        "subagents": {"explorer": _definition(model=42)},
    })
    parent = SimpleNamespace(
        provider="openai-codex", model="gpt-6-astra", api_mode="codex_responses",
        base_url="https://chatgpt.com/backend-api/codex", api_key="fixture",
        request_overrides={},
    )
    listing = json.loads(delegate_tool.delegate_task(action="list", parent_agent=parent))
    assert "subagents" in listing or "No live subagents" in json.dumps(listing)


def test_spawn_reports_the_invalid_definition(monkeypatch):
    from types import SimpleNamespace

    from tools import delegate_tool

    monkeypatch.setattr(delegate_tool, "_load_config", lambda: {
        "subagents": {"explorer": _definition(reasoning_effort="turbo")},
    })
    parent = SimpleNamespace(
        provider="openai-codex", model="gpt-6-astra", api_mode="codex_responses",
        base_url="https://chatgpt.com/backend-api/codex", api_key="fixture",
        request_overrides={},
    )
    monkeypatch.setattr(
        delegate_tool, "_build_child_preserving_parent_tools",
        lambda **kw: pytest.fail("spawned on an invalid registry"),
    )
    result = json.loads(delegate_tool.delegate_task(
        tasks=[{"goal": "Investigate the fixture", "subagent_type": "explorer", "task_label": "Investigate the fixture"}],
        parent_agent=parent,
    ))
    assert "Invalid delegation.subagents configuration" in result["error"]
    assert "explorer" in result["error"]


def test_roles_advertise_purpose_model_and_effort(monkeypatch):
    from tools import delegate_tool

    monkeypatch.setattr(delegate_tool, "_load_config", lambda: {
        "subagents": {
            "explorer": _definition(),
            "worker": _definition(
                description="Bounded implementation.", model="gpt-5.6-terra",
            ),
        },
    })
    overrides = delegate_tool._build_dynamic_schema_overrides()
    selector = (overrides["parameters"]["properties"]["tasks"]["items"]
                ["properties"]["subagent_type"])
    assert selector["enum"] == ["explorer", "worker"]
    for token in ("gpt-5.6-luna", "gpt-5.6-terra", "medium", "fixed"):
        assert token in selector["description"]
    # Precedence and the legacy-omission contract are both stated (item 8).
    assert "override" in selector["description"]
    assert "Omit subagent_type" in selector["description"]
    assert "keyword router" in overrides["description"]


def test_role_visibility_in_a_fresh_normal_session(monkeypatch, tmp_path):
    """The end-to-end path a real session takes, not the builder in isolation.

    Configuration existing is not proof a session's schema shows it — the
    generated schema is what the model actually sees.
    """
    import yaml

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_IGNORE_USER_CONFIG", raising=False)
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({
        "delegation": {"subagents": {"explorer": _definition()}},
    }), encoding="utf-8")

    import tools.delegate_tool  # noqa: F401  (registers delegate_task)
    from tools.registry import registry

    definitions = registry.get_definitions({"delegate_task"}, quiet=True)
    schema = next(d["function"] for d in definitions
                  if d["function"]["name"] == "delegate_task")
    selector = (schema["parameters"]["properties"]["tasks"]["items"]
                ["properties"]["subagent_type"])
    assert selector["enum"] == ["explorer"]
    assert "gpt-5.6-luna" in selector["description"]


# ── item 4: pinning at the final request boundary ───────────────────────────

def test_pin_rejects_a_post_middleware_model_change():
    """Middleware runs after _build_api_kwargs — the late check is the real one."""
    from types import SimpleNamespace

    from agent.chat_completion_helpers import enforce_delegation_pin
    from tools.custom_subagents import RuntimePin, parse_definitions

    definition = parse_definitions({"subagents": {"worker": _definition()}})["worker"]
    child = SimpleNamespace(
        provider="openai-codex", model="gpt-5.6-luna", api_mode="codex_responses",
        base_url="https://chatgpt.com/backend-api/codex", api_key="fixture",
        client=SimpleNamespace(api_key="fixture",
                               base_url="https://chatgpt.com/backend-api/codex"),
    )
    child._delegation_runtime_pin = RuntimePin.from_child(
        child, definition, {"enabled": True, "effort": "medium"},
    )
    good = {"model": "gpt-5.6-luna", "reasoning": {"effort": "medium"}}
    enforce_delegation_pin(child, good, client=child.client)

    tampered = {**good, "model": "gpt-4o-mini"}
    with pytest.raises(ValueError, match="pinned request model changed"):
        enforce_delegation_pin(child, tampered, client=child.client)


def test_pin_rejects_a_replaced_client_at_the_boundary():
    from types import SimpleNamespace

    from agent.chat_completion_helpers import enforce_delegation_pin
    from tools.custom_subagents import RuntimePin, parse_definitions

    raw = _definition(provider="openrouter", model="some/model")
    del raw["reasoning_effort"]  # unpinned effort: route stays pinned regardless
    definition = parse_definitions({"subagents": {"worker": raw}})["worker"]
    child = SimpleNamespace(
        provider="openrouter", model="some/model", api_mode="chat_completions",
        base_url="https://openrouter.ai/api/v1", api_key="fixture",
        client=SimpleNamespace(api_key="fixture",
                               base_url="https://openrouter.ai/api/v1"),
    )
    child._delegation_runtime_pin = RuntimePin.from_child(child, definition, None)
    kwargs = {"model": "some/model"}
    enforce_delegation_pin(child, kwargs, client=child.client)
    # A trailing slash is spelling, not a route change.
    enforce_delegation_pin(child, kwargs, client=SimpleNamespace(
        api_key="fixture", base_url="https://openrouter.ai/api/v1/"))
    other = SimpleNamespace(api_key="someone-elses-key",
                            base_url="https://openrouter.ai/api/v1")
    with pytest.raises(ValueError, match="credential changed"):
        enforce_delegation_pin(child, kwargs, client=other)


def test_codex_request_missing_reasoning_is_a_violation():
    """Codex always carries effort, so an absent field there IS a change."""
    from types import SimpleNamespace

    from tools.custom_subagents import RuntimePin, parse_definitions

    definition = parse_definitions({"subagents": {"worker": _definition()}})["worker"]
    child = SimpleNamespace(
        provider="openai-codex", model="gpt-5.6-luna", api_mode="codex_responses",
        base_url="https://chatgpt.com/backend-api/codex", api_key="fixture",
        client=SimpleNamespace(api_key="fixture",
                               base_url="https://chatgpt.com/backend-api/codex"),
    )
    pin = RuntimePin.from_child(child, definition, {"enabled": True, "effort": "medium"})
    with pytest.raises(ValueError, match="pinned request reasoning changed"):
        pin.validate_request(child, {"model": "gpt-5.6-luna"})


def test_openai_wire_may_omit_reasoning():
    """A non-reasoning model on the OpenAI wire sends no `reasoning` field."""
    from types import SimpleNamespace

    from tools.custom_subagents import RuntimePin, parse_definitions

    raw = _definition(provider="openrouter", model="some/model")
    definition = parse_definitions({"subagents": {"worker": raw}})["worker"]
    child = SimpleNamespace(
        provider="openrouter", model="some/model", api_mode="chat_completions",
        base_url="https://openrouter.ai/api/v1", api_key="fixture",
        client=SimpleNamespace(api_key="fixture",
                               base_url="https://openrouter.ai/api/v1"),
    )
    pin = RuntimePin.from_child(child, definition, {"enabled": True, "effort": "medium"})
    pin.validate_request(child, {"model": "some/model"})
    with pytest.raises(ValueError, match="pinned request reasoning changed"):
        pin.validate_request(child, {"model": "some/model", "reasoning": {"effort": "low"}})


def test_every_physical_dispatch_enforces_the_pin():
    """A pinnable route must be checked on EVERY way it reaches the network.

    The defect class this guards is "a dispatch site was missed": Anthropic
    streaming was declared pinnable while only its non-streaming sibling
    carried the check, so the guarantee held for one shape and not the other.
    Pinning the enclosing functions makes adding or moving a dispatch a
    conscious decision rather than a silent hole.
    """
    import ast
    import inspect

    from agent import chat_completion_helpers

    tree = ast.parse(inspect.getsource(chat_completion_helpers))
    guarded = set()

    def visit(node, enclosing):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                visit(child, child.name)
                continue
            if (isinstance(child, ast.Call)
                    and isinstance(child.func, ast.Name)
                    and child.func.id == "enforce_delegation_pin"):
                guarded.add(enclosing)
            visit(child, enclosing)

    visit(tree, None)
    assert guarded == {
        "_dispatch_nonstreaming_api_request",  # anthropic + openai non-stream
        # ``_open_chat_stream``, not ``_open_stream``: the decomposition split the openai-wire
        # streaming path into a wrapper (``_open_stream``, which only builds the timeout and
        # delegates) and the physical dispatch (``_open_chat_stream``, which owns the request
        # client and calls ``chat.completions.create``). The guard belongs on the physical
        # dispatch — it is the boundary another caller of ``_open_chat_stream`` could not bypass.
        # The unrelated ``_open_stream`` at the Bedrock converse path is intentionally unguarded:
        # bedrock is in UNPINNABLE_PROVIDERS and is refused at launch instead.
        "_open_chat_stream",               # openai-wire streaming (physical dispatch)
        "_open_anthropic_stream",          # anthropic-wire streaming
    }, f"pin enforcement moved or a dispatch site lost its guard: {guarded}"


@pytest.mark.parametrize("payload", [
    {"reasoning": {"max_tokens": 4096}},          # budget, not a named effort
    {"reasoning": {"exclude": True}},             # visibility flag
    {},                                           # nothing stated at all
])
def test_openai_wire_payloads_that_state_no_effort_are_not_violations(payload):
    """Silence is not a contradiction — raising here is a hard mid-run outage."""
    from types import SimpleNamespace

    from tools.custom_subagents import RuntimePin, parse_definitions

    raw = _definition(provider="openrouter", model="some/model")
    definition = parse_definitions({"subagents": {"worker": raw}})["worker"]
    child = SimpleNamespace(
        provider="openrouter", model="some/model", api_mode="chat_completions",
        base_url="https://openrouter.ai/api/v1", api_key="fixture",
        client=SimpleNamespace(api_key="fixture",
                               base_url="https://openrouter.ai/api/v1"),
    )
    pin = RuntimePin.from_child(child, definition, {"enabled": True, "effort": "medium"})
    pin.validate_request(child, {"model": "some/model", **payload})


@pytest.mark.parametrize("payload", [
    {"reasoning": {"effort": "low"}},
    {"reasoning_effort": "low"},
    {"extra_body": {"reasoning": {"effort": "low"}}},
])
def test_a_stated_effort_that_differs_is_rejected_in_every_spelling(payload):
    from types import SimpleNamespace

    from tools.custom_subagents import RuntimePin, parse_definitions

    raw = _definition(provider="openrouter", model="some/model")
    definition = parse_definitions({"subagents": {"worker": raw}})["worker"]
    child = SimpleNamespace(
        provider="openrouter", model="some/model", api_mode="chat_completions",
        base_url="https://openrouter.ai/api/v1", api_key="fixture",
        client=SimpleNamespace(api_key="fixture",
                               base_url="https://openrouter.ai/api/v1"),
    )
    pin = RuntimePin.from_child(child, definition, {"enabled": True, "effort": "medium"})
    with pytest.raises(ValueError, match="pinned request reasoning changed"):
        pin.validate_request(child, {"model": "some/model", **payload})


def test_ignore_user_config_still_degrades_instead_of_refusing(monkeypatch):
    """`--ignore-user-config` means "run on defaults", including a broken one.

    Refusing there would take plain unnamed delegation away from a user who
    configured no roles at all — a bigger loss than the silent fallback the
    loud-failure rule exists to prevent.
    """
    from tools import delegate_tool

    monkeypatch.setenv("HERMES_IGNORE_USER_CONFIG", "1")
    monkeypatch.setattr(
        delegate_tool, "_legacy_delegation_config", lambda: {},
    )
    assert delegate_tool.load_delegation_config() == {}
    assert delegate_tool.last_delegation_config_error() is None


def test_unpinnable_routes_are_rejected_rather_than_weakened():
    from tools.custom_subagents import pinning_support_error

    assert pinning_support_error("openai-codex", "codex_responses") is None
    assert pinning_support_error("anthropic", "anthropic_messages") is None
    assert "no inspectable final-request boundary" in (
        pinning_support_error("custom", "bedrock_converse") or "")
    assert "cannot be pinned" in (
        pinning_support_error("moa", "chat_completions") or "")


def test_no_pin_means_no_enforcement():
    """Ordinary delegation and the parent are untouched by all of this."""
    from types import SimpleNamespace

    from agent.chat_completion_helpers import enforce_delegation_pin

    enforce_delegation_pin(SimpleNamespace(), {"model": "anything"})


# ── items 5, 6: configuration registration and mixed-batch telemetry ────────

def test_config_key_validation_matches_the_runtime_field_set():
    from hermes_cli.config import _SUBAGENT_FIELDS, _validate_config_key
    from tools.custom_subagents import SUBAGENT_FIELDS

    assert _SUBAGENT_FIELDS == SUBAGENT_FIELDS
    assert _validate_config_key("delegation.subagents") == (True, None)
    assert _validate_config_key("delegation.subagents.explorer") == (True, None)
    assert _validate_config_key("delegation.subagents.explorer.model") == (True, None)
    known, suggestion = _validate_config_key("delegation.subagents.explorer.instruction")
    assert known is False and suggestion.endswith("instructions")
    assert _validate_config_key("delegation.subagents.Explorer.model") == (False, None)
    assert _validate_config_key("delegation.subagents.explorer.model.x")[0] is False


def test_delegation_subagents_is_a_known_default_key():
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    assert DEFAULT_CONFIG["delegation"]["subagents"] == {}


def test_mixed_batch_records_each_child_route(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from tools import delegation_live_log

    routing = [
        {"subagent_type": "explorer", "provider": "openai-codex",
         "model": "gpt-5.6-luna", "reasoning_effort": "medium"},
        {"subagent_type": "worker", "provider": "openai-codex",
         "model": "gpt-5.6-terra", "reasoning_effort": "medium"},
    ]
    tasks = [{"goal": "explore"}, {"goal": "implement"}]
    deleg_id, _writers, paths = delegation_live_log.create_live_transcripts(
        tasks, None, model="gpt-5.6-luna", provider="openai-codex", routing=routing,
    )
    assert deleg_id and paths
    manifest = json.loads(
        delegation_live_log._manifest_path(deleg_id).read_text(encoding="utf-8"))
    assert [t["model"] for t in manifest["tasks"]] == ["gpt-5.6-luna", "gpt-5.6-terra"]
    assert [t["subagent_type"] for t in manifest["tasks"]] == ["explorer", "worker"]
    # The batch label must not inherit task 0's model when children disagree.
    assert manifest["model"] == "mixed"


def test_uniform_batch_keeps_its_single_model(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from tools import delegation_live_log

    routing = [
        {"subagent_type": "worker", "provider": "openai-codex",
         "model": "gpt-5.6-terra", "reasoning_effort": "medium"},
    ] * 2
    deleg_id, _writers, _paths = delegation_live_log.create_live_transcripts(
        [{"goal": "a"}, {"goal": "b"}], None, routing=routing,
    )
    manifest = json.loads(
        delegation_live_log._manifest_path(deleg_id).read_text(encoding="utf-8"))
    assert manifest["model"] == "gpt-5.6-terra"


def test_legacy_children_record_their_inherited_route():
    """The most common shape: an unnamed child inherits, and that IS its route.

    Recording nothing for it (the pre-change manifest) reports the batch as
    routeless, which is exactly the case an operator most often needs to read.
    """
    from types import SimpleNamespace

    from tools.delegate_tool import _task_routing_metadata

    parent = SimpleNamespace(
        model="gpt-6-astra", provider="openai-codex",
        reasoning_config={"enabled": True, "effort": "high"},
    )
    routing = _task_routing_metadata([(None, {}, None), (None, {}, None)], parent)
    assert routing == [{
        "subagent_type": None, "provider": "openai-codex",
        "model": "gpt-6-astra", "reasoning_effort": "high",
    }] * 2


def test_all_unknown_routing_does_not_erase_the_batch_fallback(tmp_path, monkeypatch):
    """A routing list with nothing resolved must not blank the manifest."""
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from tools import delegation_live_log

    routing = [{"subagent_type": None, "provider": None,
                "model": None, "reasoning_effort": None}] * 2
    deleg_id, _writers, _paths = delegation_live_log.create_live_transcripts(
        [{"goal": "a"}, {"goal": "b"}], None,
        model="gpt-6-astra", provider="openai-codex", routing=routing,
    )
    manifest = json.loads(
        delegation_live_log._manifest_path(deleg_id).read_text(encoding="utf-8"))
    assert manifest["model"] == "gpt-6-astra"
    assert manifest["provider"] == "openai-codex"


def test_pin_tolerates_a_keyless_route_with_a_placeholder_client():
    """A local/keyless provider must not die on an SDK placeholder api_key."""
    from types import SimpleNamespace

    from tools.custom_subagents import RuntimePin, parse_definitions

    raw = _definition(provider="custom", model="local/model")
    del raw["reasoning_effort"]
    definition = parse_definitions({"subagents": {"worker": raw}})["worker"]
    child = SimpleNamespace(
        provider="custom", model="local/model", api_mode="chat_completions",
        base_url="http://localhost:11434/v1", api_key=None,
        client=SimpleNamespace(api_key="placeholder",
                               base_url="http://localhost:11434/v1"),
    )
    pin = RuntimePin.from_child(child, definition, None)
    pin.validate_request(child, {"model": "local/model"}, client=child.client)
    # The route itself is still pinned.
    with pytest.raises(ValueError, match="SDK client route changed"):
        pin.validate_request(child, {"model": "local/model"}, client=SimpleNamespace(
            api_key="placeholder", base_url="https://api.openai.com/v1"))


def test_codex_client_with_a_falsy_key_is_still_a_credential_swap():
    """The keyless allowance must not become a Codex exemption."""
    from types import SimpleNamespace

    from tools.custom_subagents import RuntimePin, parse_definitions

    definition = parse_definitions({"subagents": {"worker": _definition()}})["worker"]
    child = SimpleNamespace(
        provider="openai-codex", model="gpt-5.6-luna", api_mode="codex_responses",
        base_url="https://chatgpt.com/backend-api/codex", api_key="fixture",
        client=SimpleNamespace(api_key="fixture",
                               base_url="https://chatgpt.com/backend-api/codex"),
    )
    pin = RuntimePin.from_child(child, definition, {"enabled": True, "effort": "medium"})
    good = {"model": "gpt-5.6-luna", "reasoning": {"effort": "medium"}}
    pin.validate_request(child, good, client=child.client)
    for stripped in (None, ""):
        with pytest.raises(ValueError, match="credential changed"):
            pin.validate_request(child, good, client=SimpleNamespace(
                api_key=stripped, base_url=child.base_url))


@pytest.mark.parametrize("client_base_url", [
    "https://api.anthropic.com",          # SDK keeps the host root
    "https://api.anthropic.com/",         # ... with a trailing slash
])
def test_anthropic_client_path_normalization_is_not_a_route_change(client_base_url):
    """A `/v1` the SDK drops must not abort every request the child makes."""
    from types import SimpleNamespace

    from tools.custom_subagents import RuntimePin, parse_definitions

    raw = _definition(provider="anthropic", model="claude-opus-5")
    del raw["reasoning_effort"]
    definition = parse_definitions({"subagents": {"worker": raw}})["worker"]
    child = SimpleNamespace(
        provider="anthropic", model="claude-opus-5", api_mode="anthropic_messages",
        base_url="https://api.anthropic.com/v1", api_key="fixture",
        client=SimpleNamespace(api_key="fixture", base_url=client_base_url),
    )
    pin = RuntimePin.from_child(child, definition, {"enabled": True, "effort": "medium"})
    pin.validate_request(child, {"model": "claude-opus-5"}, client=child.client)
    # A different host is still rejected.
    with pytest.raises(ValueError, match="SDK client route changed"):
        pin.validate_request(child, {"model": "claude-opus-5"}, client=SimpleNamespace(
            api_key="fixture", base_url="https://example.invalid/v1"))


def test_routing_metadata_is_nonsecret():
    from tools.delegate_tool import _task_routing_metadata
    from tools.custom_subagents import parse_definitions

    definition = parse_definitions({"subagents": {"worker": _definition()}})["worker"]
    creds = {"provider": "openai-codex", "model": "gpt-5.6-luna",
             "api_key": "super-secret-token", "base_url": "https://x"}
    routing = _task_routing_metadata([
        (definition, creds, {"enabled": True, "effort": "medium"}),
        (None, {"provider": "openrouter", "model": "legacy/model"}, None),
    ], None)
    assert "super-secret-token" not in json.dumps(routing)
    assert routing[0] == {
        "subagent_type": "worker", "provider": "openai-codex",
        "model": "gpt-5.6-luna", "reasoning_effort": "medium",
    }
    # Legacy (unnamed) delegation stays legible as such.
    assert routing[1]["subagent_type"] is None
