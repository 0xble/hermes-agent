"""Launch authority stays per child through public delegation and construction."""
import json
import threading
from types import SimpleNamespace

import pytest

from tools import delegate_tool
from tools.custom_subagents import ResolvedSubagentLaunch, SubagentDefinition, resolve_named_credentials


@pytest.fixture
def parent(monkeypatch):
    monkeypatch.setattr(delegate_tool, "_load_config", lambda: {})
    monkeypatch.setattr(delegate_tool, "last_delegation_config_error", lambda: None)
    monkeypatch.setattr(delegate_tool, "_get_max_spawn_depth", lambda: 2)
    monkeypatch.setattr(delegate_tool, "_get_max_concurrent_children", lambda: 3)
    return SimpleNamespace(
        _session_db=None, session_id="parent", model="parent-model", provider="openrouter",
        api_key="parent-key", base_url="https://parent.invalid/v1", api_mode="chat_completions",
        request_overrides={"parent": True}, reasoning_config={"enabled": True, "effort": "high"},
        _delegate_depth=0, _active_children=[], _active_children_lock=threading.Lock(),
        _print_fn=None, tool_progress_callback=None, thinking_callback=None, _fallback_chain=[],
    )


@pytest.fixture
def local_dispatch(monkeypatch):
    def metadata(**kwargs):
        labels = kwargs["task_labels"]
        return {"parent_task_id": "task", "thread_refs": [f"thread-{i}" for i in range(len(labels))],
                "task_labels": labels}

    monkeypatch.setattr("tools.async_delegation.reserve_delegation_metadata", metadata)
    monkeypatch.setattr("tools.delegation_live_log.create_live_transcripts", lambda *a, **k: ("live", [], []))
    monkeypatch.setattr(delegate_tool, "_announce_batch", lambda *args: None)
    monkeypatch.setattr(delegate_tool, "_capture_origin", lambda: ("", "", None, None, False))
    batches = []
    monkeypatch.setattr(delegate_tool, "_run_batch", lambda batch, background: batches.append(batch) or "{}")
    return batches


@pytest.mark.parametrize("named_first", [True, False])
def test_public_mixed_batch_preserves_each_launch_route(parent, local_dispatch, monkeypatch, named_first):
    named = {"provider": "openai", "model": "named-model", "base_url": "https://named.invalid/v1",
             "api_key": "named-key", "api_mode": "chat_completions", "request_overrides": {"named": True}}
    role = SubagentDefinition("worker", "Work", "Inspect", inherit_parent=True)
    legacy = delegate_tool._resolve_delegation_credentials({}, parent)
    launches = [ResolvedSubagentLaunch(role, named, None), ResolvedSubagentLaunch(None, legacy, None)]
    if not named_first:
        launches.reverse()
    monkeypatch.setattr(delegate_tool, "_preflight_task_runtime", lambda *args: (launches, None))
    seen = []

    def construct(**kwargs):
        seen.append(kwargs)
        return SimpleNamespace(_progress_identity_ref={}, _delegate_role="leaf")

    monkeypatch.setattr(delegate_tool, "_build_child_preserving_parent_tools", construct)
    delegate_tool.delegate_task(tasks=[{"goal": "Inspect source carefully", "task_label": "Inspect source"},
                                       {"goal": "Check related behavior", "task_label": "Check behavior"}], parent_agent=parent)
    assert len(seen) == 2
    for launch, actual in zip(launches, seen):
        assert actual["model"] == launch.credentials["model"]
        for field in ("provider", "base_url", "api_key", "api_mode", "request_overrides"):
            assert actual[f"override_{field}"] == launch.credentials.get(field)
    assert parent.api_key == "parent-key"


def test_inherit_parent_ignores_unrelated_delegation_route(parent):
    role = SubagentDefinition("worker", "Work", "Inspect", inherit_parent=True)
    defaults = {"provider": "openai", "base_url": "https://unrelated.invalid/v1", "api_key": "other-key",
                "api_mode": "anthropic_messages", "request_overrides": {"unrelated": True},
                "reasoning_effort": "low", "command": "missing-other-transport"}
    credentials, _reasoning = resolve_named_credentials(role, defaults, parent)
    runtime = delegate_tool._resolve_child_runtime(
        parent, defaults, parent.api_key, model=credentials["model"],
        **{key: value for key, value in delegate_tool._creds_overrides(credentials).items()
           if key != "override_request_overrides"},
    )
    assert (runtime["provider"], runtime["base_url"], runtime["api_key"], runtime["model"], runtime["api_mode"]) == (
        parent.provider, parent.base_url, parent.api_key, parent.model, parent.api_mode)
    assert credentials["request_overrides"] == parent.request_overrides


