import itertools
from types import SimpleNamespace

import pytest

from gateway import response_filters
from gateway.response_filters import (
    apply_agent_origin_reply_expectation,
    is_agent_origin_text,
    is_autonomous_silence_response,
    is_intentional_silence_agent_result,
    is_intentional_silence_response,
)


def test_registered_agent_origin_headers_match_only_at_message_edges():
    header = "[relay from=agent@example.com receipt=receipt-1]"
    assert is_agent_origin_text(header)
    assert is_agent_origin_text(f"  \n{header}\nbody")
    assert is_agent_origin_text("[relay from=agent@example.com receipt=receipt-1 task=task-1]\nbody")
    assert is_agent_origin_text(f"body\n{header}")
    assert is_agent_origin_text(f"**Title**\n\n> quoted body\n> more quote\n\n{header}\n\n")
    assert not is_agent_origin_text(f"body\n{header}\nmore")
    assert not is_agent_origin_text("[relay from=agent@example.com receipt=receipt-1")
    assert not is_agent_origin_text("[relay from=agent@example.com receipt=receipt-1 extra=value]" )
    # Same value class as relay's own parser: a value may not contain "]".
    assert not is_agent_origin_text("[relay from=a] receipt=b]\nbody")
    assert is_agent_origin_text("[relay from=hermes:default/20261005_123248_1ad0e297 receipt=59666700-260a-4356-8533-8d889ca51de4]\nbody")

    addressed = SimpleNamespace(text=f"body\n{header}\n\n", reply_expected=True)
    assert apply_agent_origin_reply_expectation(addressed) is addressed
    assert addressed.reply_expected is True


def test_exact_silence_tokens_are_intentional_silence():
    for token in ("[SILENT]", " SILENT ", "NO_REPLY", "no reply"):
        assert is_intentional_silence_response(token)


@pytest.mark.parametrize(
    ("response", "expected"),
    [
        ("[SILENT]" + chr(10) + "LOOP_COMPLETE", True),
        ("NO_REPLY" + chr(10) + "LOOP_COMPLETE", True),
        ("LOOP_COMPLETE", False),
        ("Queue depth changed." + chr(10) + "LOOP_COMPLETE", False),
    ],
)
def test_silence_predicate_uses_display_text_without_loop_completion_marker(response, expected):
    assert is_intentional_silence_response(response) is expected


@pytest.mark.parametrize(
    ("response", "display"),
    [
        ("Update.\n[SILENT]\nLOOP_COMPLETE", "Update."),
        ("Update.\nLOOP_COMPLETE\n[SILENT]", "Update."),
        ("Update.\n\nNO_REPLY\n\nLOOP_COMPLETE\nLOOP_COMPLETE", "Update."),
        ("Update.\nLOOP_COMPLETE", "Update."),
        ("[SILENT]\nLOOP_COMPLETE\nLOOP_COMPLETE", "[SILENT]"),
        ("LOOP_COMPLETE\nLOOP_COMPLETE", ""),
        # Fenced control lines are content.
        ("```\n[SILENT]\nLOOP_COMPLETE\n```", "```\n[SILENT]\nLOOP_COMPLETE\n```"),
    ],
)
def test_trailing_control_run_is_stripped_as_one_unit(response, display):
    """A trailing run of silence and LOOP_COMPLETE lines, in any order, never leaks a marker."""
    assert response_filters.strip_trailing_control_markers(response) == display


def test_webhook_lane_keeps_autonomous_silence_and_strips_only_loop_complete():
    assert response_filters.strip_trailing_control_markers(
        "Update.\n[SILENT]\nLOOP_COMPLETE", strip_silence=False,
    ) == "Update.\n[SILENT]"


def test_silence_strip_removes_marker_from_a_run_that_ends_in_loop_complete():
    """The marker before a trailing LOOP_COMPLETE is stripped; the completion line stays for detection."""
    stripped = response_filters.strip_trailing_silence_marker("Update.\n[SILENT]\nLOOP_COMPLETE")
    assert stripped == "Update.\nLOOP_COMPLETE"


@pytest.mark.parametrize(
    "response",
    [
        "[SILENT]\nLOOP_COMPLETE\nLOOP_COMPLETE",
        "NO_REPLY\n\nLOOP_COMPLETE\nLOOP_COMPLETE\n",
        "[SILENT]\nLOOP_COMPLETE\nLOOP",
    ],
)
def test_partial_and_exact_silence_agree_on_repeated_loop_complete(response):
    assert response_filters.is_partial_silence_marker(response) is True
    if not response.endswith("LOOP"):
        assert is_intentional_silence_response(response) is True


def _hold_stream(chunks):
    """Everything a hold releases for ``chunks``, including the stream-end flush."""
    hold = {"buf": "", "held": ""}
    released = "".join(response_filters.hold_silence_delta(hold, d) for d in chunks)
    return released + response_filters.flush_silence_hold(hold)


def _chunkings(text, max_chunks=4):
    """Every split of ``text`` into 1..max_chunks non-empty contiguous chunks."""
    for count in range(1, max_chunks + 1):
        for cuts in itertools.combinations(range(1, len(text)), count - 1):
            bounds = (0, *cuts, len(text))
            yield [text[bounds[i]:bounds[i + 1]] for i in range(count)]


