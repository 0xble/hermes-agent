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


@pytest.mark.parametrize("operation", ["revise", "replace", "settle", "settle_noop", "noop"])
@pytest.mark.parametrize("timing", ["legacy_entry", "load", "commit", "pause_resume"])
def test_loop_cas_validates_entry_and_noop_commit(hermes_home, monkeypatch, operation, timing):
    manager = loops.LoopManager("loop-boundary")
    original = manager.set("Check the deployment", interval_seconds=300)
    if operation == "settle":
        assert manager.fire_tick()
    db = loops._get_session_db()
    if timing == "legacy_entry":
        original.mutation_id = ""
        db.set_meta("loop:loop-boundary", original.to_json())
    before = original.to_json()
    saved = []

    def publish():
        writer = loops.LoopManager(manager.session_id)
        if timing == "pause_resume":
            writer.pause()
            writer.resume()
        else:
            assert writer.revise(reason="Less polling", interval_seconds=600)["ok"]
            if timing == "legacy_entry":
                writer.state.mutation_id = ""
                db.set_meta("loop:loop-boundary", writer.state.to_json())
        saved.append(loops.load_loop(manager.session_id))

    if timing == "legacy_entry":
        publish()
    elif timing == "load":
        read = db.get_meta
        def interleave_read(*args, **kwargs):
            monkeypatch.setattr(db, "get_meta", read)
            publish()
            return read(*args, **kwargs)
        monkeypatch.setattr(db, "get_meta", interleave_read)
    else:
        execute = db._execute_write
        def interleave(write, *args, **kwargs):
            monkeypatch.setattr(db, "_execute_write", execute)
            publish()
            return execute(write, *args, **kwargs)
        monkeypatch.setattr(db, "_execute_write", interleave)
    if operation in {"settle", "settle_noop"}:
        result = manager.complete_tick("[LOOP_COMPLETE]")
    elif operation == "replace":
        source = "Watch the new deployment instead"
        result = manager.replace(prompt=source, reason="New task", user_quote=source, user_messages=[source])
    else:
        result = manager.revise(reason="Fewer checks", times=0 if operation == "noop" else 3)
    assert result["error_code"] == "state_changed"
    after = loops.load_loop(manager.session_id)
    assert original.to_json() == before
    if operation == "settle" and timing != "pause_resume":
        assert after.interval_seconds == 600
        assert after.awaiting_response is False
        assert after.revisions == saved[0].revisions
    else:
        assert after.to_json() == saved[0].to_json()
    assert manager.state.to_json() == after.to_json()


@pytest.mark.parametrize("second_change", ["new_tick", "pause", "clear", "replacement"])
def test_conflict_cleanup_cannot_release_another_claim(hermes_home, monkeypatch, second_change):
    manager = loops.LoopManager("loop-claim")
    manager.set("Check the deployment", interval_seconds=300, until="healthy")
    assert manager.fire_tick()
    original = loops.load_loop(manager.session_id)
    db = loops._get_session_db()
    saved = []

    def judge(*args, **kwargs):
        writer = loops.LoopManager(manager.session_id)
        assert writer.revise(reason="Less polling", interval_seconds=600)["ok"]
        execute = db._execute_write
        def change_at_cleanup(write, *args, **kwargs):
            monkeypatch.setattr(db, "_execute_write", execute)
            control = loops.LoopManager(manager.session_id)
            if second_change == "new_tick":
                control.abandon_tick()
                # Re-use every legacy claim field. Only the new claim ID distinguishes it.
                monkeypatch.setattr(loops.time, "time", lambda: original.last_fired_at)
                control.state.next_due_at = original.last_fired_at
                assert control.fire_tick()
                assert control.state.ticks_fired == original.ticks_fired
                assert control.state.last_fired_at == original.last_fired_at
                assert control.state.tick_id != original.tick_id
            elif second_change == "pause":
                control.pause()
            elif second_change == "clear":
                control.clear()
            else:
                control.set("Different task", interval_seconds=600)
                assert control.fire_tick()
            saved.append(db.get_meta("loop:loop-claim"))
            return execute(write, *args, **kwargs)
        monkeypatch.setattr(db, "_execute_write", change_at_cleanup)
        return "done", "obsolete verdict", False, None, False

    monkeypatch.setattr(goals, "judge_goal", judge)
    result = manager.complete_tick("old answer")
    assert result["stopped"] is False
    assert result["message"] == ""
    assert saved and db.get_meta("loop:loop-claim") == saved[-1]
    assert manager.state.to_json() == saved[-1]


