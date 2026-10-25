"""Best-effort Slack thread titles, upgraded by the shared background session titler."""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

logger = logging.getLogger(__name__)


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
        self, channel_id: str, thread_ts: str, title_source: str, *, team_id: str = "") -> None:
        """Best-effort title for visible Slack AI DM threads."""
        if (
            not self._app or not channel_id or not thread_ts or not title_source
            or not self._assistant_thread_title_enabled()):
            return
        key = self._workspace_thread_key(team_id, channel_id, thread_ts)
        if not key or key in self._titled_assistant_threads or key in self._upgraded_assistant_threads:
            return
        title = re.sub(r"\s+", " ", title_source).strip()
        if not title or title.startswith("/"):
            return
        title = title[:77].rstrip() + "..." if len(title) > 80 else title
        try:
            from .adapter import _session_title_method
            _set_title = _session_title_method(self._get_client(channel_id, team_id=team_id))
            await _set_title(channel_id=channel_id, thread_ts=thread_ts, title=title)
        except Exception as e:
            logger.debug("[Slack] session title set failed: %s", e, exc_info=True)
            return
        self._titled_assistant_threads.add(key)
        # Evict oldest thread_ts first so recently titled threads keep their guard.
        self._evict_oldest_by_ts(
            self._titled_assistant_threads, self._TITLED_ASSISTANT_THREADS_MAX, lambda e: e[2])

    def configure_session_titles(self, agent, ctx) -> bool:
        """Opt this DM into the shared titler without running a model on Slack's intake path."""
        source = ctx.source
        if source.chat_type != "dm" or not source.thread_id or not self._assistant_thread_title_enabled():
            return False
        agent._title_style = "verb_first"
        loop = ctx._loop_for_step
        current = ctx._run_still_current

        def on_title(title, provenance, *, display_title=None, **_kwargs):
            if provenance != "llm":
                return
            coroutine = self._upgrade_assistant_thread_title(source, display_title or title, current)
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
            title = re.sub(r"\s+", " ", title).strip()
            if not title or len(title) > 80 or title.startswith("/"):
                return
            self._upgraded_assistant_threads.add(key)
            self._evict_oldest_by_ts(
                self._upgraded_assistant_threads, self._TITLED_ASSISTANT_THREADS_MAX, lambda e: e[2])
            from .adapter import _session_title_method
            rename = _session_title_method(self._get_client(source.chat_id, team_id=source.scope_id or ""))
            await rename(channel_id=source.chat_id, thread_ts=source.thread_id, title=title)
        except Exception:
            logger.debug("[Slack] generated title upgrade failed", exc_info=True)
