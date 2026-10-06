from gateway.response_filters import (
    ends_with_partial_loop_complete_marker,
    strip_trailing_loop_complete_marker,
)


def test_loop_complete_display_filter_is_fence_aware():
    assert strip_trailing_loop_complete_marker("Done.\nLOOP_COMPLETE") == "Done."
    assert strip_trailing_loop_complete_marker("LOOP_COMPLETE") == ""
    assert strip_trailing_loop_complete_marker("Mention LOOP_COMPLETE in prose") == "Mention LOOP_COMPLETE in prose"
    fenced = "```text\nLOOP_COMPLETE\n```"
    assert strip_trailing_loop_complete_marker(fenced) == fenced


def test_loop_complete_partial_marker_only_matches_top_level_tail():
    assert ends_with_partial_loop_complete_marker("Done.\nLOOP_COM")
    assert ends_with_partial_loop_complete_marker("Done.\nLOOP_COMPLETE")
    assert not ends_with_partial_loop_complete_marker("```\nLOOP_COM")
    assert not ends_with_partial_loop_complete_marker("Mention LOOP_COM in prose")
