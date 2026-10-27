"""Create and edit Telegram DM topics as Hermes sessions: the gateway side of ``telegram_topic``.

One owner for a topic's visible name and icon and its bound session's title, model and reasoning,
so the tool and ``/topic edit`` share a path instead of an agent replaying slash commands as the
user. Every request is validated before Telegram is called, because a created topic is not undone.
"""

from __future__ import annotations

import asyncio
import dataclasses
import html
import logging
import re
import textwrap
from pathlib import Path
from typing import Any, Optional, cast

from gateway.platforms.event import MessageEvent, MessageType
from gateway.session import SessionSource
from gateway.session_identity import replace_source

logger = logging.getLogger("gateway.run")  # log-record parity with gateway/run.py


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


def _icon_choices(options) -> str:
    return " ".join(str(item.get("emoji")) for item in options if item.get("emoji"))


# Joiners, variation selectors and skin-tone modifiers are not base identities.
_NON_BASE_CODEPOINTS = frozenset({"\u200d", "\ufe0e", "\ufe0f"} | {chr(c) for c in range(0x1F3FB, 0x1F400)})


def _closest_icon(valid, normalized: str) -> Optional[str]:
    """Catalog icon sharing the most base codepoints with the input, else None."""
    from agent.topic_icons import normalize_emoji

    wanted = set(normalized) - _NON_BASE_CODEPOINTS
    best, best_shared = None, 0
    for item in valid:
        shared = len(wanted & (set(normalize_emoji(item)) - _NON_BASE_CODEPOINTS))
        if shared > best_shared:
            best, best_shared = item["emoji"], shared
    return best


def _match_icon(options, emoji: str) -> str:
    from agent.topic_icons import normalize_emoji

    normalized = normalize_emoji(emoji)
    valid = [item for item in options if item.get("emoji") and item.get("custom_emoji_id")]
    match = next((item for item in valid if normalize_emoji(item) == normalized), None)
    if match is None:
        closest = _closest_icon(valid, normalized)
        hint = f". Closest spelling: {closest}" if closest else ""
        raise TopicRequestError(
            f"Unsupported topic icon {emoji!r}{hint}. Available icons: {_icon_choices(valid) or 'none'}")
    return str(match["custom_emoji_id"])


async def topic_icon_id(adapter, emoji: str) -> str:
    """Native custom-emoji id for ``emoji`` from Telegram's topic icon set."""
    return _match_icon(await adapter.get_forum_topic_icon_options(), emoji)


async def _create_icon(adapter, emoji: str) -> tuple[Optional[str], list[str]]:
    """The icon id a create applies. An icon is required whenever Telegram offers a native set.
    When the set is empty or cannot be fetched, the topic is created without one and warns."""
    try:
        options = list(await adapter.get_forum_topic_icon_options() or [])
    except Exception:
        logger.warning("Telegram topic icon set could not be fetched", exc_info=True)
        options = []
    if not options:
        return None, ["Telegram's topic icon set is unavailable, so the topic was created without an icon."]
    if not emoji:
        raise TopicRequestError(
            "An icon is required: pick one emoji that fits the subject. "
            f"Available icons: {_icon_choices(options)}")
    return _match_icon(options, emoji), []


def _parse_reasoning(effort: str) -> Optional[dict]:
    if not effort:
        return None
    normalized = effort.strip().lower()
    if normalized in {"default", "reset"}:
        return None
    from hermes_constants import parse_reasoning_effort
    parsed = parse_reasoning_effort(normalized)
    if parsed is None:
        raise TopicRequestError(f"Unknown reasoning effort {effort!r}.")
    return parsed


def _reasoning_effort(config: Optional[dict]) -> Optional[str]:
    """Return the user-facing effort represented by a resolved reasoning config."""
    if not isinstance(config, dict):
        return None
    if config.get("enabled") is False:
        return "none"
    return config.get("effort") or "medium"


def _resolve_topic_model_ref(raw: str, source: Optional[SessionSource] = None, runner=None):
    """Resolve a topic role from the config that owns the topic's session: the routed profile's
    under ``multiplex_profiles``, else the gateway's module-level loader."""
    from gateway.run import _load_gateway_config
    from hermes_cli.model_preset_runtime import resolve_model_ref
    config_path = None
    if (runner is not None and source is not None
            and getattr(getattr(runner, "config", None), "multiplex_profiles", False)):
        config_path = Path(runner._resolve_profile_home_for_source(source)) / "config.yaml"
    try:
        config = _load_gateway_config() if config_path is None else _load_gateway_config(config_path)
        return resolve_model_ref(raw, config)
    except (ValueError, TypeError) as exc:
        raise TopicRequestError(str(exc)) from exc


