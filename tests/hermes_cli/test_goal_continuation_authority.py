"""Classic CLI continuations carry provenance into their worker thread."""

from __future__ import annotations

import json
import queue
import threading
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from hermes_cli import goals
from hermes_cli.cli_chat_turn_mixin import CLIChatTurnMixin
from tools import goal_tool  # noqa: F401 -- register the real goal tool
from tools.goal_authority import InternalGoalPrompt
from tools.registry import registry


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    goals._DB_CACHE.clear()
    yield
    goals._DB_CACHE.clear()


def _invoke(session_id, action, *, user_requested=False, user_task="", **args):
    return json.loads(registry.dispatch(
        "set_goal", {"action": action, "user_requested": user_requested, **args}, session_id=session_id,
        turn_id="turn", goal_control_revision=goals.get_goal_control_revision(session_id),
        user_requested=user_requested, user_task=user_task,
    ))


def _enqueue_real_continuation():
    """Exercise the real post-turn continuation enqueue path."""
    from cli import HermesCLI

    session_id = "enqueue"
    cli = HermesCLI.__new__(HermesCLI)
    cli._pending_input = queue.Queue()
    cli._last_turn_interrupted = False
    cli.conversation_history = [{"role": "assistant", "content": "more work remains"}]
    cli.session_id = session_id
    cli.agent = MagicMock(session_id=session_id)
    cli._goal_manager = goals.GoalManager(session_id, default_max_turns=5)
    cli._goal_manager.set("Finish the parser")
    with patch("hermes_cli.goals.judge_goal", return_value=("continue", "more", False, None, False)):
        cli._maybe_continue_goal_after_turn()
    return cli._pending_input.get_nowait()


def _run_queued_turn(session_id, queued, action, *, user_requested, user_task, **args):
    """Drain the CLI FIFO and run the actual agent-worker boundary in a thread."""
    pending = queue.Queue()
    pending.put(queued)
    message = pending.get_nowait()
    observed = {}

    class Agent:
        def run_conversation(self, **kwargs):
            observed["message"] = kwargs["user_message"]
            observed["result"] = _invoke(
                session_id, action, user_requested=user_requested, user_task=user_task, **args,
            )
            return {"final_response": "done"}

    cli = SimpleNamespace(
        agent=Agent(), session_id=session_id,
        conversation_history=[{"role": "user", "content": str(message)}],
        _sudo_password_callback=None, _approval_callback=None,
        _secret_capture_callback=None, _pending_turn_reasoning_config=None,
        _pending_moa_config=None, _pending_one_turn_model_restore=None,
        _flush_credit_notices=lambda: None,
    )
    turn = SimpleNamespace(voice_prefix="", stream_callback=None, result=None)
    def chat(text, *, images=None, voice_input=False, internal_goal_continuation=False):
        worker = threading.Thread(
            target=CLIChatTurnMixin._chat_run_agent,
            args=(cli, turn, text, internal_goal_continuation),
        )
        worker.start()
        worker.join(timeout=10)
        assert not worker.is_alive()

    # Exercise the production drain/normalization boundary rather than deriving
    # its internal flag in the test. Normalization intentionally loses str type.
    cli.chat = chat
    cli._tui_unwrap_input = lambda text: (str(text), False, False)
    cli._typed_voice_stop = lambda text: False
    cli._pending_resume_sessions = []
    cli.handle_bang_shell = lambda text: False
    cli._print_user_message_preview = lambda text: None
    cli._turn_summary_begin = lambda: None
    cli._app = SimpleNamespace(invalidate=lambda: None)
    cli._tui_after_turn = lambda: None
    from cli import HermesCLI
    HermesCLI._tui_process_one_input(cli, message)
    return observed


def test_synthetic_continuation_cannot_release_stop_but_autonomous_pause_still_works():
    stopped = "stopped"
    goals.GoalManager(stopped).set("Finish the parser")
    goals.GoalManager(stopped).pause(user_requested=True)

    continuation = _enqueue_real_continuation()
    assert isinstance(continuation, InternalGoalPrompt)
    denied = _run_queued_turn(
        stopped, continuation, "resume",
        user_requested=True, user_task="Please continue working.",
    )
    assert denied["result"]["error_code"] == "user_direction_required"
    assert goals.load_goal(stopped).user_stopped
    assert denied["message"].startswith("[Continuing toward")

    autonomous = "autonomous"
    goals.GoalManager(autonomous).set("Finish the parser")
    paused = _run_queued_turn(
        autonomous, InternalGoalPrompt("[Continuing toward your standing goal]"), "pause",
        user_requested=False, user_task="Please continue working.", reason="waiting on CI",
    )
    assert paused["result"]["success"] is True
    state = goals.load_goal(autonomous)
    assert state is not None and state.status == "paused" and not state.user_stopped


def test_later_real_message_can_continue_after_synthetic_turn_scope_is_closed():
    session_id = "later-real"
    goals.GoalManager(session_id).set("Finish the parser")
    goals.GoalManager(session_id).pause(user_requested=True)

    _run_queued_turn(
        session_id, InternalGoalPrompt("[Continuing toward your standing goal]"), "resume",
        user_requested=True, user_task="continue",  # Must remain denied despite nonempty fallback.
    )
    real = _run_queued_turn(
        session_id, "Continue working on it.", "resume",
        user_requested=True, user_task="Continue working on it.",
    )
    assert real["result"]["success"] is True
    state = goals.load_goal(session_id)
    assert state is not None and state.status == "active" and not state.user_stopped
