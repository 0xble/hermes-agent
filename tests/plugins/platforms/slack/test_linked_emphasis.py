"""Emphasis must span opaque links in native rich-text containers."""

import pytest

from plugins.platforms.slack.block_kit import render_blocks


def _leaves(value):
    if isinstance(value, dict):
        if value.get("type") in ("text", "link") and isinstance(value.get("text"), str):
            yield value
        else:
            for child in value.values():
                yield from _leaves(child)
    elif isinstance(value, list):
        for child in value:
            yield from _leaves(child)


def test_reported_bold_link_labels_render_as_styles():
    md = (
        "- **Core change ([#800](https://github.com/0xble/hermes-agent/pull/800/changes)):** "
        "the new setting stays off unless turned on.\n"
        "- **Goal tool ([#1580](https://github.com/0xble/agents/pull/1580/changes)):** "
        "the tool the agent calls lives separately from Hermes's core\n"
        "- **Cards hidden:** disabled in other profiles"
    )
    items = render_blocks(md)[0]["elements"][0]["elements"]
    for item, label, number in zip(
        items[:2], ("Core change", "Goal tool"), ("800", "1580")
    ):
        elements = item["elements"]
        assert elements[0] == {
            "type": "text",
            "text": f"{label} (",
            "style": {"bold": True},
        }
        assert elements[1]["type"] == "link" and elements[1]["text"] == f"#{number}"
        assert elements[1]["style"] == {"bold": True}
        assert elements[2] == {"type": "text", "text": "):", "style": {"bold": True}}
        assert not elements[3].get("style")
    assert all("**" not in element["text"] for element in _leaves(items))
    assert items[2]["elements"][0]["style"] == {"bold": True}


@pytest.mark.parametrize(
    "container", ["- {}", "> {}", "| Name | Other |\n| --- | --- |\n| {} | plain |"]
)
@pytest.mark.parametrize(
    "link_template", ["[{label}]({url})", "<{url}|{label}>", "<{url}>"]
)
@pytest.mark.parametrize(
    "opening,closing,style",
    [
        ("**", "**", {"bold": True}),
        ("__", "__", {"bold": True}),
        ("_", "_", {"italic": True}),
        ("*", "*", {"italic": True}),
        ("~~", "~~", {"strike": True}),
        ("**_", "_**", {"bold": True, "italic": True}),
        ("~~**", "**~~", {"strike": True, "bold": True}),
        ("**~~", "~~**", {"bold": True, "strike": True}),
        ("~~_", "_~~", {"strike": True, "italic": True}),
    ],
)
def test_emphasis_spans_links_without_parsing_link_or_code_contents(
    container,
    link_template,
    opening,
    closing,
    style,
):
    url = "https://example.com/a_b_c/*d*"
    label = "**label** `a*b*c` _raw_"
    link = link_template.format(label=label, url=url)
    visible_label = label if "{label}" in link_template else url
    inner = f"before {link} and `a*b*c` after"
    code_literal = "[literal](https://example.com/a_b_c/*d*)"
    content = f"{opening}{inner}{closing} plain *neighbor* {link} and `{code_literal}`"
    # A pipe in a Markdown table cell must be escaped, including Slack's label separator.
    blocks = render_blocks(
        container.format(
            content.replace("|", r"\|") if container.startswith("|") else content
        )
    )
    if container.startswith("|"):
        assert blocks[0]["type"] == "table"
        blocks = blocks[0]["rows"][1][0]
    leaves = list(_leaves(blocks))
    wrapped = leaves[:5]
    assert (
        "".join(el["text"] for el in wrapped)
        == f"before {visible_label} and a*b*c after"
    )
    assert [el["style"] for el in wrapped] == [
        style,
        style,
        style,
        {**style, "code": True},
        style,
    ]
    assert wrapped[1]["type"] == "link" and wrapped[1]["url"] == url
    # Styles end at the closing delimiter, and markup inside labels/URLs/code stays literal.
    assert (
        "".join(el["text"] for el in leaves[5:])
        == f" plain neighbor {visible_label} and {code_literal}"
    )
    assert leaves[5].get("style", {}) == {}
    assert leaves[6]["style"] == {"italic": True}
    assert leaves[8]["type"] == "link" and leaves[8]["url"] == url
    assert leaves[8].get("style", {}) == {}
    assert leaves[-1]["style"] == {"code": True}


def test_literal_placeholder_characters_are_preserved_beside_links():
    literal = "\ue0000\ue001 \ue000\ue0001\ue001"
    elements = list(
        _leaves(render_blocks(f"- {literal} **[link](https://example.com)**"))
    )
    assert elements == [
        {"type": "text", "text": f"{literal} "},
        {
            "type": "link",
            "url": "https://example.com",
            "text": "link",
            "style": {"bold": True},
        },
    ]
