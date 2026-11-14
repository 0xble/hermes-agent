"""Bot Mode never renders or relays a bare intentional-silence marker (#110782).

Silence is a delivery decision shared with the gateway (``gateway/response_filters``):
the assistant row stays persisted, only the outbound text is emptied; failed turns and
prose that merely mentions a marker are delivered unchanged.
"""

import contextlib
from types import SimpleNamespace

import tui_gateway.server as srv


def _turn(result):
    return SimpleNamespace(
        result=result, agent=SimpleNamespace(_session_title_hint="Bot Chat"), terminal_callback=None,
        receipt_committed=True, receipt_attempted=False, marker_key="", error_retained=False,
        error_detail="", prompt_text="ping",
    )


def test_live_bot_chat_completion_empties_marker_only_for_successful_turns(monkeypatch):
    monkeypatch.setattr(srv, "_get_usage", lambda _agent: {})
    monkeypatch.setattr(srv, "render_message", lambda _text, _cols: None)
    monkeypatch.setattr(srv, "_clear_inflight_turn", lambda _session: None)
    session = {"pending_title": None, "session_key": "k", "history_lock": contextlib.nullcontext(),
               "agent": SimpleNamespace(_session_title_hint="Bot Chat")}

    payload, _, status = srv._complete_turn_payload(session, _turn({"final_response": " *NO_REPLY* "}), None, 80)
    assert (status, payload["text"]) == ("complete", "")

    prose = "[SILENT] is mentioned here, but this is a real answer."
    payload, _, _ = srv._complete_turn_payload(session, _turn({"final_response": prose}), None, 80)
    assert payload["text"] == prose

    failed = {"final_response": "NO_REPLY", "error": "provider failed", "failed": True}
    payload, _, status = srv._complete_turn_payload(session, _turn(failed), None, 80)
    assert (status, payload["text"]) == ("error", "NO_REPLY")

    # A plain (non-Bot-Chat) desktop session keeps the marker: the gate is the canonical title.
    session["agent"] = SimpleNamespace(_session_title_hint="Scratch")
    monkeypatch.setattr(srv, "_session_live_title", lambda _s, _k: "Scratch")
    payload, _, _ = srv._complete_turn_payload(session, _turn({"final_response": "NO_REPLY"}), None, 80)
    assert payload["text"] == "NO_REPLY"


def test_bot_chat_receipt_still_names_the_saved_row_when_a_trailing_marker_is_dropped(monkeypatch):
    """The delivered copy drops the marker; the stored row keeps it, and the receipt must
    still bind this turn to that row."""
    from agent.context_compressor import _DB_PERSISTED_MARKER
    monkeypatch.setattr(srv, "_get_usage", lambda _agent: {})
    monkeypatch.setattr(srv, "render_message", lambda _text, _cols: None)
    monkeypatch.setattr(srv, "_clear_inflight_turn", lambda _session: None)
    session = {"pending_title": None, "session_key": "k", "history_lock": contextlib.nullcontext(),
               "agent": SimpleNamespace(_session_title_hint="Bot Chat")}
    raw = "Paris.\n\nNO_REPLY"
    rows = [{"role": "user", "content": "q", "_row_id": 7, _DB_PERSISTED_MARKER: True},
            {"role": "assistant", "content": raw, "_row_id": 8, _DB_PERSISTED_MARKER: True}]
    st = _turn({"final_response": raw, "messages": rows})
    st.agent._persist_user_message_idx = 0
    st.history, st.compression_count = [], None

    payload, _, _ = srv._complete_turn_payload(session, st, None, 80)

    assert payload["text"] == "Paris."
    assert payload["persisted_turn"]["final_assistant_row_id"] == 8


