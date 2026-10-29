"""Best-effort Slack thread titles, upgraded by the shared background session titler.

DM threads are renamed natively. Channel threads have no native title, so the generated
title becomes the root's bold first line, and only on roots whose text we may rewrite.
"""
from __future__ import annotations

import asyncio
import html
import logging
import re
from typing import Any, Optional

logger = logging.getLogger(__name__)

# agents.sessions.rename answers invalid_name for typographic apostrophes.
_SLACK_TITLE_DROP = str.maketrans({"\u2018": None, "\u2019": None})


# The opener shape the Telegram migration posted as the owner (user token), e.g.
# "*Hindsight Maintenance*\nMoved from Telegram. Hermes continues here ...".
_SEEDED_OPENER = re.compile(r"\*([^*\n]+)\*\n(Moved from Telegram\b.*)", re.DOTALL)
_BOLD_FIRST_LINE = re.compile(r"\*[^*\n]+\*(?:\n|$)")
_MAX_BLOCKS = 50


def _slack_title_text(title: str) -> str:
    return re.sub(r"\s+", " ", title.translate(_SLACK_TITLE_DROP)).strip()


def _bold_mrkdwn(title: str) -> str:
    escaped = title.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return f"*{escaped.replace('*', '')}*"


def _retitled_rich_text(blocks: Any, old_title: str, title: str) -> Any:
    """Swap only the leading bold run Slack renders for the opener's first line, else None."""
    if not isinstance(blocks, list) or not blocks:
        return None
    first = blocks[0]
    sections = first.get("elements") if isinstance(first, dict) and first.get("type") == "rich_text" else None
    section = sections[0] if isinstance(sections, list) and sections else None
    runs = section.get("elements") if isinstance(section, dict) else None
    head = runs[0] if isinstance(runs, list) and runs else None
    if not (isinstance(head, dict) and head.get("type") == "text" and head.get("text") == old_title
            and (head.get("style") or {}).get("bold")):
        return None
    head = {**head, "text": title}
    section = {**section, "elements": [head, *runs[1:]]}
    return [{**first, "elements": [section, *sections[1:]]}, *blocks[1:]]


def _root_title_edit(root: dict, title: str, bot_user_id: str) -> Optional[tuple[str, dict]]:
    """``("bot" | "owner", chat.update kwargs)`` for a root we may retitle; None leaves it untouched.

    Our bot's root gains a bold first line unless it already leads with a title. A human root
    is edited only in the seeded opener shape, never after any edit (ours or theirs, which
    survives restarts unlike the in-memory guard), and only its bold first line changes.
    """
    text = root.get("text") or ""
    blocks = root.get("blocks")
    if bot_user_id and root.get("user") == bot_user_id:
        if _BOLD_FIRST_LINE.match(text) or (blocks and blocks[0].get("type") == "header"):
            return None
        heading = _bold_mrkdwn(title)
        edit: dict[str, Any] = {"text": f"{heading}\n{text}" if text else heading}
        if blocks:
            # chat.update keeps the old blocks when none are sent, so the heading must be a block.
            if len(blocks) >= _MAX_BLOCKS:
                return None
            edit["blocks"] = [{"type": "section", "text": {"type": "mrkdwn", "text": heading}}, *blocks]
        return "bot", edit
    match = None if root.get("edited") else _SEEDED_OPENER.fullmatch(text)
    old_title = html.unescape(match.group(1)) if match else ""
    if not old_title or old_title == title:
        return None
    edit = {"text": f"{_bold_mrkdwn(title)}\n{match.group(2)}"}
    if blocks:
        retitled = _retitled_rich_text(blocks, old_title, title)
        if retitled is None:
            return None
        edit["blocks"] = retitled
    return "owner", edit


