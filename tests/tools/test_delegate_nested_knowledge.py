"""Nested delegation cannot shed inherited shared-knowledge authority."""

import json

import pytest


@pytest.mark.parametrize("named", [True, False])
def test_unnamed_descendant_preserves_knowledge_authority(tmp_path, monkeypatch, named):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)
    (tmp_path / "config.yaml").write_text(
        "delegation:\n  max_spawn_depth: 3\n  child_timeout_seconds: 30\n"
    )
    from run_agent import AIAgent
    from tools import delegate_tool as dt
    from tools.delegate_tool_child_run import _ChildRun
    from tools.custom_subagents import parse_definitions
    from agent.delegation_context import is_read_only_knowledge_context
    from model_tools import handle_function_call
    from tools.memory_tool import memory_tool
    from tools.skill_manager_tool import skill_manage
    from tools.knowledge_boundary import command_denial_reason

    home = tmp_path
    (home / "memories").mkdir(exist_ok=True)
    target = home / "memories" / "probe.txt"
    target.write_text("original")
    observed = []
    parent = AIAgent(
        api_key="fixture",
        base_url="https://openrouter.ai/api/v1",
        provider="openrouter",
        api_mode="chat_completions",
        model="fixture-model",
        quiet_mode=True,
        skip_context_files=True,
        skip_memory=True,
        save_trajectories=False,
        enabled_toolsets=["file", "skills", "terminal", "delegation"],
    )
    definition = (
        parse_definitions({
            "subagents": {
                "fixture": {
                    "description": "Fixture",
                    "instructions": "Inspect fixture",
                    "provider": "openrouter",
                    "model": "fixture-model",
                    "reasoning_effort": "medium",
                }
            }
        })["fixture"]
        if named
        else None
    )
    mid = dt._build_child_agent(
        0,
        "mid",
        None,
        None,
        None,
        2,
        1,
        parent,
        subagent_definition=definition,
        resolved_reasoning={"effort": "medium"} if named else None,
    )

    def scripted_conversation(self, user_message, **kwargs):
        if self is mid:
            assert "delegate_task" in self.valid_tool_names
            result = json.loads(
                dt.delegate_task(
                    goal="nested",
                    task_label="Nested fixture",
                    background=False,
                    parent_agent=self,
                )
            )
            assert result.get("results"), result
            assert result["results"][0]["status"] == "completed", result
        else:
            observed.append({
                "ambient": is_read_only_knowledge_context(),
                "memory_manager": self._memory_manager,
                "memory_store": self._memory_store,
                "skill_exposed": "skill_manage" in self.valid_tool_names,
                "skill_disabled": "skill_management" in self.disabled_toolsets,
            })
            result = json.loads(
                handle_function_call(
                    "write_file",
                    {"path": str(target), "content": "changed"},
                    task_id=kwargs["task_id"],
                    enabled_tools=list(self.valid_tool_names),
                )
            )
            if named:
                assert "Parent-owned shared knowledge" in result["error"], result
                patch = json.loads(
                    handle_function_call(
                        "patch",
                        {
                            "path": str(target),
                            "old_string": "original",
                            "new_string": "changed",
                        },
                        task_id=kwargs["task_id"],
                        enabled_tools=list(self.valid_tool_names),
                    )
                )
                assert "Parent-owned shared knowledge" in patch["error"], patch
                terminal = json.loads(
                    handle_function_call(
                        "terminal",
                        {"command": f"printf changed > {target}"},
                        task_id=kwargs["task_id"],
                        enabled_tools=list(self.valid_tool_names),
                    )
                )
                assert "Parent-owned shared knowledge" in terminal["error"], terminal
                assert (
                    "parent-owned"
                    in json.loads(
                        memory_tool(action="add", content="changed", store=object())
                    )["error"]
                )
                assert (
                    "read-only"
                    in json.loads(
                        skill_manage(action="create", name="blocked", content="blocked")
                    )["error"]
                )
            else:
                assert target.read_text() == "changed", result
                assert command_denial_reason(f"printf changed > {target}") is None
        return {
            "final_response": "completed fixture",
            "completed": True,
            "messages": [],
            "api_calls": 0,
        }

    # Replace model work only. Construction, nested admission, worker context,
    # tool selection, and mediated writes all use the production implementation.
    monkeypatch.setattr(AIAgent, "run_conversation", scripted_conversation)
    try:
        run = _ChildRun(
            mid, parent, 0, "mid", mid._subagent_id, None, child_task_id="outer"
        )
        result, error, deferred = run.await_child()
        assert error is None, error
        assert result["completed"] and not deferred
        assert len(observed) == 1
        row = observed[0]
        assert row["ambient"] is named
        assert row["skill_disabled"] is named
        assert row["skill_exposed"] is (not named)
        assert row["memory_manager"] is None and row["memory_store"] is None
        assert target.read_text() == ("original" if named else "changed")
    finally:
        mid.close()
        parent.close()
