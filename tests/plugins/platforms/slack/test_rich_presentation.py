"""Native presentation syntax and conservative degradation at Slack's boundary."""

import pytest

from plugins.platforms.slack.block_kit import render_blocks, sanitize_blocks

URL = "https://example.com/photo.png"
CARDS = """```cards
- title: First
  subtitle: Today
  body: Ready
  image: https://example.com/photo.png
  url: https://example.com/details
- title: Second
  body: Waiting
```"""


def test_images_thumbnails_context_and_cards_in_reading_order():
    markdown = (
        f"![Landscape]({URL})\n\nParagraph\n![thumb:Portrait]({URL})\n\n"
        f"-# ![Source]({URL}) Updated today\n\n{CARDS}"
    )
    blocks = sanitize_blocks(render_blocks(markdown))
    assert [b["type"] for b in blocks] == ["image", "section", "context", "carousel"]
    assert blocks[0]["alt_text"] == "Landscape"
    assert blocks[0]["title"]["text"] == "Landscape"
    assert blocks[1]["text"]["text"] == "Paragraph"
    assert blocks[1]["accessory"]["alt_text"] == "Portrait"
    assert [e["type"] for e in blocks[2]["elements"]] == ["image", "mrkdwn"]
    assert blocks[2]["elements"][1]["text"] == "Updated today"
    cards = blocks[3]["elements"]
    assert [c["title"]["text"] for c in cards] == ["[First](https://example.com/details)", "Second"]
    assert cards[0]["hero_image"]["image_url"] == URL
    assert all("actions" not in card for card in cards)


@pytest.mark.parametrize("body", ["not: a list", "- title: First\n  unknown: Lost", "- title: First\n  image: http://example.com/x.png"])
def test_malformed_cards_remain_readable_code(body):
    blocks = render_blocks(f"```cards\n{body}\n```")
    assert blocks[0]["type"] == "rich_text"
    assert body in blocks[0]["elements"][0]["elements"][0]["text"]


def test_only_https_images_and_opaque_code():
    blocks = render_blocks(f"![Insecure](http://example.com/x.png)\n\n```text\n![Code]({URL})\n```")
    assert [b["type"] for b in blocks] == ["section", "rich_text"]
    assert "http://example.com/x.png" in blocks[0]["text"]["text"]
    assert "![Code]" in blocks[1]["elements"][0]["elements"][0]["text"]


def test_table_alignment_survives_rich_media():
    blocks = render_blocks(f"![Photo]({URL})\n\n| Left | Center | Right |\n|:---|:---:|---:|\n| a | b | c |")
    assert [b["type"] for b in blocks] == ["image", "table"]
    assert [c["align"] for c in blocks[1]["column_settings"]] == ["left", "center", "right"]


def test_new_payload_limits_are_sanitized_without_mutating_input():
    long = "x" * 4000
    card = {"type": "card", "title": {"type": "mrkdwn", "text": long}, "body": {"type": "mrkdwn", "text": long},
            "actions": [{"type": "button", "url": "https://example.com/details",
                         "text": {"type": "plain_text", "text": "Details"}}]}
    source = [
        {"type": "image", "image_url": URL, "alt_text": long, "title": {"type": "plain_text", "text": long}},
        {"type": "context", "elements": [{"type": "mrkdwn", "text": long}] * 12},
        {"type": "carousel", "elements": [card] * 11},
    ]
    blocks = sanitize_blocks(source)
    assert blocks is not None
    assert len(blocks[0]["alt_text"]) == 2000
    assert len(blocks[0]["title"]["text"]) == 2000
    assert len(blocks[1]["elements"]) == 10
    assert len(blocks[2]["elements"]) == 10
    assert len(blocks[2]["elements"][0]["title"]["text"]) == 150
    assert len(blocks[2]["elements"][0]["body"]["text"]) == 200
    assert all("actions" not in card for card in blocks[2]["elements"])
    assert "actions" in source[2]["elements"][0]
    assert source[0]["alt_text"] == long


def test_too_many_cards_preserve_every_record_in_code_fallback():
    body = "\n".join(f"- title: Record {n}" for n in range(11))
    blocks = render_blocks(f"```cards\n{body}\n```")
    assert blocks[0]["type"] == "rich_text"
    assert "Record 10" in str(blocks)
