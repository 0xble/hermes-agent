"""Fakes exercise the acceptance oracle, never native continuity."""
from copy import deepcopy
from pathlib import Path

import pytest

from tests.drills.zero_cut_handover import native


A = "a" * 40
B = "b" * 40


def plan(tmp_path):
    return {"source": str(tmp_path / "source"), "reservation": "parent-owned",
            "passes": [{"route": "update", "before": A, "after": B, "branch": "qualified"},
                       {"route": "request_restart", "before": A, "after": A},
                       {"route": "slash_restart", "before": A, "after": A}],
            "topics": {"work": 1, "approval": 2, "control": 3, "admission": 4}}


def test_requires_three_route_scenarios_and_real_forward_revisions(tmp_path):
    value = plan(tmp_path)
    with pytest.raises(ValueError, match="continuity"):
        native.validate_plan(value)
    value["passes"][1].update(before=B, after=B)
    value["passes"][2].update(before=B, after=B)
    native.validate_plan(value)
    value["passes"][2]["after"] = "HEAD"
    with pytest.raises(ValueError, match="SHA"):
        native.validate_plan(value)


@pytest.mark.parametrize("route", ["update", "request_restart", "slash_restart"])
def test_routes_use_ordinary_entry_points_not_handover(tmp_path, route):
    calls = []
    fx = type("FX", (), {"run_command": staticmethod(lambda argv, **kw: calls.append((argv, kw))),
                         "send_as_user": staticmethod(lambda peer, text: (calls.append((peer, text)), {"id": 1})[1])})()
    native.trigger(route, fx, "peer", tmp_path / "source", "branch", "run-token")
    assert calls
    assert not any(isinstance(c[0], list) and "handover" in c[0] for c in calls)
    if route == "update":
        assert calls[0][0][-4:] == ["update", "--yes", "--branch", "branch"]
        assert any(str(tmp_path / "source") in arg for arg in calls[0][0])
    elif route == "slash_restart":
        assert calls[0][1] == "/restart"
    else:
        assert "request_restart" in calls[0][1]
        assert "built-in /restart" in calls[0][1]


def test_incomplete_and_xfail_are_never_native_passes():
    assert not native.native_pass({"passed": True, "xfail_reason": "expected crash"})
    assert not native.native_pass({"passed": True})
    assert not native.native_pass({"passed": False, "complete": True, "native": True})
    assert native.native_pass({"passed": True, "complete": True, "native": True})


def test_ambiguous_send_not_retried(tmp_path):
    calls = []
    def send(peer, text):
        calls.append(text)
        return {"error": "timeout"}
    with pytest.raises(RuntimeError, match="ambiguous"):
        native.send_once(send, "peer", "text")
    assert calls == ["text"]


def test_consecutive_native_acceptance_does_not_accept_fakes_or_partial():
    result = {"native": True, "complete": True, "passed": True}
    cycles = [dict(result, route=route) for route in ("update", "request_restart", "slash_restart")]
    assert native.accepted(cycles)
    bad = deepcopy(cycles)
    bad[1]["native"] = False
    assert not native.accepted(bad)
    assert not native.accepted(cycles[:2])
    assert not native.accepted(cycles + [dict(result, route="update")])


@pytest.mark.parametrize("missing", ["request_restart", "slash_restart"])
def test_native_suite_rejects_a_missing_restart_route(missing):
    result = {"native": True, "complete": True, "passed": True}
    routes = ["update", "request_restart", "slash_restart"]
    replacement = "slash_restart" if missing == "request_restart" else "request_restart"
    routes[routes.index(missing)] = replacement
    assert not native.accepted([dict(result, route=route) for route in routes])


@pytest.mark.parametrize("missing", ["request_restart", "slash_restart"])
def test_native_plan_requires_both_distinct_restart_routes(tmp_path, missing):
    value = plan(tmp_path)
    value["passes"][1].update(before=B, after=B)
    value["passes"][2].update(before=B, after=B)
    replacement = "slash_restart" if missing == "request_restart" else "request_restart"
    for item in value["passes"]:
        if item["route"] == missing:
            item["route"] = replacement
    with pytest.raises(ValueError, match="routes"):
        native.validate_plan(value)


def test_no_guard_or_preflight_failure_can_launch_route(tmp_path):
    calls = []
    p = plan(tmp_path)
    with pytest.raises(ValueError):
        native.run_native(home=tmp_path, peer="peer", plan=p, output=tmp_path / "out",
                          effects=None, live=False)
    assert not calls
    assert not (tmp_path / "out").exists()
