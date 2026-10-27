"""Images, subtext and peer cards for the opt-in markdown renderer.

Cards use a fenced ``cards`` YAML list with title, subtitle, body, image and url
keys. URLs are ordinary title links because the sender has no button handler.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

import hermes_yaml as yaml

IMAGE_RE = re.compile(r"!\[([^\]\n]*)\]\((https://[^\s)]+)\)")


def extract_attachment_images(content: str, extract, native_image_lines: set[int]):
    """Shield only renderer-owned image occurrences from the legacy extractor.

    The renderer supplies source-line ownership, including its list/quote/fence
    boundaries. URLs alone cannot identify occurrences: a native image and an
    attachment can legitimately use the same URL.
    """
    prefix = "\x00HERMES_NATIVE_IMAGE"
    while prefix in content:
        prefix += "_"
    saved = {}
    lines = []
    for index, line in enumerate(content.split("\n")):
        match = IMAGE_RE.search(line) if index in native_image_lines else None
        if match:
            key = f"{prefix}{len(saved)}\x00"
            saved[key] = match[0]
            line = line.replace(match[0], key, 1)
        lines.append(line)
    images, cleaned = extract("\n".join(lines))
    for key, value in saved.items():
        cleaned = cleaned.replace(key, value)
    return images, cleaned


def https_url(value, limit: int = 3000) -> bool:
    if not isinstance(value, str) or len(value) > limit or any(c.isspace() for c in value):
        return False
    parsed = urlsplit(value)
    return parsed.scheme == "https" and bool(parsed.netloc)


def image_element(url: str, alt: str) -> dict:
    return {"type": "image", "image_url": url, "alt_text": alt or "Image"}


def image_line(line: str):
    match = IMAGE_RE.fullmatch(line.strip())
    if match and https_url(match[2]):
        return match
    return None


def presentation_line(line: str, fmt) -> dict | None:
    """Standalone images and ``-#`` context, leaving ordinary lines alone."""
    stripped = line.strip()
    if stripped.startswith("-# "):
        text = stripped[3:].strip()
        elements = []
        icon = IMAGE_RE.match(text)
        if icon and https_url(icon[2]):
            elements.append(image_element(icon[2], icon[1]))
            text = text[icon.end():].strip()
        if text:
            elements.append({"type": "mrkdwn", "text": fmt(text)})
        return {"type": "context", "elements": elements} if elements else None
    match = image_line(line)
    if match:
        alt = match[1].removeprefix("thumb:")
        block = image_element(match[2], alt)
        if alt:
            block["title"] = {"type": "plain_text", "text": alt}
        return block
    return None


def emit_presentation(blocks: list, block: dict, line: str, para_open: bool, flush, image_lines: set[int],
                      index: int) -> None:
    """Emit a presentation line: a paragraph-final thumbnail becomes its section's accessory.

    ``flush`` closes the open paragraph first; ``image_lines`` records source lines whose
    image the renderer owns, so the attachment extractor leaves them alone.
    """
    accessory = thumbnail(line) if para_open else None
    flush()
    if accessory and blocks and blocks[-1]["type"] == "section":
        blocks[-1]["accessory"] = accessory
    else:
        blocks.append(block)
    if block["type"] == "image" or any(
            element.get("type") == "image" for element in block.get("elements", [])):
        image_lines.add(index)


def fenced_block(info: str, text: str, fmt, fallback) -> dict:
    """A carousel for a valid ``cards`` fence, else ``fallback(text)`` (preformatted code)."""
    cards = cards_block(text, fmt) if info.strip() == "cards" else None
    return cards or fallback(text)


def record_native_images(out: set[int] | None, image_lines: set[int]) -> None:
    """Publish renderer-owned image lines to the caller's optional output set."""
    if out is not None:
        out.update(image_lines)


def thumbnail(line: str) -> dict | None:
    match = image_line(line)
    if match and match[1].startswith("thumb:"):
        return image_element(match[2], match[1][6:])
    return None


def cards_block(text: str, fmt) -> dict | None:
    """Decline malformed/oversized YAML rather than silently dropping records."""
    try:
        records = yaml.safe_load(text)
    except yaml.YAMLError:
        return None
    if not isinstance(records, list) or not 1 <= len(records) <= 10:
        return None
    cards = []
    for record in records:
        card = _card(record, fmt)
        if card is None:
            return None
        cards.append(card)
    return {"type": "carousel", "elements": cards}


def _card(record, fmt) -> dict | None:
    if not isinstance(record, dict) or set(record) - {"title", "subtitle", "body", "image", "url"}:
        return None
    if not isinstance(record.get("title"), str) or not record["title"].strip():
        return None
    for key in ("image", "url"):
        if key in record and not https_url(record[key]):
            return None
    card = {"type": "card"}
    for key, limit in (("title", 150), ("subtitle", 150), ("body", 200)):
        value = record.get(key, "")
        if not isinstance(value, str):
            return None
        if value:
            if key == "title" and "url" in record:
                value = f"[{value}]({record['url']})"
            rendered = fmt(value)
            if len(rendered) > limit:
                return None
            card[key] = {"type": "mrkdwn", "text": rendered}
    if "image" in record:
        card["hero_image"] = image_element(record["image"], record["title"])
    return card


def sanitize_image(element, *, block: bool = False) -> dict | None:
    if not isinstance(element, dict) or not https_url(element.get("image_url")):
        return None
    result = dict(element)
    result["alt_text"] = str(element.get("alt_text") or "Image")[:2000]
    if block and "title" in result:
        result["title"] = _text(result["title"], 2000)
        if result["title"] is None:
            result.pop("title")
    return result


def _text(element, limit: int) -> dict | None:
    if not isinstance(element, dict) or element.get("type") not in ("mrkdwn", "plain_text"):
        return None
    text = element.get("text")
    if not isinstance(text, str) or not text.strip():
        return None
    return {**element, "text": text[:limit]}


def sanitize_context(block: dict) -> dict | None:
    elements = []
    for element in (block.get("elements") or [])[:10]:
        if not isinstance(element, dict):
            continue
        clean = sanitize_image(element) if element.get("type") == "image" else _text(element, 3000)
        if clean:
            elements.append(clean)
    return {**block, "elements": elements} if elements else None


def sanitize_card(card) -> dict | None:
    if not isinstance(card, dict) or card.get("type") != "card":
        return None
    result = {"type": "card"}
    if card.get("block_id"):
        result["block_id"] = str(card["block_id"])[:255]
    for key, limit in (("title", 150), ("subtitle", 150), ("body", 200), ("subtext", 200)):
        value = _text(card.get(key), limit)
        if value:
            result[key] = value
    for key in ("hero_image", "icon"):
        image = sanitize_image(card.get(key))
        if image:
            result[key] = image
    return result if any(k in result for k in ("hero_image", "title", "body")) else None


def sanitize_carousel(block: dict) -> dict | None:
    cards = [clean for card in (block.get("elements") or [])[:10] if (clean := sanitize_card(card))]
    return {**block, "elements": cards} if cards else None


def sanitize_section_accessory(block: dict) -> dict:
    """Validate an image accessory, dropping only the accessory when it is unusable."""
    accessory = block.get("accessory")
    if not isinstance(accessory, dict) or accessory.get("type") != "image":
        return block
    result = {key: value for key, value in block.items() if key != "accessory"}
    clean = sanitize_image(accessory)
    if clean:
        result["accessory"] = clean
    return result


RICH_BLOCK_SANITIZERS = {
    "context": sanitize_context,
    "image": lambda block: sanitize_image(block, block=True),
    "carousel": sanitize_carousel,
}
