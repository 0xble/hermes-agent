#!/usr/bin/env python3
"""React to the triggering gateway message or persist a desktop tapback."""

import contextlib
import json

from gateway.session_context import get_session_env
from tools import desktop_ui
from tools.registry import registry, tool_error


def _gateway_platform():
    from gateway.config import Platform
    try:
        platform = Platform(get_session_env("HERMES_SESSION_PLATFORM"))
    except ValueError:
        return None
    return platform if platform != Platform.LOCAL else None


def _react_gateway(platform, emoji: str) -> str:
    from gateway.session_context import acknowledge_current_message_reaction
    from model_tools import _run_async
    from tools.send_message_senders import _live_adapter
    from tools.send_message_tool import _dispatch_on_gateway_loop

    chat_id = get_session_env("HERMES_SESSION_CHAT_ID")
    message_id = get_session_env("HERMES_SESSION_MESSAGE_ID")
    if not chat_id or not message_id:
        return tool_error("No triggering message is bound to this gateway turn.")
    if not emoji:
        return tool_error("Gateway reactions require an emoji. Retraction is desktop-only.")
    runner, adapter = _live_adapter(platform, reaction=True)
    if adapter is None:
        return tool_error(f"No live {platform.value} adapter is available for this session.")
    routing = {key: get_session_env(f"HERMES_SESSION_{key.upper()}")
               for key in ("scope_id", "thread_id", "user_id", "parent_chat_id")}
    routing["team_id"] = routing["scope_id"]
    try:
        result = _run_async(_dispatch_on_gateway_loop(
            runner, lambda: adapter.add_reaction(chat_id, message_id, emoji, **routing),
            "react_to_message: failed to schedule reaction on gateway loop",
        ))
    except Exception as exc:
        return tool_error(f"Reaction failed: {exc}")
    if result is not True:
        return tool_error(f"Platform '{platform.value}' does not support this reaction or delivery failed.")
    acknowledge_current_message_reaction()
    return json.dumps({"success": True, "platform": platform.value, "chat_id": chat_id,
                       "message_id": message_id, "emoji": emoji}, ensure_ascii=False)


def _open_session_db():
    """Open the SessionDB for the profile owning this turn, or ``None``."""
    try:
        from hermes_state_registry import acquire
        return acquire()
    except Exception:
        return None


def react_to_message_tool(emoji: str, message_row_id=None, messages_back=None) -> str:
    """Attach (or with an empty ``emoji`` retract) the agent's reaction."""
    emoji = (emoji or "").strip()
    platform = _gateway_platform()
    if platform is not None:
        if message_row_id is not None or messages_back is not None:
            return tool_error("Earlier-message selectors are desktop-only. Omit them to react to the triggering message.")
        return _react_gateway(platform, emoji)
    session_key = get_session_env("HERMES_SESSION_KEY", "") or get_session_env("HERMES_SESSION_ID", "")
    if not session_key:
        return tool_error("No active session — reactions need a persisted conversation.")
    db = _open_session_db()
    if db is None:
        return tool_error("Session storage is unavailable.")
    try:
        row_id, target_role = message_row_id, "user"
        if row_id is None:
            # Default: the latest user message; `messages_back` steps to earlier user turns
            # (ids aren't visible to the model; "two messages ago" is how a person thinks).
            back = max(0, int(messages_back or 0))
            row_id = db.latest_message_row_id(session_key, role="user", offset=back)
            if row_id is None:
                return tool_error(f"No user message found {back} back." if back else "No user message to react to yet.")
        else:
            target_role = db.get_message_role(session_key, int(row_id)) or "user"
        try:
            reactions = db.set_message_reaction(session_key, int(row_id), emoji or None, author="agent")
        except Exception as exc:
            return tool_error(f"Failed to set the reaction: {exc}")
        if reactions is None:
            return tool_error(f"Message {row_id} is not part of this conversation.")
        # Paint it live; a missing bridge (non-desktop) is not an error — the reaction is
        # persisted. `role` lets the renderer match a live message without a durable row id.
        with contextlib.suppress(Exception):
            desktop_ui.emit("message.reaction", {"row_id": int(row_id), "reactions": reactions, "role": target_role})
        return json.dumps({"success": True, "row_id": int(row_id), "reactions": reactions}, ensure_ascii=False)
    finally:
        with contextlib.suppress(Exception):
            from hermes_state_registry import release_or_close
            release_or_close(db)


def reaction_disabled_toolsets(disabled, platform):
    """Freeze the desktop opt-in in the agent's existing per-session denylist.

    Availability checks are profile-cached, so the shared registry must not decide
    whether a gateway or desktop session gets this capability.
    """
    from gateway.config import Platform
    try:
        is_gateway = Platform(platform) != Platform.LOCAL
    except ValueError:
        is_gateway = False
    if not is_gateway and not check_react_requirements():
        return [*(disabled or ()), "reactions"]
    return disabled


def check_react_requirements() -> bool:
    """Desktop opt-in, resolved during session toolset selection, never profile-cached."""
    return desktop_ui.user_enabled("message_reactions", default=False)


REACT_TO_MESSAGE_SCHEMA = {
    "name": "react_to_message",
    "description": (
        "React to a message with a single emoji, the way you'd tapback in iMessage. "
        "Reach for it when a reaction is what a person would do: something funny gets "
        "a 😂, warmth gets a ❤️, a plan you're on board with gets a 👍 — then just "
        "carry on with whatever the message actually needs. If a reaction says it "
        "all, it can BE the reply (skip the redundant 'sounds good!' turn). Use it "
        "like a person would: occasionally, when felt — not on every message, and "
        "never as a status signal. NEVER narrate or explain a reaction ('I reacted "
        "with...', 'Reacting now') — the emoji appearing on the bubble is the whole "
        "point, and commentary kills it. Defaults to the user's most recent message. "
        "On messaging platforms, omit the desktop-only message selectors to react to "
        "the inbound message that triggered this turn. Slack uses a reaction name "
        "such as '+1', other platforms use their supported emoji. Replacement rules "
        "are platform-specific. On desktop, a different emoji replaces yours and an "
        "empty string retracts it."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "emoji": {
                "type": "string",
                "description": (
                    "The emoji to react with (e.g. '❤️', '😂', '👍'). Pass an empty "
                    "string to remove your reaction."
                ),
            },
            "message_row_id": {
                "type": "integer",
                "description": (
                    "Optional. The specific message to react to. Omit to react to the "
                    "user's latest message, which is almost always what you want."
                ),
            },
            "messages_back": {
                "type": "integer",
                "description": (
                    "Optional. React to an EARLIER user message: 1 = the one before "
                    "the latest, 2 = two before, and so on. For when something lands "
                    "late — the joke you only got after answering."
                ),
            },
        },
        "required": ["emoji"],
    },
}


registry.register(
    name="react_to_message", toolset="reactions", schema=REACT_TO_MESSAGE_SCHEMA,
    handler=lambda args, **kw: react_to_message_tool(
        emoji=args.get("emoji", ""), message_row_id=args.get("message_row_id"),
        messages_back=args.get("messages_back")),
    emoji="💛",
)
