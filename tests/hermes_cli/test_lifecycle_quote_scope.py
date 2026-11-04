"""Quotes retain the session where the user typed them across authenticated relays."""
from __future__ import annotations

from typing import Any

import pytest

from tests.hermes_cli.test_lifecycle_edit_latitude import relay

pytest_plugins = ["tests.hermes_cli.test_session_controls"]


def manager_for(kind, sid="requester") -> Any:
    from hermes_cli.goals import GoalManager
    from hermes_cli.loops import LoopManager
    return (GoalManager if kind == "goal" else LoopManager)(sid)


@pytest.mark.parametrize("kind,action", [
    ("goal", "clear"), ("goal", "replace"), ("goal", "pause"), ("goal", "resume"),
    ("loop", "stop"), ("loop", "pause"), ("loop", "resume"),
])
@pytest.mark.parametrize("named", [False, True, "title"])
def test_relay_control_requires_the_controlled_session_not_the_receiver(
        state, monkeypatch, tmp_path, kind, action, named):
    from hermes_cli.session_controls import apply_control
    manager = manager_for(kind)
    manager.set("Watch build")
    if action == "resume":
        manager.pause("User paused")
    before = manager.state.to_json()
    state.set_session_title("requester", "Build watch")
    name = "Build watch " if named == "title" else "requester " if named else ""
    quote = f"{action.capitalize()} the {name}{kind}"
    relay(state, monkeypatch, tmp_path, quote)
    kwargs = {"payload": {"goal": "Watch the verified build"}} if action == "replace" else {}
    result = apply_control(kind, action, "requester", requester_sid="requester",
                           user_quote=quote, reason="Apply Brian's instruction", **kwargs)
    assert result["status"] == ("applied" if named else "pending"), result
    if named:
        assert result["record"]["authority"]["via"] == "quote"
        assert result["record"]["authority"]["relay_receipt"] == "receipt-1"
        assert result["record"]["quote_source_session_id"] == "target"
        if kind == "goal" and action == "replace":
            assert manager_for(kind).state.revisions[-1]["quote_source_session_id"] == "target"
    else:
        assert manager_for(kind).state.to_json() == before


@pytest.mark.parametrize("kind", ["control", "revision"])
def test_scope_uses_the_normalized_full_instruction_not_a_trimmed_excerpt(state, monkeypatch, tmp_path, kind):
    from hermes_cli.goals import GoalContract
    from hermes_cli.session_controls import apply_control
    manager = manager_for("goal")
    manager.set("Watch build", actor="agent", contract=GoalContract(constraints="Retain build logs"))
    if kind == "control":
        message, quote = "Please  clear the requester goal", "Please clear the"
    else:
        message = 'Remove  the constraint "Retain build logs" from the requester goal'
        # Protected removals still require the entire instruction, not an excerpt.
        quote = message
    relay(state, monkeypatch, tmp_path, message)
    if kind == "control":
        result = apply_control("goal", "clear", "requester", requester_sid="requester", user_quote=quote)
        assert result["status"] == "applied", result
    else:
        from hermes_cli.goals_autonomy import current_removal_quote
        checked = current_removal_quote("requester", quote)
        assert not isinstance(checked, str), checked
        result = manager.revise(reason="Apply the exact scoped instruction", contract={"constraints": ""}, user_quote=quote)
        assert result["ok"], result


def test_own_typed_quote_still_authorizes_its_origin(state):
    from hermes_cli.session_controls import apply_control
    manager_for("goal").set("Watch build")
    quote = "Clear the goal"
    state.append_message("requester", "user", quote)
    result = apply_control("goal", "clear", "requester", requester_sid="requester", user_quote=quote)
    assert result["status"] == "applied", result
    assert manager_for("goal").state.status == "cleared"
    assert result["record"]["quote_source_session_id"] == "requester"


def test_relay_quote_self_reference_authorizes_the_original_session(state, monkeypatch, tmp_path):
    from hermes_cli.session_controls import apply_control
    manager_for("goal", "target").set("Watch build")
    quote = "Clear the goal"
    relay(state, monkeypatch, tmp_path, quote)
    result = apply_control("goal", "clear", "target", requester_sid="requester", user_quote=quote)
    assert result["status"] == "applied", result
    assert manager_for("goal", "target").state.status == "cleared"


