"""Every declarative authority cell exercised against durable production managers.

Creation, user commitments and pauses use the native APIs, not hand-built JSON
that forgets production ownership or implicit-requirement migration (PR #937).
"""
import pytest

from hermes_cli.lifecycle_policy import POLICY, A

pytest_plugins = ["tests.hermes_cli.test_session_controls"]


def _goal_edit(manager, operation, actor, quote):
    kwargs = dict(reason="Clarify the same release", actor=actor, user_quote=quote)
    # In default mode a user's contract is edited via the existing explicit
    # command/approved-control surface, not the additive-only revise method.
    from hermes_cli.goals_autonomy import goal_autonomy_enabled
    if actor == "agent" and manager.state.created_by == "user" and not goal_autonomy_enabled() and quote and operation != "revise":
        from hermes_cli.session_controls import apply_control
        payload = {"goal": manager.state.goal}
        if operation in {"outcome", "verification", "constraints", "boundaries", "stop_when"}:
            payload["contract"] = {operation: ""}
        elif operation == "subgoals":
            # Native explicit removal remains a user command, not replace's job.
            from hermes_cli.session_controls import check_user_quote
            assert not isinstance(check_user_quote(manager.session_id, quote, quoted_targets=True), str)
            manager.remove_subgoal(1)
            return {"ok": True}
        elif operation == "max_turns":
            payload["max_turns"] = 0
        else:
            payload["goal"] = "Release v2 safely"
        return apply_control("goal", "replace", manager.session_id, requester_sid=manager.session_id,
                             reason="User requested replacement", payload=payload, user_quote=quote)
    fields = {"outcome", "verification", "constraints", "boundaries", "stop_when"}
    if operation in fields:
        if actor == "user":
            return manager.replace(goal=manager.state.goal, contract={operation: ""}, **kwargs)
        return manager.revise(contract={operation: ""}, **kwargs)
    if operation == "subgoals":
        if actor == "user":
            manager.remove_subgoal(1)
            return {"ok": True}
        return manager.revise(subgoals=[], **kwargs)
    if operation == "max_turns":
        return manager.replace(goal=manager.state.goal, max_turns=0, **kwargs)
    if operation == "revise":
        return manager.revise(contract={"verification": "CI green\nStaging green"}, **kwargs)
    if operation == "objective":
        return manager.revise(goal="Release v2 safely", **kwargs) if actor == "agent" else manager.replace(goal="Release v2 safely", **kwargs)
    return manager.replace(goal="Release v2 safely", **kwargs)


def _loop_edit(manager, operation, actor, quote):
    kwargs = dict(reason="Clarify the same release watch", actor=actor, user_quote=quote)
    edits = {
        "outcome": {"prompt": "Watch the v2 release status"},
        "stop_when": {"until": ""},
        "budget": {"times": 0},
        "budget_reduce": {"times": 2},
        "cadence_faster": {"interval_seconds": 60},
        "cadence_slower": {"interval_seconds": 1200},
        "revise": {"interval_seconds": 1200},
    }
    if operation == "replace":
        return manager.replace(prompt="Watch the v2 release status", interval_seconds=600,
                               times=5, until=manager.state.until, **kwargs)
    return manager.revise(**edits[operation], **kwargs)


def _control(manager, kind, operation, actor, quote):
    from hermes_cli import session_controls as controls
    if operation in {"resume", "resume_user_pause", "stop_user_pause"}:
        manager.pause("Wait for the release window", by="user" if operation.endswith("user_pause") else "agent")
        operation = "stop" if operation == "stop_user_pause" else "resume"
    if operation == "stop_user_condition":
        operation = "stop"
    if kind == "goal" and operation == "stop":
        operation = "clear"
    if actor == "user":
        if (kind, operation) == ("loop", "clear"):
            return controls.apply_control(kind, operation, "requester", requester_sid="requester")
        result = controls._manager_apply(kind, operation, "requester", reason="Release watch changed",
                                         authority={"via": "user"})
        value = result.get("result")
        return value if isinstance(value, dict) else {"ok": value is not False}
    return controls.apply_control(kind, operation, "requester", requester_sid="requester",
                                  reason="Release watch changed", user_quote=quote)