_SILENT_STREAMS = [
    "[SILENT]", "NO_REPLY", "[SILENT]\nLOOP_COMPLETE", "[SILENT]\n\nLOOP_COMPLETE",
    "NO_REPLY\n\nLOOP_COMPLETE\n",
]
_SILENT_STREAMS += ["\n\n" + s for s in _SILENT_STREAMS]  # first text after a tool round
_SPOKEN_STREAMS = [
    "NO\n\nhello", "Update.\n[SILENT]", "[SILENT] is a token", "NO_REPLY was the token\nLOOP soon",
]
_SPOKEN_STREAMS += ["\n\n" + s for s in _SPOKEN_STREAMS]


@pytest.mark.parametrize("text", _SILENT_STREAMS)
def test_hold_never_releases_a_silent_response_under_any_chunking(text):
    """Streaming TTS: no split of a silent response into 1-4 chunks speaks a control marker."""
    for chunks in _chunkings(text):
        released = _hold_stream(chunks)
        assert not any(m in released for m in ("SILENT", "NO_REPLY", "LOOP_COMPLETE")), (chunks, released)


@pytest.mark.parametrize("text", _SPOKEN_STREAMS)
def test_hold_releases_spoken_text_exactly_under_any_chunking(text):
    for chunks in _chunkings(text):
        assert _hold_stream(chunks) == text, chunks


@pytest.mark.parametrize("marker", ("NO_REPLY", "[SILENT]"))
def test_hold_rearms_after_break_before_a_fresh_marker(marker):
    """A held prefix is content once a new segment starts; the fresh marker is held again."""
    assert _hold_stream(["NO", "\n\n", marker]) == "NO"


@pytest.mark.parametrize("chunks", [
    ["\n\n[SILENT]\n\n", "LOOP_COMPLETE"],
    ["\n\n[", "SILENT", "]\n\n", "LOOP", "_COMPLETE"],
    ["\n\nNO_REPLY\n\n", "LOOP_COMPLETE"],
    ["\n\n[SILENT]", "\n\n", "LOOP_COMPLETE"],
    ["[SILENT]", "\n\n", "LOOP_COMPLETE"],
])
def test_hold_keeps_a_marker_after_a_tool_round_break(chunks):
    """The agent prefixes the first post-tool delta with a paragraph break; it must not unhold."""
    hold = {"buf": "", "held": ""}
    assert [response_filters.hold_silence_delta(hold, d) for d in chunks] == [""] * len(chunks)
    assert response_filters.flush_silence_hold(hold) == ""


def test_hold_still_releases_a_held_prefix_when_the_next_segment_is_prose():
    assert _hold_stream(["Ok", "\n\nQueue is 2."]) == "Ok\n\nQueue is 2."
    assert _hold_stream(["NO", "\n\nmore text"]) == "NO\n\nmore text"


def test_autonomous_silence_accepts_marker_with_own_line_note():
    """The loose rule for cron/webhook lanes: marker + explanation suppresses."""
    assert is_autonomous_silence_response("[SILENT]")
    assert is_autonomous_silence_response("[SILENT]\n\nNothing new this tick.")
    assert is_autonomous_silence_response("2 deals filtered\n\n[SILENT]")
    assert is_autonomous_silence_response("no_reply\nduplicate inbound, already handled")
    assert is_autonomous_silence_response("[SILENT] No changes detected")


def test_translated_sentinel_is_silence_in_every_form_the_english_one_is():
    """#110935: a lane that answers the cron instruction in its own language translates the
    sentinel; ``[静默]`` must suppress delivery exactly like ``[SILENT]`` (exact, own-line note,
    reordered lines, bracketless, edge punctuation)."""
    assert is_intentional_silence_response("[静默]")
    assert is_intentional_silence_response("**沉默**")
    assert is_autonomous_silence_response("[静默]\n\nNothing new this tick.")
    assert is_autonomous_silence_response("2 deals filtered\n\n[沉默]")
    assert is_autonomous_silence_response("静默")


def test_prose_mentioning_the_translated_sentinel_is_delivered():
    assert not is_intentional_silence_response("status: 静默 means the lane is quiet")
    assert not is_autonomous_silence_response("the lane said 静默 mid-sentence and kept talking")


def test_autonomous_lane_agrees_with_interactive_lane_on_cjk_punctuation_variants():
    """A Chinese lane emits fullwidth brackets or a trailing ``。``; cron/webhook must suppress
    exactly what the interactive predicate suppresses, or the two lanes drift on the new tokens."""
    for variant in ("【静默】", "静默。", "【沉默】", "沉默。", "**[静默]**", "NO_REPLY."):
        assert is_intentional_silence_response(variant)
        assert is_autonomous_silence_response(variant) == is_intentional_silence_response(variant), variant


def test_trailing_standalone_marker_is_removed_for_substantive_interactive_reply():
    assert response_filters.strip_trailing_silence_marker("Done.\n\nNO_REPLY") == "Done."
    assert response_filters.strip_trailing_silence_marker("Done.\n\n[SILENT]") == "Done."
    assert response_filters.strip_trailing_silence_marker("Done.\n\nno reply") == "Done."


def test_trailing_marker_inside_sentence_or_fenced_code_is_untouched():
    assert response_filters.strip_trailing_silence_marker("The token NO_REPLY is documented.") == (
        "The token NO_REPLY is documented."
    )
    fenced = "```text\nNO_REPLY\n```"
    assert response_filters.strip_trailing_silence_marker(fenced) == fenced


def test_bare_marker_keeps_existing_silence_path():
    for marker in ("NO_REPLY", "[SILENT]", "no reply"):
        assert response_filters.strip_trailing_silence_marker(marker) == marker
