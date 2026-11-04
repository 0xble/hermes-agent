"""Lifecycle callers retain authority, pause ownership and transactional quote freshness."""
from __future__ import annotations

import json

import pytest

pytest_plugins = ["tests.hermes_cli.test_session_controls"]


@pytest.mark.parametrize("kind", ["goal", "loop"])
@pytest.mark.parametrize("action", ["revise", "replace"])
def test_unknown_edit_actor_returns_a_structured_refusal(state, kind, action):
    manager = manager_for(kind)
    manager.set("Watch build", actor="agent")
    before = manager.state.to_json()
    kwargs = {"reason": "Clarify wording", "actor": "unknown-actor"}
    kwargs["goal" if kind == "goal" else "prompt"] = "Watch the build status"
    result = getattr(manager, action)(**kwargs)
    assert not result["ok"]
    assert result["error_code"] == "invalid_actor"
    assert manager_for(kind).state.to_json() == before


def manager_for(kind):
    from hermes_cli.goals import GoalManager
    from hermes_cli.loops import LoopManager
    return (GoalManager if kind == "goal" else LoopManager)("requester")


@pytest.mark.parametrize("kind", ["goal", "loop"])
@pytest.mark.parametrize("actor", ["agent", "autonomy", "user"])
@pytest.mark.parametrize("pauser", ["", "agent", "user", "system"])
@pytest.mark.parametrize("action", ["pause", "resume", "stop", "revise", "replace"])
def test_lifecycle_authority_matrix(state, kind, actor, pauser, action):
    from hermes_cli.session_controls import _manager_apply
    manager = manager_for(kind)
    manager.set("Watch build", actor="agent")
    if pauser:
        manager.pause("Existing pause", by=pauser)
    if action in {"revise", "replace"}:
        from hermes_constants import get_hermes_home
        get_hermes_home().joinpath("config.yaml").write_text(
            f"{kind}s:\n  autonomy: {str(actor == 'autonomy').lower()}\n")
        kwargs = {"reason": "Clearer operational wording", "actor": "agent" if actor == "autonomy" else actor}
        if kind == "goal" and action == "revise":
            kwargs["subgoals"] = ["Watch build", "Read the build status"]
        else:
            kwargs["goal" if kind == "goal" else "prompt"] = "Watch the build status"
        result = getattr(manager, action)(**kwargs)
        assert result["ok"], result
        assert result["revision"]["actor"] == ("user" if actor == "user" else "agent")
        assert result["revision"]["lifecycle_authority"] == actor
        fresh = manager_for(kind).state
        assert fresh.status == ("paused" if pauser else "active")
        assert fresh.paused_by == pauser
        return
    # Goals expose user-only clear; loops expose agent finish through stop.
    native_action = "clear" if kind == "goal" and action == "stop" else action
    result = _manager_apply(kind, native_action, "requester", reason="Operational decision",
                            authority={"via": actor})
    outcome = result["result"]
    ok = (outcome is not None or (native_action == "clear" and getattr(result.get("state"), "status", "") == "cleared"))
    ok = ok and not (isinstance(outcome, dict) and not outcome.get("ok", True))
    agent = actor != "user"
    allowed = True
    if action == "resume":
        allowed = (bool(pauser) or (kind == "loop" and not agent)) and (not agent or pauser == "agent")
    elif action == "pause" and agent:
        allowed = not pauser
    elif action == "stop" and agent:
        allowed = kind == "loop" and pauser in {"", "agent"}
    assert ok == allowed, result
    fresh = manager_for(kind).state
    if not allowed:
        assert fresh.status == ("paused" if pauser else "active")
        assert fresh.paused_by == pauser
    elif action == "pause":
        # Opted-in goal autonomy waits rather than suspending its continuation.
        if kind == "goal" and actor == "autonomy":
            assert fresh.status == "active" and fresh.waiting_until
        else:
            assert fresh.status == "paused"
            assert fresh.paused_by == ("agent" if agent else "user")
    elif action == "resume":
        assert fresh.status == "active" and fresh.paused_by == ""
    else:
        assert fresh.status == ("done" if agent else "cleared")


