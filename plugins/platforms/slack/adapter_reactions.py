"""Slack reaction identity, observer publication and opt-in message routing."""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, ClassVar, Optional

from gateway.platforms._shared import get_scoped_secret as _get_scoped_secret

logger = logging.getLogger(__name__)


class SlackReactionsMixin:
    # Reaction names → unicode emoji, so skills matching on ``text`` see the same character
    # whether the user typed it or reacted with it.
    _REACTION_EMOJI_MAP: ClassVar[dict[str, str]] = {
        "thumbsup": "👍", "+1": "👍", "thumbsdown": "👎", "-1": "👎", "white_check_mark": "✅",
        "heavy_check_mark": "✅", "x": "❌", "no_entry": "⛔", "warning": "⚠️", "rotating_light": "🚨",
        "eyes": "👀", "rocket": "🚀", "tada": "🎉", "fire": "🔥", "wave": "👋",
        "pray": "🙏", "joy": "😂", "heart": "❤️"}

    async def _reaction_chat_type(
        self, client: Any, channel_id: str, team_id: str,
    ) -> Optional[str]:
        """Resolve the authorization class for a reaction from Slack channel identity.

        ``reaction_added`` payloads omit ``channel_type`` in practice, and a ``G`` id is
        ambiguous: Slack uses it for both private channels and MPIMs. Never let that
        ambiguous id use the group policy; modern MPIMs may also have C-prefixed ids.
        Ask Slack for the conversation kind and fail closed when it cannot be resolved.
        """
        channel_id = str(channel_id or "")
        if channel_id.startswith("D"):
            return "dm"
        cache_key = (str(team_id or ""), channel_id)
        cached = getattr(self, "_reaction_channel_type_cache", {}).get(cache_key)
        if cached:
            return cached
        if client is None or not channel_id:
            return None
        try:
            response = await client.conversations_info(channel=channel_id)
            payload = response if isinstance(response, dict) else getattr(response, "data", {})
            channel = payload.get("channel") or {}
            if payload.get("ok") is not True or channel.get("id") != channel_id:
                return None
            if channel.get("is_im") or channel.get("is_mpim"):
                chat_type = "dm"
            elif channel.get("is_channel") or channel.get("is_group"):
                chat_type = "group"
            else:
                return None
        except Exception:
            logger.debug("[Slack] Could not resolve reaction channel %s", channel_id, exc_info=True)
            return None
        cache = getattr(self, "_reaction_channel_type_cache", None)
        if cache is None:
            cache = self._reaction_channel_type_cache = {}
        cache[cache_key] = chat_type
        self._trim_oldest_dict_entries(cache, self._CHANNEL_TEAM_MAX)
        return chat_type

    def _cached_reaction_thread_ts(
        self, channel_id: str, message_ts: str, team_id: str,
    ) -> str:
        """Recover a reacted-to message's thread root from cached Slack replies.

        Slack reaction payloads omit ``item.thread_ts``. Only use a cache entry whose
        raw messages contain the exact reacted-to timestamp; a cache miss stays empty
        rather than inventing a thread id or making an authorization-time API call.
        """
        cache = getattr(self, "_thread_context_cache", {})
        prefix = f"{channel_id}:"
        suffix = f":{team_id}"
        for cache_key, entry in cache.items():
            if not cache_key.startswith(prefix) or not cache_key.endswith(suffix):
                continue
            root_ts = cache_key[len(prefix):-len(suffix)] if suffix else cache_key[len(prefix):]
            for message in getattr(entry, "messages", []) or []:
                if str(message.get("ts") or "") != str(message_ts):
                    continue
                return str(message.get("thread_ts") or root_ts or "")
        return ""

    async def _handle_slack_reaction(self, event: dict, removed: bool = False) -> None:
        """Forward reactions as a synthetic ``reaction:<added|removed>:<emoji>`` message
        (Feishu/Photon convention) from the reactor in the reacted-to thread, so the normal auth
        gate applies. Hooks fire for every non-self reaction; agent routing is opt-in via
        ``reaction_triggers`` and, without an explicit allowlist, only on the bot's own messages."""
        item = event.get("item") or {}
        if item.get("type") != "message":
            return
        channel_id = item.get("channel")
        msg_ts = item.get("ts")
        reaction_name = event.get("reaction") or ""
        user_id = event.get("user")
        if not channel_id or not msg_ts or not user_id or not reaction_name:
            return
        # Self-reactions (e.g. :eyes: lifecycle marker) would feed back.
        if self._bot_user_id and user_id == self._bot_user_id:
            return
        team_id = self._channel_team.get(channel_id) or ""
        if not team_id and self._team_clients:
            team_id = next(iter(self._team_clients))
        client = self._team_clients.get(team_id) if team_id else None
        action = "removed" if removed else "added"
        # Hooks fire before the opt-in gate so consumers see every human
        # reaction. getattr: tests build adapters via object.__new__.
        reaction_handler = getattr(self, "_reaction_handler", None)
        if reaction_handler is not None:
            try:
                await reaction_handler({
                    "platform": "slack", "event_name": f"reaction:{action}",
                    "reaction": reaction_name, "user_id": user_id,
                    "item_user_id": event.get("item_user"), "item_type": item.get("type"),
                    "channel_id": channel_id, "message_ts": msg_ts, "team_id": team_id,
                    "event_ts": event.get("event_ts"), "raw_event": event})
            except Exception:  # pragma: no cover - hook contract is non-blocking
                logger.debug("[Slack] reaction hook forwarding failed", exc_info=True)
        # Publish the stable platform-event contract through the gateway-owned,
        # profile-bound handler. This remains independent of reaction_triggers: the
        # latter controls agent routing, not observation hooks. Skip users.info and
        # source construction when nothing is listening, matching Telegram.
        platform_event_handler = getattr(self, "_platform_event_handler", None)
        from hermes_cli.lifecycle import has_hook
        if platform_event_handler is not None and has_hook("gateway_platform_event"):
            try:
                chat_type = await self._reaction_chat_type(client, channel_id, team_id)
                # Observation must fail closed without disabling independent opt-in routing.
                if chat_type is not None:
                    reactor_is_bot = await self._resolve_user_is_bot(user_id, channel_id, team_id) if client else False
                    # Use Slack's conversation identity for auth. In particular, MPIMs have
                    # G-prefixed ids and must use the same DM policy as message intake; the
                    # event's optional channel_type is not an authorization source.
                    # Slack reaction payloads normally omit item.thread_ts. A previously fetched
                    # thread cache can provide an exact root without an authorization-time API call;
                    # otherwise leave group reactions unscoped rather than inventing a thread id.
                    thread_ts = str(item.get("thread_ts") or event.get("thread_ts") or "")
                    if not thread_ts:
                        thread_ts = self._cached_reaction_thread_ts(channel_id, msg_ts, team_id)
                    thread_id = thread_ts or (str(msg_ts) if chat_type == "dm" else None)
                    source = self.build_source(
                        chat_id=str(channel_id), chat_type=chat_type, user_id=str(user_id),
                        thread_id=thread_id or None,
                        scope_id=str(team_id) if team_id else None, message_id=str(msg_ts),
                        is_bot=bool(reactor_is_bot),
                    )
                    await platform_event_handler({
                        "platform": "slack", "event_type": "reaction",
                        "payload": {
                            "reaction": str(reaction_name)[:128], "action": action,
                            "user_id": str(user_id)[:128], "is_bot": bool(reactor_is_bot),
                            "item_user_id": str(event.get("item_user"))[:128]
                            if event.get("item_user") else None,
                            "chat_id": str(channel_id)[:128], "message_id": str(msg_ts)[:128],
                            "thread_id": thread_id or None, "team_id": str(team_id)[:128] if team_id else None,
                            "event_ts": str(event.get("event_ts"))[:128]
                            if event.get("event_ts") else None,
                        },
                    }, source)
            except Exception:
                logger.debug("[Slack] gateway platform-event forwarding failed", exc_info=True)
        # None → routing disabled; empty set → all emoji; non-empty → allowlist.
        triggers = self._slack_reaction_triggers()
        if triggers is None:
            return
        explicit_allowlist = bool(triggers)
        if explicit_allowlist and reaction_name.strip(":") not in triggers:
            return
        thread_ts = await self._reaction_thread_ts(
            client, channel_id, msg_ts, event, team_id, explicit_allowlist)
        if thread_ts is None:
            return
        await self._handle_slack_message(
            self._synthetic_reaction_event(event, action, thread_ts, team_id))

    def _synthetic_reaction_event(
        self, event: dict, action: str, thread_ts: str, team_id: str) -> dict:
        """Message-shaped event for a reaction. The reaction's own event_ts keeps the deduplicator
        from conflating it with the reacted-to message; ``_hermes_force_process`` skips the mention
        requirement (user auth and allowed_channels still apply); ``_hermes_reaction`` is
        informational. An optional handoff target channel replaces the reacted-to channel; a
        channel-only target is a handoff, not a reply — respond top-level there."""
        item = event.get("item") or {}
        channel_id, msg_ts = item.get("channel"), item.get("ts")
        reaction_name, user_id = event.get("reaction") or "", event.get("user")
        emoji_text = self._REACTION_EMOJI_MAP.get(reaction_name, reaction_name)
        synthetic: dict = {
            "type": "message",
            "user": user_id,
            "text": f"reaction:{action}:{emoji_text}",
            "channel": channel_id,
            "ts": event.get("event_ts") or f"reaction-{msg_ts}-{reaction_name}-{user_id}",
            "thread_ts": thread_ts,
            "_hermes_force_process": True,
            "_hermes_reaction": {
                "name": reaction_name, "action": action, "reacted_to_ts": msg_ts,
                "event_ts": event.get("event_ts")}}
        if team_id:
            synthetic["team"] = team_id
        # Optional handoff target (#45265): route the reaction-triggered turn into a configured channel (and
        # optionally thread) instead of the source thread. A channel-only target is a handoff, not a reply —
        # respond top-level there.
        target_channel, target_thread = self._slack_reaction_trigger_target()
        if target_channel:
            synthetic["channel"] = target_channel
            synthetic["channel_type"] = "im" if target_channel.startswith("D") else "channel"
            synthetic["_hermes_reaction_source_channel"] = channel_id
            if target_thread:
                synthetic["thread_ts"] = target_thread
            else:
                synthetic.pop("thread_ts", None)
                synthetic["_hermes_no_thread_response"] = True
        return synthetic

    async def _reaction_thread_ts(
        self, client, channel_id: str, msg_ts: str, event: dict, team_id: str,
        explicit_allowlist: bool) -> Optional[str]:
        """Thread to route a reaction into, or None to drop. Looks up the reacted-to message for
        thread + author; on failure the message itself is the parent (right top-level, loses
        linkage in-thread). Without an explicit allowlist only the bot's own messages route."""
        thread_ts: str = msg_ts
        item_user = event.get("item_user") or ""
        if client is not None:
            try:
                history = await client.conversations_replies(
                    channel=channel_id, ts=msg_ts, limit=1, inclusive=True)
                messages = (history or {}).get("messages") or []
                if messages:
                    first = messages[0]
                    thread_ts = first.get("thread_ts") or first.get("ts") or msg_ts
                    item_user = item_user or first.get("user") or ""
                else:
                    return thread_ts
            except Exception as e:  # pragma: no cover - network path
                logger.debug("[Slack] reaction thread_ts lookup failed for %s: %s", msg_ts, e)
                return thread_ts
        if not explicit_allowlist:
            bot_uid = self._team_bot_user_ids.get(team_id) or self._bot_user_id
            if item_user and bot_uid and item_user != bot_uid:
                return None
        return thread_ts

    def _slack_reaction_triggers(self) -> Optional[set]:
        """Reaction-routing opt-in: None = disabled (default, events acked+dropped);
        empty set = all emoji, bot's own messages only; non-empty = these emoji on
        any message. From ``slack.reaction_triggers`` or ``SLACK_REACTION_TRIGGERS``."""
        raw = self.config.extra.get("reaction_triggers")
        if raw is None:
            raw = _get_scoped_secret("SLACK_REACTION_TRIGGERS") or None
        if raw is None:
            return None
        if isinstance(raw, bool):
            return set() if raw else None
        if isinstance(raw, (list, tuple, set)):
            return {str(p).strip().strip(":") for p in raw if str(p).strip().strip(":")}
        text = str(raw or "").strip()
        if not text or text.lower() in {"false", "0", "no", "off"}:
            return None
        if text.lower() in {"true", "1", "yes", "on", "all", "*"}:
            return set()
        return {p.strip().strip(":") for p in re.split(r"[,\s]+", text) if p.strip().strip(":")}

    def _slack_reaction_trigger_target(self) -> tuple[str, str]:
        """Optional (channel, thread) reaction handoff target: ``C123`` or ``C123:<ts>``.
        Empty (default) routes into the reacted-to message's thread."""
        raw = self.config.extra.get("reaction_trigger_target")
        if raw is None:
            raw = _get_scoped_secret("SLACK_REACTION_TRIGGER_TARGET", "")
        channel, _, thread = str(raw or "").strip().partition(":")
        return channel.strip(), thread.strip()
