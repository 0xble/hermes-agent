"""``telegram_topic``: create or edit a Telegram DM topic as its own Hermes session.

A thin wrapper over ``gateway/telegram_topic_sessions.py``. The work runs on the gateway's event
loop because the Bot API client, the ``/model`` switch lock and turn admission all belong to it.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from gateway.config import Platform
from gateway.session import SessionSource
from gateway.session_context import get_session_env
from gateway.session_identity import replace_source
from tools.registry import registry, tool_error

TELEGRAM_TOPIC_SCHEMA = {
    "name": "telegram_topic",
    "description": (
        "Create or edit a topic in the user's Telegram DM with Hermes. Each topic is its own Hermes "
        "session. action=create opens a topic with a name, an optional native icon, model, provider "
        "and reasoning effort, and an optional opening brief that the new session receives as its "
        "first message and starts working on. It returns the topic's thread_id, session_id and relay "
        "address. action=edit changes an existing topic's name or icon and its session's model or "
        "reasoning; it defaults to the current topic. Settings persist across gateway restarts. "
        "Only available in a Telegram DM session."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["create", "edit"],
                       "description": "create a new topic session, or edit an existing topic."},
            "name": {"type": "string",
                     "description": "Topic name, also stored as the session title. Required for create."},
            "icon": {"type": "string",
                     "description": "One emoji from Telegram's topic icon set. Omit on create to let Hermes pick one."},
            "model": {"type": "string", "description": "Model for the topic's session, as accepted by /model."},
            "provider": {"type": "string", "description": "Provider for that model, as accepted by /model --provider."},
            "reasoning": {"type": "string",
                          "description": "Reasoning effort for the topic's session: none, minimal, low, medium, high or xhigh."},
            "message": {"type": "string",
                        "description": "create only: opening brief the new session receives as its first message."},
            "thread_id": {"type": "string", "description": "edit only: the topic to edit. Defaults to the current topic."},
        },
        "required": ["action"],
        "additionalProperties": False,
    },
}


def _live_runner():
    try:
        from gateway.run import _gateway_runner_ref
        return _gateway_runner_ref()
    except Exception:
        return None


def check_telegram_topic_tool() -> bool:
    """A live gateway, nothing more. The ``telegram_topic`` toolset (Telegram's platform bundle only)
    is the surface gate, and each call resolves the calling profile's own adapter through
    ``_delivery_adapter_for``, failing closed when it is not connected.

    The answer must not change while the gateway runs: ``get_tool_definitions`` memoizes its result
    by toolset selection, not by check outcome, and the boot warm-up builds tool schemas before any
    adapter connects. A check on connected adapters is False at that moment, and the first turns'
    memoized schemas then drop the tool until the gateway restarts."""
    return _live_runner() is not None


def _calling_source(runner) -> Optional[SessionSource]:
    """The calling session's own source: the gateway's cached copy (it carries chat and user names,
    so the new topic's session context matches what a human message there would render), else one
    rebuilt from the session variables."""
    if get_session_env("HERMES_SESSION_PLATFORM") != Platform.TELEGRAM.value:
        return None
    cached = runner._get_cached_session_source(get_session_env("HERMES_SESSION_KEY"))
    if cached is not None:
        return replace_source(cached)  # keeps the receiving bot's routing provenance
    chat_id = get_session_env("HERMES_SESSION_CHAT_ID")
    if not chat_id:
        return None
    return SessionSource(
        platform=Platform.TELEGRAM, chat_id=str(chat_id),
        # Unknown chat type fails the DM-only gate rather than passing it.
        chat_type=get_session_env("HERMES_SESSION_CHAT_TYPE") or "unknown",
        chat_name=get_session_env("HERMES_SESSION_CHAT_NAME") or None,
        user_id=get_session_env("HERMES_SESSION_USER_ID") or None,
        user_name=get_session_env("HERMES_SESSION_USER_NAME") or None,
        thread_id=get_session_env("HERMES_SESSION_THREAD_ID") or None,
        profile=get_session_env("HERMES_SESSION_PROFILE") or None,
    )


async def telegram_topic_tool(args: dict, **_kwargs: Any) -> str:
    from gateway.telegram_topic_sessions import TopicSpec, create_topic_session, edit_topic_session
    from tools.send_message_tool import _dispatch_on_gateway_loop

    runner = _live_runner()
    if runner is None:
        return tool_error("telegram_topic needs the running gateway.")
    source = _calling_source(runner)
    if source is None or source.chat_type != "dm":
        return tool_error("telegram_topic works only from a Telegram DM session.")
    action = str(args.get("action") or "").strip().lower()
    if action not in {"create", "edit"}:
        return tool_error("action must be create or edit.")
    spec = TopicSpec(**{field: str(args.get(field) or "").strip()
                        for field in ("name", "icon", "model", "provider", "reasoning", "message")})
    if action == "create":
        if not spec.name:
            return tool_error("name is required for action=create.")
        make_coro = lambda: create_topic_session(runner, source, spec)  # noqa: E731
    else:
        if spec.message:
            return tool_error("message is only for action=create; relay to an existing topic's session instead.")
        thread_id = str(args.get("thread_id") or "").strip()
        make_coro = lambda: edit_topic_session(runner, source, thread_id, spec)  # noqa: E731
    try:
        result = await _dispatch_on_gateway_loop(runner, make_coro, "telegram_topic: failed to schedule on gateway loop")
    except ValueError as exc:  # TopicRequestError and title validation: caller-correctable
        return tool_error(str(exc))
    if isinstance(result, dict) and set(result) == {"error"}:
        return tool_error(result["error"])
    return json.dumps(result, ensure_ascii=False)


registry.register(
    name="telegram_topic", toolset="telegram_topic", schema=TELEGRAM_TOPIC_SCHEMA,
    handler=telegram_topic_tool, check_fn=check_telegram_topic_tool, is_async=True, emoji="🧵",
)
