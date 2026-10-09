"""Create and edit Telegram DM topics as Hermes sessions: the gateway side of ``telegram_topic``.

One owner for a topic's visible name and icon and its bound session's title, model and reasoning,
so the tool and ``/topic edit`` share a path instead of an agent replaying slash commands as the
user. Every request is validated before Telegram is called, because a created topic is not undone.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from typing import Any, Optional

from gateway.platforms.event import MessageEvent, MessageType
from gateway.session import SessionSource
from gateway.session_identity import replace_source

logger = logging.getLogger("gateway.run")  # log-record parity with gateway/run.py

BRIEF_HEADER = "Opening brief"


class TopicRequestError(ValueError):
    """A request the caller can correct: bad name, icon, model, reasoning or target."""


@dataclasses.dataclass(frozen=True)
class TopicSpec:
    """What to set on a topic. Empty fields are left unchanged."""

    name: str = ""
    icon: str = ""
    model: str = ""
    provider: str = ""
    reasoning: str = ""
    message: str = ""


def _adapter(runner, source: SessionSource, method: str):
    adapter = runner._delivery_adapter_for(source)
    if adapter is None or not callable(getattr(adapter, method, None)):
        raise TopicRequestError("Telegram topic editing is unavailable right now.")
    return adapter


def _clean_name(runner, name: str) -> str:
    """The name as both the visible topic name and the session title (the tighter title limit wins)."""
    from hermes_state import SessionDB
    clean = SessionDB.sanitize_title(name)  # ValueError past the session-title length limit
    if not clean:
        raise TopicRequestError("Topic name is empty.")
    return runner._sanitize_telegram_topic_title(clean)


async def topic_icon_id(adapter, emoji: str) -> str:
    """Native custom-emoji id for ``emoji`` from Telegram's topic icon set."""
    options = await adapter.get_forum_topic_icon_options()
    match = next((item for item in options if item.get("emoji") == emoji), None)
    if match is None or not match.get("custom_emoji_id"):
        choices = " ".join(str(item.get("emoji")) for item in options if item.get("emoji"))
        raise TopicRequestError(f"Unsupported topic icon {emoji!r}. Available icons: {choices or 'none'}")
    return str(match["custom_emoji_id"])


def _parse_reasoning(effort: str) -> Optional[dict]:
    if not effort:
        return None
    from hermes_constants import parse_reasoning_effort
    parsed = parse_reasoning_effort(effort.strip().lower())
    if parsed is None:
        raise TopicRequestError(f"Unknown reasoning effort {effort!r}.")
    return parsed


async def _resolve_model(runner, source: SessionSource, spec: TopicSpec, session_key: str = ""):
    if not (spec.model or spec.provider):
        return None
    ctx, result, error = await runner.resolve_session_model_selection(
        source, spec.model, spec.provider, session_key=session_key)
    if error is not None:
        raise TopicRequestError(error)
    return ctx, result


async def _record_manual_icon(runner, source: SessionSource, thread_id: str, emoji: str, icon_id: str) -> None:
    """Record an explicitly chosen icon as manual so automatic renames keep it."""
    db = runner._sync_session_db()
    if db is None:
        return
    profile = runner._telegram_topic_profile_name(source)
    await asyncio.to_thread(
        db.record_telegram_topic_icon_state, str(source.chat_id), str(thread_id),
        custom_emoji_id=icon_id, emoji=emoji, owner="manual", profile_name=profile)
    await asyncio.to_thread(
        db.record_telegram_topic_icon_history, str(source.chat_id),
        emoji=emoji, custom_emoji_id=icon_id, profile_name=profile)


async def set_topic_appearance(
    runner, source: SessionSource, thread_id: str, *, name: Optional[str] = None,
    icon_emoji: str = "", icon_id: Optional[str] = None,
) -> None:
    """Rename and/or re-icon one DM topic. An icon-only edit omits the name so Telegram keeps the
    visible one (resending a stored title reverted an earlier rename)."""
    adapter = _adapter(runner, source, "rename_dm_topic")
    kwargs: dict[str, Any] = {"chat_id": str(source.chat_id), "thread_id": str(thread_id), "name": name or None}
    if icon_id:
        kwargs["icon_custom_emoji_id"] = icon_id
    if await adapter.rename_dm_topic(**kwargs) is not True:
        raise TopicRequestError("Telegram rejected the topic edit.")
    if icon_id:
        await _record_manual_icon(runner, source, thread_id, icon_emoji, icon_id)


