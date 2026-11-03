"""Shared picker dispatch and exact sent-message provenance."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class PickerCallback:
    prefixes: tuple[str, ...]
    state_attribute: str
    handler: str


_PICKER_CALLBACKS = (
    PickerCallback(
        ("mp:", "mpg:", "mpv:", "mm:", "mc:", "mb", "mx", "mg:"),
        "_model_picker_state",
        "_handle_model_picker_callback",
    ),
    PickerCallback(("cp:",), "_choice_picker_state", "_handle_choice_picker_callback"),
)


def picker_callback(data: str) -> PickerCallback | None:
    """Use the same callback families for dispatch and ingress provenance."""
    return next(
        (route for route in _PICKER_CALLBACKS if data.startswith(route.prefixes)), None
    )


def matching_picker_state(
    adapter: Any, query: Any, chat_id: str, state_attribute: str
) -> dict[str, Any] | None:
    """Never use a newer same-chat prompt to resolve an old button."""
    message = getattr(query, "message", None)
    message_id = getattr(message, "message_id", None)
    if (
        not isinstance(message_id, int)
        or isinstance(message_id, bool)
        or str(getattr(message, "chat_id", None)) != chat_id
    ):
        return None
    state = getattr(adapter, state_attribute, {}).get(chat_id)
    if isinstance(state, dict) and state.get("msg_id") == message_id:
        return state
    return None
