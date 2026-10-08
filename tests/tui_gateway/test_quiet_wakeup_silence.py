"""Heartbeat and /loop wakeups may end with a bare ``[SILENT]`` on a no-change tick.

The gateway suppresses that marker on machinery turns. The TUI/Desktop completion path and the
interactive CLI must hide it on wakeup turns too, while ordinary turns, failed turns and prose that
merely mentions the marker stay visible.
"""

import contextlib
import queue
from types import SimpleNamespace
from unittest.mock import MagicMock

import tui_gateway.server as srv
from hermes_cli.cli_chat_turn_mixin import CLIChatTurnMixin
from hermes_cli.heartbeat import HEARTBEAT_PROMPT_TEMPLATE
from hermes_cli.loops import WAKEUP_PROMPT_TEMPLATE, is_quiet_wakeup_prompt

LOOP_WAKEUP = WAKEUP_PROMPT_TEMPLATE.format(tick=2, cadence=" · every 5m", prompt="check the queue")
HEARTBEAT = HEARTBEAT_PROMPT_TEMPLATE.format(interval="30m", prompt="check the queue")


def test_wakeup_prompts_are_recognized_and_user_text_is_not():
    assert is_quiet_wakeup_prompt(LOOP_WAKEUP)
    assert is_quiet_wakeup_prompt(HEARTBEAT)
    assert not is_quiet_wakeup_prompt("check the queue")
    assert not is_quiet_wakeup_prompt(None)


# -- TUI / Desktop completion ------------------------------------------------------------------


def _turn(result, prompt_text):
    return SimpleNamespace(
        result=result, agent=SimpleNamespace(_session_title_hint="Scratch"), terminal_callback=None,
        receipt_committed=True, receipt_attempted=False, marker_key="", error_retained=False,
        error_detail="", prompt_text=prompt_text,
    )


def test_tui_completion_hides_bare_marker_only_on_successful_wakeup_turns(monkeypatch):
    monkeypatch.setattr(srv, "_get_usage", lambda _agent: {})
    monkeypatch.setattr(srv, "render_message", lambda _text, _cols: None)
    monkeypatch.setattr(srv, "_clear_inflight_turn", lambda _session: None)
    monkeypatch.setattr(srv, "_session_live_title", lambda _s, _k: "Scratch")
    session = {"pending_title": None, "session_key": "k", "history_lock": contextlib.nullcontext(),
               "agent": SimpleNamespace(_session_title_hint="Scratch")}

    for prompt in (LOOP_WAKEUP, HEARTBEAT):
        payload, raw, status = srv._complete_turn_payload(
            session, _turn({"final_response": "[SILENT]"}, prompt), None, 80)
        # Rendered text is empty; the raw reply still reaches the /loop hook (backoff, judge skip).
        assert (status, payload["text"], raw) == ("complete", "", "[SILENT]")

    changed = "Queue depth changed to 2."
    payload, _, _ = srv._complete_turn_payload(session, _turn({"final_response": changed}, LOOP_WAKEUP), None, 80)
    assert payload["text"] == changed

    failed = {"final_response": "[SILENT]", "error": "provider failed", "failed": True}
    payload, _, status = srv._complete_turn_payload(session, _turn(failed, LOOP_WAKEUP), None, 80)
    assert (status, payload["text"]) == ("error", "[SILENT]")

    # A typed message is not a wakeup: its bare marker stays visible.
    payload, _, _ = srv._complete_turn_payload(
        session, _turn({"final_response": "[SILENT]"}, "check the queue"), None, 80)
    assert payload["text"] == "[SILENT]"