def test_live_bot_chat_stream_holds_back_partial_silence_marker(monkeypatch):
    """Mirror of stream_consumer's hold-back: a marker never reaches message.delta, prose that
    diverges from every marker is flushed intact once it diverges."""
    events = []
    monkeypatch.setattr(srv, "_emit", lambda event, _sid, payload=None: events.append((event, payload)))
    monkeypatch.setattr(srv, "_load_interim_assistant_messages", lambda: False)
    monkeypatch.setattr(srv, "_start_usage_ticker", lambda _sid, _agent: (SimpleNamespace(set=lambda: None), SimpleNamespace(join=lambda: None)))

    def _run(final, chunks):
        events.clear()

        def run_conversation(_message, **kwargs):
            for chunk in chunks:
                kwargs["stream_callback"](chunk)
            return {"final_response": final}

        agent = SimpleNamespace(_session_title_hint="Bot Chat", run_conversation=run_conversation)
        session = {"pending_title": None, "session_key": "k", "history_lock": contextlib.nullcontext(), "agent": agent}
        st = srv._TurnRun(agent=agent, one_turn_restore=None, terminal_callback=None, receipt_committed=True)
        srv._invoke_agent("sid", session, st, "ping", "ping", None, [], None, None)
        return [p["text"] for e, p in events if e == "message.delta"], (session.get("inflight_turn") or {}).get("assistant", "")

    assert _run("NO_REPLY", ["NO_", "REPLY"]) == ([], "")
    assert _run("NO way, here is the answer.", ["NO", " way,", " here is the answer."]) == (
        ["NO way,", " here is the answer."], "NO way, here is the answer.")


def test_live_bot_chat_stream_holds_back_loop_complete_marker(monkeypatch):
    events = []
    monkeypatch.setattr(srv, "_emit", lambda event, _sid, payload=None: events.append((event, payload)))
    monkeypatch.setattr(srv, "_load_interim_assistant_messages", lambda: False)
    monkeypatch.setattr(srv, "_start_usage_ticker", lambda _sid, _agent: (SimpleNamespace(set=lambda: None), SimpleNamespace(join=lambda: None)))

    def run_conversation(_message, **kwargs):
        for chunk in ["Done.\n", "LOOP_COM", "PLETE"]:
            kwargs["stream_callback"](chunk)
        return {"final_response": "Done.\nLOOP_COMPLETE"}

    agent = SimpleNamespace(_session_title_hint="Bot Chat", run_conversation=run_conversation)
    session = {"pending_title": None, "session_key": "k", "history_lock": contextlib.nullcontext(), "agent": agent}
    st = srv._TurnRun(agent=agent, one_turn_restore=None, terminal_callback=None, receipt_committed=True)
    srv._invoke_agent("sid", session, st, "ping", "ping", None, [], None, None)
    deltas = [p["text"] for e, p in events if e == "message.delta"]
    assert deltas == ["Done.\n"]


def test_live_stream_keeps_marker_inside_open_fence(monkeypatch):
    """A marker-looking line inside an unclosed code fence is content, not control text."""
    events = []
    monkeypatch.setattr(srv, "_emit", lambda event, _sid, payload=None: events.append((event, payload)))
    monkeypatch.setattr(srv, "_load_interim_assistant_messages", lambda: False)
    monkeypatch.setattr(srv, "_start_usage_ticker", lambda _sid, _agent: (SimpleNamespace(set=lambda: None), SimpleNamespace(join=lambda: None)))

    def run_conversation(_message, **kwargs):
        for chunk in ["```text\n", "LOOP_COMPLETE\n", "```\n"]:
            kwargs["stream_callback"](chunk)
        return {"final_response": "```text\nLOOP_COMPLETE\n```"}

    agent = SimpleNamespace(_session_title_hint="Bot Chat", run_conversation=run_conversation)
    session = {"pending_title": None, "session_key": "k", "history_lock": contextlib.nullcontext(), "agent": agent}
    st = srv._TurnRun(agent=agent, one_turn_restore=None, terminal_callback=None, receipt_committed=True)
    srv._invoke_agent("sid", session, st, "ping", "ping", None, [], None, None)
    streamed = "".join(p["text"] for e, p in events if e == "message.delta")
    assert "LOOP_COMPLETE" in streamed


