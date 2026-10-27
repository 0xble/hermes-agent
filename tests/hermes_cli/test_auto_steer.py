"""Auto deferral restores input chronology without retrying committed queue handoffs."""
import re
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from agent.steer_deferral import defer_message


def shell(monkeypatch, reenter=False):
    import cli
    monkeypatch.setattr(cli, "_cprint", Mock())
    app = object.__new__(cli.HermesCLI)
    app._init_ui_state()
    if reenter:
        from hermes_cli import plugins
        monkeypatch.setattr(plugins, "get_plugin_manager", lambda: SimpleNamespace())
        monkeypatch.setenv("HERMES_DEFER_AGENT_STARTUP", "1")
        app.config = {}
        app._tui_init_run_state()
    app._agent_running = True
    app.busy_input_mode = "auto"
    app._expand_paste_references = lambda text: text
    app.agent = SimpleNamespace(_current_turn_id="turn", _supports_active_turn_redirect=True,
                                redirect=Mock(return_value=True), steer=Mock(return_value=True), interrupt=Mock())
    return app


def auto_submit(app, text):
    app._tui_enter_while_busy(text, [], text)
    return re.search(r"Auto-steer message_id: ([a-f0-9-]+)", app.agent.redirect.call_args.args[0]).group(1)


@pytest.mark.parametrize("queue_first", [True, False])
@pytest.mark.parametrize("ordinary", ["explicit", "media", "voice"])
@pytest.mark.parametrize("reenter", [True, False])
def test_deferred_inputs_keep_submission_order_with_explicit_queue(monkeypatch, queue_first, ordinary, reenter):
    app = shell(monkeypatch, reenter=reenter)
    import cli
    images = ["image.png"]
    payload = {"media": ("C", images), "voice": cli._VoiceInputMessage("C"), "explicit": "C"}[ordinary]

    def enqueue_ordinary():
        if ordinary == "explicit":
            app._cmd_queue("/queue C")
        elif ordinary == "media":
            app._tui_enter_while_busy("C", images, payload)
        else:
            app._pending_input.put(payload)

    if queue_first:
        enqueue_ordinary()
    tokens = [auto_submit(app, text) for text in ("A", "B")]
    if not queue_first:
        enqueue_ordinary()
    for token in reversed(tokens):
        for _ in range(2):
            result = defer_message(app.agent, {"message_id": token})
            assert result["accepted"] and result["message_id"] == token
    expected = [payload, "A", "B"] if queue_first else ["A", "B", payload]
    assert app._pending_input_items() == expected
    observed = [app._pending_input.get_nowait() for _ in expected]
    assert observed == expected
    assert observed[0 if queue_first else 2] is payload or ordinary == "explicit"
    assert app._pending_input.empty()
    app.agent.interrupt.assert_not_called()


@pytest.mark.parametrize("refuse_once", [True, False])
def test_failed_ack_cannot_undo_or_repeat_a_committed_handoff(monkeypatch, refuse_once):
    import cli
    app = shell(monkeypatch)
    token = auto_submit(app, "original input")
    if refuse_once:
        with patch.object(app._pending_input, "put", side_effect=OSError("queue unavailable")):
            refused = defer_message(app.agent, {"message_id": token})
        assert not refused["accepted"] and refused["message_id"] == token
        assert app._pending_input.empty()
    # The captured presentation callback fails only after offer_auto_steer accepted the input.
    cli._cprint.side_effect = OSError("terminal closed")
    results = [defer_message(app.agent, {"message_id": token}) for _ in range(2)]
    observed = app._pending_input_items()
    assert all(result["accepted"] and result["message_id"] == token for result in results), (results, observed)
    assert observed == ["original input"]
    assert cli._cprint.call_count == 2  # initial steer status, then exactly 1 queue acknowledgement
    assert app._pending_input.get_nowait() == "original input"
    assert app._pending_input.empty()
