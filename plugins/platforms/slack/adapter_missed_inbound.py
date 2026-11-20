"""Slack side of missed inbound recovery (``gateway/missed_inbound_recovery.py``).

Slack does not queue events for a Socket Mode app that is not connected, so a message sent while
the gateway was offline is only reachable through the Web API. ``fetch_missed_inbound`` reads the
offline interval back and returns the messages a person addressed to the bot: every message in a
1:1 DM, @-mentions in group DMs and member channels, and replies in threads the bot already has a
session for. ``dispatch_missed_inbound`` replays one of them through ``_handle_slack_message``,
the handler Socket Mode calls, so nothing about authorization, gating, routing or threading is
decided here.

A scan is bounded: paginated ``users.conversations``, one ``conversations.history`` page per
conversation, one ``conversations.replies`` page per thread, a fixed call budget with spacing
between calls, and it ends on the first rate limit. 1:1 and group DMs are read first.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import TYPE_CHECKING, Any, Optional

logger = logging.getLogger(__name__)

_DM_KINDS = ("im", "mpim")
_KIND_ORDER = {"im": 0, "mpim": 1}
# A person speaking. Joins, topic changes, pins and the like are not, as on the live path.
_HUMAN_SUBTYPES = (None, "", "file_share", "thread_broadcast", "me_message")
# Fields conversations.history adds that a Socket Mode message event never carries.
_HISTORY_ONLY_FIELDS = frozenset({
    "reply_count", "reply_users_count", "latest_reply", "reply_users", "is_locked", "subscribed", "last_read"})


def _data(response: Any) -> dict[str, Any]:
    """The payload of a Web API response (an ``AsyncSlackResponse`` or a plain dict)."""
    data = getattr(response, "data", response)
    return data if isinstance(data, dict) else {}


def _rate_limited(exc: BaseException) -> bool:
    response = getattr(exc, "response", None)
    if getattr(response, "status_code", None) == 429:
        return True
    get = getattr(response, "get", None)  # a SlackResponse or a plain dict
    return callable(get) and get("error") == "ratelimited"


def _ts_value(message: dict[str, Any]) -> float:
    try:
        return float(message.get("ts") or 0)
    except (TypeError, ValueError):
        return 0.0


def _kind(channel: dict[str, Any]) -> str:
    """The ``channel_type`` Slack puts on a message event in this conversation."""
    if channel.get("is_im"):
        return "im"
    if channel.get("is_mpim"):
        return "mpim"
    return "group" if channel.get("is_private") or channel.get("is_group") else "channel"


class _Scan:
    """One scan's window, results and API budget. A rate limit or a spent budget ends the scan."""

    def __init__(self, since: float, until: float, max_calls: int, spacing_s: float) -> None:
        self.since, self.until = float(since), float(until)
        self.oldest, self.latest = f"{self.since:.6f}", f"{self.until:.6f}"
        self.max_calls, self.spacing_s = max_calls, spacing_s
        self.calls = 0
        self.stopped = ""
        self.records: list[dict[str, Any]] = []
        self.threads_read: set[tuple[str, str]] = set()
        self.complete = True

    def in_window(self, message: dict[str, Any]) -> bool:
        return self.since < _ts_value(message) <= self.until

    async def call(self, method: Any, **kwargs: Any) -> dict[str, Any]:
        """One Web API call, or ``{}`` when the scan has ended or this call failed."""
        if self.stopped:
            return {}
        if self.calls >= self.max_calls:
            self.stopped = "call budget spent"
            return {}
        if self.calls and self.spacing_s > 0:
            await asyncio.sleep(self.spacing_s)
        self.calls += 1
        try:
            return _data(await method(**kwargs))
        except Exception as exc:
            if _rate_limited(exc):
                self.stopped = "rate limited"
            else:  # one unreadable conversation must not end the scan
                logger.debug("[Slack] Missed inbound call failed", exc_info=True)
            return {}


