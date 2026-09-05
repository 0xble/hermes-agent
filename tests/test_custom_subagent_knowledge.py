import inspect
import json
from types import SimpleNamespace

import pytest

from agent.agent_init import init_agent
from agent.delegation_context import delegated_child_context
from agent.memory_manager import MemoryManager, inject_memory_provider_tools
from agent.memory_provider import MemoryProvider
from toolsets import TOOLSETS


class RecordingProvider(MemoryProvider):
    @property
    def name(self):
        return "recording"

    def __init__(self, *, opt_in=True):
        self.opt_in = opt_in
        self.calls = []

    def is_available(self):
        return True

    def initialize(self, session_id, **kwargs):
        self.calls.append(("initialize", session_id, kwargs))

    def supports_read_only_prefetch(self):
        return self.opt_in

    def get_read_only_tool_names(self):
        return {"memory_read"} if self.opt_in else set()

    def get_tool_schemas(self):
        return [
            {"name": "memory_read", "description": "read", "parameters": {"type": "object", "properties": {}}},
            {"name": "memory_write", "description": "write", "parameters": {"type": "object", "properties": {}}},
        ]

    def handle_tool_call(self, tool_name, args, **kwargs):
        self.calls.append(("tool", tool_name))
        return json.dumps({"tool": tool_name})

    def prefetch(self, query, *, session_id=""):
        self.calls.append(("prefetch", query))
        return "remembered"

    def queue_prefetch(self, query, *, session_id=""):
        self.calls.append(("queue", query))

    def sync_turn(self, user_content, assistant_content, **kwargs):
        self.calls.append(("sync", user_content))

    def on_turn_start(self, turn_number, message, **kwargs):
        self.calls.append(("turn", turn_number))

    def on_session_end(self, messages):
        self.calls.append(("end", len(messages)))

    def on_pre_compress(self, messages, **kwargs):
        self.calls.append(("compress", len(messages)))
        return "write-derived context"


def test_read_only_memory_manager_filters_and_blocks_writes(tmp_path):
    provider = RecordingProvider()
    manager = MemoryManager(read_only=True)
    manager.add_provider(provider)
    manager.initialize_all("child-session", hermes_home=str(tmp_path), agent_context="subagent")

    assert manager.get_all_tool_names() == {"memory_read"}
    assert [schema["name"] for schema in manager.get_all_tool_schemas()] == ["memory_read"]
    assert json.loads(manager.handle_tool_call("memory_read", {}))["tool"] == "memory_read"
    assert "read-only" in json.loads(manager.handle_tool_call("memory_write", {}))["error"]
    assert manager.prefetch_all("important query") == "remembered"

    manager.queue_prefetch_all("next")
    manager.sync_all("user", "assistant")
    manager.on_turn_start(1, "hello")
    manager.on_session_end([])
    assert manager.on_pre_compress([]) == ""
    assert manager.supports_pre_compress_checkpoint() is False

    names = [call[0] for call in provider.calls]
    assert names.count("initialize") == 1
    init = provider.calls[0]
    assert init[2]["read_only"] is True
    assert init[2]["agent_context"] == "subagent"
    assert "sync" not in names
    assert "queue" not in names
    assert "turn" not in names
    assert "end" not in names
    assert "compress" not in names


def test_read_only_memory_manager_fails_closed_without_provider_contract(tmp_path):
    provider = RecordingProvider(opt_in=False)
    manager = MemoryManager(read_only=True)
    manager.add_provider(provider)
    manager.initialize_all("child-session", hermes_home=str(tmp_path), agent_context="subagent")
    assert provider.calls == []
    assert manager.get_all_tool_names() == set()
    assert manager.prefetch_all("query") == ""


def test_read_only_provider_tools_inject_without_builtin_memory_tool(tmp_path):
    provider = RecordingProvider()
    manager = MemoryManager(read_only=True)
    manager.add_provider(provider)
    manager.initialize_all("child", hermes_home=str(tmp_path))
    agent = SimpleNamespace(
        _memory_manager=manager,
        tools=[],
        valid_tool_names=set(),
        enabled_toolsets=[],
        disabled_toolsets=["memory"],
    )
    assert inject_memory_provider_tools(agent) == 1
    assert agent.tools[0]["function"]["name"] == "memory_read"


def test_hindsight_declares_only_pure_retrieval_in_read_only_mode():
    from plugins.memory.hindsight import HindsightMemoryProvider

    provider = HindsightMemoryProvider()
    provider._read_only = True
    assert provider.supports_read_only_prefetch() is True
    assert provider.get_read_only_tool_names() == {"hindsight_recall", "hindsight_reflect"}
    assert {schema["name"] for schema in provider.get_tool_schemas()} == {
        "hindsight_recall",
        "hindsight_reflect",
    }
    blocked = json.loads(provider.handle_tool_call("hindsight_retain", {"content": "no"}))
    assert "read-only" in blocked["error"]


