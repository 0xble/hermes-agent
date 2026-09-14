"""Rejected construction must release actual agent and SessionDB ownership."""
from unittest.mock import Mock

import pytest

from tests.agent.test_custom_subagent_runtime import make_child
from tools import delegate_tool
from tools.custom_subagents import SubagentDefinition
import hermes_state_registry as registry


@pytest.fixture
def allocation(make_child, monkeypatch, tmp_path):
    import run_agent

    parent = make_child()
    if parent._session_db is None:
        parent._session_db = registry.acquire(tmp_path / "parent.db")
        parent._owns_session_db = True
    monkeypatch.setattr(delegate_tool, "_load_config", lambda: {})
    original = run_agent.AIAgent
    children = []

    def construct(**kwargs):
        child = original(**kwargs)
        child.close = Mock(wraps=child.close)
        children.append(child)
        return child

    monkeypatch.setattr(run_agent, "AIAgent", construct)
    baseline = registry.stats()["total_refcounts"]
    yield parent, children, baseline
    # Failed red runs must not leave allocated test children behind.
    for child in children:
        delegate_tool._detach_child(parent, child)
        if child.close.call_count == 0:
            child._owns_session_db = True
            child.close()


@pytest.mark.parametrize("boundary", ["pin", "moa", "review_policy"])
def test_postconstruction_failure_releases_child_and_its_database_reference(allocation, monkeypatch, boundary):
    parent, children, baseline = allocation
    role = SubagentDefinition("worker", "Work", "Inspect", provider=parent.provider, model=parent.model)
    if boundary == "pin":
        monkeypatch.setattr("tools.custom_subagents.RuntimePin.from_child", Mock(side_effect=ValueError("pin rejected")))
    elif boundary == "moa":
        import run_agent
        construct = run_agent.AIAgent

        def moa_child(**kwargs):
            child = construct(**kwargs)
            child.provider = "moa"
            return child

        monkeypatch.setattr(run_agent, "AIAgent", moa_child)
        monkeypatch.setattr("agent.moa_loop.build_moa_facade", Mock(side_effect=ValueError("moa rejected")))
    else:
        monkeypatch.setattr("agent.review_policy.remove_parent_only_review_tools", Mock(side_effect=ValueError("policy rejected")))
    with pytest.raises(ValueError, match="rejected"):
        delegate_tool._build_child_agent(0, "Inspect", None, None, parent.model, 1, 1, parent,
                                        subagent_definition=role, resolved_reasoning=parent.reasoning_config)
    assert len(children) == 1
    children[0].close.assert_called_once_with()
    assert children[0] not in parent._active_children
    assert registry.stats()["total_refcounts"] == baseline
    assert parent._session_db is not None


@pytest.mark.parametrize("failure", [None, ValueError, RuntimeError, KeyboardInterrupt])
def test_failed_batch_closes_prior_sibling_and_preserves_claim_release(allocation, monkeypatch, failure):
    parent, children, baseline = allocation
    build = delegate_tool._build_child_preserving_parent_tools

    def construct(**kwargs):
        if failure is not None and kwargs["task_index"] == 1:
            raise failure("next child rejected")
        return build(**kwargs)

    monkeypatch.setattr(delegate_tool, "_build_child_preserving_parent_tools", construct)
    release = Mock()
    monkeypatch.setattr(delegate_tool, "_release_resume_launches", release)
    creds = {key: getattr(parent, key) for key in ("model", "provider", "base_url", "api_key", "api_mode")}
    args = ([{"goal": "First"}, {"goal": "Second"}], [None, None], creds)
    kwargs = dict(top_role="leaf", max_iterations=1, parent_agent=parent, live_deleg_id=None, live_writers=[])
    if failure is None:
        built, error = delegate_tool._build_children(*args, **kwargs)
        assert error is None and len(built) == 2
        assert [row[2] for row in built] == children
        for child in children:
            child.close.assert_not_called()
            assert child in parent._active_children
        assert registry.stats()["total_refcounts"] == baseline + len(children)
        release.assert_not_called()
        return
    if failure is ValueError:
        assert delegate_tool._build_children(*args, **kwargs) == ([], "next child rejected")
    else:
        with pytest.raises(failure, match="next child rejected"):
            delegate_tool._build_children(*args, **kwargs)
    assert len(children) == 1
    children[0].close.assert_called_once_with()
    assert children[0] not in parent._active_children
    assert registry.stats()["total_refcounts"] == baseline
    release.assert_called_once_with(parent, [])


def test_public_batch_handoff_failure_closes_allocations_before_run(allocation, monkeypatch):
    parent, children, baseline = allocation
    monkeypatch.setattr("tools.async_delegation.current_delegation_owner", lambda parent: {
        "profile": "default", "session_id": parent.session_id, "session_key": "fixture",
        "chat_id": "42", "thread_id": ""})
    monkeypatch.setattr("tools.delegation_live_log.create_live_transcripts", lambda *a, **k: ("fixture", [], []))
    monkeypatch.setattr(delegate_tool, "_announce_batch", lambda *args: None)
    monkeypatch.setattr(delegate_tool, "_Batch", Mock(side_effect=RuntimeError("batch handoff rejected")))
    execute = Mock()
    monkeypatch.setattr(delegate_tool, "_run_batch", execute)
    with pytest.raises(RuntimeError, match="batch handoff rejected"):
        delegate_tool.delegate_task(tasks=[{"goal": "Inspect source", "task_label": "Inspect source"}], parent_agent=parent)
    assert len(children) == 1
    children[0].close.assert_called_once_with()
    assert children[0] not in parent._active_children
    assert registry.stats()["total_refcounts"] == baseline
    execute.assert_not_called()
