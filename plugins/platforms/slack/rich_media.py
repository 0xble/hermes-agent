"""Images, subtext and peer cards for the opt-in markdown renderer.

Cards use a fenced ``cards`` YAML list with title, subtitle, body, image and url
keys. Only link buttons are emitted, never callback-only interactivity.
"""

from __future__ import annotations

import re
from urllib.parse import urlsplit

import hermes_yaml as yaml

IMAGE_RE = re.compile(r"!\[([^\]\n]*)\]\((https://[^\s)]+)\)")


def extract_attachment_images(content: str, extract):
    """Shield only renderer-owned image occurrences from the legacy extractor.

    Occurrences, not URLs: the same URL can legitimately appear again inline as
    an attachment. Fences remain opaque to native-image parsing.
    """
    prefix = "\x00HERMES_NATIVE_IMAGE"
    while prefix in content:
        prefix += "_"
    saved = {}
    lines = []
    fence = None
    for line in content.splitlines(keepends=True):
        marker = re.match(r"^\s*(`{3,}|~{3,})", line)
        if marker:
            token = marker[1]
            if fence is None:
                fence = token
            elif token[0] == fence[0] and len(token) >= len(fence):
                fence = None
            lines.append(line)
            continue
        match = None
        if fence is None:
            match = image_line(line)
            if match is None and line.strip().startswith("-# "):
                match = IMAGE_RE.match(line.strip()[3:].strip())
        if match and https_url(match[2]):
            key = f"{prefix}{len(saved)}\x00"
            saved[key] = match[0]
            line = line.replace(match[0], key, 1)
        lines.append(line)
    images, cleaned = extract("".join(lines))
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
    card = {"type": "card"}
    for key, limit in (("title", 150), ("subtitle", 150), ("body", 200)):
        value = record.get(key, "")
        if not isinstance(value, str):
            return None
        if value:
            rendered = fmt(value)
            if len(rendered) > limit:
                return None
            card[key] = {"type": "mrkdwn", "text": rendered}
    for key in ("image", "url"):
        if key in record and not https_url(record[key]):
            return None
    if "image" in record:
        card["hero_image"] = image_element(record["image"], record["title"])
    if "url" in record:
        card["actions"] = [{"type": "button", "text": {"type": "plain_text", "text": "Details"},
                            "url": record["url"]}]
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
    actions = []
    for action in (card.get("actions") or [])[:3]:
        if not isinstance(action, dict) or action.get("type") != "button" or not https_url(action.get("url")):
            continue
        label = _text(action.get("text"), 75)
        if label and label["type"] == "plain_text":
            actions.append({"type": "button", "text": label, "url": action["url"]})
    if actions:
        result["actions"] = actions
    return result if any(k in result for k in ("hero_image", "title", "actions", "body")) else None


def sanitize_carousel(block: dict) -> dict | None:
    cards = [clean for card in (block.get("elements") or [])[:10] if (clean := sanitize_card(card))]
    return {**block, "elements": cards} if cards else None
