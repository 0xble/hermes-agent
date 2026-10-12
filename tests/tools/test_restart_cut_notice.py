from tools.process_registry_notifications import _format_async_delegation


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
