from gateway import response_filters
from gateway.response_filters import (
    is_agent_origin_text,
    is_autonomous_silence_response,
    is_intentional_silence_agent_result,
    is_intentional_silence_response,
)


def test_registered_agent_origin_headers_match_only_as_the_first_full_line():
    header = "[relay from=agent@example.com receipt=receipt-1]"
    assert is_agent_origin_text(header)
    assert is_agent_origin_text(f"  \n{header}\nbody")
    assert is_agent_origin_text("[relay from=agent@example.com receipt=receipt-1 task=task-1]\nbody")
    assert not is_agent_origin_text(f"body\n{header}")
    assert not is_agent_origin_text("[relay from=agent@example.com receipt=receipt-1")
    assert not is_agent_origin_text("[relay from=agent@example.com receipt=receipt-1 extra=value]" )
    assert not is_agent_origin_text("body\n[relay from=x receipt=y]\n\n[relay from=z receipt=q]")
    # Same value class as relay's own parser: a value may not contain "]".
    assert not is_agent_origin_text("[relay from=a] receipt=b]\nbody")
    assert is_agent_origin_text("[relay from=hermes:default/20261005_123248_1ad0e297 receipt=59666700-260a-4356-8533-8d889ca51de4]\nbody")


def test_exact_silence_tokens_are_intentional_silence():
    for token in ("[SILENT]", " SILENT ", "NO_REPLY", "no reply"):
        assert is_intentional_silence_response(token)


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