def test_live_stream_hides_marker_after_cross_chunk_fence_close(monkeypatch):
    """A fence opened in one chunk and closed in the next; the trailing marker is control text."""
    events = []
    monkeypatch.setattr(srv, "_emit", lambda event, _sid, payload=None: events.append((event, payload)))
    monkeypatch.setattr(srv, "_load_interim_assistant_messages", lambda: False)
    monkeypatch.setattr(srv, "_start_usage_ticker", lambda _sid, _agent: (SimpleNamespace(set=lambda: None), SimpleNamespace(join=lambda: None)))

    def run_conversation(_message, **kwargs):
        for chunk in ["Example:\n```text\nLOOP_COMPLETE\n", "```\nLOOP_COMPLETE"]:
            kwargs["stream_callback"](chunk)
        return {"final_response": "Example:\n```text\nLOOP_COMPLETE\n```\nLOOP_COMPLETE"}

    agent = SimpleNamespace(_session_title_hint="Bot Chat", run_conversation=run_conversation)
    session = {"pending_title": None, "session_key": "k", "history_lock": contextlib.nullcontext(), "agent": agent}
    st = srv._TurnRun(agent=agent, one_turn_restore=None, terminal_callback=None, receipt_committed=True)
    srv._invoke_agent("sid", session, st, "ping", "ping", None, [], None, None)
    streamed = "".join(p["text"] for e, p in events if e == "message.delta")
    assert streamed.count("LOOP_COMPLETE") == 1


def _stream_turn(monkeypatch, chunks, *, tts_queue=None):
    import queue
    events = []
    monkeypatch.setattr(srv, "_emit", lambda event, _sid, payload=None: events.append((event, payload)))
    monkeypatch.setattr(srv, "_load_interim_assistant_messages", lambda: False)
    monkeypatch.setattr(srv, "_start_usage_ticker", lambda _sid, _agent: (SimpleNamespace(set=lambda: None), SimpleNamespace(join=lambda: None)))

    def run_conversation(_message, **kwargs):
        for chunk in chunks:
            kwargs["stream_callback"](chunk)
        return {"final_response": "".join(c for c in chunks if c)}

    agent = SimpleNamespace(_session_title_hint="Scratch", run_conversation=run_conversation)
    session = {"pending_title": None, "session_key": "k", "history_lock": contextlib.nullcontext(), "agent": agent}
    st = srv._TurnRun(agent=agent, one_turn_restore=None, terminal_callback=None, receipt_committed=True)
    st.tts_queue = tts_queue
    srv._invoke_agent("sid", session, st, "ping", "ping", None, [], None, None)
    return "".join(p["text"] for e, p in events if e == "message.delta" and p["text"])


def test_live_stream_new_message_starts_copy_detection_on_a_new_line(monkeypatch):
    streamed = _stream_turn(monkeypatch, ["Checking now.", None, "[[copy]]\nPaste exactly.\n[[/copy]]"])
    assert "[[" not in streamed and "Paste exactly." in streamed


def test_live_stream_voice_never_speaks_copy_bodies(monkeypatch):
    import queue
    spoken = queue.Queue()
    _stream_turn(monkeypatch, ["before\n[[copy]]\nPaste exactly.\n[[/copy]]\nafter"], tts_queue=spoken)
    items = []
    while not spoken.empty():
        items.append(spoken.get_nowait())
    said = "".join(i for i in items if isinstance(i, str))
    assert "Paste exactly." not in said and "before" in said and "after" in said


def test_live_stream_voice_restarts_copy_detection_per_message(monkeypatch):
    import queue
    spoken = queue.Queue()
    _stream_turn(monkeypatch, ["Checking now.", None, "[[copy]]\nPaste exactly.\n[[/copy]]"], tts_queue=spoken)
    items = []
    while not spoken.empty():
        items.append(spoken.get_nowait())
    said = "".join(i for i in items if isinstance(i, str))
    assert "Paste exactly." not in said and "[[" not in said and "Checking now." in said


def test_history_display_renders_copy_blocks_inline_without_touching_storage():
    stored = {"role": "assistant", "content": "before\n[[copy]]\nbody\n[[/copy]]\nafter"}
    shown = srv._history_to_messages([stored])
    assert [m["text"] for m in shown if m["role"] == "assistant"] == ["before\nbody\nafter"]
    assert stored["content"] == "before\n[[copy]]\nbody\n[[/copy]]\nafter"