@pytest.mark.parametrize("reasoning", [{"enabled": True, "effort": "low"}, {"enabled": False}, None])
@pytest.mark.parametrize("resumed", [False, True])
def test_named_child_installs_resolved_reasoning_before_pinning(parent, monkeypatch, reasoning, resumed):
    parent.provider = "openai-codex"
    parent.model = "gpt-5.6-luna"
    parent.base_url = "https://chatgpt.com/backend-api/codex"
    parent.api_mode = "codex_responses"
    parent.request_overrides = {}
    role = SubagentDefinition("worker", "Work", "Inspect", provider=parent.provider, model=parent.model)
    monkeypatch.setattr(delegate_tool, "_get_orchestrator_enabled", lambda: False)
    monkeypatch.setattr(delegate_tool, "_resolve_child_toolsets", lambda *a, **k: ([], []))
    monkeypatch.setattr(delegate_tool, "_build_child_system_prompt", lambda *a, **k: "Instructions")
    monkeypatch.setattr(delegate_tool, "_build_child_progress_callback", lambda *a, **k: None)
    monkeypatch.setattr("tools.custom_subagents.inherited_credential_pool", lambda *a, **k: None)
    monkeypatch.setattr("hermes_cli.lifecycle.invoke_hook", lambda *a, **k: None)
    monkeypatch.setattr("run_agent.AIAgent", lambda **kwargs: SimpleNamespace(
        **kwargs, tools=[], valid_tool_names=set(), _session_init_model_config={}))
    child = delegate_tool._build_child_agent(
        0, "Inspect source", None, [], parent.model, 1, 1, parent,
        subagent_definition=role, resolved_reasoning=reasoning,
        resume_session_id="resumed-child" if resumed else None,
        resume_launch_metadata={"version": 1} if resumed else None,
    )
    assert child.reasoning_config == reasoning
    assert child.reasoning_config is not reasoning or reasoning is None
    pin = child._delegation_runtime_pin
    request = {"model": child.model, "reasoning": {"effort": pin.reasoning_effort}}
    pin.validate_request(child, request, client=SimpleNamespace(api_key=child.api_key, base_url=child.base_url))
    assert parent.reasoning_config["effort"] == "high"


@pytest.mark.parametrize("failure", ["malformed", "value", "timeout", "live-log", "metadata"])
def test_pre_admission_failure_preserves_resume_grant(tmp_path, monkeypatch, parent, local_dispatch, failure):
    from hermes_state import SessionDB

    db = SessionDB(tmp_path / "resume.db")
    db.create_session("child", source="tool", model_config={"_delegation_completed": True})
    parent._session_db = db
    credentials = delegate_tool._resolve_delegation_credentials({}, parent)
    launch = ResolvedSubagentLaunch(None, credentials, None, resume_session_id="child")
    monkeypatch.setattr(delegate_tool, "_resolve_resume_launch", lambda *args, defaults=None: launch)
    monkeypatch.setattr(delegate_tool, "_effective_task_labels", lambda *args: (["Continue task"], None))
    task = {"goal": "Continue the previous inspection task", "task_label": "Continue task", "resume_session_id": "child"}
    if failure in {"malformed", "value", "timeout"}:
        task["replaces"] = {"bad": "shape"} if failure == "malformed" else {"parent_task_id": "p", "thread_ref": "t"}
        exception = TimeoutError if failure == "timeout" else ValueError

        def refuse(*args):
            raise exception("replacement refused")

        monkeypatch.setattr(delegate_tool, "_card_handling", refuse)
        result = json.loads(delegate_tool.delegate_task(tasks=[task], parent_agent=parent))
        assert result.get("error")
    else:
        def refuse(*args, **kwargs):
            raise RuntimeError("before admission")

        if failure == "live-log":
            monkeypatch.setattr("tools.delegation_live_log.create_live_transcripts", refuse)
        else:
            child = SimpleNamespace(_progress_identity_ref={}, _delegate_role="leaf")
            monkeypatch.setattr(delegate_tool, "_build_children", lambda *a, **k: ([(0, task, child)], None))
            monkeypatch.setattr(delegate_tool, "_Batch", refuse)
        with pytest.raises(RuntimeError, match="before admission"):
            delegate_tool.delegate_task(tasks=[task], parent_agent=parent)
    config = json.loads(db.get_session("child")["model_config"])
    assert config["_delegation_completed"] is True
    assert "_delegation_resume_claimed_at" not in config
    assert db.claim_delegated_resumes(["child"], claim_id="corrected-retry") is True
    db.close()


@pytest.mark.parametrize("kind,expected", [("native_review_result_v1", True), ("ordinary", False)])
def test_public_delegation_sets_native_review_card_identity(parent, local_dispatch, monkeypatch, kind, expected):
    child = SimpleNamespace(_progress_identity_ref={}, _delegate_role="leaf")
    monkeypatch.setattr(delegate_tool, "_build_children", lambda *a, **k: ([(0, {}, child)], None))
    from agent.review_candidate import ReviewCandidateV1, native_review_completion_contract

    candidate = ReviewCandidateV1("/repo", "base", "head", ("file.py",), "patch", (), "candidate")
    contract = native_review_completion_contract(candidate) if expected else {"kind": kind}
    delegate_tool.delegate_task(tasks=[{"goal": "Review source changes", "task_label": "Review source"}],
                                parent_agent=parent, completion_contract=contract)
    assert child._progress_identity_ref["native_review"] is expected
