"""Live-parent routes protect real private construction without disabling public logging."""
from contextlib import nullcontext
import json
from typing import Any
from unittest.mock import Mock, patch

import pytest

from agent import prompt_builder
from agent.incognito import incognito_scope, is_incognito, register_session
from agent.subagent_lifecycle import SubagentLaunchRequest, SubagentLifecycleService
from hermes_state import SessionDB
import hermes_logging
from run_agent import AIAgent
from tests.agent.test_incognito_auxiliary_health import _files
from tests.tools.test_delegate_by_parent import MEDIUM, PARENT_CHAIN, PINNED_CHAIN, VENICE, _authored, config_file
from tools import delegate_tool as delegate


@pytest.mark.parametrize("private_parent", [False, True])
@pytest.mark.parametrize("caller_identity", [None, "unrelated-public-caller"])
@pytest.mark.parametrize("plugin_launch", [False, True])
@pytest.mark.parametrize("route", ["inherit", "@worker"])
def test_live_parent_private_route_builds_real_private_child_and_grandchild(
    config_file, tmp_path, monkeypatch, private_parent, caller_identity, plugin_launch, route,
):
    raw: dict[str, Any] = _authored()
    raw["delegation"]["by_parent"]["@uncensored"] = route
    raw["context_file_max_chars"] = 256
    config_file(raw)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "AGENTS.md").write_text("Context fixture line.\n" * 128, encoding="utf-8")
    monkeypatch.setattr("agent.runtime_cwd.scope_terminal_cwd", lambda: str(workspace))
    monkeypatch.setattr("hermes_cli.runtime_provider.resolve_runtime_provider", lambda **kwargs: {
        "provider": "custom", "base_url": "https://worker.test/v1", "api_key": "test-only", "api_mode": "chat_completions",
    })
    monkeypatch.setattr("agent.model_metadata.fetch_model_metadata", lambda **kwargs: {})
    warning = Mock(wraps=prompt_builder.logger.warning)
    monkeypatch.setattr(prompt_builder.logger, "warning", warning)
    hermes_logging.setup_logging(hermes_home=tmp_path, log_level="INFO", force=True)
    db = SessionDB(db_path=tmp_path / "state.db")
    parent = AIAgent(model=VENICE, provider="custom:venice", api_key="test-only",
                     base_url="https://venice.test/v1", quiet_mode=True, enabled_toolsets=[],
                     skip_context_files=True, skip_memory=True, session_id="private-route-parent",
                     session_db=db, reasoning_config=MEDIUM, fallback_model=PARENT_CHAIN)
    parent._ensure_db_session()
    parent._cached_system_prompt = "public frozen prompt"
    parent.set_incognito(private_parent)
    created = []

    def collect(batch, background):
        assert is_incognito() is private_parent
        created.extend(child for _index, _task, child in batch.children)
        return json.dumps({"ok": True})

    try:
        warning.reset_mock()
        before = _files(tmp_path)
        caller_scope = incognito_scope(False, caller_identity) if caller_identity else nullcontext()
        with caller_scope:
            assert not is_incognito()
            if plugin_launch:
                from agent import subagent_lifecycle as lifecycle
                with patch.object(lifecycle._EXECUTOR, "submit"):
                    handle = SubagentLifecycleService(lambda: parent).launch(SubagentLaunchRequest(goal="PRIVATE_ROUTE_GOAL"))
                with lifecycle._REGISTRY.lock:
                    child = lifecycle._REGISTRY.records.pop(handle.subagent_id).agent
                created.append(child)
            else:
                with patch.object(delegate, "_run_batch", side_effect=collect):
                    assert json.loads(delegate.delegate_task(goal="PRIVATE_ROUTE_GOAL", parent_agent=parent)) == {"ok": True}
                child = created[0]
            assert isinstance(child, AIAgent)
            if private_parent:
                assert child.incognito and child._persist_disabled
                assert child._session_db is None
            else:
                assert not child.incognito and not child._persist_disabled
                assert child._session_db is not None
            if route == "inherit":
                assert child.model == parent.model == VENICE
                assert child.provider == parent.provider
                assert child.base_url == parent.base_url
                assert child._fallback_chain == PARENT_CHAIN
                assert child.reasoning_config == MEDIUM
                with patch.object(delegate, "_run_batch", side_effect=collect):
                    assert json.loads(delegate.delegate_task(goal="PRIVATE_NESTED_GOAL", parent_agent=child)) == {"ok": True}
                grandchild = created[-1]
                if private_parent:
                    assert grandchild.incognito and grandchild._persist_disabled
                    assert grandchild._session_db is None
                else:
                    assert not grandchild.incognito and not grandchild._persist_disabled
                    assert grandchild._session_db is not None
                assert grandchild.model == VENICE and grandchild.reasoning_config == MEDIUM
            else:
                assert child.model == "worker-model"
                assert child.base_url == "https://worker.test/v1"
                assert child._fallback_chain == PINNED_CHAIN
                assert child.reasoning_config == {"enabled": True, "effort": "low"}
            for agent in created:
                agent._ensure_db_session()
                if private_parent:
                    assert not agent._session_db_created
                else:
                    assert agent._session_db_created
        # Observe the real producer, not a stubbed warning or a warmed baseline.
        assert any("Context file AGENTS.md TRUNCATED" in str(call.args[0]) for call in warning.call_args_list)
        after = _files(tmp_path)
        if private_parent:
            assert after == before
        else:
            assert b"Context file AGENTS.md TRUNCATED" in after["logs/agent.log"]
    finally:
        for agent in created:
            register_session(agent.session_id, False)
            agent.close()
        parent.set_incognito(False)
        parent.close()
        register_session(parent.session_id, False)
        db.close()
        hermes_logging._reset_queued_handlers()