class SlackThreadTitlesMixin:
    config: Any
    _app: Any
    _titled_assistant_threads: set
    _upgraded_assistant_threads: set
    _TITLED_ASSISTANT_THREADS_MAX: int
    _workspace_thread_key: Any
    _get_client: Any
    _evict_oldest_by_ts: Any
    _team_bot_user_ids: dict
    _bot_user_id: Any
    _proxy_url: Any
    _new_web_client: Any
    _slack_api_human_users: Any

    def _assistant_thread_title_enabled(self) -> bool:
        raw = self.config.extra.get("assistant_thread_titles", True)
        if isinstance(raw, str):
            return raw.strip().lower() not in {"0", "false", "no", "off"}
        return bool(raw)

    async def _set_assistant_thread_title(
        self, channel_id: str, thread_ts: str, title_source: str, *, team_id: str = "",
        is_reply: bool = False) -> None:
        """Best-effort title for visible Slack AI DM threads."""
        if (
            not self._app or not channel_id or not thread_ts or not title_source
            or not self._assistant_thread_title_enabled()):
            return
        key = self._workspace_thread_key(team_id, channel_id, thread_ts)
        if not key or key in self._titled_assistant_threads or key in self._upgraded_assistant_threads:
            return
        title = _slack_title_text(title_source)
        if not title or title.startswith("/"):
            return
        title = title[:77].rstrip() + "..." if len(title) > 80 else title
        try:
            # The in-memory guard resets on restart. A reply must not overwrite a title the
            # thread already has (generated or set by the user) with its own raw text.
            if is_reply and await self._thread_has_title(channel_id, thread_ts, team_id):
                self._titled_assistant_threads.add(key)
                return
            await self._rename_thread(channel_id, thread_ts, title, team_id)
        except Exception as e:
            logger.debug("[Slack] session title set failed: %s", e, exc_info=True)
            return
        self._titled_assistant_threads.add(key)
        # Evict oldest thread_ts first so recently titled threads keep their guard.
        self._evict_oldest_by_ts(
            self._titled_assistant_threads, self._TITLED_ASSISTANT_THREADS_MAX, lambda e: e[2])

    async def _thread_has_title(self, channel_id: str, thread_ts: str, team_id: str) -> bool:
        response = await self._get_client(channel_id, team_id=team_id).conversations_replies(
            channel=channel_id, ts=thread_ts, limit=1)
        root = (response.get("messages") or [{}])[0]
        return any((root.get(field) or {}).get("title") for field in ("agent_session", "assistant_app_thread"))

    async def _rename_thread(self, channel_id: str, thread_ts: str, title: str, team_id: str) -> None:
        """Rename via agents.sessions.rename; legacy Assistant roots answer not_authorized and need setTitle."""
        from slack_sdk.errors import SlackApiError
        from .adapter import _session_title_method
        client = self._get_client(channel_id, team_id=team_id)
        rename = _session_title_method(client)
        try:
            await rename(channel_id=channel_id, thread_ts=thread_ts, title=title)
        except SlackApiError as e:
            legacy = client.assistant_threads_setTitle
            if rename is legacy or (getattr(e, "response", None) or {}).get("error") != "not_authorized":
                raise
            await legacy(channel_id=channel_id, thread_ts=thread_ts, title=title)

    def configure_session_titles(self, agent, ctx, session_current) -> bool:
        """Opt this thread into the shared titler without running a model on Slack's intake path."""
        source = ctx.source
        if (source.chat_type not in {"dm", "group"} or not source.thread_id
                or not self._assistant_thread_title_enabled()):
            return False
        agent._title_style = "verb_first"
        loop = ctx._loop_for_step
        upgrade = "_upgrade_assistant_thread_title" if source.chat_type == "dm" else "_title_channel_thread_root"

        def on_title(title, provenance, *, display_title=None, **_kwargs):
            if provenance != "llm":
                return
            coroutine = getattr(self, upgrade)(source, display_title or title, session_current)
            try:
                asyncio.run_coroutine_threadsafe(coroutine, loop)
            except Exception:
                coroutine.close()
                logger.debug("[Slack] title upgrade scheduling failed", exc_info=True)

        agent._on_session_title = on_title
        return True

    def _reserve_title_upgrade(self, source, title: str, current) -> Optional[str]:
        """Reserve before I/O: an ambiguous API failure must not retry over a manual rename."""
        if not self._app or not self._assistant_thread_title_enabled() or not current():
            return None
        key = self._workspace_thread_key(source.scope_id or "", source.chat_id, source.thread_id)
        if not key or key in self._upgraded_assistant_threads:
            return None
        title = _slack_title_text(title)
        if not title or len(title) > 80 or title.startswith("/"):
            return None
        self._upgraded_assistant_threads.add(key)
        self._evict_oldest_by_ts(
            self._upgraded_assistant_threads, self._TITLED_ASSISTANT_THREADS_MAX, lambda e: e[2])
        return title

    async def _upgrade_assistant_thread_title(self, source, title: str, current) -> None:
        try:
            title = self._reserve_title_upgrade(source, title, current)
            if title:
                await self._rename_thread(source.chat_id, source.thread_id, title, source.scope_id or "")
        except Exception:
            logger.debug("[Slack] generated title upgrade failed", exc_info=True)

    async def _title_channel_thread_root(self, source, title: str, current) -> None:
        """Show the generated title on a channel thread's root.

        agents.sessions.rename covers channel threads only when Slack holds an agent session for
        them (else session_not_found); Hermes never opens one there, so channels do not use it.
        """
        from slack_sdk.errors import SlackApiError
        try:
            title = self._reserve_title_upgrade(source, title, current)
            if not title:
                return
            team_id = source.scope_id or ""
            client = self._get_client(source.chat_id, team_id=team_id)
            response = await client.conversations_replies(channel=source.chat_id, ts=source.thread_id, limit=1)
            root = (response.get("messages") or [{}])[0]
            if root.get("ts") != source.thread_id:
                return
            bot_user_id = self._team_bot_user_ids.get(team_id) or self._bot_user_id or ""
            edit = _root_title_edit(root, title, bot_user_id)
            if edit is None:
                return
            author, kwargs = edit
            if author == "owner":
                client = await self._opener_author_client(team_id, root.get("user") or "")
                if client is None:
                    return
            await client.chat_update(channel=source.chat_id, ts=source.thread_id, **kwargs)
        except SlackApiError as e:
            logger.info("[Slack] channel thread root title rejected: %s",
                        (getattr(e, "response", None) or {}).get("error") or e)
        except Exception:
            logger.debug("[Slack] channel thread root title failed", exc_info=True)

    async def _opener_author_client(self, team_id: str, user_id: str) -> Any:
        """A client for the owner's ``SLACK_USER_TOKEN`` when it authenticates as the root's allowlisted author."""
        from .adapter import _get_scoped_secret, _slack_response_payload
        if user_id not in self._slack_api_human_users():
            return None
        token = _get_scoped_secret("SLACK_USER_TOKEN")
        if not token:
            logger.info("[Slack] SLACK_USER_TOKEN is not configured; seeded thread opener keeps its title")
            return None
        client = self._new_web_client(token, self._proxy_url)
        auth = _slack_response_payload(await client.auth_test())
        if (not auth.get("ok") or auth.get("user_id") != user_id or auth.get("bot_id")
                or (team_id and auth.get("team_id") != team_id)):
            logger.info("[Slack] SLACK_USER_TOKEN is not the opener author's; seeded thread opener keeps its title")
            return None
        return client
