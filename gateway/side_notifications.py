"""Compact, stable presentation for continuable side sessions."""

from __future__ import annotations

CONTINUE_TEXT = "Reply here to continue"
SIDE_EMOJI = "↗️"
WAITING_TEXT = "Waiting for the current tool step to finish."


def side_root_from_route(session_key: str) -> str:
    """Read the canonical side root already embedded in its route key."""
    route = str(session_key or "").strip()
    marker = ":side:"
    return route.rsplit(marker, 1)[-1] if marker in route else route


def side_display_id(side_root_session_id: str) -> str:
    """Return the existing root session ID's compact random suffix."""
    canonical = str(side_root_session_id or "").strip()
    if not canonical:
        return "unknown"
    suffix = canonical.rsplit("_", 1)[-1]
    return suffix or canonical


def side_rich_text_supported(adapter: object) -> bool:
    """Use the adapter's established Markdown-rendering capability."""
    return bool(getattr(adapter, "supports_code_blocks", False))


def _side_identity(side_root_session_id: str, *, rich_text: bool) -> str:
    display_id = side_display_id(side_root_session_id)
    return f"`{display_id}`" if rich_text else display_id


def _continue_hint(*, rich_text: bool) -> str:
    return f"*{CONTINUE_TEXT}*" if rich_text else CONTINUE_TEXT


def format_side_queued(preview: str, *, rich_text: bool = True) -> str:
    """Format a queued status with a capability-aware waiting hint."""
    waiting = f"*{WAITING_TEXT}*" if rich_text else WAITING_TEXT
    return f'{SIDE_EMOJI} Side queued: "{preview}"\n{waiting}'


def format_side_started(side_root_session_id: str, *, rich_text: bool = True) -> str:
    """Format the notification emitted after a side route exists."""
    identity = _side_identity(side_root_session_id, rich_text=rich_text)
    return f"{SIDE_EMOJI} Side {identity} started\n{_continue_hint(rich_text=rich_text)}"


def format_side_closed(side_root_session_id: str, *, rich_text: bool = True) -> str:
    """Render a closed status using the existing side identity."""
    identity = _side_identity(side_root_session_id, rich_text=rich_text)
    return f"{SIDE_EMOJI} Side {identity} closed."


def side_response_parts(
    side_root_session_id: str, *, rich_text: bool = True
) -> tuple[str, str]:
    """Return the streaming prefix and suffix framing side response content."""
    identity = _side_identity(side_root_session_id, rich_text=rich_text)
    return (
        f"{SIDE_EMOJI} Side {identity}\n",
        f"\n{_continue_hint(rich_text=rich_text)}",
    )
