"""Bounded conversion of Telegram Bot API Rich Messages to Markdown."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

MAX_RICH_MESSAGE_CHARS = 32_768
MAX_RICH_MESSAGE_DEPTH = 32
MAX_RICH_MESSAGE_NODES = 2_048
MAX_RICH_MESSAGE_BLOCK_TYPES = 64


@dataclass(frozen=True)
class RichMessageProjection:
    """Readable Rich Message projection plus bounded structural metadata."""

    text: str
    block_count: int
    block_types: tuple[str, ...]
    truncated: bool


@dataclass
class _RenderState:
    max_chars: int
    max_depth: int
    max_nodes: int
    nodes: int = 0
    block_count: int = 0
    block_types: list[str] = field(default_factory=list)
    truncated: bool = False

    def enter(self, depth: int) -> bool:
        if depth > self.max_depth or self.nodes >= self.max_nodes:
            self.truncated = True
            return False
        self.nodes += 1
        return True

    def record_block(self, block_type: str) -> None:
        self.block_count += 1
        if (
            block_type
            and block_type not in self.block_types
            and len(self.block_types) < MAX_RICH_MESSAGE_BLOCK_TYPES
        ):
            self.block_types.append(block_type)


def _mapping(value: Any) -> Mapping[str, Any] | None:
    if isinstance(value, Mapping):
        return value
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        try:
            converted = to_dict()
        except Exception:
            return None
        if isinstance(converted, Mapping):
            return converted
    return None


def _sequence(value: Any) -> Sequence[Any] | None:
    if isinstance(value, Sequence) and not isinstance(
        value, (str, bytes, bytearray)
    ):
        return value
    return None


def _inline(value: Any, state: _RenderState, depth: int) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if not state.enter(depth):
        return ""

    items = _sequence(value)
    if items is not None:
        return "".join(_inline(item, state, depth + 1) for item in items)

    node = _mapping(value)
    if node is None:
        return ""

    node_type = str(node.get("type") or "").lower()
    rendered = _inline(node.get("text"), state, depth + 1)
    if not rendered:
        rendered = _inline(node.get("children"), state, depth + 1)

    if node_type == "url":
        url = str(node.get("url") or "").strip()
        return f"[{rendered}]({url})" if rendered and url else (url or rendered)
    if node_type == "email_address":
        address = str(node.get("email_address") or "").strip()
        return (
            f"[{rendered}](mailto:{address})"
            if rendered and address
            else (address or rendered)
        )
    if node_type == "text_mention":
        user = _mapping(node.get("user"))
        user_id = user.get("id") if user else None
        return (
            f"[{rendered}](tg://user?id={user_id})"
            if rendered and user_id is not None
            else rendered
        )
    if node_type == "mention":
        username = str(node.get("username") or rendered or "").strip()
        return f"@{username.lstrip('@')}" if username else ""
    if node_type == "bot_command":
        return rendered or str(node.get("bot_command") or "")
    if node_type == "mathematical_expression":
        expression = str(node.get("expression") or rendered or "")
        return f"${expression}$" if expression else ""

    wrappers = {
        "bold": ("**", "**"),
        "italic": ("*", "*"),
        "underline": ("<u>", "</u>"),
        "strikethrough": ("~~", "~~"),
        "spoiler": ("||", "||"),
        "marked": ("==", "=="),
        "code": ("`", "`"),
        "subscript": ("<sub>", "</sub>"),
        "superscript": ("<sup>", "</sup>"),
    }
    wrapper = wrappers.get(node_type)
    if wrapper and rendered:
        return f"{wrapper[0]}{rendered}{wrapper[1]}"
    return rendered


def _caption(value: Any, state: _RenderState, depth: int) -> str:
    caption = _mapping(value)
    if caption is None:
        return _inline(value, state, depth)
    text = _inline(caption.get("text"), state, depth + 1)
    credit = _inline(caption.get("credit"), state, depth + 1)
    if text and credit:
        return f"{text} — {credit}"
    return text or credit


def _quote(lines: list[str]) -> list[str]:
    return [f"> {line}".rstrip() for line in "\n".join(lines).splitlines()]


def _render_list(
    block: Mapping[str, Any], state: _RenderState, depth: int
) -> list[str]:
    items = _sequence(block.get("items")) or ()
    lines: list[str] = []
    for item_value in items:
        if not state.enter(depth + 1):
            break
        item = _mapping(item_value)
        if item is None:
            continue
        content = _render_blocks(item.get("blocks"), state, depth + 2)
        if not content:
            item_text = _inline(item.get("text"), state, depth + 2).strip()
            content = [item_text] if item_text else []
        if not content:
            continue

        label = str(item.get("label") or "").strip()
        value = item.get("value")
        prefix = f"{value}." if value is not None else (label or "-")
        if item.get("has_checkbox"):
            prefix = f"{prefix} [{'x' if item.get('is_checked') else ' '}]"
        lines.append(f"{prefix} {content[0]}".strip())
        lines.extend(f"  {line}" if line else "" for line in content[1:])
    return lines


def _render_table(
    block: Mapping[str, Any], state: _RenderState, depth: int
) -> list[str]:
    rows = _sequence(block.get("cells")) or ()
    rendered_rows: list[list[str]] = []
    header_index: int | None = None
    max_columns = 0

    for row_value in rows:
        row = _sequence(row_value)
        if row is None:
            continue
        rendered_row: list[str] = []
        row_is_header = False
        for cell_value in row:
            if not state.enter(depth + 1):
                break
            cell = _mapping(cell_value)
            if cell is None:
                text = _inline(cell_value, state, depth + 2)
            else:
                text = _inline(cell.get("text"), state, depth + 2)
                row_is_header = row_is_header or bool(cell.get("is_header"))
            rendered_row.append(
                text.replace("\n", " ").replace("|", "\\|").strip()
            )
        if rendered_row:
            if header_index is None and row_is_header:
                header_index = len(rendered_rows)
            max_columns = max(max_columns, len(rendered_row))
            rendered_rows.append(rendered_row)

    caption = _caption(block.get("caption"), state, depth + 1).strip()
    if not rendered_rows or max_columns == 0:
        return [caption] if caption else []

    header_index = 0 if header_index is None else min(
        header_index, len(rendered_rows) - 1
    )
    header = rendered_rows[header_index] + [""] * (
        max_columns - len(rendered_rows[header_index])
    )
    lines = [caption] if caption else []
    lines.append("| " + " | ".join(header) + " |")
    lines.append("| " + " | ".join(["---"] * max_columns) + " |")
    for index, row in enumerate(rendered_rows):
        if index == header_index:
            continue
        padded = row + [""] * (max_columns - len(row))
        lines.append("| " + " | ".join(padded) + " |")
    return lines


def _render_block(
    block: Mapping[str, Any], state: _RenderState, depth: int
) -> list[str]:
    block_type = str(block.get("type") or "unknown").lower()
    state.record_block(block_type)

    if block_type == "divider":
        return ["---"]
    if block_type == "anchor":
        return []
    if block_type == "list":
        return _render_list(block, state, depth)
    if block_type == "table":
        return _render_table(block, state, depth)

    if block_type == "heading":
        text = _inline(block.get("text"), state, depth + 1).strip()
        try:
            size = int(block.get("size") or 1)
        except (TypeError, ValueError):
            size = 1
        size = min(max(size, 1), 6)
        return [f"{'#' * size} {text}".rstrip()] if text else []

    if block_type == "preformatted":
        text = _inline(block.get("text"), state, depth + 1)
        language = str(block.get("language") or "").strip()
        return [f"```{language}\n{text}\n```"] if text else []

    if block_type == "footer":
        text = _inline(block.get("text"), state, depth + 1).strip()
        return [f"*{text}*"] if text else []

    if block_type == "mathematical_expression":
        expression = str(block.get("expression") or "").strip()
        return [f"$${expression}$$"] if expression else []

    if block_type == "paragraph":
        text = _inline(block.get("text"), state, depth + 1)
        return text.splitlines() if text else []

    if block_type in {"blockquote", "expandable_blockquote"}:
        nested = _render_blocks(block.get("blocks"), state, depth + 1)
        if not nested:
            text = _inline(block.get("text"), state, depth + 1)
            nested = text.splitlines() if text else []
        credit = _inline(block.get("credit"), state, depth + 1).strip()
        quoted = _quote(nested)
        if credit:
            quoted.append(f"> — {credit}")
        return quoted

    if block_type == "pullquote":
        text = _inline(block.get("text"), state, depth + 1)
        quoted = _quote(text.splitlines()) if text else []
        credit = _inline(block.get("credit"), state, depth + 1).strip()
        if credit:
            quoted.append(f"> — {credit}")
        return quoted

    if block_type == "details":
        summary = _inline(block.get("summary"), state, depth + 1).strip()
        nested = _render_blocks(block.get("blocks"), state, depth + 1)
        return ([f"**{summary}**"] if summary else []) + nested

    if block_type in {"collage", "slideshow"}:
        nested = _render_blocks(block.get("blocks"), state, depth + 1)
        caption = _caption(block.get("caption"), state, depth + 1).strip()
        return nested + ([caption] if caption else [])

    if block_type == "map":
        location = _mapping(block.get("location"))
        marker = "[Map]"
        if location is not None:
            latitude = location.get("latitude")
            longitude = location.get("longitude")
            if latitude is not None and longitude is not None:
                marker = f"[Map: {latitude},{longitude}]"
        caption = _caption(block.get("caption"), state, depth + 1).strip()
        return [marker] + ([caption] if caption else [])

    if block_type == "buttons":
        buttons = _sequence(block.get("buttons")) or ()
        rendered: list[str] = []
        for row in buttons:
            for button_value in _sequence(row) or ():
                button = _mapping(button_value)
                if button is None:
                    continue
                text = _inline(button.get("text"), state, depth + 1).strip()
                url = str(button.get("url") or "").strip()
                if text and url:
                    rendered.append(f"[{text}]({url})")
                elif text or url:
                    rendered.append(text or url)
        return rendered

    media_types = {
        "animation",
        "audio",
        "document",
        "photo",
        "video",
        "voice_note",
    }
    if block_type in media_types:
        caption = _caption(block.get("caption"), state, depth + 1).strip()
        label = f"[{block_type.replace('_', ' ').title()}]"
        return [label] + ([caption] if caption else [])

    text = _inline(block.get("text"), state, depth + 1).strip()
    nested = _render_blocks(
        block.get("blocks") or block.get("children"), state, depth + 1
    )
    summary = _inline(block.get("summary"), state, depth + 1).strip()
    caption = _caption(block.get("caption"), state, depth + 1).strip()
    expression = str(block.get("expression") or "").strip()
    return (
        ([text] if text else [])
        + ([summary] if summary else [])
        + nested
        + ([caption] if caption else [])
        + ([expression] if expression else [])
    )


def _render_blocks(value: Any, state: _RenderState, depth: int) -> list[str]:
    blocks = _sequence(value)
    if blocks is None:
        return []

    lines: list[str] = []
    for block_value in blocks:
        if not state.enter(depth):
            break
        block = _mapping(block_value)
        if block is None:
            continue
        lines.extend(_render_block(block, state, depth + 1))
    return lines


def project_rich_message(
    rich_message: Any,
    *,
    max_chars: int = MAX_RICH_MESSAGE_CHARS,
    max_depth: int = MAX_RICH_MESSAGE_DEPTH,
    max_nodes: int = MAX_RICH_MESSAGE_NODES,
) -> RichMessageProjection:
    """Project a RichMessage mapping or typed object without trusting its shape."""

    state = _RenderState(
        max_chars=max(1, max_chars),
        max_depth=max(1, max_depth),
        max_nodes=max(1, max_nodes),
    )
    rich = _mapping(rich_message)
    lines = _render_blocks(rich.get("blocks"), state, 0) if rich else []
    text = "\n".join(line.rstrip() for line in lines if line is not None).strip()

    if len(text) > state.max_chars:
        marker = "\n[Rich message truncated]"
        if state.max_chars <= len(marker):
            text = marker[: state.max_chars]
        else:
            keep = state.max_chars - len(marker)
            text = text[:keep].rstrip() + marker
        state.truncated = True

    return RichMessageProjection(
        text=text,
        block_count=state.block_count,
        block_types=tuple(state.block_types),
        truncated=state.truncated,
    )
