from pathlib import Path
from types import SimpleNamespace

import pytest

from gateway.copy_blocks import (
    CopyMarkerStreamFilter,
    copy_free_text_for,
    copy_markup_error,
    extract_copy_blocks,
    map_outside_copy_blocks,
    protect_inline_copy_bodies,
    render_copy_blocks_inline,
    split_copy_blocks_for,
    split_copy_blocks_protected,
    strip_copy_blocks,
)


INCIDENT = Path(__file__).with_name("fixtures").joinpath("repeated_copy_open.txt").read_text()
EXPECTED = "".join(line for line in INCIDENT.splitlines(keepends=True) if line.strip() != "[[copy]]")


def test_repeated_open_incident_keeps_draft_and_footnotes_inline():
    assert extract_copy_blocks(INCIDENT) == (EXPECTED, [])


@pytest.mark.parametrize("text, expected, reason", [
    ("before\r\n [[copy]] \r\n\r\nsecret  \r\n[[copy]]\r\nnext\r\n[[/copy]]\r\nafter",
     "before\r\n\r\nsecret  \r\nnext\r\nafter", "nested open marker"),
    ("before\n[[copy]]\nsecret  \n\n", "before\nsecret  \n\n", "unclosed copy block"),
    ("before\n[[/copy]]\nsecret\n", "before\nsecret\n", "stray close marker"),
    ("[[copy]]\nvalid\n[[/copy]]\n[[/copy]]\nsecret", "valid\nsecret", "stray close marker"),
    ("[[copy]]\nvalid\n[[/copy]]\n[[copy]]\nsecret", "valid\nsecret", "unclosed copy block"),
])
@pytest.mark.parametrize("entry", [
    "extract", "split", "protected_split", "protect", "strip", "copy_free", "map", "inline",
])
@pytest.mark.parametrize("platform", ["telegram", "discord"])
def test_invalid_contract_is_shared(text, expected, reason, entry, platform, caplog):
    adapter = SimpleNamespace(platform=platform)
    with caplog.at_level("WARNING", logger="gateway.copy_blocks"):
        if entry == "extract":
            assert extract_copy_blocks(text) == (expected, [])
        elif entry == "split":
            assert split_copy_blocks_for(adapter, text) == (expected, [])
        elif entry == "protected_split":
            assert split_copy_blocks_protected(adapter, text) == (expected, [], {})
        elif entry == "protect":
            assert protect_inline_copy_bodies(text) == (expected, {})
        elif entry == "strip":
            assert strip_copy_blocks(text) == expected
        elif entry == "copy_free":
            assert copy_free_text_for(adapter, text) == expected
        elif entry == "map":
            assert map_outside_copy_blocks(text, str.upper, keep_markers=True) == expected.upper()
        else:
            assert render_copy_blocks_inline(text) == expected
    warnings = [r for r in caplog.records if r.name == "gateway.copy_blocks"]
    assert len(warnings) == 1
    assert reason in warnings[0].message
    assert "secret" not in warnings[0].message
    assert copy_markup_error(text) == reason


@pytest.mark.parametrize("fence", ["```", "~~~~"])
def test_invalid_fallback_preserves_fenced_markers_inside_and_outside_body(fence):
    literal = f"{fence}\n[[copy]]\n[[/copy]]\n{fence}\n"
    text = literal + "[[copy]]\nbody\n" + literal + "[[copy]]\ntail\n"
    expected = literal + "body\n" + literal + "tail\n"
    assert extract_copy_blocks(text) == (expected, [])
    assert protect_inline_copy_bodies(text) == (expected, {})
    assert render_copy_blocks_inline(text) == expected


@pytest.mark.parametrize("size", [1, 2, 3, 8, 13, 2048])
def test_incident_inline_stream_matches_final_render(size):
    stream = CopyMarkerStreamFilter()
    actual = "".join(stream.feed(INCIDENT[i:i + size]) for i in range(0, len(INCIDENT), size))
    assert actual + stream.flush() == EXPECTED


@pytest.mark.parametrize("drop_bodies", [False, True])
def test_valid_block_can_span_tool_message_boundary(drop_bodies):
    stream = CopyMarkerStreamFilter(drop_bodies=drop_bodies)
    actual = stream.feed("before\n[[copy]]\nfirst\n")
    stream.message_boundary()
    actual += stream.feed("second\n[[/copy]]\nafter\n") + stream.flush()
    assert actual == ("before\nafter\n" if drop_bodies else "before\nfirst\nsecond\nafter\n")
    assert extract_copy_blocks("before\n[[copy]]\nfirst\nsecond\n[[/copy]]\nafter\n") == (
        "before\nafter\n", ["first\nsecond"])


@pytest.mark.asyncio
@pytest.mark.parametrize("text, expected", [
    (INCIDENT, EXPECTED),
    ("before\n[[copy]]\nbody", "before\nbody"),
    ("before\n[[/copy]]\n[[copy]]\nbody\n[[/copy]]", "before\nbody\n"),
])
async def test_telegram_final_reconciliation_restores_hidden_invalid_body(text, expected):
    from gateway.stream_consumer import GatewayStreamConsumer, StreamConsumerConfig
    from tests.gateway.test_stream_final_contract import _make_draft_adapter

    adapter = _make_draft_adapter()
    adapter.platform = "telegram"
    consumer = GatewayStreamConsumer(adapter, "D1", StreamConsumerConfig(cursor="", buffer_threshold=1))
    consumer.on_delta(text)
    # Drain the preview first, proving the body was hidden rather than never streamed.
    consumer._drain_queue()
    assert "[[copy]]" not in consumer._accumulated
    assert consumer._accumulated != expected
    consumer._already_sent = True
    consumer._record_turn_final_payload(consumer._accumulated)
    assert consumer.delivered_final_matches(text) is False
    consumer._already_sent = False
    consumer.finish(text)
    await consumer.run()
    assert adapter.send_calls[-1]["content"].rstrip("\n") == expected.rstrip("\n")
    assert consumer.delivered_final_matches(text) is True


def test_stream_replay_captures_hidden_partial_bytes_and_reset_clears_them():
    stream = CopyMarkerStreamFilter(drop_bodies=True, capture_hidden=True)
    assert stream.feed("before\n[[copy]]\nbody\n[[copy]]\nmore\n[[co") == "before\n"
    assert stream.flush() == ""
    assert stream.hidden_text == "body\nmore\n[[co"
    stream.reset()
    assert stream.hidden_text == ""