@pytest.mark.parametrize("kind", ["goal", "loop"])
@pytest.mark.parametrize("action", ["revise", "replace"])
@pytest.mark.parametrize("named", [False, True])
def test_relay_revision_removal_requires_its_origin_or_a_named_session(
        state, monkeypatch, tmp_path, kind, action, named):
    from hermes_cli.goals import GoalContract
    manager = manager_for(kind)
    if kind == "goal":
        manager.set("Watch build", actor="agent", contract=GoalContract(
            verification="Build passes", constraints="Retain build logs"))
        quote = 'Remove "Retain build logs"' + (" from the requester goal" if named else "")
        kwargs: dict[str, Any] = {"contract": {"constraints": ""}}
    else:
        manager.set("Watch build", until="Build settles")
        quote = "Remove the " + ("requester " if named else "") + "build stop condition"
        kwargs = {"until": ""}
    if action == "replace":
        kwargs["goal" if kind == "goal" else "prompt"] = "Watch the verified build"
    before = manager.state.to_json()
    relay(state, monkeypatch, tmp_path, quote)
    result = getattr(manager, action)(reason="Brian changed the limit", user_quote=quote, **kwargs)
    assert result["ok"] is bool(named), result
    if named:
        assert result["revision"]["relay_receipt"] == "receipt-1"
    else:
        assert manager_for(kind).state.to_json() == before


@pytest.mark.parametrize("kind", ["goal", "loop"])
@pytest.mark.parametrize("action", ["revise", "replace"])
@pytest.mark.parametrize("autonomy", [False, True])
def test_autonomy_actor_is_not_a_caller_selectable_authority(state, kind, action, autonomy):
    from hermes_constants import get_hermes_home
    get_hermes_home().joinpath("config.yaml").write_text(f"{kind}s:\n  autonomy: {str(autonomy).lower()}\n")
    manager = manager_for(kind)
    manager.set("Watch build")
    before = manager.state.to_json()
    kwargs: dict[str, Any] = {"reason": "Clearer instructions", "actor": "autonomy",
              "goal" if kind == "goal" else "prompt": "Watch the verified build"}
    if kind == "goal" and action == "replace":
        kwargs["max_turns"] = 0
    result = getattr(manager, action)(**kwargs)
    assert result["error_code"] == "invalid_actor", result
    assert manager_for(kind).state.to_json() == before


@pytest.mark.parametrize("kind", ["goal", "loop"])
@pytest.mark.parametrize("autonomy", [False, True])
def test_agent_edit_authority_is_derived_from_config(state, kind, autonomy):
    from hermes_constants import get_hermes_home
    get_hermes_home().joinpath("config.yaml").write_text(f"{kind}s:\n  autonomy: {str(autonomy).lower()}\n")
    manager = manager_for(kind)
    manager.set("Watch build", actor="agent")
    result = manager.replace(reason="Clearer instructions", **{
        "goal" if kind == "goal" else "prompt": "Watch the verified build"})
    assert result["ok"], result
    assert result["revision"]["lifecycle_authority"] == ("autonomy" if autonomy else "agent")


@pytest.mark.parametrize("kind", ["goal", "loop"])
@pytest.mark.parametrize("action", ["revise", "replace"])
@pytest.mark.parametrize("message,quote", [
    ("Do not remove the build stop condition", "remove the build stop condition"),
    ("Remove the build stop condition only if the build passes", "Remove the build stop condition"),
    ("Remove the build stop condition?", "Remove the build stop condition"),
])
def test_revision_quotes_obey_control_negation_and_qualification_checks(state, kind, action, message, quote):
    from hermes_cli.goals import GoalContract
    manager = manager_for(kind)
    if kind == "goal":
        manager.set("Watch build", actor="agent", contract=GoalContract(verification="Build passes"))
        kwargs: dict[str, Any] = {"contract": {"stop_when": "Build settles"}}
    else:
        manager.set("Watch build", until="Build settles")
        kwargs = {"until": ""}
    if action == "replace":
        kwargs["goal" if kind == "goal" else "prompt"] = "Watch the verified build"
    before = manager.state.to_json()
    state.append_message("requester", "user", message)
    result = getattr(manager, action)(reason="Apply user instruction", user_quote=quote, **kwargs)
    assert not result["ok"], result
    assert manager_for(kind).state.to_json() == before


@pytest.mark.parametrize("kind", ["goal", "loop"])
@pytest.mark.parametrize("action", ["revise", "replace"])
@pytest.mark.parametrize("autonomy", [False, True])
def test_quoted_goal_removal_preserves_resolved_authority(state, kind, action, autonomy):
    from hermes_constants import get_hermes_home
    from hermes_cli.goals import GoalContract
    get_hermes_home().joinpath("config.yaml").write_text(f"{kind}s:\n  autonomy: {str(autonomy).lower()}\n")
    manager = manager_for(kind)
    kwargs: dict[str, Any]
    if kind == "goal":
        manager.set("Watch build", actor="agent", contract=GoalContract(constraints="Never publish secrets"))
        quote = 'Remove "Never publish secrets"'
        kwargs = {"contract": {"constraints": ""}}
    else:
        manager.set("Watch build", actor="agent", until="Build settles")
        quote = "Remove the build stop condition"
        kwargs = {"until": ""}
    state.append_message("requester", "user", quote)
    if action == "replace":
        kwargs["goal" if kind == "goal" else "prompt"] = "Watch the verified build"
    result = getattr(manager, action)(reason="User removed the exact limit", user_quote=quote, **kwargs)
    assert result["ok"], result
    assert manager_for(kind).state.revisions[-1]["lifecycle_authority"] == ("autonomy" if autonomy else "agent")


