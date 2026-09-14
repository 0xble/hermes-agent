"""Distinct user controls must invalidate turns across process boundaries."""
import multiprocessing
import os


def _advance_after_read(home, session_id, ready, release, results):
    os.environ["HERMES_HOME"] = home
    from hermes_cli import goals
    db = goals._get_session_db()
    original = db.get_meta
    paused = False

    def synchronized_read(key):
        nonlocal paused
        value = original(key)
        if key == goals._goal_control_revision_key(session_id) and not paused:
            paused = True
            ready.put(int(value or 0))
            if not release.wait(20):
                raise TimeoutError("revision test parent did not release writer")
        return value

    db.get_meta = synchronized_read
    results.put(goals.advance_goal_control_revision(session_id))


def test_two_process_controls_cannot_reuse_captured_revision(tmp_path, monkeypatch):
    from hermes_cli import goals
    home = tmp_path / "profile"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    session_id = "cross-process-controls"
    db = goals._get_session_db()
    db.set_meta(goals._goal_control_revision_key(session_id), "0")
    context = multiprocessing.get_context("spawn")
    ready, results = context.Queue(), context.Queue()
    releases = [context.Event(), context.Event()]
    writers = [context.Process(target=_advance_after_read,
               args=(str(home), session_id, ready, release, results)) for release in releases]
    try:
        for writer in writers:
            writer.start()
        # Both real DB reads see the same baseline before either writer may publish.
        assert [ready.get(timeout=20), ready.get(timeout=20)] == [0, 0]
        releases[0].set()
        first = results.get(timeout=20)
        captured = goals.get_goal_control_revision(session_id)
        assert captured == first
        releases[1].set()
        second = results.get(timeout=20)
        for writer in writers:
            writer.join(timeout=20)
            assert writer.exitcode == 0
        assert second > first
        assert goals.get_goal_control_revision(session_id) == second
        with goals.guard_goal_activation(session_id, captured) as authorized:
            assert not authorized
    finally:
        for release in releases:
            release.set()
        for writer in writers:
            if writer.is_alive():
                writer.terminate()
            writer.join(timeout=5)
        ready.close()
        results.close()
