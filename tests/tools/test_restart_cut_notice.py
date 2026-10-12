import pytest

from tools.process_registry_notifications import _format_async_delegation


@pytest.mark.parametrize("goals", [None, ["recover this task"]])
def test_scalar_cut_notice_uses_task_wording(goals):
    text = _format_async_delegation({
        "type": "async_delegation", "delegation_id": "deleg_scalar_notice",
        "goal": "recover this task", "status": "interrupted", "is_batch": bool(goals), "goals": goals,
        "completed_at": 1_760_000_000.0, "cut_reason": "gateway shutdown (restart)",
    })
    assert "The task did not finish." in text
    assert "Unfinished children:" not in text
    assert "Do not rerun completed children." not in text
    assert "delegate_task(action='resume', subagent_id='deleg_scalar_notice')" in text


def test_cut_batch_notice_has_fixed_resume_block():
    text = _format_async_delegation({
        "type": "async_delegation",
        "delegation_id": "deleg_cut_notice",
        "is_batch": True,
        "goal": "3 parallel subagents",
        "goals": ["first", "second", "third"],
        "results": [
            {"task_index": 0, "status": "completed", "summary": "done"},
            {"task_index": 1, "status": "interrupted", "error": "gateway restart"},
            {"task_index": 2, "status": "unknown", "error": "cut"},
        ],
        "status": "completed",
        "cut_reason": "gateway shutdown (restart)",
        "completed_at": 1_760_000_000.0,
        "total_duration_seconds": 10,
    })
    assert "Cut by gateway restart" in text
    assert "Unfinished children: 2, 3" in text
    assert "delegate_task(action='resume', subagent_id='deleg_cut_notice')" in text
    assert "Do not rerun completed children." in text


@pytest.mark.parametrize("indexes, expected", [(None, "2, 3"), ([0, 2], "3")])
def test_cut_notice_includes_missing_children_but_not_other_units(indexes, expected):
    evt = {
        "type": "async_delegation", "delegation_id": "deleg_cut_missing", "is_batch": True,
        "goals": ["first", "second", "third"],
        "results": [{"task_index": 0, "status": "completed", "summary": "done"}],
        "cut_reason": "gateway shutdown (restart)", "completed_at": 1_760_000_000.0,
    }
    if indexes is not None:
        evt["task_indexes"] = indexes
    assert f"Unfinished children: {expected}." in _format_async_delegation(evt)


def test_live_cut_completion_preserves_unit_indexes_for_notice_and_resume(tmp_path, monkeypatch):
    import json
    import queue
    import threading
    from tools import async_delegation as ad
    from tools.delegate_tool_registry import _handle_control_action
    from tools.process_registry import process_registry

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    q = queue.Queue()
    monkeypatch.setattr(process_registry, "completion_queue", q)
    ad._reset_for_tests()
    started, release = threading.Event(), threading.Event()

    def runner():
        started.set()
        assert release.wait(5)
        return {"results": [{"task_index": 0, "status": "completed", "summary": "done"}]}

    try:
        handle = ad.dispatch_async_delegation_batch(
            goals=["first", "second", "third"], task_indexes=[0, 2],
            context="ctx", toolsets=None, role="leaf", model="m", session_key="session",
            parent_session_id="owner", runner=runner, interrupt_fn=lambda reason=None: release.set(),
            max_async_children=1,
        )
        assert started.wait(5)
        assert ad.interrupt_all("gateway shutdown (restart)") == 1
        event = q.get(timeout=5)
        assert event["task_indexes"] == [0, 2]
        assert "Unfinished children: 3." in _format_async_delegation(event)
        parent = type("Parent", (), {"session_id": "owner", "_session_db": None})()
        resume = json.loads(_handle_control_action("resume", handle["delegation_id"], None, parent))
        assert [task["goal"] for task in resume["tasks"]] == ["third"]
    finally:
        release.set()
        ad._reset_for_tests()