async def _resolve_model(runner, source: SessionSource, spec: TopicSpec, session_key: str = ""):
    if not (spec.model or spec.provider):
        return None
    from hermes_cli.model_presets import preset_ref_name
    if preset_ref_name(spec.model):
        if spec.provider:
            raise TopicRequestError(
                f"model {spec.model.strip()!r} is a role, which carries its own provider. "
                "Drop provider, or pass a concrete model.")
        route = _resolve_topic_model_ref(spec.model.strip(), source, runner)
        if route.model:
            _ctx, _result, error = await runner.resolve_session_model_selection(
                source, route.model, route.provider, session_key=session_key,
            )
            if error:
                raise TopicRequestError(error)
        return ("role", spec.model.strip()[1:].strip().lower())
    ctx, result, error = await runner.resolve_session_model_selection(
        source, spec.model, spec.provider, session_key=session_key)
    if error is not None:
        raise TopicRequestError(error)
    return ctx, result


async def _record_manual_icon(runner, source: SessionSource, thread_id: str, emoji: str, icon_id: str) -> None:
    """Record an explicitly chosen icon as manual so automatic renames keep it."""
    db = await asyncio.to_thread(runner._sync_session_db)
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


async def _apply_session_settings(
    runner, dest: SessionSource, entry, model, effort: Optional[dict], *, reasoning_reset: bool = False,
    announce: bool = True,
) -> dict:
    """Commit topic settings through the slash-command paths and report their effective timing."""
    warnings = []
    model_status = "unchanged"
    reasoning_status = "unchanged"
    model_effective_when = "unchanged"
    reasoning_effective_when = "unchanged"
    # A stale route can come back flagged for auto-reset, whose cleanup on the next turn would wipe
    # the overrides set here; ``/model`` consumes the flag for the same reason (#48031).
    entry.was_auto_reset = False
    if model is not None:
        if isinstance(model, tuple) and model and model[0] == "role":
            role = model[1]
            await runner._set_session_model_role(entry.session_key, role)
            model_status = "queued" if runner._is_session_running(entry.session_key) else "applied"
            model_effective_when = "next_turn"
        else:
            ctx, result = model
            error, model_status = await runner.commit_session_model_selection_with_status(
                ctx, result, dest, announce=announce,
            )
            if error is not None:
                raise TopicRequestError(error)
            model_effective_when = "next_turn"
            if getattr(result, "warning_message", ""):
                warnings.append(result.warning_message)
    if effort is not None or reasoning_reset:
        runner._session_state(entry.session_key)
        previous = await runner._session_reasoning_override_async(entry.session_key)
        selected = None if reasoning_reset else effort
        if previous != selected:
            await asyncio.to_thread(runner._set_reasoning_override, entry.session_key, selected, dest)
            reasoning_status = "applied"
            reasoning_effective_when = (
                "next_request" if runner._is_session_running(entry.session_key) else "next_turn"
            )
    return {
        "warnings": warnings,
        "model_status": model_status,
        "reasoning_status": reasoning_status,
        "model_effective_when": model_effective_when,
        "reasoning_effective_when": reasoning_effective_when,
    }


async def _session_settings_readback(runner, dest: SessionSource, entry, selection) -> dict[str, Any]:
    """Optional inspection must not turn a successful topic mutation into a retry."""
    from gateway.run import _load_gateway_config
    from gateway.run_session_readback import read_session_settings

    readback = {
        "model": None, "provider": None, "effective_model": None,
        "effective_provider": None, "effective_effort": None, "warnings": [],
    }
    if selection is not None and not (isinstance(selection, tuple) and selection[0] == "role"):
        _ctx, result = selection
        readback.update(model=result.new_model, provider=result.target_provider)
    try:
        if isinstance(selection, tuple) and selection and selection[0] == "role":
            route = await asyncio.to_thread(_resolve_topic_model_ref, "@" + selection[1], dest, runner)
            readback.update(model=route.model, provider=route.provider)
        async with runner._async_profile_scope_for_source(dest):
            profile_home = runner._profile_scope_key_for_source(dest)
            user_config = await asyncio.to_thread(
                _load_gateway_config, *([profile_home / "config.yaml"] if profile_home is not None else []),
            )
            effective_model, runtime, reasoning = await read_session_settings(
                runner, source=dest, session_key=entry.session_key, user_config=user_config,
            )
            readback.update(
                effective_model=effective_model, effective_provider=runtime.get("provider") or None,
                effective_effort=_reasoning_effort(reasoning or {}),
            )
    except Exception:
        logger.warning("Topic %s settings readback failed after successful changes", dest.thread_id, exc_info=True)
        readback.update(effective_model=None, effective_provider=None, effective_effort=None)
        readback["warnings"].append("Topic changes succeeded, but settings readback is unavailable.")
    return readback


