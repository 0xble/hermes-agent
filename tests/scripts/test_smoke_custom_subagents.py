"""Public result-identity contracts used by the custom-subagent smoke."""

import pytest

from scripts.smoke_custom_subagents import _result_for_task


def test_result_join_uses_emitted_task_index_not_unpublished_role():
    reader = _result_for_task([
        {"task_index": 1, "summary": "worker"},
        {"task_index": 0, "summary": "reader"},
    ], 0, "reader")
    assert reader["summary"] == "reader"


@pytest.mark.parametrize("results, match", [
    ([{"task_index": 1}], "identity missing"),
    ([{"task_index": 0}, {"task_index": 0}], "identity duplicate"),
])
def test_result_join_rejects_missing_or_duplicate_task_identity(results, match):
    with pytest.raises(RuntimeError, match=match):
        _result_for_task(results, 0, "reader")
