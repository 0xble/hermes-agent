"""Depth-aware display-label admission and authoring guidance."""

import json
from types import SimpleNamespace
from unittest.mock import Mock

from agent.delegation_labels import task_label_limit_for_depth, runtime_parent_spawn_depth
from tools import delegate_tool
from tools.registry import registry


class _Parent:
    _delegate_depth = 0
    session_id = "parent-session"


def test_depth_policy_uses_runtime_spawn_depth_and_floors_at_twelve():
    assert task_label_limit_for_depth(0) == 24
    assert task_label_limit_for_depth(1) == 20
    assert task_label_limit_for_depth(2) == 16
    assert task_label_limit_for_depth(3) == 12
    assert task_label_limit_for_depth(99) == 12


def test_runtime_spawn_depth_is_not_model_supplied():
    parent = SimpleNamespace(_delegate_depth=2)
    assert runtime_parent_spawn_depth(parent) == 2


def test_registry_dispatch_rejects_depth_overlong_label_before_admission_side_effects(monkeypatch):
    monkeypatch.setattr(delegate_tool, "_get_max_spawn_depth", lambda: 5)
    parent = SimpleNamespace(_delegate_depth=2, session_id="parent-session")
    resolve = Mock()
    reserve = Mock()
    monkeypatch.setattr(delegate_tool, "_resolve_delegation_credentials", resolve)
    monkeypatch.setattr("tools.async_delegation.reserve_delegation_metadata", reserve)

    raw = registry.dispatch(
        "delegate_task",
        {"tasks": [{"goal": "Inspect fixture", "task_label": "x" * 17}]},
        parent_agent=parent,
    )
    payload = json.loads(raw)
    assert "maximum is 16" in payload["error"]
    assert "No child was started" in payload["error"]
    assert not resolve.called
    assert not reserve.called


def test_registry_dispatch_accepts_exact_depth_limit_without_truncation(monkeypatch):
    monkeypatch.setattr(delegate_tool, "_get_max_spawn_depth", lambda: 5)
    parent = SimpleNamespace(_delegate_depth=2, session_id="parent-session")
    creds = {"provider": None, "model": None, "base_url": None, "api_key": None, "api_mode": None}
    launch = SimpleNamespace(
        definition=None, credentials=creds, reasoning=None,
        resume_session_id=None, resume_claim_id=None, launch_metadata=None,
    )
    monkeypatch.setattr(delegate_tool, "_load_config", lambda: {})
    monkeypatch.setattr(delegate_tool, "last_delegation_config_error", lambda: None)
    monkeypatch.setattr(delegate_tool, "_resolve_delegation_credentials", lambda *_: creds)
    monkeypatch.setattr(delegate_tool, "_preflight_task_runtime", lambda *_: ([launch], None))
    monkeypatch.setattr("tools.delegation_live_log.create_live_transcripts", lambda *_args, **_kw: (None, [], []))
    monkeypatch.setattr(delegate_tool, "_announce_batch", lambda *_: None)
    monkeypatch.setattr(delegate_tool, "_capture_origin", lambda: (None, None, None, None))
    monkeypatch.setattr(
        delegate_tool, "_build_children",
        lambda task_list, *_args, **_kw: ([(0, task_list[0], SimpleNamespace(_delegate_role=None))], None),
    )
    captured = {}
    def finish(batch, _background):
        captured["batch"] = batch
        return json.dumps({"status": "dispatched"})
    monkeypatch.setattr(delegate_tool, "_run_batch", finish)
    monkeypatch.setattr(delegate_tool, "_Batch", lambda *args, **kwargs: SimpleNamespace(delegation_metadata=kwargs["delegation_metadata"]))

    label = "🧪" * 16
    raw = registry.dispatch(
        "delegate_task",
        {"tasks": [{"goal": "Inspect fixture", "task_label": label}]},
        parent_agent=parent,
    )
    assert json.loads(raw)["status"] == "dispatched"
    assert captured["batch"].delegation_metadata["task_labels"] == [label]


def test_historical_resume_label_is_preserved_even_when_long(monkeypatch):
    historical = "Historical label intentionally longer than twenty four"
    row = {"model_config": {"_delegation_launch": {"task_label": historical}}}
    db = SimpleNamespace(resolve_resume_session_id=lambda _sid: "child", get_session=lambda _sid: row)
    parent = SimpleNamespace(_delegate_depth=3, _session_db=db)

    labels, error = delegate_tool._effective_task_labels(
        [{"goal": "Continue", "resume_session_id": "child"}], None, parent,
    )
    assert error is None
    assert labels == [historical]
    assert row["model_config"]["_delegation_launch"]["task_label"] == historical


def test_schema_keeps_static_upper_bound_and_explains_depth_policy():
    schema = registry.get_definitions({"delegate_task"}, quiet=True)[0]["function"]["parameters"]
    item = schema["properties"]["tasks"]["items"]["properties"]["task_label"]
    assert item["maxLength"] == 24
    assert "depth" in item["description"]
    assert "never silently truncated" in item["description"]

    # This guards the admission contract independently of schema caching.
    labels, error = delegate_tool.admit_task_labels(
        [{"goal": "x", "task_label": "x" * 13}], None, SimpleNamespace(_delegate_depth=3),
    )
    assert labels is None
    assert error is not None and "maximum is 12" in error
