"""Behavioral witness fixtures are deliberately fake, not native proof."""
from copy import deepcopy
import json
from pathlib import Path

import pytest

from tests.drills.zero_cut_handover import native_witnesses as w

TOKEN = "abc123"
HOME = Path("/isolated/home")
OWNER = {"pid": 10, "start_time": 1, "kernel_start_time": 1, "code_sha": "a" * 40, "standby": False,
         "supervisor": "launchd", "hermes_home": str(HOME), "status": {"ready": True}, "manifest_sha": "a" * 40}
SUCCESSOR = dict(OWNER, pid=20, code_sha="b" * 40, manifest_sha="b" * 40)


def fixture(final=False):
    rows, events = [], {}
    for kind in w.KINDS:
        call = {"id": kind, "function": {"name": "terminal", "arguments": json.dumps({"command": f"worker {TOKEN} --kind {kind}"})}}
        rows.append({"role": "assistant", "session_id": kind, "tool_calls": json.dumps([call])})
        event = {"token": TOKEN, "kind": kind, "phase": "start", "pid": 100,
                 "created": 9, "monotonic": 1, "at": 1, "seconds": 150,
                 "ancestors": [{"pid": 20 if kind == "admission" else 10, "created": 0.01}]}
        events[kind] = [event] if final or kind in {"long", "background", "delegation"} else []
        if final:
            rows.append({"role": "tool", "tool_call_id": kind, "content": f"EFFECT {TOKEN} {kind} proc_bg"})
            rows.append({"role": "assistant", "content": f"RESULT {TOKEN} {kind}"})
            events[kind].append(dict(event, phase="finish", monotonic=152, at=152))
            if kind == "followup":
                events[kind][0] = dict(event, at=153)
                events[kind][1]["at"] = 154
    chat = [{"id": 1000 + i, "sender_id": 8899335561, "text": f"RESULT {TOKEN} {k}"} for i, k in enumerate(w.KINDS)] if final else [
        {"id": 88, "sender_id": 8899335561, "text": f"approval {TOKEN}", "reply_markup": {"buttons": ["approve"]}}]
    meta = [{"key": p + "long", "value": json.dumps({"goal": TOKEN, "status": "done" if final else "active", "ticks_fired": 1 if final else 0})} for p in ("goal:", "loop:")]
    return {"events": events, "tables": {"messages": rows,
        "state_meta": meta, "async_delegations": [{"task_json": TOKEN, "state": "completed" if final else "running", "owner_pid": 10, "owner_started_at": 1,
        "delivery_state": "delivered", "delivery_attempts": 1}], "delivery_obligations": [{"content": "\n".join(f"RESULT {TOKEN} {kind}" for kind in w.KINDS if kind != "cron"), "state": "delivered", "attempts": 0}]},
        "transcripts": deepcopy(rows), "chat": chat, "living": {"100": 9},
        "process_results": [{"id": "proc_bg", "exit_code": 0, "output": f"EFFECT {TOKEN} background"}] if final else [],
        "executions": [{"status": "completed", "code_sha": SUCCESSOR["code_sha"], "delivery_status": "delivered", "output": TOKEN}] if final else [],
        "processes": [{"command": f"worker {TOKEN} --kind background", "registry_owner_pid": 10, "registry_owner_started_at": 1}],
        "cron": {"jobs": [{"prompt": TOKEN, "enabled": True}]}, "owners": [OWNER]}


def test_real_mixed_witnesses_required_together():
    snap = fixture()
    assert all(w.armed(snap, TOKEN, OWNER).values())
    snap["events"]["long"][0]["seconds"] = 45
    assert not w.armed(snap, TOKEN, OWNER)["tool150"]


@pytest.mark.parametrize("category", ["events", "tables", "processes", "cron", "chat"])
def test_prompt_alone_never_arms_work(category):
    snap = fixture()
    snap[category] = {} if category in {"events", "tables", "cron"} else []
    assert not all(w.armed(snap, TOKEN, OWNER).values())


def test_completed_or_dead_tool_not_inflight():
    snap = fixture(True)
    assert not w.event_pair(snap, TOKEN, "long")
    snap = fixture()
    snap["living"] = {}
    assert not w.event_pair(snap, TOKEN, "long")


def test_fake_fixture_can_exercise_complete_oracle_but_not_native_acceptance():
    snap = fixture(True)
    assert all(w.completed(snap, TOKEN, OWNER, SUCCESSOR, []).values())


@pytest.mark.parametrize("change", ["effect", "tool_result", "telegram", "db_final", "delivery", "cut", "owner", "duration"])
def test_each_duplicate_loss_or_cut_is_nonpass(change):
    snap = fixture(True)
    if change == "effect":
        snap["events"]["long"].append(snap["events"]["long"][1])
    elif change == "tool_result":
        snap["tables"]["messages"].append({"role": "tool", "tool_call_id": "long", "content": "duplicate"})
    elif change == "telegram":
        snap["chat"].append(snap["chat"][0])
    elif change == "db_final":
        snap["tables"]["messages"].append({"role": "assistant", "content": f"RESULT {TOKEN} long"})
    elif change == "delivery":
        snap["tables"]["delivery_obligations"][0]["attempts"] = 2
    elif change == "cut":
        snap["tables"]["messages"].append({"role": "assistant", "content": f"{TOKEN} interrupted by provider error"})
    elif change == "owner":
        snap["events"]["admission"][0]["ancestors"] = [{"pid": 10}]
    else:
        snap["events"]["long"][1]["monotonic"] = 46
    assert not all(w.completed(snap, TOKEN, OWNER, SUCCESSOR, []).values())


def test_wrong_sha_home_or_two_serving_owners_fails():
    snap = fixture()
    assert w.active_owner(snap, OWNER["code_sha"], HOME) == OWNER
    with pytest.raises(ValueError):
        w.active_owner(snap, "b" * 40, HOME)
    with pytest.raises(ValueError):
        w.active_owner(snap, OWNER["code_sha"], Path("/other"))
    snap["owners"].append(SUCCESSOR)
    with pytest.raises(ValueError):
        w.active_owner(snap, OWNER["code_sha"], HOME)


def test_unrelated_goal_and_loop_do_not_prove_work_session_state():
    snap = fixture()
    for row in snap["tables"]["state_meta"]:
        row["key"] = row["key"].split(":")[0] + ":unrelated-session"
    checks = w.armed(snap, TOKEN, OWNER)
    assert not checks["goal"] and not checks["loop"]


def test_missing_per_result_delivery_obligation_is_not_complete():
    snap = fixture(True)
    snap["tables"]["delivery_obligations"] = [{"content": f"RESULT {TOKEN} long", "state": "delivered", "attempts": 0}]
    assert not all(w.completed(snap, TOKEN, OWNER, SUCCESSOR, []).values())


@pytest.mark.parametrize("payload", [{"success": False, "routed": False}, {"success": True}, "ACK"])
def test_restart_tool_call_alone_is_not_route_success(payload):
    snap = {"tables": {"messages": [
        {"role": "assistant", "tool_calls": [{"id": "restart", "function": {"name": "request_restart", "arguments": TOKEN}}]},
        {"role": "tool", "tool_call_id": "restart", "content": json.dumps(payload)}]}}
    assert not w.restart_tool_executed(snap, TOKEN)
    snap["tables"]["messages"][1]["content"] = json.dumps({"success": True, "routed": True})
    assert w.restart_tool_executed(snap, TOKEN)
    snap["tables"]["messages"].append(snap["tables"]["messages"][1])
    assert not w.restart_tool_executed(snap, TOKEN)
