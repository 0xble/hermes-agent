"""Legacy command.dispatch binds the selected session, never a previous turn."""
import pytest

from gateway.session_context import bound_session_env, preserved_session_context, reset_session_vars, set_session_vars
from hermes_cli import goals
from hermes_state import SessionDB
from tui_gateway import server

PROOF = "pytest peer_artifact.py: 1 passed, artifact readback verified"


@pytest.mark.parametrize("running", [False, True])
def test_goal_peer_dispatch_uses_selected_parent_and_restores_caller(tmp_path, monkeypatch, running):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("goals:\n  autonomy: true\n")
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        db.create_session("parent", source="tui")
        monkeypatch.setattr(goals, "_get_session_db", lambda: db)
        mgr = goals.GoalManager("parent")
        mgr.set("Deliver the verified peer artifact")
        monkeypatch.setitem(server._sessions, "selected-view", {
            "session_key": "parent", "profile_home": str(tmp_path), "running": running})

        def dispatch(arg):
            return server._methods["command.dispatch"](1, {
                "session_id": "selected-view", "name": "goal", "arg": arg})

        with preserved_session_context():
            set_session_vars(session_id="prior-turn")
            reset_session_vars()
            instance = mgr.state.created_at
            reply = dispatch(f'peer wait {instance} hermes:other/peer existing-work "artifact readback"')
            assert reply["result"] == {"type": "exec", "output": "Peer work registered."}
            assert goals.load_goal("parent").pending_peers == [
                {"peer": "hermes:other/peer", "ref": "existing-work", "result": "artifact readback"}]
            assert bound_session_env("HERMES_SESSION_ID", "") == ""
            before = db.get_meta("goal:parent")
            assert "error" in dispatch(f'peer resolve {instance} hermes:other/peer existing-work "{PROOF}"')
            assert db.get_meta("goal:parent") == before
            db.append_message("parent", "tool", PROOF, tool_name="terminal")
            assert dispatch(f'peer resolve {instance} hermes:other/peer existing-work "{PROOF}"')["result"] == {
                "type": "exec", "output": "Peer result verified and resolved."}
            assert goals.load_goal("parent").pending_peers == []
            assert bound_session_env("HERMES_SESSION_ID", "") == ""
    finally:
        db.close()
