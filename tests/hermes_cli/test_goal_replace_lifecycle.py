"""Goal replacement: what binds afterwards, and which pauses it may lift.

Regressions for the PR #551 review. (1) After a session-control replacement, revision history in the
continuation and judge prompts rendered a later user ``/goal remove`` as an agent edit without user
authority and kept the removed check binding. (2) ``replace()`` reactivated user-paused goals.
"""

import pytest

from hermes_cli import goals
from hermes_cli.goal_command import dispatch_goal_command
from hermes_cli.goals import GoalManager, load_goal


@pytest.fixture
def state(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    goals._DB_CACHE.clear()
    from hermes_state_registry import acquire, release_or_close

    db = acquire(home / "state.db")
    for sid in ("requester", "target"):
        db.create_session(sid, "telegram", profile_name="default", chat_id=sid,
                          chat_type="private", session_key=f"agent:main:telegram:dm:{sid}")
    goals._DB_CACHE[str(home)] = db
    monkeypatch.setattr(goals, "collect_goal_evidence", lambda *a, **k: [])
    yield db
    goals._DB_CACHE.clear()
    release_or_close(db)


def _dispatch(manager, command):
    return dispatch_goal_command(manager, command, authorize_gate=lambda: None)


def _capture_judge(monkeypatch):
    prompts = []

    def capture(_call, _system, prompt, _timeout):
        prompts.append(prompt)
        return '{"verdict": "continue", "reason": "more work"}'

    monkeypatch.setattr(goals, "_call_goal_judge_llm", capture)
    return prompts


def test_bare_goal_replaces_paused_in_place_without_kickoff(state):
    manager = GoalManager("target")
    manager.set("old objective")
    assert manager.state is not None
    manager.state.turns_used = 3
    manager.state.status = "paused"
    manager.state.paused_reason = "user-paused"
    manager._save()

    result = _dispatch(GoalManager("target"), "new objective")

    stored = load_goal("target")
    assert not result.error
    assert result.prompt is None and result.kickoff is False
    assert result.clear_pending == "replace"
    assert stored.goal == "new objective"
    assert stored.status == "paused"
    assert stored.turns_used == 3
    assert len(stored.revisions) == 1
    assert stored.revisions[0]["kind"] == "replace"


def test_bare_goal_replaces_active_without_kickoff(state):
    manager = GoalManager("target")
    manager.set("old objective")

    result = _dispatch(GoalManager("target"), "new objective")

    stored = load_goal("target")
    assert not result.error
    assert result.prompt is None and result.kickoff is False
    assert stored.goal == "new objective" and stored.status == "active"


def test_bare_goal_lifts_blocked_pause_without_kickoff(state):
    manager = GoalManager("target")
    manager.set("old objective")
    manager.state.turns_used = 3
    manager.state.status = "paused"
    _blocked(manager)
    manager._save()

    result = _dispatch(GoalManager("target"), "new objective")

    stored = load_goal("target")
    assert not result.error
    assert result.prompt is None and result.kickoff is False
    assert stored.goal == "new objective"
    assert stored.status == "active" and stored.paused_reason is None
    assert stored.turns_used == 3



def test_check_removed_after_button_replace_is_not_binding(state, monkeypatch):
    from hermes_cli.session_controls import request_control, resolve_request

    GoalManager("target").set("old objective")
    request = request_control("goal", "replace", "target", requester_sid="requester", reason="pivot",
                              payload={"goal": "new objective",
                                       "contract": {"verification": "Audit passes.\nLint passes."}})
    assert resolve_request(request["id"], "approve", "42")["status"] == "applied"
    manager = GoalManager("target")
    assert manager.remove_contract_item("check", 2) == "Lint passes."

    prompts = _capture_judge(monkeypatch)
    decision = manager.evaluate_after_turn("Audit passes.")
    continuation = GoalManager("target").next_continuation_prompt()

    assert decision["should_continue"] and len(prompts) == 1
    for prompt in (prompts[0], continuation):
        assert "Audit passes." in prompt
        assert "Lint passes." not in prompt and "old objective" not in prompt
        for marker in ("This goal has been revised", "Revision history", "no user authority",
                       "superseded", "approved by the user"):
            assert marker not in prompt


def _blocked(manager):
    manager.state.last_verdict = "blocked"
    manager.state.paused_reason = goals._BLOCKED_PAUSE_PREFIX + "needs the API key"


def _budget(manager):
    manager.state.turns_used = manager.state.max_turns = 2
    manager.state.paused_reason = "turn budget exhausted (2/2)"


def _reason(text):
    return lambda manager: setattr(manager.state, "paused_reason", text)


PAUSES = [
    ("blocked", _blocked, True),
    ("user", _reason("user-paused"), False),
    ("stop", _reason("user-interrupted (/stop)"), False),
    ("ctrl-c", _reason("user-interrupted (Ctrl+C)"), False),
    ("session-control", _reason("session-control"), False),
    ("budget", _budget, False),
    ("judge-unreachable", _reason("judge API unreachable 3 turns in a row"), False),
]


@pytest.mark.parametrize("kind,arrange,lifts", PAUSES, ids=[kind for kind, _, _ in PAUSES])
def test_replace_lifts_only_a_judge_contract_pause(state, kind, arrange, lifts):
    manager = GoalManager("target")
    manager.set("old objective")
    manager.state.status = "paused"
    arrange(manager)
    manager._save()
    paused_reason = manager.state.paused_reason

    result = _dispatch(GoalManager("target"), "replace new objective")

    stored = load_goal("target")
    assert not result.error and stored.goal == "new objective"
    if lifts:
        assert (stored.status, stored.paused_reason) == ("active", None)
        assert result.prompt
    else:
        assert (stored.status, stored.paused_reason) == ("paused", paused_reason)
        assert result.prompt is None


def test_silent_stall_stop_is_not_replaceable(state):
    manager = GoalManager("target")
    manager.set("old objective")
    manager.state.status = "cleared"
    manager.state.paused_reason = "silent completion rechecks produced no new evidence"
    manager._save()

    result = _dispatch(GoalManager("target"), "replace new objective")

    assert result.error
    assert load_goal("target").goal == "old objective"
