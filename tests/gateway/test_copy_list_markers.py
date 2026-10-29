"""List decoration must not turn copy control lines into visible text."""

from types import SimpleNamespace

import pytest

from gateway.copy_blocks import (
    CopyMarkerStreamFilter,
    copy_free_text_for,
    extract_copy_blocks,
    map_outside_copy_blocks,
    protect_inline_copy_bodies,
    render_copy_blocks_inline,
    restore_inline_copy_bodies,
    split_copy_blocks_protected,
    strip_copy_blocks,
)


@pytest.mark.parametrize("prefix", ["", " \t", "1. ", "42) ", "  - ", "\t* ", "    + ", " • "])
@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_list_marker_boundaries_match_on_every_copy_surface(prefix, newline):
    # The reported numbered-list leak, including its bare closing marker.
    repro = f"Before{newline}{prefix}[[copy]]{newline}exact{newline}[[/copy]]{newline}After"
    assert extract_copy_blocks(repro) == (f"Before{newline}After", ["exact"])

    ordinary = (f"1. [[copy]] with explanation{newline}- ordinary list item{newline}"
                f"```{newline}{prefix}[[copy]]{newline}{prefix}[[/copy]]{newline}```{newline}")
    bodies = [f"- body {n}  {newline}```{newline}{prefix}[[copy]]{newline}```" for n in range(1, 6)]
    text = ordinary
    inline = ordinary
    for n, body in enumerate(bodies, 1):
        opener = f"{n}. " if prefix == "1. " else prefix
        text += f"{opener}[[copy]]{newline}{body}{newline}{prefix}[[/copy]]{newline}"
        inline += body + newline
    text += "After"
    inline += "After"
    remaining = ordinary + "After"
    assert extract_copy_blocks(text) == (remaining, bodies)
    assert strip_copy_blocks(text) == remaining
    assert render_copy_blocks_inline(text) == inline
    assert map_outside_copy_blocks(text, str.upper) == ordinary.upper() + "".join(body + newline for body in bodies) + "AFTER"
    protected, tokens = protect_inline_copy_bodies(text)
    assert restore_inline_copy_bodies(protected, tokens) == inline
    for platform, shown, copies in (("telegram", remaining, bodies), ("discord", inline, [])):
        adapter = SimpleNamespace(platform=platform)
        shown_protected, extracted, tokens = split_copy_blocks_protected(adapter, text)
        assert restore_inline_copy_bodies(shown_protected, tokens) == shown
        assert extracted == copies
        assert copy_free_text_for(adapter, text) == shown
    for drop_bodies, expected in ((True, remaining), (False, inline)):
        for size in (1, 2, 3, 7, len(text)):
            stream = CopyMarkerStreamFilter(drop_bodies=drop_bodies)
            shown = "".join(stream.feed(text[i:i + size]) for i in range(0, len(text), size))
            assert shown + stream.flush() == expected


@pytest.mark.parametrize("prefix", ["1. ", "  27) ", "- ", " * ", "+ ", "\t• "])
@pytest.mark.parametrize("close", ["[[/copy]]", "[[copy]]"])
def test_list_markers_at_message_boundaries_do_not_leak_or_swallow_text(prefix, close):
    stream = CopyMarkerStreamFilter(drop_bodies=True)
    assert stream.feed("Before") == "Before"
    stream.message_boundary()
    assert "".join(stream.feed(ch) for ch in prefix + "[[copy]]") == ""
    stream.message_boundary()
    assert stream.feed("exact\n") == ""
    assert "".join(stream.feed(ch) for ch in prefix + close) == ""
    stream.message_boundary()
    assert stream.feed("After") + stream.flush() == "After"
    # A genuinely partial marker, unlike a list token, still spans messages.
    stream = CopyMarkerStreamFilter(drop_bodies=True)
    assert stream.feed(prefix + "[[co") == ""
    stream.message_boundary()
    assert stream.feed("py]]\nexact\n[[/copy]]\nAfter") + stream.flush() == "After"

    # Incomplete or ordinary list text is released unchanged at end of response.
    for tail in (prefix, prefix + "[[co", prefix + "[[copy]] with explanation", "123", "-body"):
        stream = CopyMarkerStreamFilter(drop_bodies=True)
        assert "".join(stream.feed(ch) for ch in tail) + stream.flush() == tail


@pytest.mark.parametrize("tail", ["42", "-", "3.", "+", "•", " * ", " 27) ", "- 42", "1. 2", "* -", "1. ", "- ", " \t"])
@pytest.mark.parametrize("drop_bodies", [True, False])
def test_list_token_at_message_end_cannot_absorb_next_copy_marker(tail, drop_bodies):
    stream = CopyMarkerStreamFilter(drop_bodies=drop_bodies)
    shown = stream.feed("Count:\n" + tail)
    stream.message_boundary()
    text = "[[copy]]\nexact\n[[/copy]]\nAfter"
    shown += "".join(stream.feed(ch) for ch in text) + stream.flush()
    assert shown == "Count:\n" + tail + ("" if drop_bodies else "exact\n") + "After"

    # A message boundary followed by end-of-turn must release ordinary bytes once.
    stream.reset()
    shown = stream.feed(tail)
    stream.message_boundary()
    stream.message_boundary()
    assert shown + stream.flush() == tail
    assert stream.flush() == ""


def test_list_token_at_message_end_inside_copy_body_stays_hidden():
    stream = CopyMarkerStreamFilter(drop_bodies=True)
    assert stream.feed("[[copy]]\n42") == ""
    stream.message_boundary()
    assert stream.feed("[[/copy]]\nAfter") + stream.flush() == "After"