def test_tui_wakeup_stream_never_shows_the_marker(monkeypatch):
    events = []
    monkeypatch.setattr(srv, "_emit", lambda event, _sid, payload=None: events.append((event, payload)))
    monkeypatch.setattr(srv, "_load_interim_assistant_messages", lambda: False)
    monkeypatch.setattr(srv, "_start_usage_ticker", lambda _sid, _agent: (
        SimpleNamespace(set=lambda: None), SimpleNamespace(join=lambda: None)))
    monkeypatch.setattr(srv, "_session_live_title", lambda _s, _k: "Scratch")

    def _run(prompt, final, chunks):
        events.clear()

        def run_conversation(_message, **kwargs):
            for chunk in chunks:
                kwargs["stream_callback"](chunk)
            return {"final_response": final}

        agent = SimpleNamespace(_session_title_hint="Scratch", run_conversation=run_conversation)
        session = {"pending_title": None, "session_key": "k", "history_lock": contextlib.nullcontext(), "agent": agent}
        st = srv._TurnRun(agent=agent, one_turn_restore=None, terminal_callback=None, receipt_committed=True)
        srv._invoke_agent("sid", session, st, prompt, prompt, None, [], None, None)
        return [p["text"] for e, p in events if e == "message.delta"]

    assert _run(LOOP_WAKEUP, "[SILENT]", ["[SIL", "ENT]"]) == []
    assert "".join(_run(LOOP_WAKEUP, "Queue is 2.", ["Queue", " is 2."])) == "Queue is 2."
    # Ordinary turns stream unchanged.
    assert _run("check the queue", "[SILENT]", ["[SIL", "ENT]"]) == ["[SIL", "ENT]"]


# -- interactive CLI ---------------------------------------------------------------------------


def _cli_stub(monkeypatch, *, quiet):
    from cli import HermesCLI
    import cli as climod

    cli = HermesCLI.__new__(HermesCLI)
    cli.show_reasoning = False
    cli.final_response_markdown = "raw"
    cli.show_timestamps = False
    cli._quiet_wakeup_turn = quiet
    cli._reset_stream_state()
    emitted = []
    monkeypatch.setattr(climod, "_cprint", lambda s: emitted.append(s))
    monkeypatch.setattr(climod, "_terminal_width_for_streaming", lambda: 74)
    monkeypatch.setattr(HermesCLI, "_scrollback_box_width", lambda self: 74)
    return cli, emitted


def test_cli_wakeup_stream_drops_a_bare_marker_and_streams_real_replies(monkeypatch):
    cli, emitted = _cli_stub(monkeypatch, quiet=True)
    cli._stream_delta("[SIL")
    cli._stream_delta("ENT]")
    cli._flush_stream()
    assert emitted == [] and cli._stream_box_opened is False

    cli, emitted = _cli_stub(monkeypatch, quiet=True)
    cli._stream_delta("[SIL")
    cli._stream_delta("ENT] noted, but the queue is 2.\n")
    cli._flush_stream()
    assert any("the queue is 2." in line for line in emitted)

    cli, emitted = _cli_stub(monkeypatch, quiet=False)
    cli._stream_delta("[SILENT]\n")
    cli._flush_stream()
    assert any("[SILENT]" in line for line in emitted)


class _RenderStub(CLIChatTurnMixin):
    def __init__(self, quiet):
        self._quiet_wakeup_turn = quiet
        self._interrupt_queue = queue.Queue()
        self._pending_input = queue.Queue()
        self._voice_tts = None
        self._voice_continuous = False
        self.agent = MagicMock(max_iterations=500)
        self.panels = []
        self._chat_print_reasoning_box = lambda turn: None
        self._chat_print_response_panel = lambda turn, response: self.panels.append(response)
        self._emit_focus_recovery_line = lambda: None
        self._ring_bell = lambda **kwargs: None


def _finished_turn(result):
    turn = MagicMock()
    turn.mute_notification_reply = False
    turn.result = result
    turn.use_streaming_tts = False
    return turn


def test_cli_wakeup_turn_renders_no_panel_for_a_bare_marker():
    ok = {"final_response": "[SILENT]", "completed": True}
    quiet = _RenderStub(quiet=True)
    assert quiet._chat_render_turn(_finished_turn(ok), MagicMock(), None) == ""
    assert quiet.panels == [""]

    typed = _RenderStub(quiet=False)
    typed._chat_render_turn(_finished_turn(ok), MagicMock(), None)
    assert typed.panels == ["[SILENT]"]

    failed = _RenderStub(quiet=True)
    failed._chat_render_turn(_finished_turn({"final_response": "[SILENT]", "failed": True}), MagicMock(), None)
    assert failed.panels == ["[SILENT]"]
