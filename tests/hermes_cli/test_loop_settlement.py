"""Loop revision and settlement conflicts must preserve the durable winner."""

import pytest

from hermes_cli import goals, loops

pytest_plugins = ["tests.hermes_cli.test_loops"]


@pytest.mark.parametrize("verdict", ["continue", "done", "blocked"])
@pytest.mark.parametrize("change", ["revise", "pause", "clear", "replace"])
def test_judge_cannot_settle_over_a_newer_loop(hermes_home, monkeypatch, verdict, change):
    manager = loops.LoopManager("loop-race")
    manager.set("Check the deployment", interval_seconds=300, until="deployment healthy")
    assert manager.fire_tick()
    saved = []

    def judge(*args, **kwargs):
        writer = loops.LoopManager(manager.session_id)
        if change == "revise":
            assert writer.revise(reason="Less polling", interval_seconds=600)["ok"]
        elif change == "replace":
            source = "Watch the new deployment instead"
            assert writer.replace(prompt=source, reason="User changed task", user_quote=source,
                                  user_messages=[source], interval_seconds=600)["ok"]
        elif change == "pause":
            writer.pause()
        else:
            writer.clear()
        saved.append(loops.load_loop(manager.session_id))
        return verdict, "old judgment", False, None, False

    monkeypatch.setattr(goals, "judge_goal", judge)
    decision = manager.complete_tick("The prior deployment is healthy")
    after = loops.load_loop(manager.session_id)
    winner = saved[0]
    assert after.status == winner.status
    assert after.prompt == winner.prompt
    assert after.interval_seconds == winner.interval_seconds
    assert after.revisions == winner.revisions
    assert after.ticks_fired == winner.ticks_fired
    assert after.created_at == winner.created_at
    assert after.awaiting_response is (winner.awaiting_response if change == "clear" else False)
    assert decision["stopped"] is False
    assert decision["message"] == ""
    if change == "revise":
        assert after.next_due_at >= after.last_fired_at + 600
    else:
        assert after.next_due_at == winner.next_due_at


@pytest.mark.parametrize("timing", ["before", "commit"])
def test_stale_revision_is_rejected_without_erasing_the_winner(hermes_home, monkeypatch, timing):
    creator = loops.LoopManager("loop-revision-race")
    creator.set("Check the deployment", interval_seconds=300, times=5)
    first, stale = loops.LoopManager(creator.session_id), loops.LoopManager(creator.session_id)
    saved = []

    def publish_winner():
        assert first.revise(reason="Less polling", interval_seconds=600)["ok"]
        saved.append(loops.load_loop(creator.session_id).to_json())

    if timing == "before":
        publish_winner()
    else:
        db = loops._get_session_db()
        execute = db._execute_write
        armed = True
        def interleave(write, *args, **kwargs):
            nonlocal armed
            if armed:
                armed = False
                publish_winner()
            return execute(write, *args, **kwargs)
        monkeypatch.setattr(db, "_execute_write", interleave)
    result = stale.revise(reason="Fewer checks", times=3)
    assert result["ok"] is False
    assert result["error_code"] == "state_changed"
    assert loops.load_loop(creator.session_id).to_json() == saved[0]
    assert stale.state.to_json() == saved[0]
    assert stale.revise(reason="Fewer checks", times=3)["ok"]
    after = loops.load_loop(creator.session_id)
    assert after.interval_seconds == 600
    assert after.times == 3
    assert after.version == 3
