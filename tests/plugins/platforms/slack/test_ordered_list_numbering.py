"""Ordered list starts survive Slack rich-text run boundaries."""

from plugins.platforms.slack.block_kit import render_blocks, sanitize_blocks


def _displayed_ordered_items(markdown):
    blocks = sanitize_blocks(render_blocks(markdown))
    assert blocks is not None
    displayed = []
    for block in blocks:
        for element in block.get("elements", []):
            if element.get("type") != "rich_text_list":
                continue
            if element["style"] == "bullet":
                assert "offset" not in element
                continue
            for number, section in enumerate(
                element["elements"], start=element.get("offset", 0) + 1
            ):
                text = "".join(part.get("text", "") for part in section["elements"])
                displayed.append((element["indent"], number, text))
    return displayed


def test_written_numbering_survives_paragraph_bullet_and_nesting_boundaries():
    items = [
        (0, 1, "first"),
        (0, 2, "second"),
        (1, 1, "nested first"),
        (1, 2, "nested second"),
        (0, 3, "third"),
        (0, 4, "fourth"),
        (0, 5, "fifth"),
        (1, 3, "nested resumed"),
        (0, 6, "sixth"),
    ]
    separators = {4: "Paragraph.\n\n", 5: "- bullet\n\n"}
    markdown = "\n\n".join(
        separators.get(index, "") + "  " * indent + f"{number}. {text}"
        for index, (indent, number, text) in enumerate(items)
    )
    assert _displayed_ordered_items(markdown) == items


def test_each_run_uses_its_written_start_instead_of_guessing_continuation():
    for start in (2, 7, 23):
        markdown = (
            f"{start}) **first**\n"
            "   continued\n\n"
            f"{start + 1}) second\n"
            "  4) nested\n"
            "1) deliberately restarted\n\n"
            "Paragraph.\n\n"
            "12) independent list"
        )
        assert _displayed_ordered_items(markdown) == [
            (0, start, "first continued"),
            (0, start + 1, "second"),
            (1, 4, "nested"),
            (0, 1, "deliberately restarted"),
            (0, 12, "independent list"),
        ]
