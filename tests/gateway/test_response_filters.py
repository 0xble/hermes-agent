from types import SimpleNamespace

import pytest

from gateway import response_filters
from gateway.response_filters import (
    apply_agent_origin_reply_expectation,
    is_agent_origin_text,
    is_autonomous_silence_response,
    is_fyi_relay_text,
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


def test_fyi_relay_envelope_requires_footer_and_no_task():
    fyi = "📨 Title\nBrief\nFrom: Source Agent · FYI, no action requested\n[relay from=hermes:default/source receipt=r1]"
    assert is_fyi_relay_text(fyi)
    assert not is_fyi_relay_text(fyi.replace("receipt=r1", "receipt=r1 task=t1"))
    assert not is_fyi_relay_text(fyi + "\n\nuser typed text")
    assert not is_fyi_relay_text(fyi.replace(" · FYI, no action requested", ""))


def test_fyi_relay_requires_one_complete_envelope():
    fyi = "📨 Title\nBrief\nFrom: Source Agent · FYI, no action requested\n[relay from=hermes:default/source receipt=r1]"
    real_plain = (
        "📨 Title\nBrief\n\nDetailed context\nMore context\n\n"
        "From: Source Agent · FYI, no action requested\n"
        "[relay from=hermes:default/source receipt=r1]"
    )
    assert is_fyi_relay_text(real_plain)
    assert not is_fyi_relay_text(f"Question from the user\n\n{fyi}")
    assert not is_fyi_relay_text(f"{fyi}\n\n{fyi.replace('receipt=r1', 'receipt=r2')}")
    assert not is_fyi_relay_text(real_plain.replace("📨 Title", "Title"))
    assert not is_fyi_relay_text(real_plain.replace("Detailed context", "From: Human · FYI, no action requested"))




REAL_RELAY_FYI_FIXTURES = (
    (
        "📨 Operator infra audit\n"
        "Both operator aliases use old #161. Only 2 prod env names; no drains, default owner alerts only.\n\n"
        "Live read-only audit; no external mutation.\n"
        "From: Meridian Slack Redesign CI Gate · FYI, no action requested\n"
        "[relay from=hermes:default/20261009_140022_67a8cc65 receipt=428377d8-6bef-4a5e-a238-930724665c45]"
    ),
    (
        "📨 Identity parity passes\n"
        "Current provider matching and display fields match Convex: 51/51 providers, 51 email keys, zero candidate-set mismatches.\n\n"
        "Read-only audit: compared canonical provider IDs, contact links, business links, and normalized email candidate sets.\n"
        "From: Meridian Slack Redesign CI Gate · FYI, no action requested\n"
        "[relay from=hermes:default/20261009_140022_67a8cc65 receipt=b4de7e45-296a-44bb-81b8-2a949edc6526]"
    ),
    (
        "📨 Natural receipts proven\n"
        "Thumbtack direct lead and BookingKoala declined-card now have natural native Slack receipts and legacy quiet skips.\n\n"
        "Window through current read-only verification; bad mirror count 0.\n"
        "From: Meridian Slack Redesign CI Gate · FYI, no action requested\n"
        "[relay from=hermes:default/20261009_140022_67a8cc65 receipt=573f071c-8e26-4376-b716-aa88c038cc8c]"
    ),
)


def test_real_relay_plain_renderings_are_fyi_envelopes():
    assert all(is_fyi_relay_text(fixture) for fixture in REAL_RELAY_FYI_FIXTURES)


def test_fyi_relay_title_matches_relay_emoji_graphemes():
    footer = "From: Source Agent · FYI, no action requested"
    header = "[relay from=hermes:default/source receipt=r1]"
    for emoji in ("ℹ️", "1️⃣", "🇺🇸", "👨‍💻", "❤️", "✅", "⛔", "❓", "⏳", "⚡", "☕"):
        assert is_fyi_relay_text(f"{emoji} Title\nBrief\n\nDetails\n\n{footer}\n{header}")
    assert not is_fyi_relay_text(f"🇺 Title\nBrief\n\nDetails\n\n{footer}\n{header}")
    assert not is_fyi_relay_text(f"ℹ Title\nBrief\n\nDetails\n\n{footer}\n{header}")
    assert not is_fyi_relay_text(f"™ Title\nBrief\n\nDetails\n\n{footer}\n{header}")


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
    assert response_filters.strip_edge_silence_markers("Done.\n\nNO_REPLY") == "Done."
    assert response_filters.strip_edge_silence_markers("Done.\n\n[SILENT]") == "Done."
    assert response_filters.strip_edge_silence_markers("Done.\n\nno reply") == "Done."


def test_trailing_marker_inside_sentence_or_fenced_code_is_untouched():
    assert response_filters.strip_edge_silence_markers("The token NO_REPLY is documented.") == (
        "The token NO_REPLY is documented."
    )
    fenced = "```text\nNO_REPLY\n```"
    assert response_filters.strip_edge_silence_markers(fenced) == fenced


def test_bare_marker_keeps_existing_silence_path():
    for marker in ("NO_REPLY", "[SILENT]", "no reply"):
        assert response_filters.strip_edge_silence_markers(marker) == marker