async def _origin_line(runner, adapter, source: SessionSource) -> str:
    """Build the caller-owned origin line for a topic opening brief.

    The source session is deliberately read without creating or switching a session: a tool call
    must not change the caller's routing state just to describe its origin. Telegram renders the
    returned Markdown link through the adapter's normal text path, so it remains a real clickable
    link instead of literal Markdown.
    """
    thread_id = str(source.thread_id or "")
    if not thread_id or thread_id in runner._TELEGRAM_GENERAL_TOPIC_IDS:
        return "Split from the main chat"

    title = None
    icon = None
    db = getattr(runner, "_session_db", None)
    try:
        session_id = await asyncio.to_thread(
            runner.session_store.peek_session_id, runner._session_key_for_source(source))
        if db is not None:
            if not session_id:
                binding = await db.get_telegram_topic_binding(
                    chat_id=str(source.chat_id), thread_id=thread_id,
                    profile_name=runner._telegram_topic_profile_name(source),
                )
                session_id = binding.get("session_id") if binding else None
            if session_id:
                title = await db.get_session_title(session_id)
            state = await db.get_telegram_topic_icon_state(
                str(source.chat_id), thread_id, runner._telegram_topic_profile_name(source)
            )
            icon = str(state.get("emoji") or "").strip() if state else None
    except Exception:
        # Origin decoration must never prevent a topic from being created.
        logger.debug("Failed to read Telegram topic origin metadata", exc_info=True)

    label = str(title or "the calling topic").strip() or "the calling topic"
    if icon:
        label = f"{icon} {label}"
    # The adapter's Markdown link parser intentionally accepts simple, balanced labels only. A
    # session title may contain a bracket; keep the link clickable rather than shipping malformed
    # Markdown, and retain the icon when one is known.
    if "[" in label or "]" in label:
        label = f"{icon} the calling topic" if icon else "the calling topic"

    # The adapter refreshes from Bot.get_me() when this cache is stale. Test doubles and older
    # adapters may only expose the cached bot object, so use those as a graceful fallback.
    refresh = getattr(adapter, "_refresh_bot_identity", None)
    if callable(refresh):
        try:
            await cast(Any, refresh)()
        except Exception:
            logger.debug("Failed to refresh Telegram bot identity for topic origin", exc_info=True)
    username_reader = getattr(adapter, "_current_bot_username", None)
    username = str(username_reader() or "") if callable(username_reader) else ""
    if not username:
        username = str(getattr(getattr(adapter, "_bot", None), "username", "") or "")
    username = username.lstrip("@").strip()
    if not username:
        return f"Split from {label}"
    link = f"https://t.me/{username}/{thread_id}/{thread_id}"
    return f"Split from [{label}]({link})"


