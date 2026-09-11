"""Required display labels are enforced before delegation can reserve or spawn."""

import json
from types import SimpleNamespace
from unittest.mock import Mock

from tools import delegate_tool
from tools.registry import registry


class _Parent:
    _delegate_depth = 0
    session_id = "parent-session"


def _valid_runtime(monkeypatch):
    creds = {"provider": None, "model": None, "base_url": None, "api_key": None, "api_mode": None}
    launch = SimpleNamespace(definition=None, credentials=creds, reasoning=None)
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


def test_model_schema_requires_per_task_label_and_keeps_guidance_unbounded():
    schema = registry.get_definitions({"delegate_task"}, quiet=True)[0]["function"]["parameters"]
    item = schema["properties"]["tasks"]["items"]
    label = item["properties"]["task_label"]

    assert set(item["required"]) == {"goal"}
    import jsonschema
    for payload in (
        {"task_label": "Check receipt", "tasks": [{"goal": "Inspect receipt"}]},
        {"tasks": [{"goal": "Inspect", "task_label": "Check receipt"}]},
        {"tasks": [{"goal": "Continue", "resume_session_id": "child"}]},
        {"action": "list"},
    ):
        jsonschema.validate(payload, schema)
    assert "AUTHORING GUIDANCE" in label["description"]
    assert "24-character total task-card row" in label["description"]
    assert "maxLength" not in label


def test_model_dispatch_rejects_missing_or_blank_labels_before_any_spawn_side_effect(monkeypatch):
    parent = _Parent()
    reserve = Mock()
    resolve = Mock()
    monkeypatch.setattr(delegate_tool, "_load_config", lambda: {})
    monkeypatch.setattr(delegate_tool, "last_delegation_config_error", lambda: None)
    monkeypatch.setattr(delegate_tool, "_resolve_delegation_credentials", resolve)
    monkeypatch.setattr("tools.async_delegation.reserve_delegation_metadata", reserve)

    for label in (None, "   "):
        task = {"goal": "Inspect the test fixture"}
        if label is not None:
            task["task_label"] = label
        raw = registry.dispatch("delegate_task", {"tasks": [task]}, parent_agent=parent)
        assert isinstance(raw, str)
        payload = json.loads(raw)
        assert "nonempty" in payload["error"]
        assert "verb-first" in payload["error"]

    assert not resolve.called
    assert not reserve.called


def test_model_dispatch_valid_batch_uses_full_labels_and_top_level_fallback(monkeypatch):
    _valid_runtime(monkeypatch)
    captured = {}

    def finish(batch, _background):
        captured["metadata"] = batch.delegation_metadata
        return json.dumps({"status": "dispatched"})

    monkeypatch.setattr(delegate_tool, "_run_batch", finish)
    monkeypatch.setattr(delegate_tool, "_Batch", lambda *args, **kwargs: SimpleNamespace(
        delegation_metadata=kwargs["delegation_metadata"]))
    raw = registry.dispatch("delegate_task", {
        "tasks": [{"goal": "Inspect the test fixture", "task_label": "Inspect every fixture thoroughly beyond display guidance"}],
    }, parent_agent=_Parent())
    assert isinstance(raw, str)
    payload = json.loads(raw)
    assert payload["status"] == "dispatched"
    assert captured["metadata"]["task_labels"] == ["Inspect every fixture thoroughly beyond display guidance"]

    raw = registry.dispatch("delegate_task", {
        "task_label": "Check receipt", "tasks": [{"goal": "Use legacy fallback"}],
    }, parent_agent=_Parent())
    assert json.loads(raw)["status"] == "dispatched"
    assert captured["metadata"]["task_labels"] == ["Check receipt"]
    labels, error = delegate_tool._effective_task_labels(
        [{"goal": "Inspect", "task_label": " "}], "Check receipt", _Parent())
    assert labels is None and error


def test_resume_reuses_only_a_preserved_nonempty_historical_label():
    row = {"model_config": {"_delegation_launch": {"task_label": "Continue review"}}}
    db = SimpleNamespace(resolve_resume_session_id=lambda _sid: "child", get_session=lambda _sid: row)
    parent = SimpleNamespace(_session_db=db)

    labels, error = delegate_tool._effective_task_labels(
        [{"goal": "Continue the captured review", "resume_session_id": "child"}], None, parent)
    assert error is None
    assert labels == ["Continue review"]

    row["model_config"]["_delegation_launch"].pop("task_label")
    labels, error = delegate_tool._effective_task_labels(
        [{"goal": "Continue the captured review", "resume_session_id": "child"}], None, parent)
    assert labels is None
    assert error is not None and "task_label" in error
