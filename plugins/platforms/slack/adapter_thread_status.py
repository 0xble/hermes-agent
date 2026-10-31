"""Free-text thread progress alongside transition-only native agent sessions."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Literal, Optional

from agent.i18n import t

logger = logging.getLogger(__name__)


class SlackThreadStatusMixin:
    def _status_entry(self, chat_id, team_id, thread_ts, owner):
        key = self._workspace_thread_key(team_id, chat_id, str(thread_ts))
        previous = self._active_status_threads.get(key)
        if previous and not previous.get("ending") and previous.get("owner") is owner:
            return key, previous
        # Both protocols mutate session state through Slack's compatibility bridge.
        # Successors share the call lock, not the retired turn's lifecycle.
        entry = {
            "thread_ts": str(thread_ts), "team_id": str(team_id or ""),
            "started": time.monotonic(), "owner": owner,
            "status_lock": previous.get("status_lock", asyncio.Lock()) if previous else asyncio.Lock(),
        }
        self._active_status_threads[key] = entry
        self._evict_oldest_by_ts(
            self._active_status_threads, self._ACTIVE_STATUS_THREADS_MAX, lambda k: k[2])
        return key, entry

    async def _set_agent_session_status(
        self, chat_id: str, team_id: str, thread_ts: str,
        status: Literal["processing", "active"],
    ) -> bool:
        from plugins.platforms.slack.adapter import _sdk_supports_agent_sessions

        if not _sdk_supports_agent_sessions():
            return True
        try:
            client = self._get_client(chat_id, team_id=team_id)
            await client.agents_sessions_setStatus(channel_id=chat_id, thread_ts=thread_ts, status=status)
            return True
        except Exception as error:
            logger.info("[Slack] agents.sessions.setStatus status=%s channel=%s thread=%s team=%s failed: %s",
                        status, chat_id, thread_ts, team_id, error, exc_info=True)
            return False

    async def send_typing(self, chat_id: str, metadata=None) -> None:
        """Keep legacy phrases while creating the native processing session once per turn."""
        if self._suppressed_ignored(chat_id, "typing/status in", level=logging.DEBUG) or not self._app:
            return
        thread_ts = self._resolve_thread_ts(
            reply_to=metadata.get("message_id"), metadata=metadata) if metadata else None
        if not thread_ts:
            return
        team_id = self._metadata_team_id(metadata) or self._channel_team.get(chat_id, "")
        owner = getattr(self, "_typing_refresh_owners", {}).get(self._typing_scope_key(chat_id, metadata))
        key, entry = self._status_entry(chat_id, team_id, thread_ts, owner)
        async with entry["status_lock"]:
            if (self._active_status_threads.get(key) is not entry or entry.get("ending")
                    or self._typing_owner_retired(chat_id, metadata, owner or asyncio.current_task())):
                return
            if not entry.get("processing_attempted"):
                # Mark before awaiting, including failure, so refreshes cannot exhaust Tier 3.
                entry["processing_attempted"] = True
                await self._set_agent_session_status(chat_id, team_id, str(thread_ts), "processing")
            if (self._active_status_threads.get(key) is not entry or entry.get("ending")
                    or self._typing_owner_retired(chat_id, metadata, owner or asyncio.current_task())):
                return
            status = self.get_status_text(chat_id, metadata) or getattr(self.config, "typing_status_text", None)
            status = status or self._default_status_text(entry.get("started"))
            await self._set_thread_status(chat_id, team_id, thread_ts, status, "failed")
        if self._typing_owner_retired(chat_id, metadata, owner or asyncio.current_task()):
            return
        # A late set after final cleanup must not leave an idle indicator behind.
        if (key not in self._active_status_threads
                or (owner is not None and not self._typing_is_active(chat_id, metadata))):
            await self.stop_typing(chat_id, metadata=metadata)

    async def _set_thread_status(
        self, chat_id: str, team_id: str, thread_ts: str, status: str, fail_label: str,
    ) -> None:
        from plugins.platforms.slack.adapter import _session_status_method

        try:
            setter = _session_status_method(self._get_client(chat_id, team_id=team_id))
            await setter(channel_id=chat_id, thread_ts=thread_ts, status=status)
        except Exception as error:
            logger.debug("[Slack] assistant.threads.setStatus %s: %s", fail_label, error, exc_info=True)

    @staticmethod
    def _default_status_text(started: Optional[float]) -> str:
        """Live phrases win over the elapsed fallback for long-running turns."""
        elapsed = int(time.monotonic() - started) if started is not None else 0
        if elapsed < 30:
            return t("platform.slack.status.thinking")
        mins, secs = divmod(elapsed, 60)
        return t("platform.slack.status.still_working",
                 elapsed=f"{mins}m{secs:02d}s" if mins else f"{secs}s")

    def _status_clear_target(self, chat_id, metadata):
        md = metadata or {}
        thread_ts = str(md.get("thread_id") or md.get("thread_ts") or "")
        team_id = self._metadata_team_id(metadata)
        if thread_ts and team_id:
            key = self._workspace_thread_key(team_id, chat_id, thread_ts)
            return key, self._active_status_threads.get(key)
        matches = [key for key in self._active_status_threads
                   if key[1] == str(chat_id) and (not team_id or key[0] == team_id)
                   and (not thread_ts or key[2] == thread_ts)]
        if len(matches) == 1:
            key = matches[0]
            return key, self._active_status_threads[key]
        if matches or not thread_ts:
            return None, None
        # An exact untracked root can still be cleared after restart/eviction.
        return self._workspace_thread_key(team_id, chat_id, thread_ts), None

    async def stop_typing(self, chat_id: str, metadata=None) -> None:
        """Clear both protocols, retaining the target if native active fails."""
        if self._suppressed_ignored(chat_id, "status clear in", level=logging.DEBUG) or not self._app:
            return
        key, entry = self._status_clear_target(chat_id, metadata)
        if key is None:
            return
        team_id, _, thread_ts = key
        md = {"thread_id": thread_ts, "slack_team_id": team_id}
        owner = getattr(self, "_typing_refresh_owners", {}).get(self._typing_scope_key(chat_id, md))
        if entry is None:
            _, entry = self._status_entry(chat_id, team_id, thread_ts, owner)
        entry["ending"] = True
        async with entry.setdefault("status_lock", asyncio.Lock()):
            if (self._active_status_threads.get(key) is not entry
                    or self._typing_owner_retired(chat_id, md, owner or asyncio.current_task())):
                return
            await self._set_thread_status(chat_id, team_id, thread_ts, "", "clear failed")
            if (self._active_status_threads.get(key) is not entry
                    or self._typing_owner_retired(chat_id, md, owner or asyncio.current_task())):
                return
            if await self._set_agent_session_status(chat_id, team_id, thread_ts, "active"):
                if (self._active_status_threads.get(key) is entry
                        and not self._typing_owner_retired(chat_id, md, owner or asyncio.current_task())):
                    self._active_status_threads.pop(key, None)