def _opening_brief_display(brief: str, title: str, origin_line: str) -> str:
    """Bound the visible preview, not the brief that the new session receives.

    Relay-style ``title\nobjective\n---`` is a multiline setext heading. Never
    send that envelope directly: rich paragraph spacers can pull even the origin
    into its heading. Parse before transport shaping, then give the title its
    own paragraph and keep the complete authored brief behind a native toggle.
    """
    from markdown_it import MarkdownIt

    parser = MarkdownIt()
    blocks = parser.parse(brief)
    paragraphs = [t for t in blocks if t.type == "inline" and t.level == 1]

    def plain(text: str) -> str:
        children = parser.parseInline(text)[0].children or []
        return "".join(
            " " if t.type in {"softbreak", "hardbreak"} else t.content
            for t in children if t.type in {"text", "code_inline", "image", "softbreak", "hardbreak"}
        ).strip()

    def excerpt(text: str, width: int) -> str:
        shortened = textwrap.shorten(plain(text), width=width, placeholder="…")
        # Excerpts are plain text. Cutting a code span/link/emphasis mid-way must
        # not leak formatting into the next paragraph or toggle.
        return re.sub(r"([\\`*_\[\]<>])", r"\\\1", shortened)

    next_action = re.compile(r"Next(?: action)?:\s*(.*)", re.IGNORECASE)

    def split_next_action(text: str) -> tuple[str, str]:
        lines = text.splitlines()
        for index, line in enumerate(lines):
            if next_action.match(plain(line)):
                # The label starts on this line, but Markdown soft/hard breaks
                # can continue the action through the rest of its paragraph.
                match = next_action.match(plain("\n".join(lines[index:])))
                return "\n".join(lines[:index]), match.group(1).strip() if match else ""
        return text, ""

    objective = paragraphs[0].content if paragraphs else ""
    if blocks and blocks[0].type == "heading_open" and paragraphs:
        title, _, objective = paragraphs[0].content.partition("\n")
        if not objective and len(paragraphs) > 1:
            objective = paragraphs[1].content
    elif paragraphs:
        first_line, _, remainder = objective.partition("\n")
        # A plain or emphasized title is still a paragraph to Markdown. It is
        # the envelope title, not an objective to repeat beneath that title.
        if plain(first_line).casefold() == plain(title).casefold():
            objective = remainder or (paragraphs[1].content if len(paragraphs) > 1 else "")
    objective, _ = split_next_action(objective)

    preview = [f"**{excerpt(title, 100)}**"]
    if objective:
        preview.append(excerpt(objective, 280))
    for paragraph in paragraphs:
        _, action = split_next_action(paragraph.content)
        if action:
            action = re.split(r"(?<=[.!?])\s+", action, maxsplit=1)[0]
            preview.append(f"▸ Next: {excerpt(action, 160)}")
            break

    # Keep nested/literal disclosure tags from closing our toggle early. Their
    # text remains available, and the session still receives the original bytes.
    details = re.sub(r"</?(?:details|summary)\b[^>]*>", lambda m: html.escape(m[0]), brief, flags=re.IGNORECASE)
    preview.append(f"<details>\n<summary>Details</summary>\n\n{details}\n\n</details>")
    if origin_line:
        preview.append(f"*{origin_line}*")
    return "\n\n".join(preview)