def test_hindsight_read_only_initialize_skips_repair_and_retention(monkeypatch):
    import plugins.memory.hindsight as hindsight

    monkeypatch.setattr(hindsight, "_load_config", lambda: {"mode": "cloud", "auto_retain": True})
    provider = hindsight.HindsightMemoryProvider()
    monkeypatch.setattr(
        provider,
        "_ensure_supported_client",
        lambda: (_ for _ in ()).throw(AssertionError("must not install or repair")),
    )
    provider.initialize("child", read_only=True)
    assert provider._read_only is True
    assert provider._auto_retain is False
    assert provider._writer_thread is None


@pytest.mark.parametrize("mode", ["local", "local_embedded"])
def test_embedded_read_only_unavailability_is_reported_without_startup(monkeypatch, tmp_path, mode):
    import plugins.memory.hindsight as hindsight
    from tools.custom_subagents import RuntimePin, resolution_metadata

    monkeypatch.setattr(hindsight, "_load_config", lambda: {"mode": mode})
    monkeypatch.setattr(hindsight, "_check_local_runtime", lambda: pytest.fail("must not start embedded runtime"))
    provider = hindsight.HindsightMemoryProvider()
    monkeypatch.setattr(provider, "_ensure_supported_client", lambda: pytest.fail("must not repair packages"))
    manager = MemoryManager(read_only=True)
    manager.add_provider(provider)
    manager.initialize_all("child", hermes_home=str(tmp_path), agent_context="subagent")
    assert manager.get_all_tool_names() == set()
    child = SimpleNamespace(
        _memory_manager=manager,
        _delegation_runtime_pin=RuntimePin("explorer", "openai-codex", "gpt-5.6-luna", "", "codex_responses", "medium", ""),
    )
    assert resolution_metadata(child)["unavailable_memory_providers"] == [
        {"provider": "hindsight", "reason": "initialization_failed"}
    ]


def test_delegated_child_cannot_mutate_skills(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    from tools.skill_manager_tool import skill_manage

    with delegated_child_context("child", read_only_knowledge=True):
        result = json.loads(
            skill_manage(
                action="create",
                name="forbidden",
                content="---\nname: forbidden\ndescription: Use when testing. Test.\n---\nbody",
            )
        )
    assert result["success"] is False
    assert "read-only" in result["error"]
    assert not (tmp_path / "skills" / "forbidden").exists()


def test_named_child_builtin_memory_write_is_denied_before_store_access():
    from tools.memory_tool import memory_tool
    with delegated_child_context("named", read_only_knowledge=True):
        result = json.loads(memory_tool(action="add", content="not written", store=object()))
    assert result["success"] is False
    assert "parent-owned" in result["error"]


def test_delegated_child_skill_view_does_not_bump_usage(monkeypatch):
    import tools.skills_tool as skills_tool
    import tools.skill_usage as usage

    monkeypatch.setattr(skills_tool, "_check_skill_view_dedup", lambda *a, **k: None)
    monkeypatch.setattr(skills_tool, "skill_view", lambda *a, **k: json.dumps({"success": True, "name": "demo"}))
    bumped = []
    monkeypatch.setattr(usage, "bump_view", lambda *a, **k: bumped.append("view"))
    monkeypatch.setattr(usage, "bump_use", lambda *a, **k: bumped.append("use"))
    with delegated_child_context("child", read_only_knowledge=True):
        result = json.loads(skills_tool._skill_view_with_bump({"name": "demo"}))
    assert result["success"] is True
    assert bumped == []


def test_read_only_knowledge_context_is_scoped_and_reaches_subprocess_env():
    from agent.delegation_context import (
        is_read_only_knowledge_context, delegated_child_subprocess_env,
        READ_ONLY_KNOWLEDGE_ENV_MARKER,
    )
    assert is_read_only_knowledge_context() is False
    with delegated_child_context("legacy"):
        assert is_read_only_knowledge_context() is False
    with delegated_child_context("named", read_only_knowledge=True):
        assert is_read_only_knowledge_context() is True
        assert delegated_child_subprocess_env({})[READ_ONLY_KNOWLEDGE_ENV_MARKER] == "1"
    assert is_read_only_knowledge_context() is False


def test_singleton_skill_management_deny_toolset_and_init_signature():
    assert TOOLSETS["skill_management"]["tools"] == ["skill_manage"]
    parameter = inspect.signature(init_agent).parameters["memory_access_mode"]
    assert parameter.default is None
    assert parameter.annotation in ("Optional[str]", "typing.Optional[str]") or parameter.annotation is not inspect.Parameter.empty