@pytest.mark.parametrize("action,kwargs", [("revise", {}), ("replace", {"prompt": ""})])
@pytest.mark.parametrize("actor", ["autonomy", {}, []])
def test_unknown_loop_actor_refuses_even_for_a_noop(state, action, kwargs, actor):
    manager = manager_for("loop")
    manager.set("Watch build")
    before = manager.state.to_json()
    result = getattr(manager, action)(reason="No operational change", actor=actor, **kwargs)
    assert result["error_code"] == "invalid_actor", result
    assert manager_for("loop").state.to_json() == before


@pytest.mark.parametrize("named", [False, True])
def test_relay_scope_applies_to_a_third_session(state, monkeypatch, tmp_path, named):
    from hermes_cli.session_controls import apply_control
    manager_for("goal", "other").set("Watch build")
    quote = "Clear the " + ("other " if named else "") + "goal"
    relay(state, monkeypatch, tmp_path, quote)
    result = apply_control("goal", "clear", "other", requester_sid="requester", user_quote=quote)
    assert (result["status"] == "applied") is named, result
    assert manager_for("goal", "other").state.status == ("cleared" if named else "active")


def test_quoted_requirement_data_cannot_name_the_controlled_session(state, monkeypatch, tmp_path):
    from hermes_cli.goals import GoalContract
    manager = manager_for("goal")
    manager.set("Watch build", actor="agent", contract=GoalContract(constraints="Retain requester build logs"))
    before = manager.state.to_json()
    quote = 'Remove "Retain requester build logs"'
    relay(state, monkeypatch, tmp_path, quote)
    result = manager.revise(reason="User changed scope", contract={"constraints": ""}, user_quote=quote)
    assert not result["ok"], result
    assert manager_for("goal").state.to_json() == before


@pytest.mark.parametrize("racing", [False, True])
def test_relay_scope_rechecks_title_uniqueness_under_commit(state, monkeypatch, tmp_path, racing):
    from hermes_cli import session_controls
    manager_for("goal").set("Watch build")
    state.set_session_title("requester", "Build watch")
    quote = "Clear the Build watch goal"
    relay(state, monkeypatch, tmp_path, quote)
    if not racing:
        state.set_session_title("other", "build WATCH")
    else:
        real = state._execute_write
        overtaken = False

        def overtake(fn, *args, **kwargs):
            nonlocal overtaken
            if not overtaken:
                overtaken = True
                state.set_session_title("other", "build WATCH")
            return real(fn, *args, **kwargs)

        monkeypatch.setattr(state, "_execute_write", overtake)
    result = session_controls.apply_control("goal", "clear", "requester", requester_sid="requester", user_quote=quote)
    assert result["status"] == "pending", result
    assert manager_for("goal").state.status == "active"


@pytest.mark.parametrize("kind", ["goal", "loop"])
@pytest.mark.parametrize("message,condition", [
    ("Stop when the build is green", "the build is green"),
    ("Keep checking until the build is green", "build is green"),
])
def test_positive_stop_specifications_remain_user_owned(state, kind, message, condition):
    manager = manager_for(kind)
    manager.set("Watch build", actor="agent")
    state.append_message("requester", "user", message)
    result = manager.revise(reason="User set a stop condition", user_quote=message, **(
        {"contract": {"stop_when": condition}} if kind == "goal" else {"until": condition}))
    assert result["ok"], result
    assert manager_for(kind).state.stop_condition_owner == "user"


@pytest.mark.parametrize("action", ["revise", "replace"])
def test_stop_specification_never_authorizes_condition_removal(state, action):
    manager = manager_for("loop")
    manager.set("Watch build", until="Build settles")
    before = manager.state.to_json()
    quote = "Stop when the build is green"
    state.append_message("requester", "user", quote)
    kwargs = {"prompt": "Watch the verified build"} if action == "replace" else {}
    result = getattr(manager, action)(reason="Cannot reinterpret a stop specification", until="", user_quote=quote, **kwargs)
    assert not result["ok"], result
    assert manager_for("loop").state.to_json() == before