def _quote(kind, operation, owner="user", autonomous=False):
    names = {"outcome": "Release v2 to all users", "verification": "CI green",
             "constraints": "Keep backups", "boundaries": "Release service only",
             "subgoals": "Staging green", "stop_when": "Release window closes", "max_turns": "turn budget"}
    if kind == "goal" and owner == "user" and not autonomous and operation in {
            "outcome", "verification", "constraints", "boundaries", "stop_when", "max_turns", "objective", "replace", "revise"}:
        return "Replace the goal immediately"
    if kind == "goal" and operation == "stop_when" and owner == "agent":
        return "Remove the stop condition immediately"
    if kind == "goal" and operation in names:
        return f'Remove "{names[operation]}"'
    if operation in {"resume", "resume_user_pause"}:
        return f"Resume the {kind} immediately"
    if operation == "pause":
        return f"Pause the {kind} immediately"
    if operation in {"clear", "stop", "stop_user_condition", "stop_user_pause"}:
        return f"Stop the {kind} immediately"
    if kind == "loop" and operation == "stop_when":
        return "Remove the loop stop condition"
    return f"Replace the {kind} immediately"


@pytest.mark.parametrize("cell,expected", POLICY.items(), ids=lambda value: str(value))
def test_every_authority_cell(state, cell, expected):
    from hermes_constants import get_hermes_home
    from hermes_cli.goals import GoalManager, GoalContract
    from hermes_cli.loops import LoopManager

    kind, operation, owner, actor, autonomous, quoted = cell
    get_hermes_home().joinpath("config.yaml").write_text(
        f"goals:\n  autonomy: {str(autonomous).lower()}\nloops:\n  autonomy: {str(autonomous).lower()}\n")
    manager = GoalManager("requester") if kind == "goal" else LoopManager("requester")
    if kind == "goal":
        manager.set("Release v2", actor=owner, max_turns=20, contract=GoalContract(
            outcome="Release v2 to all users", verification="CI green", constraints="Keep backups",
            boundaries="Release service only", stop_when="Release window closes"))
        manager.add_subgoal("Staging green")
    else:
        manager.set("Watch v2 release", actor=owner, interval_seconds=600, times=5,
                    until="Release window closes")
    # A user stop commitment on an agent-created item is a different policy row
    # from an agent's own stop condition; construct it through an actual user edit.
    if operation == "stop_user_condition":
        assert manager.revise(reason="User commitment", actor="user", until="Release window closes soon")["ok"]
    quote = _quote(kind, operation, owner, autonomous) if quoted else ""
    if quote:
        state.append_message("requester", "user", quote)
    controls = {"pause", "resume", "resume_user_pause", "stop", "clear", "stop_user_condition", "stop_user_pause"}
    if operation in controls:
        result = _control(manager, kind, operation, actor, quote)
    else:
        result = (_goal_edit if kind == "goal" else _loop_edit)(manager, operation, actor, quote)
    allowed = bool(result.get("ok")) and result.get("status") != "pending"
    assert allowed == (expected == A), (cell, expected, result)
    durable = type(manager)("requester").state
    if not allowed:
        if kind == "goal":
            assert durable.contract.to_dict() == {
                "outcome": "Release v2 to all users", "verification": "CI green",
                "constraints": "Keep backups", "boundaries": "Release service only",
                "stop_when": "Release window closes"}
            assert durable.max_turns == 20
        assert durable.status != "done"
    elif kind == "goal":
        if operation in {"outcome", "verification", "constraints", "boundaries", "stop_when"}:
            assert getattr(durable.contract, operation) == ""
        elif operation == "max_turns":
            assert durable.max_turns == 0
        elif operation == "subgoals":
            assert durable.subgoals == []
        elif operation in {"objective", "replace"}:
            assert durable.goal == "Release v2 safely"
        elif operation in {"stop", "clear"}:
            assert durable.status == "cleared"
    elif operation in {"stop", "stop_user_condition", "stop_user_pause"}:
        # Explicit user/quote stop clears; autonomous/owned agent stop finishes.
        assert durable.status == ("cleared" if actor == "user" or quoted else "done")
    if allowed and actor == "agent" and not autonomous and durable.revisions:
        assert durable.revisions[-1].get("authority") != "autonomy"


@pytest.mark.parametrize("target,payload", [("missing", None), ("requester", {}), ("missing", {"goal": "Changed"})])
def test_hidden_approval_refusal_precedes_request_validation(state, target, payload):
    from hermes_constants import get_hermes_home
    from hermes_cli.session_controls import request_control
    get_hermes_home().joinpath("config.yaml").write_text("goals:\n  notices: hidden\n")
    result = request_control("goal", "replace", target, requester_sid="requester", payload=payload)
    assert result["error_code"] == "approval_cards_hidden"