@pytest.mark.parametrize("autonomy", [False, True])
def test_owned_pause_resume_keeps_loop_cadence(state, monkeypatch, autonomy):
    if autonomy:
        from hermes_constants import get_hermes_home
        get_hermes_home().joinpath("config.yaml").write_text("loops:\n  autonomy: true\n")
    from hermes_cli.loops import LoopManager
    from hermes_cli.session_controls import apply_control
    manager = LoopManager("requester")
    manager.set("Watch build", interval_seconds=600, actor="agent")
    data = json.loads(state.get_meta("loop:requester"))
    data.update(last_fired_at=1000.0, next_due_at=1600.0)
    state.set_meta("loop:requester", json.dumps(data))
    monkeypatch.setattr("hermes_cli.loops.time.time", lambda: 1010.0)
    assert apply_control("loop", "pause", "requester", requester_sid="requester", reason="Waiting on build")["ok"]
    assert LoopManager("requester").state.paused_by == "agent"
    assert apply_control("loop", "resume", "requester", requester_sid="requester", reason="Build available")["ok"]
    assert LoopManager("requester").state.next_due_at >= 1600.0


@pytest.mark.parametrize("kind", ["goal", "loop"])
def test_owned_controls_do_not_depend_on_approval_card_visibility(state, kind, monkeypatch):
    from hermes_cli.session_controls import request_control
    manager_for(kind).set("Watch build", actor="agent")
    monkeypatch.setattr("hermes_cli.goal_notices.lifecycle_cards_hidden", lambda _: True)
    result = request_control(kind, "pause", "requester", requester_sid="requester", reason="Wait for build")
    assert result["ok"], result
    assert manager_for(kind).state.paused_by == "agent"


@pytest.mark.parametrize("action", ["revise", "replace"])
def test_quoted_goal_stop_addition_becomes_user_owned(state, action):
    manager = manager_for("goal")
    manager.set("Ship build", actor="agent")
    quote = "Stop when the release window closes"
    state.append_message("requester", "user", quote)
    kwargs = {"reason": "User supplied a stop condition", "contract": {"stop_when": quote}, "user_quote": quote}
    if action == "replace":
        kwargs["goal"] = "Ship the verified build"
    result = getattr(manager, action)(**kwargs)
    assert result["ok"], result
    assert manager_for("goal").state.stop_condition_owner == "user"
    assert result["revision"]["user_quote"] == quote
    assert not manager.revise(reason="Keep going", contract={"stop_when": ""})["ok"]


@pytest.mark.parametrize("kind", ["goal", "loop"])
@pytest.mark.parametrize("action", ["revise", "replace"])
@pytest.mark.parametrize("relayed", [False, True])
def test_revision_quotes_rechecked_inside_commit(state, monkeypatch, tmp_path, kind, action, relayed):
    from tests.hermes_cli.test_lifecycle_edit_latitude import relay
    from hermes_cli import lifecycle_snapshot
    manager = manager_for(kind)
    manager.set("Ship build", actor="agent")
    quote = "Stop when the release window closes"
    if relayed:
        quote = f"Add the release-window stop condition to the requester {kind}"
        relay(state, monkeypatch, tmp_path, quote)
    else:
        state.append_message("requester", "user", quote)
    before = manager.state.to_json()
    real = lifecycle_snapshot.commit_and_publish_snapshot

    def overtake(*args, **kwargs):
        state.append_message("target" if relayed else "requester", "user", "Do not change the stop condition")
        return real(*args, **kwargs)

    monkeypatch.setattr(lifecycle_snapshot, "commit_and_publish_snapshot", overtake)
    # loops_snapshot imports this symbol at module scope.
    monkeypatch.setattr("hermes_cli.loops_snapshot.commit_and_publish_snapshot", overtake)
    kwargs = {"reason": "Apply user stop condition", "user_quote": quote}
    kwargs.update({"contract": {"stop_when": quote}} if kind == "goal" else {"until": quote})
    if action == "replace":
        kwargs["goal" if kind == "goal" else "prompt"] = "Ship the verified build"
    result = getattr(manager, action)(**kwargs)
    assert not result["ok"], result
    assert result["error_code"] == "state_changed"
    assert manager_for(kind).state.to_json() == before
