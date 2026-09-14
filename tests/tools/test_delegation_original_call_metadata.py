import copy
from types import SimpleNamespace

from tools import async_delegation as ad
from tools.delegate_tool_dispatch import _Batch, _units_of
from tools.delegate_tool_progress import _ChildProgressRelay


def test_fresh_calls_and_reused_parent_get_distinct_complete_original_manifests(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    owner = {"session_id": "parent", "session_key": "route"}
    first = ad.reserve_delegation_metadata(
        parent_task_id=None, owner=owner, task_labels=["one", "two"]
    )
    second = ad.reserve_delegation_metadata(
        parent_task_id=first["parent_task_id"], owner=owner, task_labels=["three"]
    )

    first_call = next(iter(first["original_calls"].values()))
    second_call = next(iter(second["original_calls"].values()))
    assert first_call["id"] != second_call["id"]
    assert first_call["parent_task_id"] == second_call["parent_task_id"] == first["parent_task_id"]
    assert first_call["member_refs"] == first["thread_refs"]
    assert second_call["member_refs"] == second["thread_refs"]


def test_resume_deduplicates_known_original_calls_and_keeps_full_roster(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    owner = {"session_id": "parent", "session_key": "route"}
    seeded = ad.reserve_delegation_metadata(parent_task_id=None, owner=owner, task_labels=["a"])
    parent_task_id = seeded["parent_task_id"]
    original_a = {"id": "call-a", "parent_task_id": parent_task_id, "member_refs": ["A", "B", "C"]}
    original_b = {"id": "call-b", "parent_task_id": parent_task_id, "member_refs": ["D", "E"]}
    result = ad.reserve_delegation_metadata(
        parent_task_id=parent_task_id,
        owner=owner,
        task_labels=["resume C", "resume D"],
        resume_refs=["C", "D"],
        resume_original_calls=[original_a, original_b],
    )
    assert result["thread_refs"] == ["C", "D"]
    assert result["original_calls"] == {"call-a": original_a, "call-b": original_b}
    original_a["member_refs"].append("MUTATED")
    assert result["original_calls"]["call-a"]["member_refs"] == ["A", "B", "C"]


def test_completion_units_narrow_threads_but_preserve_full_original_calls():
    descriptor = {"id": "call-a", "parent_task_id": "p" * 32, "member_refs": ["A", "B"]}
    metadata = {
        "parent_task_id": "p" * 32,
        "threads": [
            {"thread_ref": "A", "task_index": 0, "task_label": "a"},
            {"thread_ref": "B", "task_index": 1, "task_label": "b"},
        ],
        "original_calls": {"call-a": descriptor},
    }
    narrowed = ad._completion_metadata_fields(metadata, [{"task_index": 1}])
    assert [t["thread_ref"] for t in narrowed["delegation_metadata"]["threads"]] == ["B"]
    assert narrowed["delegation_metadata"]["original_calls"] == {"call-a": descriptor}
    assert metadata["threads"][0]["thread_ref"] == "A"



def test_partitioned_completion_units_keep_the_full_original_manifest(monkeypatch):
    monkeypatch.setattr("tools.delegate_tool_config._get_independent_completions", lambda: True)
    descriptor = {"id": "call-a", "parent_task_id": "p" * 32, "member_refs": ["A", "B", "C"]}
    batch = _Batch(
        task_list=[{"goal": "a"}, {"goal": "b", "group": "g"}, {"goal": "c", "group": "g"}],
        children=[(0, {"goal": "a"}, SimpleNamespace()),
                  (1, {"goal": "b", "group": "g"}, SimpleNamespace()),
                  (2, {"goal": "c", "group": "g"}, SimpleNamespace())],
        parent_agent=None, creds={}, context=None, top_role="leaf", max_children=3,
        live_deleg_id="deleg", live_writers=[], live_paths=[], origin_wake_sid="",
        origin_ui_session_id="", origin_owner_transport=None, origin_owner_session_record=None,
        origin_session_history_delivery=False, overall_start=0.0,
        delegation_metadata={"original_calls": {"call-a": descriptor}, "threads": [
            {"thread_ref": "A", "task_index": 0}, {"thread_ref": "B", "task_index": 1},
            {"thread_ref": "C", "task_index": 2},
        ]},
    )
    units = _units_of(batch)
    assert [u.group for u in units] == [None, "g"]
    assert all(u.delegation_metadata["original_calls"] == {"call-a": descriptor} for u in units)


def test_child_progress_relay_explicitly_passes_original_call_and_nested_identity():
    descriptor = {"id": "call-a", "parent_task_id": "p" * 32, "member_refs": ["A", "B"]}
    ref = {"original_call": copy.deepcopy(descriptor), "session_id": "child"}
    seen = []
    relay = _ChildProgressRelay(
        0, "goal", None, lambda *args, **kwargs: seen.append(kwargs), 1,
        "sa-0", None, 0, "model", ["terminal"], ref,
    )
    assert relay._identity_kwargs()["original_call"] == descriptor
    relay("subagent.admitted")
    assert seen[0]["original_call"] == descriptor
    assert seen[0]["original_call"] is not descriptor


def test_registry_dispatch_stamps_complete_birth_roster_before_any_unit_runs(monkeypatch):
    import json
    from tools import delegate_tool
    from tools.registry import registry
    from tests.tools.test_delegate_required_labels import _Parent, _valid_runtime

    _valid_runtime(monkeypatch)
    monkeypatch.setattr(delegate_tool, "_capture_origin", lambda: (None, None, None, None, False))
    launch = SimpleNamespace(definition=None, credentials={}, reasoning=None,
                             resume_session_id=None, resume_claim_id=None, launch_metadata=None)
    monkeypatch.setattr(delegate_tool, "_preflight_task_runtime", lambda *_: ([launch, launch], None))
    children = []

    def build(tasks, *_args, **_kwargs):
        for _ in tasks:
            children.append(SimpleNamespace(_delegate_role="leaf", _progress_identity_ref={},
                                            _delegation_launch_metadata={}))
        return [(i, task, children[i]) for i, task in enumerate(tasks)], None

    def dispatch(batch, _background):
        descriptor = next(iter(batch.delegation_metadata["original_calls"].values()))
        assert descriptor["member_refs"] == batch.delegation_metadata["thread_refs"]
        assert len(descriptor["member_refs"]) == len(children)
        for i, child in enumerate(children):
            identity = child._progress_identity_ref
            assert identity["original_call"] == descriptor
            assert identity["original_call"] is not descriptor
            assert child._delegation_launch_metadata["card_identity"]["original_call"] == descriptor
            assert batch.delegation_metadata["threads"][i]["original_call_id"] == descriptor["id"]
        children[0]._progress_identity_ref["original_call"]["member_refs"].clear()
        assert children[1]._progress_identity_ref["original_call"]["member_refs"] == descriptor["member_refs"]
        return json.dumps({"status": "verified"})

    monkeypatch.setattr(delegate_tool, "_build_children", build)
    monkeypatch.setattr(delegate_tool, "_run_batch", dispatch)
    result = json.loads(registry.dispatch("delegate_task", {
        "tasks": [{"goal": "Inspect first fixture", "task_label": "Check first"},
                  {"goal": "Inspect second fixture", "task_label": "Check second"}],
    }, parent_agent=_Parent()))
    assert result == {"status": "verified"}
