from __future__ import annotations

import importlib.util
from pathlib import Path


SPEC = importlib.util.spec_from_file_location(
    "tool_choice_eval", Path(__file__).parent / "eval.py"
)
assert SPEC and SPEC.loader
EVAL = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(EVAL)


def test_typed_session_lookup_passes_without_raw_state_access():
    result = EVAL.score_trial(
        [{"name": "session_search", "args": {"lookup": {"title": "Everguide Review"}}}],
        [],
        "topic_name",
    )
    assert result["passed"] is True


def test_tool_search_then_typed_status_passes():
    result = EVAL.score_trial(
        [{"name": "mcp__relay__status", "args": {"receipt": "r-eval-delivery-7", "wait": "30s"}}],
        [{"name": "tool_search", "args": {"query": "relay receipt delivery wait"}}],
        "receipt_wait",
    )
    assert result["passed"] is True


def test_sqlite_state_db_is_always_a_failure():
    result = EVAL.score_trial(
        [{"name": "session_search", "args": {}}],
        [{"name": "terminal", "args": {"command": "sqlite3 ~/.hermes/state.db 'select * from sessions'"}}],
        "topic_name",
    )
    assert result["passed"] is False
    assert result["raw_state_db_attempt"] is True


def test_missing_typed_lookup_fails():
    result = EVAL.score_trial([], [{"name": "terminal", "args": {"command": "echo give up"}}], "title_relay")
    assert result["passed"] is False
    assert result["first_state_tool"] is None