async def _apply_session_settings(runner, dest: SessionSource, entry, model, effort: Optional[dict],
                                  *, announce: bool = True) -> list:
    """Commit a resolved model and a parsed reasoning pick as ``dest``'s session overrides, through
    the same commits as ``/model --session`` and ``/reasoning``. Returns model warnings."""
    warnings = []
    # A stale route can come back flagged for auto-reset, whose cleanup on the next turn would wipe
    # the overrides set here; ``/model`` consumes the flag for the same reason (#48031).
    entry.was_auto_reset = False
    if model is not None:
        ctx, result = model
        error = await runner.commit_session_model_selection(ctx, result, dest, announce=announce)
        if error is not None:
            raise TopicRequestError(error)
        if getattr(result, "warning_message", ""):
            warnings.append(result.warning_message)
    if effort is not None:
        runner._set_reasoning_override(entry.session_key, effort)
    return warnings


async def _deliver_brief(adapter, dest: SessionSource, brief: str) -> None:
    """Show the brief in the topic, then admit it as the session's first turn. The turn is internal
    (the calling session already holds the user's authority) and never a gateway command, so a
    brief starting with ``/`` is read as text. A brief that could not be shown is not admitted, so
    the session never works from a brief the user cannot see; the caller reports the failure.

    Internal events skip the ``hermes pause`` gate, so it is checked here: a paused Hermes starts
    no new work, and the brief would start a new session's first turn."""
    from agent.estop import paused_reply
    from gateway.wake import admit_internal_event
    paused = paused_reply()
    if paused is not None:
        raise RuntimeError(paused)
    sent = await adapter.send(str(dest.chat_id), f"{BRIEF_HEADER}\n\n{brief}",
                              metadata={"thread_id": str(dest.thread_id)})
    if not getattr(sent, "success", False):
        raise RuntimeError(getattr(sent, "error", None) or "Telegram did not accept the message")
    # Anchor the turn on the shown brief, as a typed message anchors its own turn, so the session's
    # replies thread under it instead of relying on the anchor-less synthetic-send fallback.
    anchor = str(sent.message_id) if getattr(sent, "message_id", None) else None
    event = MessageEvent(text=brief, message_type=MessageType.TEXT, source=replace_source(dest, message_id=anchor),
                         message_id=anchor, internal=True, allow_gateway_control=False,
                         metadata={"notification_category": "result"})
    await admit_internal_event(adapter, event)


def relay_address(runner, source: SessionSource, session_id: str) -> str:
    """The relay address another agent session uses to message this one. ``source.profile`` is
    unset on the primary of a gateway launched under a named profile, whose sessions still live in
    that profile's state.db, so the gateway's own profile is the fallback, never ``default``."""
    profile = str(getattr(source, "profile", None) or "").strip()
    if not profile:
        profile = str(getattr(runner, "_primary_profile_name", None) or "").strip()
    if not profile:
        profile = runner._active_profile_name()
    return f"hermes:{profile or 'default'}/{session_id}"


async def _topic_session(runner, dest: SessionSource, session_key: str):
    """The session ``dest``'s topic belongs to, bound the way a turn there binds it: an existing
    binding (healed to its compression tip, switching the route to it) wins over the route, and a
    topic with no binding is bound to the session it routes to. Never rebinds a bound topic."""
    entry = await runner.async_session_store.get_or_create_session(dest)
    return await runner._hmwa_heal_telegram_topic_binding(dest, entry, session_key)


async def _topic_exists(runner, dest: SessionSource, thread_id: str) -> bool:
    """Whether Hermes knows topic ``thread_id`` in ``dest``'s chat: a recorded binding or a routed
    session. Telegram's Bot API has no read for one DM topic, so this is the evidence available
    without a rename."""
    if runner._session_db is not None:
        binding = await runner._session_db.get_telegram_topic_binding(
            chat_id=str(dest.chat_id), thread_id=str(thread_id),
            profile_name=runner._telegram_topic_profile_name(dest))
        if binding:
            return True
    return runner.session_store.peek_session_id(runner._session_key_for_source(dest)) is not None


