"""Best-effort Slack thread titles, upgraded by the shared background session titler."""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

# agents.sessions.rename answers invalid_name for typographic apostrophes.
_SLACK_TITLE_DROP = str.maketrans({"\u2018": None, "\u2019": None})


def _slack_title_text(title: str) -> str:
    return re.sub(r"\s+", " ", title.translate(_SLACK_TITLE_DROP)).strip()


class SlackThreadTitlesMixin:
    config: Any
    _app: Any
    _titled_assistant_threads: set
    _upgraded_assistant_threads: set
    _TITLED_ASSISTANT_THREADS_MAX: int
    _workspace_thread_key: Any
    _get_client: Any
    _evict_oldest_by_ts: Any

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
        """Opt this DM into the shared titler without running a model on Slack's intake path."""
        source = ctx.source
        if source.chat_type != "dm" or not source.thread_id or not self._assistant_thread_title_enabled():
            return False
        agent._title_style = "verb_first"
        loop = ctx._loop_for_step

        def on_title(title, provenance, *, display_title=None, **_kwargs):
            if provenance != "llm":
                return
            coroutine = self._upgrade_assistant_thread_title(source, display_title or title, session_current)
            try:
                asyncio.run_coroutine_threadsafe(coroutine, loop)
            except Exception:
                coroutine.close()
                logger.debug("[Slack] title upgrade scheduling failed", exc_info=True)

        agent._on_session_title = on_title
        return True

    async def _upgrade_assistant_thread_title(self, source, title: str, current) -> None:
        """Reserve before I/O: an ambiguous API failure must not retry over a manual rename."""
        try:
            if not self._app or not self._assistant_thread_title_enabled() or not current():
                return
            key = self._workspace_thread_key(source.scope_id or "", source.chat_id, source.thread_id)
            if not key or key in self._upgraded_assistant_threads:
                return
            title = _slack_title_text(title)
            if not title or len(title) > 80 or title.startswith("/"):
                return
            self._upgraded_assistant_threads.add(key)
            self._evict_oldest_by_ts(
                self._upgraded_assistant_threads, self._TITLED_ASSISTANT_THREADS_MAX, lambda e: e[2])
            await self._rename_thread(source.chat_id, source.thread_id, title, source.scope_id or "")
        except Exception:
            logger.debug("[Slack] generated title upgrade failed", exc_info=True)