@pytest.mark.parametrize("operation", ["revise", "settle"])
@pytest.mark.parametrize("failure", ["read", "write", "unavailable", "ephemeral"])
def test_loop_persistence_failure_preserves_state(hermes_home, monkeypatch, caplog, operation, failure):
    manager = loops.LoopManager("" if failure == "ephemeral" else "loop-failure")
    manager.set("Check the deployment", interval_seconds=300)
    if operation == "settle":
        assert manager.fire_tick()
    original = manager.state
    before = original.to_json()
    db = loops._get_session_db()
    read = db.get_meta
    def fail(*args, **kwargs):
        raise OSError("disk unavailable")
    if failure == "read":
        monkeypatch.setattr(db, "get_meta", fail)
    elif failure == "write":
        monkeypatch.setattr(db, "_execute_write", fail)
    else:
        monkeypatch.setattr(loops, "_get_session_db", lambda: None)
    result = (manager.revise(reason="Less polling", interval_seconds=600) if operation == "revise"
              else manager.complete_tick("LOOP_COMPLETE"))
    if failure == "ephemeral":
        assert result.get("ok", result.get("stopped")) is True
        if operation == "settle":
            assert manager.state is not None
            assert manager.state.status == "done"
            assert manager.state.awaiting_response is False
            assert manager.state.ticks_fired == 1
        assert read("loop:") is None
    else:
        assert result["error_code"] == "persistence_failed"
        assert original.to_json() == before
        assert manager.state.to_json() == before
        assert read("loop:loop-failure") == before
        assert "could not read or commit" in caplog.text


@pytest.mark.parametrize("verdict", ["continue", "done", "blocked"])
@pytest.mark.parametrize("change", ["pause_resume", "pause_resume_refire", "abandon_refire"])
def test_same_manager_controls_cannot_mutate_the_settled_claim(hermes_home, monkeypatch, verdict, change):
    manager = loops.LoopManager("loop-shared-manager")
    manager.set("Check the deployment", interval_seconds=300, until="deployment healthy")
    assert manager.fire_tick()
    live_reference = manager.state
    initial = loops.LoopState.from_json(live_reference.to_json())
    # A new claim may re-use the old timestamp/count after abandon/refire.
    monkeypatch.setattr(loops.time, "time", lambda: initial.last_fired_at)
    saved = []

    def pause_resume():
        manager.pause()
        manager.resume()

    def refire():
        manager.state.next_due_at = initial.last_fired_at
        assert manager.fire_tick()

    def pause_resume_refire():
        pause_resume()
        refire()

    def abandon_refire():
        manager.abandon_tick()
        refire()

    def judge(*args, **kwargs):
        {"pause_resume": pause_resume, "pause_resume_refire": pause_resume_refire,
         "abandon_refire": abandon_refire}[change]()
        assert manager.state is live_reference
        if manager.state.awaiting_response:
            assert live_reference.tick_id != initial.tick_id
        saved.append(loops.load_loop(manager.session_id).to_json())
        return verdict, "old judgment", False, None, False

    monkeypatch.setattr(goals, "judge_goal", judge)
    result = manager.complete_tick("old answer")
    assert result["stopped"] is False
    assert result["error_code"] == "state_changed"
    assert result["message"] == ""
    assert saved and loops.load_loop(manager.session_id).to_json() == saved[-1]
    assert manager.state.to_json() == saved[-1]
    assert manager.state.awaiting_response is (change != "pause_resume")