class SlackMissedInboundMixin:
    """Fetch and replay the messages Slack delivered to nobody while the adapter was offline."""

    _MISSED_INBOUND_PAGE_LIMIT = 200
    _MISSED_INBOUND_MAX_CALLS = 100
    _MISSED_INBOUND_MAX_THREADS = 40
    _MISSED_INBOUND_THREAD_DAYS = 7
    _missed_inbound_call_spacing_s = 1.2
    missed_inbound_scan_complete = True
    # Set by gateway.missed_inbound_recovery when recovery is enabled for this adapter.
    missed_inbound_observer: Any = None
    missed_inbound_reconnected: Any = None

    if TYPE_CHECKING:  # everything else used here is provided by SlackAdapter
        def __getattr__(self, name: str) -> Any: ...

    async def fetch_missed_inbound(self, since: float, until: float) -> list[dict[str, Any]]:
        """Messages a person addressed to the bot in ``(since, until]``, oldest first."""
        if not getattr(self, "_app", None):
            return []
        scan = _Scan(since, until, self._MISSED_INBOUND_MAX_CALLS, self._missed_inbound_call_spacing_s)
        self.missed_inbound_scan_complete = False
        clients = dict(getattr(self, "_team_clients", None) or {}) or {"": self._app.client}
        for team_id, client in clients.items():
            await self._missed_scan_workspace(scan, str(team_id or ""), client)
        self.missed_inbound_scan_complete = not bool(scan.stopped)
        if scan.stopped:
            logger.warning(
                "[Slack] Missed inbound scan ended early (%s) after %d calls; conversations not yet "
                "read were skipped", scan.stopped, scan.calls)
        scan.records.sort(key=lambda record: float(record["ts"]))
        return scan.records

    async def dispatch_missed_inbound(self, record: dict[str, Any], note: str) -> None:
        """Replay one fetched message through the handler Socket Mode calls."""
        event = dict(record.get("event") or {})
        if not event.get("ts") or not event.get("channel"):
            raise ValueError("missed inbound record has no Slack event")
        event["_hermes_late_note"] = str(note or "")
        # From here on a Socket Mode delivery of the same message is a replay.
        self.mark_missed_inbound_recovered(str(event["channel"]), str(event["ts"]))
        await self._handle_slack_message(event)

    def mark_missed_inbound_recovered(self, conversation: str, ts: str) -> None:
        """Remember a recovered message, so a Socket Mode replay of it is dropped."""
        recovered = self._lazy_attr("_missed_inbound_recovered", set)
        recovered.add((str(conversation), str(ts)))

    def _is_recovered_replay(self, event: dict[str, Any]) -> bool:
        """True for a live event whose message recovery already replayed."""
        recovered = getattr(self, "_missed_inbound_recovered", None)
        # An edit carries the original ts but is a new event, as it is for the dedup cache.
        if not recovered or event.get("_hermes_late_note") is not None or event.get("_slack_changed_event_ts"):
            return False
        key = (str(event.get("channel") or ""), str(event.get("ts") or ""))
        if key not in recovered:
            return False
        logger.info("[Slack] Dropping replay of a message already answered late: channel=%s ts=%s", *key)
        return True

    def _note_live_inbound(self, event: dict[str, Any], channel_id: str) -> None:
        """Tell recovery this message was accepted live, so a later scan never replays it."""
        observer = getattr(self, "missed_inbound_observer", None)
        ts = str(event.get("ts") or "")
        if not callable(observer) or not ts or not channel_id or event.get("_hermes_late_note") is not None:
            return
        try:
            observer(str(channel_id), ts)
        except Exception:
            logger.debug("[Slack] Could not record live inbound %s %s", channel_id, ts, exc_info=True)

    def _apply_late_note(self, event: dict[str, Any], msg_event: Any) -> None:
        """Put the late note in front of a replayed message.

        The note also keeps a late ``/command`` from running: it reaches the agent as text, which
        can ask before acting on it.
        """
        note = event.get("_hermes_late_note")
        if not note:
            return
        from gateway.platforms.event import MessageType
        msg_event.text = f"{note}\n\n{msg_event.text or ''}"
        if msg_event.message_type == MessageType.COMMAND:
            msg_event.message_type = MessageType.TEXT

    def _missed_inbound_transport_restarted(self) -> None:
        """Socket Mode was rebuilt in place: ask recovery to scan what the dead socket missed."""
        hook = getattr(self, "missed_inbound_reconnected", None)
        if not callable(hook):
            return
        try:
            hook()
        except Exception:
            logger.debug("[Slack] Could not schedule a missed inbound scan", exc_info=True)

    async def missed_inbound_live_until(self) -> Any:
        """When Socket Mode was last proven connected; ``False`` while it is down."""
        task = getattr(self, "_socket_mode_task", None)
        if not getattr(self, "_running", False) or task is None or task.done():
            return False
        if await self._socket_transport_connected() is False or self._socket_ping_pong_stale():
            return False
        last = getattr(getattr(getattr(self, "_handler", None), "client", None), "last_ping_pong_time", None)
        if isinstance(last, (int, float)) and not isinstance(last, bool) and last > 0:
            return float(last)  # the socket answered a ping then, whatever happened since
        return time.time()

    # --- scan -----------------------------------------------------------------------------

    async def _missed_scan_workspace(self, scan: _Scan, team_id: str, client: Any) -> None:
        kinds = await self._missed_conversations(scan, client)
        ordered = sorted(kinds.items(), key=lambda item: _KIND_ORDER.get(item[1], 2))
        for channel_id, kind in ordered:
            if kind in _DM_KINDS:
                await self._missed_scan_history(scan, client, team_id, channel_id, kind)
        for thread_team, channel_id, thread_ts in self._missed_known_threads():
            kind = kinds.get(channel_id)
            if kind and (not thread_team or not team_id or thread_team == team_id):
                await self._missed_scan_thread(scan, client, team_id, channel_id, kind, thread_ts, known=True)
        for channel_id, kind in ordered:
            if kind not in _DM_KINDS:
                await self._missed_scan_history(scan, client, team_id, channel_id, kind)

    async def _missed_conversations(self, scan: _Scan, client: Any) -> dict[str, str]:
        """``{channel_id: kind}`` for every conversation the bot is in and may answer in."""
        kinds: dict[str, str] = {}
        allowed = self._slack_allowed_channels()
        dms_disabled = self._slack_disable_dms()
        cursor = ""
        while True:
            kwargs: dict[str, Any] = {
                "types": "im,mpim,public_channel,private_channel", "exclude_archived": True,
                "limit": self._MISSED_INBOUND_PAGE_LIMIT}
            if cursor:
                kwargs["cursor"] = cursor
            page = await scan.call(client.users_conversations, **kwargs)
            for channel in page.get("channels") or []:
                channel_id = str(channel.get("id") or "") if isinstance(channel, dict) else ""
                if not channel_id or self._is_ignored_channel(channel_id):
                    continue
                kind = _kind(channel)
                if dms_disabled and kind in _DM_KINDS:
                    continue
                if kind != "im" and allowed and channel_id not in allowed:
                    continue
                kinds[channel_id] = kind
            cursor = str((page.get("response_metadata") or {}).get("next_cursor") or "")
            if not cursor:
                return kinds

    def _missed_known_threads(self) -> list[tuple[str, str, str]]:
        """``(team, channel, thread_ts)`` of recently active Slack threads the bot has a session for."""
        list_sessions = getattr(getattr(self, "_session_store", None), "list_sessions", None)
        if not callable(list_sessions):
            return []
        try:
            entries = list_sessions(active_minutes=self._MISSED_INBOUND_THREAD_DAYS * 24 * 60)
        except Exception:
            logger.debug("[Slack] Could not list sessions for missed inbound threads", exc_info=True)
            return []
        threads: list[tuple[str, str, str]] = []
        for entry in entries if isinstance(entries, list) else []:
            origin = getattr(entry, "origin", None)
            platform = getattr(origin, "platform", None)
            if str(getattr(platform, "value", platform)) != "slack":
                continue
            thread = (str(getattr(origin, "scope_id", None) or ""), str(getattr(origin, "chat_id", None) or ""),
                      str(getattr(origin, "thread_id", None) or ""))
            if not thread[1] or not thread[2] or thread in threads:
                continue
            threads.append(thread)
            if len(threads) >= self._MISSED_INBOUND_MAX_THREADS:
                break
        return threads

    async def _missed_scan_history(self, scan: _Scan, client: Any, team_id: str, channel_id: str, kind: str) -> None:
        page = await scan.call(
            client.conversations_history, channel=channel_id, oldest=scan.oldest, latest=scan.latest,
            inclusive=True, limit=self._MISSED_INBOUND_PAGE_LIMIT)
        messages = sorted((m for m in page.get("messages") or [] if isinstance(m, dict)), key=_ts_value)
        if page.get("has_more"):
            logger.info("[Slack] Missed inbound: %s has more than %d messages in the offline interval; "
                        "only the newest were read", channel_id, self._MISSED_INBOUND_PAGE_LIMIT)
        # When the bot answers flat (no thread per message), any later top-level bot post is the answer.
        flat = self._missed_replies_flat(kind)
        for message in messages:
            if message.get("reply_count"):
                await self._missed_scan_thread(scan, client, team_id, channel_id, kind, str(message.get("ts") or ""))
            elif not (flat and self._missed_bot_posted_after(message, messages)):
                self._missed_consider(scan, team_id, channel_id, kind, message)

    async def _missed_scan_thread(self, scan: _Scan, client: Any, team_id: str, channel_id: str, kind: str,
                                  thread_ts: str, *, known: bool = False) -> None:
        """Read one thread's messages in the window. ``known``: the bot already has a session for it."""
        if not thread_ts or (channel_id, thread_ts) in scan.threads_read:
            return
        scan.threads_read.add((channel_id, thread_ts))
        page = await scan.call(
            client.conversations_replies, channel=channel_id, ts=thread_ts, oldest=scan.oldest,
            latest=scan.latest, inclusive=True, limit=self._MISSED_INBOUND_PAGE_LIMIT)
        messages = sorted((m for m in page.get("messages") or [] if isinstance(m, dict)), key=_ts_value)
        for message in messages:
            if not self._missed_bot_posted_after(message, messages):
                is_reply = str(message.get("ts") or "") != thread_ts
                self._missed_consider(scan, team_id, channel_id, kind, message, known_thread=known and is_reply)

    def _missed_replies_flat(self, kind: str) -> bool:
        if not self.config.extra.get("reply_in_thread", True):
            return True
        return kind in _DM_KINDS and not self._dm_top_level_threads_as_sessions()

    def _missed_bot_posted_after(self, message: dict[str, Any], messages: list[dict[str, Any]]) -> bool:
        """True when this bot already posted after *message* in the same thread or flat conversation."""
        sent_at = _ts_value(message)
        return any(_ts_value(other) > sent_at and self._is_own_bot_user(other.get("user")) for other in messages)

    def _missed_mentions_bot(self, message: dict[str, Any], team_id: str) -> bool:
        from plugins.platforms.slack.adapter import _slack_mention_detection_text
        text = _slack_mention_detection_text(message) or str(message.get("text") or "")
        bot_uid = self._team_bot_user_ids.get(team_id, self._bot_user_id)
        return bool(bot_uid and f"<@{bot_uid}>" in text) or self._slack_message_matches_mention_patterns(text)

    def _missed_consider(self, scan: _Scan, team_id: str, channel_id: str, kind: str, message: dict[str, Any],
                         *, known_thread: bool = False) -> None:
        """Record *message* when a person sent it to the bot inside the window."""
        ts, user = str(message.get("ts") or ""), str(message.get("user") or "")
        if not ts or not user or not scan.in_window(message) or message.get("subtype") not in _HUMAN_SUBTYPES:
            return
        if self._is_own_bot_user(user) or self._event_declares_bot_sender(message):
            return
        # A 1:1 DM is always addressed to the bot. Elsewhere it takes an @-mention, or a reply in
        # a thread the bot is already part of; the live handler's gate has the final word.
        if kind != "im" and not known_thread and not self._missed_mentions_bot(message, team_id):
            return
        event = self._missed_event(message, team_id, channel_id, kind)
        is_dm = kind in _DM_KINDS
        thread_id: Optional[str] = self._session_thread_ts(event, ts, is_dm, {})
        if self._early_reject_unauthorized(user, channel_id, is_dm, thread_id=thread_id):
            return
        metadata = {"slack_team_id": team_id} if team_id else None
        scan.records.append({"conversation": channel_id, "ts": ts, "event": event, "reply_metadata": metadata})

    @staticmethod
    def _missed_event(message: dict[str, Any], team_id: str, channel_id: str, kind: str) -> dict[str, Any]:
        """The ``message`` event Socket Mode would have delivered for a history message."""
        event = {key: value for key, value in message.items() if key not in _HISTORY_ONLY_FIELDS}
        event.update(type="message", channel=channel_id, channel_type=kind, event_ts=str(message.get("ts") or ""))
        if team_id:
            event["team"] = team_id
        return event