async def _deliver_brief(adapter, dest: SessionSource, brief: str, origin_line: str = "", *, title: str = "Opening brief") -> None:
    """Show a compact preview, then admit the complete original brief as the first turn.
    The turn is internal (the calling session already holds the user's authority) and never a
    gateway command, so a brief starting with ``/`` is read as text. A brief that could not be
    shown is not admitted, so the session never works from a brief the user cannot see; the caller
    reports the failure.

    Internal events skip the ``hermes pause`` gate, so it is checked here: a paused Hermes starts
    no new work, and the brief would start a new session's first turn."""
    from agent.estop import paused_reply
    from gateway.wake import admit_internal_event
    paused = paused_reply()
    if paused is not None:
        raise RuntimeError(paused)
    session_brief = f"{origin_line}\n\n{brief}" if origin_line else brief
    shown_brief = _opening_brief_display(brief, title, origin_line)
    sent = await adapter.send(str(dest.chat_id), shown_brief,
                              metadata={"thread_id": str(dest.thread_id), "rich_fallback_content": session_brief})
    if not getattr(sent, "success", False):
        raise RuntimeError(getattr(sent, "error", None) or "Telegram did not accept the message")
    # Anchor the turn on the shown brief, as a typed message anchors its own turn, so the session's
    # replies thread under it instead of relying on the anchor-less synthetic-send fallback.
    anchor = str(sent.message_id) if getattr(sent, "message_id", None) else None
    event = MessageEvent(text=session_brief, message_type=MessageType.TEXT, source=replace_source(dest, message_id=anchor),
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
    session_id = await asyncio.to_thread(
        runner.session_store.peek_session_id, runner._session_key_for_source(dest))
    return session_id is not None


async def create_topic_session(runner, source: SessionSource, spec: TopicSpec) -> dict:
    """Create a DM topic in ``source``'s chat, bind a new session to it with the requested title,
    icon, model and reasoning, and optionally start it with an opening brief."""
    adapter = _adapter(runner, source, "_create_dm_topic")
    root = replace_source(source, thread_id=None, message_id=None)
    name = _clean_name(runner, spec.name)
    icon_id, warnings = await _create_icon(adapter, spec.icon)
    effort = _parse_reasoning(spec.reasoning)
    model = await _resolve_model(runner, root, spec)

    thread_id = await adapter._create_dm_topic(int(source.chat_id), name=name, icon_custom_emoji_id=icon_id)
    if not thread_id:
        raise TopicRequestError("Telegram did not create the topic; the gateway log has the reason.")
    dest = replace_source(root, thread_id=str(thread_id))
    settings = {
        "warnings": [], "model_status": "unchanged", "reasoning_status": "unchanged",
        "model_effective_when": "unchanged", "reasoning_effective_when": "unchanged",
    }
    try:
        entry = await runner.async_session_store.get_or_create_session(dest)
        title = name
        if runner._session_db is not None:
            title = await runner._session_db.set_session_title_in_lineage(entry.session_id, name)
        await asyncio.to_thread(runner._record_telegram_topic_binding, dest, entry)
        if icon_id:
            await _record_manual_icon(runner, dest, str(thread_id), spec.icon, icon_id)
        settings = await _apply_session_settings(
            runner, dest, entry, model, effort,
            reasoning_reset=spec.reasoning.strip().lower() in {"default", "reset"},
            announce=False,
        )
        warnings.extend(settings["warnings"])
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
            origin_line = await _origin_line(runner, adapter, source)
            await _deliver_brief(adapter, dest, brief, origin_line, title=name)
        except Exception as exc:
            logger.warning("Opening brief for topic %s was not delivered", thread_id, exc_info=True)
            raise TopicRequestError(
                f"Topic {thread_id} and session {entry.session_id} are set up, but the opening brief "
                f"was not delivered: {exc}. Send it with relay to {relay}.") from exc
    readback = await _session_settings_readback(runner, dest, entry, model)
    warnings.extend(readback.pop("warnings"))
    return {
        "action": "create", "thread_id": str(thread_id), "session_id": entry.session_id,
        "relay": relay, "title": title,
        "icon": spec.icon if icon_id else None,
        "reasoning": effort, "brief_delivered": bool(brief), "warnings": warnings,
        "model_status": settings["model_status"], "reasoning_status": settings["reasoning_status"],
        **readback,
        "model_effective_when": settings["model_effective_when"],
        "reasoning_effective_when": settings["reasoning_effective_when"],
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
    reasoning_reset = spec.reasoning.strip().lower() in {"default", "reset"}
    effort = _parse_reasoning(spec.reasoning)
    model = None
    if not (name or icon_id) and not await _topic_exists(runner, dest, thread_id):
        # A settings-only edit never calls Telegram, so a mistyped thread_id would otherwise get
        # a routing entry, a session and a binding for a topic that does not exist.
        raise TopicRequestError(
            f"No topic {thread_id} in this chat is known to Hermes. Check thread_id, or give name or "
            f"icon so Telegram confirms the topic.")
    if spec.model or spec.provider:
        # Resolution can block for seconds, but commit_session_model_selection_with_status re-checks
        # the running slot under the model lock and queues rather than swapping a live client.
        model = await _resolve_model(runner, dest, spec, session_key=session_key)

    if name or icon_id:
        await set_topic_appearance(runner, dest, thread_id, name=name, icon_emoji=spec.icon, icon_id=icon_id)
    # The topic's own session, bound as a turn there binds it: it finishes a create whose setup
    # failed after Telegram made the topic, and never repoints a bound topic.
    entry = await _topic_session(runner, dest, session_key)
    title = None
    if name and runner._session_db is not None:
        title = await runner._session_db.set_session_title_in_lineage(entry.session_id, name)
    warnings = []
    settings = {
        "model_status": "unchanged", "reasoning_status": "unchanged",
        "model_effective_when": "unchanged", "reasoning_effective_when": "unchanged",
    }
    if model is not None or effort is not None or reasoning_reset:
        settings = await _apply_session_settings(
            runner, dest, entry, model, effort, reasoning_reset=reasoning_reset,
        )
        warnings = settings["warnings"]
    readback = await _session_settings_readback(runner, dest, entry, model)
    warnings.extend(readback.pop("warnings"))
    return {
        "action": "edit", "thread_id": thread_id, "session_id": entry.session_id,
        "relay": relay_address(runner, source, entry.session_id), "name": name, "title": title,
        "icon": spec.icon if icon_id else None,
        "reasoning": effort, "warnings": warnings,
        "model_status": settings["model_status"], "reasoning_status": settings["reasoning_status"],
        **readback,
        "model_effective_when": settings["model_effective_when"],
        "reasoning_effective_when": settings["reasoning_effective_when"],
    }
