from gateway.side_notifications import (
    format_side_closed,
    format_side_queued,
    format_side_started,
    side_root_from_route,
    side_response_parts,
)


def test_side_queued_italicizes_waiting_hint():
    assert format_side_queued("Research flight options") == (
        '↗️ Side queued: "Research flight options"\n'
        "*Waiting for the current tool step to finish.*"
    )


def test_side_started_uses_existing_root_suffix_and_italic_hint():
    assert format_side_started("side_20260831_004611_3f45e1") == (
        "↗️ Side `3f45e1` started\n*Reply here to continue*"
    )


def test_side_response_frames_content_with_single_line_breaks_and_italic_hint():
    initial_text, final_suffix = side_response_parts("side_20260831_004611_3f45e1")

    assert initial_text == "↗️ Side `3f45e1`\n"
    assert final_suffix == "\n*Reply here to continue*"


def test_plain_side_notifications_do_not_emit_markdown_punctuation():
    assert format_side_queued("Research flight options", rich_text=False) == (
        '↗️ Side queued: "Research flight options"\n'
        "Waiting for the current tool step to finish."
    )
    assert (
        format_side_started("side_20260831_004611_3f45e1", rich_text=False)
        == "↗️ Side 3f45e1 started\nReply here to continue"
    )
    assert (
        format_side_closed("side_20260831_004611_3f45e1", rich_text=False)
        == "↗️ Side 3f45e1 closed."
    )

    prefix, suffix = side_response_parts("side_20260831_004611_3f45e1", rich_text=False)

    assert prefix == "↗️ Side 3f45e1\n"
    assert suffix == "\nReply here to continue"


def test_proxy_stream_delivery_requires_the_exact_framed_final():
    from types import SimpleNamespace

    from gateway.run import _stream_consumer_delivered_exact_final

    consumer = SimpleNamespace(
        final_response_sent=True,
        final_content_delivered=True,
        delivered_final_matches=lambda text: text == "framed final",
    )

    assert _stream_consumer_delivered_exact_final(consumer, "framed final") is True
    assert _stream_consumer_delivered_exact_final(consumer, "stale preview") is False
    consumer.delivered_final_matches = lambda _text: None
    assert _stream_consumer_delivered_exact_final(consumer, "framed final") is False
    assert (
        _stream_consumer_delivered_exact_final(
            SimpleNamespace(
                final_response_sent=True,
                final_content_delivered=True,
            ),
            "framed final",
        )
        is False
    )


def test_side_root_comes_from_existing_route_identity():
    assert (
        side_root_from_route(
            "agent:main:telegram:dm:1:2:side:side_20260831_004611_3f45e1"
        )
        == "side_20260831_004611_3f45e1"
    )


def test_side_closed_uses_existing_root_suffix():
    assert format_side_closed("side_20260831_004611_3f45e1") == (
        "↗️ Side `3f45e1` closed."
    )
