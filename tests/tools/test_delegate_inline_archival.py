"""Completed inline work survives storage faults without rerunning children."""
import copy
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from tools import async_delegation as ad
from tools import delegate_tool_dispatch as dispatch


@pytest.mark.parametrize("route", ["inline", "no_async", "rejected", "partial"])
@pytest.mark.parametrize("fault", ["archive", "prune", "none"])
def test_completed_inline_result_survives_archival_fault(tmp_path, monkeypatch, route, fault):
    monkeypatch.setattr(ad, "_db_path", lambda: tmp_path / "delegations.db")
    owner = {"session_id": "parent", "profile": "default"}
    metadata = {"owner": owner, "owner_json": json.dumps(owner, sort_keys=True, separators=(",", ":")),
                "parent_task_id": "task", "threads": [{"thread_ref": "A", "task_index": 0}]}
    batch = dispatch._Batch(
        task_list=[{"goal": "Complete work"}], children=[],
        parent_agent=SimpleNamespace(session_id="parent"), creds={}, context=None,
        top_role="leaf", max_children=1, live_deleg_id=None, live_writers=[], live_paths=[],
        origin_wake_sid="", origin_ui_session_id="", origin_owner_transport=None,
        origin_owner_session_record=None, origin_session_history_delivery=False,
        overall_start=0, delegation_metadata=metadata)
    payload = {"results": [{"task_index": 0, "status": "completed", "summary": "finished payload",
                            "child_session_id": "child", "resume_blocked": True}],
               "delegation_metadata": metadata}
    execute = Mock(side_effect=lambda *a, **k: copy.deepcopy(payload))
    monkeypatch.setattr(dispatch, "_execute_and_aggregate", execute)
    monkeypatch.setattr(dispatch, "_resolve_async_wake_sid", lambda *a: None if route == "no_async" else "")
    monkeypatch.setattr(dispatch, "_resolve_async_session_key", lambda *a: ("parent", ""))
    monkeypatch.setattr(dispatch, "_units_of", lambda *a: [copy.copy(batch)] * (2 if route == "partial" else 1))
    admission = Mock(side_effect=([{"status": "dispatched", "delegation_id": "accepted"},
                                 {"status": "rejected"}] if route == "partial" else [{"status": "rejected"}]))
    monkeypatch.setattr(dispatch, "_dispatch_unit", admission)
    def unavailable():
        raise OSError("injected storage fault")
    if fault == "archive":
        monkeypatch.setattr(ad, "_connect", unavailable)
    elif fault == "prune":
        monkeypatch.setattr(ad, "_prune_durable_records", unavailable)

    result = json.loads(dispatch._run_batch(batch, background=route != "inline"))
    execute.assert_called_once()
    entries = result["inline_results" if route == "partial" else "results"]
    entry = entries[0]
    assert all(entry[key] == value for key, value in payload["results"][0].items())
    if fault == "archive":
        assert entry["result_archival"]["status"] == "failed"
        assert entry["result_archival"]["handle_available"] is False
        assert "result_delegation_id" not in entry
        if route != "partial":
            assert "delegation_id" not in result
            assert result["result_archival"] == entry["result_archival"]
    else:
        uid = entry["result_delegation_id"]
        if route != "partial":
            assert result["delegation_id"] == uid
        # Read back the real SQLite archive, including when post-commit pruning failed.
        retained = ad.get_delegation_result(uid, owner=owner)
        assert retained is not None and retained["result"] == payload
        assert ad.get_delegation_result(uid, owner={**owner, "session_id": "foreign"}) is None
    if route in {"no_async", "rejected"}:
        assert "SYNCHRONOUSLY" in result["note"]