def _spoken_text(monkeypatch, chunks, *, title="Scratch"):
    import queue
    spoken = queue.Queue()
    events = []
    monkeypatch.setattr(srv, "_emit", lambda event, _sid, payload=None: events.append((event, payload)))
    monkeypatch.setattr(srv, "_load_interim_assistant_messages", lambda: False)
    monkeypatch.setattr(srv, "_start_usage_ticker", lambda _sid, _agent: (SimpleNamespace(set=lambda: None), SimpleNamespace(join=lambda: None)))

    def run_conversation(_message, **kwargs):
        for chunk in chunks:
            kwargs["stream_callback"](chunk)
        return {"final_response": "".join(c for c in chunks if c)}

    agent = SimpleNamespace(_session_title_hint=title, run_conversation=run_conversation)
    session = {"pending_title": None, "session_key": "k", "history_lock": contextlib.nullcontext(), "agent": agent}
    st = srv._TurnRun(agent=agent, one_turn_restore=None, terminal_callback=None, receipt_committed=True)
    st.tts_queue = spoken
    srv._invoke_agent("sid", session, st, "ping", "ping", None, [], None, None)
    items = []
    while not spoken.empty():
        items.append(spoken.get_nowait())
    return "".join(i for i in items if isinstance(i, str))


def test_live_stream_voice_never_speaks_control_markers(monkeypatch):
    assert _spoken_text(monkeypatch, ["NO_", "REPLY"], title="Bot Chat") == ""
    said = _spoken_text(monkeypatch, ["Done.\n", "LOOP_COM", "PLETE"])
    assert "LOOP_COMPLETE" not in said and "Done." in said


def test_live_bot_chat_stream_never_shows_or_speaks_a_closing_silence_marker(monkeypatch):
    """A marker closing a real reply is held line-wise: no delta and no TTS chunk carries it,
    while a last line that only starts like one ("NO problem", "NOTE: done", "NO") arrives whole."""
    events = []
    monkeypatch.setattr(srv, "_emit", lambda event, _sid, payload=None: events.append((event, payload)))
    monkeypatch.setattr(srv, "_load_interim_assistant_messages", lambda: False)
    monkeypatch.setattr(srv, "_start_usage_ticker", lambda _sid, _agent: (SimpleNamespace(set=lambda: None), SimpleNamespace(join=lambda: None)))

    def _run(chunks):
        events.clear()
        spoken = []

        def run_conversation(_message, **kwargs):
            for chunk in chunks:
                kwargs["stream_callback"](chunk)
            return {"final_response": "".join(chunks)}

        agent = SimpleNamespace(_session_title_hint="Bot Chat", run_conversation=run_conversation)
        session = {"pending_title": None, "session_key": "k", "history_lock": contextlib.nullcontext(), "agent": agent}
        st = srv._TurnRun(agent=agent, one_turn_restore=None, terminal_callback=None, receipt_committed=True)
        st.tts_queue = SimpleNamespace(put=spoken.append)
        srv._invoke_agent("sid", session, st, "ping", "ping", None, [], None, None)
        return [p["text"] for e, p in events if e == "message.delta"], spoken

    for chunks in (["Paris.", "\n\nNO", "_RE", "PLY"], ["Paris.\n", "\n[SIL", "ENT]\n"]):
        deltas, spoken = _run(chunks)
        assert "".join(deltas).strip() == "Paris."
        assert "".join(spoken).strip() == "Paris."
        assert not any(c in "".join(deltas + spoken) for c in ("NO", "SIL", "["))

    for tail in ("NO problem", "NOTE: done", "NO"):
        chunks = ["Paris.", "\n\nNO", tail[2:]] if len(tail) > 2 else ["Paris.", "\n\nNO"]
        deltas, spoken = _run(chunks)
        assert "".join(deltas) == "".join(spoken) == f"Paris.\n\n{tail}"