async def create_topic_session(runner, source: SessionSource, spec: TopicSpec) -> dict:
    """Create a DM topic in ``source``'s chat, bind a new session to it with the requested title,
    icon, model and reasoning, and optionally start it with an opening brief."""
    adapter = _adapter(runner, source, "_create_dm_topic")
    root = replace_source(source, thread_id=None, message_id=None)
    name = _clean_name(runner, spec.name)
    icon_id, icon_records = None, None
    if spec.icon:
        icon_id = await topic_icon_id(adapter, spec.icon)
    else:
        # Same pick an automatic rename would make; None when auto_topic_icons is off.
        icon_id, state_record, history_record, owner = await runner._select_telegram_topic_icon(
            root, adapter, name, preserve_manual=False)
        icon_records = (state_record, history_record, owner) if icon_id else None
    effort = _parse_reasoning(spec.reasoning)
    model = await _resolve_model(runner, root, spec)

    thread_id = await adapter._create_dm_topic(int(source.chat_id), name=name, icon_custom_emoji_id=icon_id)
    if not thread_id:
        raise TopicRequestError("Telegram did not create the topic; the gateway log has the reason.")
    dest = replace_source(root, thread_id=str(thread_id))
    try:
        entry = await runner.async_session_store.get_or_create_session(dest)
        title = name
        if runner._session_db is not None:
            title = await runner._session_db.set_session_title_in_lineage(entry.session_id, name)
        await asyncio.to_thread(runner._record_telegram_topic_binding, dest, entry)
        if spec.icon:
            await _record_manual_icon(runner, dest, str(thread_id), spec.icon, icon_id)
        elif icon_records:
            await runner._persist_telegram_topic_icon_records(dest, *icon_records)
        warnings = await _apply_session_settings(runner, dest, entry, model, effort, announce=False)
    except Exception as exc:
        retry = " ".join(f"{field}={value!r}" for field, value in (
            ("name", name), ("icon", spec.icon), ("model", spec.model), ("provider", spec.provider),
            ("reasoning", spec.reasoning)) if value)
        raise TopicRequestError(
            f"Topic {thread_id} was created, but setting it up failed: {exc}. "
            f"Finish it with action=edit thread_id={thread_id} {retry}.") from exc
    relay = relay_address(runner, source, entry.session_id)
    brief = spec.message.strip()
    if brief:
        try:
            await _deliver_brief(adapter, dest, brief)
        except Exception as exc:
            logger.warning("Opening brief for topic %s was not delivered", thread_id, exc_info=True)
            raise TopicRequestError(
                f"Topic {thread_id} and session {entry.session_id} are set up, but the opening brief "
                f"was not delivered: {exc}. Send it with relay to {relay}.") from exc
    return {
        "action": "create", "thread_id": str(thread_id), "session_id": entry.session_id,
        "relay": relay, "title": title,
        "icon": spec.icon or None, "model": getattr(model[1], "new_model", None) if model else None,
        "provider": getattr(model[1], "target_provider", None) if model else None,
        "reasoning": effort, "brief_delivered": bool(brief), "warnings": warnings,
    }


async def edit_topic_session(runner, source: SessionSource, thread_id: str, spec: TopicSpec) -> dict:
    """Change an existing DM topic's name or icon and its session's title, model or reasoning."""
    thread_id = str(thread_id or source.thread_id or "").strip()
    if not thread_id or thread_id in runner._TELEGRAM_GENERAL_TOPIC_IDS:
        raise TopicRequestError("Name the topic with thread_id; the General topic cannot be edited.")
    if not any((spec.name, spec.icon, spec.model, spec.provider, spec.reasoning)):
        raise TopicRequestError("Nothing to change: give name, icon, model, provider or reasoning.")
    dest = replace_source(source, thread_id=thread_id, message_id=None)
    session_key = runner._session_key_for_source(dest)
    name = _clean_name(runner, spec.name) if spec.name else None
    icon_id = await topic_icon_id(_adapter(runner, source, "rename_dm_topic"), spec.icon) if spec.icon else None
    effort = _parse_reasoning(spec.reasoning)
    model = None
    if spec.model or spec.provider:
        if runner._is_session_running(session_key):
            raise TopicRequestError(
                "That topic's session is mid-turn, so its model cannot change now. "
                "Retry when it is idle, or use /model in the topic.")
        model = await _resolve_model(runner, dest, spec, session_key=session_key)

    if name or icon_id:
        await set_topic_appearance(runner, dest, thread_id, name=name, icon_emoji=spec.icon, icon_id=icon_id)
    elif not await _topic_exists(runner, dest, thread_id):
        # A settings-only edit never calls Telegram, so a mistyped thread_id would otherwise get
        # a routing entry, a session and a binding for a topic that does not exist.
        raise TopicRequestError(
            f"No topic {thread_id} in this chat is known to Hermes. Check thread_id, or give name or "
            f"icon so Telegram confirms the topic.")
    # The topic's own session, bound as a turn there binds it: it finishes a create whose setup
    # failed after Telegram made the topic, and never repoints a bound topic.
    entry = await _topic_session(runner, dest, session_key)
    title = None
    if name and runner._session_db is not None:
        title = await runner._session_db.set_session_title_in_lineage(entry.session_id, name)
    warnings = []
    if model is not None or effort is not None:
        warnings = await _apply_session_settings(runner, dest, entry, model, effort)
    return {
        "action": "edit", "thread_id": thread_id, "session_id": entry.session_id,
        "relay": relay_address(runner, source, entry.session_id), "name": name, "title": title,
        "icon": spec.icon or None,
        "model": getattr(model[1], "new_model", None) if model else None,
        "provider": getattr(model[1], "target_provider", None) if model else None,
        "reasoning": effort, "warnings": warnings,
    }
