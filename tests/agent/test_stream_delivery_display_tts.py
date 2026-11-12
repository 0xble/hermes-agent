"""Chat-display delivery tracking is independent of streaming TTS success."""

from typing import Any, Callable

from agent import chat_completion_helpers as helpers
from agent.stream_delivery import StreamDeliveryMixin


class _Agent(StreamDeliveryMixin):
    stream_delta_callback: Callable[..., Any] | None = None
    _stream_callback: Callable[..., Any] | None = None
    _hold_stream = False

    @staticmethod
    def _strip_think_blocks(text: str) -> str:
        return text


def _emit_text(call, text: str, monkeypatch):
    resets = []
    monkeypatch.setattr(helpers, "_reset_stale_streak", lambda agent: resets.append(agent))
    call._emit_text(text)
    return resets


def _streaming_call(agent):
    call = object.__new__(helpers._StreamingCall)
    call.agent = agent
    call.first_delta_fired = {"done": False}
    call.on_first_delta = None
    call.deltas_were_sent = {"yes": False}
    return call


def test_tts_only_visible_acceptance_clears_breaker_but_not_display_tracking(monkeypatch):
    agent = _Agent()
    agent.stream_delta_callback = lambda _text: (_ for _ in ()).throw(RuntimeError("display rejected"))
    agent._stream_callback = lambda _text: None

    resets = _emit_text(_streaming_call(agent), "answer", monkeypatch)

    assert resets == [agent]
    assert agent._current_streamed_assistant_text == "answer"
    assert agent._display_streamed_assistant_text == ""
    assert agent._interim_content_fully_streamed("answer") is False


def test_tts_only_text_is_retained_for_partial_stream_stub(monkeypatch):
    agent = _Agent()
    agent.stream_delta_callback = None
    agent._stream_callback = lambda _text: None
    call = _streaming_call(agent)
    call.result = {"partial_tool_names": []}
    call._partial_tool_accumulator = None
    _emit_text(call, "answer", monkeypatch)

    stub = call._partial_stream_stub(RuntimeError("stream ended"))

    assert stub.choices[0].message.content == "answer"
    assert agent._current_streamed_assistant_text == "answer"


def test_no_consumer_acceptance_does_not_clear_breaker(monkeypatch):
    agent = _Agent()

    resets = _emit_text(_streaming_call(agent), "answer", monkeypatch)

    assert resets == []
    assert agent._current_streamed_assistant_text == ""


def test_display_rejection_does_not_count_tts_success_as_chat_delivery():
    spoken = []

    def reject_display(_text):
        raise RuntimeError("display rejected text")

    def accept_tts(text):
        spoken.append(text)

    agent = _Agent()
    agent.stream_delta_callback = reject_display
    agent._stream_callback = accept_tts

    assert agent._deliver_to_stream_callbacks("answer") is False
    assert spoken == ["answer"]


def test_display_success_counts_even_when_tts_rejects():
    displayed = []

    def accept_display(text):
        displayed.append(text)

    def reject_tts(_text):
        raise RuntimeError("tts rejected text")

    agent = _Agent()
    agent.stream_delta_callback = accept_display
    agent._stream_callback = reject_tts

    assert agent._deliver_to_stream_callbacks("answer") is True
    assert displayed == ["answer"]


def test_display_rejection_plus_tts_success_does_not_record_chat_delivery():
    spoken = []

    def reject_display(_text):
        raise RuntimeError("display rejected text")

    agent = _Agent()
    agent.stream_delta_callback = reject_display
    agent._stream_callback = spoken.append

    agent._fire_stream_delta("answer")

    assert spoken == ["answer"]
    assert agent._interim_content_fully_streamed("answer") is False


def test_display_and_tts_success_records_chat_delivery():
    displayed, spoken = [], []
    agent = _Agent()
    agent.stream_delta_callback = displayed.append
    agent._stream_callback = spoken.append

    agent._fire_stream_delta("answer")

    assert displayed == spoken == ["answer"]
    assert agent._interim_content_fully_streamed("answer") is True


def test_whitespace_delta_stays_in_display_tracking():
    displayed = []
    agent = _Agent()
    agent.stream_delta_callback = displayed.append
    agent._stream_callback = lambda _text: None

    for chunk in ("Para one.", "\n\n", "Para two."):
        agent._fire_stream_delta(chunk)

    assert displayed == ["Para one.", "\n\n", "Para two."]
    assert agent._current_streamed_assistant_text == "Para one.\n\nPara two."
    assert agent._display_streamed_assistant_text == agent._current_streamed_assistant_text
    assert agent._interim_content_fully_streamed("Para one.\n\nPara two.") is True
