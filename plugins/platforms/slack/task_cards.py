"""Cleanup for native progress cards, never the assistant's reply stream."""

import asyncio
import copy
import json
import logging
import time
from contextlib import suppress

from hermes_constants import get_hermes_home
from utils import atomic_write_text

logger = logging.getLogger(__name__)
RETRY_SECONDS = 45
API_TIMEOUT_SECONDS = 5


async def _stop_card(client, channel: str, ts: str) -> None:
    try:
        await asyncio.wait_for(
            client.api_call("chat.stopStream", json={"channel": channel, "ts": ts}),
            timeout=API_TIMEOUT_SECONDS)
    except Exception as exc:
        # Expired streams are already sealed. An outage must not prevent deletion.
        logger.debug("[Slack] Native task-card stopStream failed for %s: %s", ts, exc, exc_info=True)


async def _delete_card(client, channel: str, ts: str) -> bool:
    # Adapter discovery must remain importable without the optional Slack SDK.
    from slack_sdk.errors import SlackApiError

    try:
        await asyncio.wait_for(
            client.api_call("chat.delete", json={"channel": channel, "ts": ts}),
            timeout=API_TIMEOUT_SECONDS)
        return True
    except SlackApiError as exc:
        if exc.response.get("error") == "message_not_found":
            return True
        logger.warning("[Slack] Native task-card deletion failed for %s: %s", ts, exc)
    except Exception as exc:
        # Any transport failure keeps the ledger entry for the next retry.
        logger.warning("[Slack] Native task-card deletion failed for %s: %s", ts, exc, exc_info=True)
    return False


async def finish_task_card(client, stream, *, delete: bool, cleanup=None) -> None:
    """Seal first, retaining failed deletions for the next cleanup sweep."""
    if stream.stream_ts:
        await _stop_card(client, stream.channel, stream.stream_ts)
    if not delete:
        return
    for ts in stream.message_timestamps:
        if await _delete_card(client, stream.channel, ts) and cleanup is not None:
            cleanup.forget(stream.team_id, stream.channel, ts)


class TaskCardCleanup:
    """A profile-local ledger plus one retry worker owned by the Slack adapter."""

    def __init__(self, adapter):
        self.adapter = adapter
        # Bind once in the adapter's owning profile, not a later routed turn.
        self.path = get_hermes_home() / "state" / "slack_task_cards.json"
        self.task = None
        self.wake = asyncio.Event()

    def enabled(self) -> bool:
        extra = self.adapter.config.extra or {}
        return self.adapter._truthy_config(extra.get("native_task_cards_ephemeral", True))

    def _read(self) -> list[dict]:
        try:
            entries = json.loads(self.path.read_text(encoding="utf-8-sig"))
            if not isinstance(entries, list):
                raise ValueError("expected a list")
            return entries
        except FileNotFoundError:
            return []
        except (OSError, ValueError) as exc:  # includes JSON and UTF-8 decode errors
            logger.warning("[Slack] Cannot read task-card ledger %s: %s", self.path, exc)
            return []

    def _write(self, entries: list[dict]) -> None:
        try:
            atomic_write_text(self.path, json.dumps(entries), fsync_dir=True)
        except (OSError, TypeError, ValueError) as exc:
            # Cleanup bookkeeping must not stop progress or reply delivery.
            logger.warning("[Slack] Cannot write task-card ledger %s: %s", self.path, exc)

    async def start_stream(self, client, stream, metadata) -> None:
        payload = {"channel": stream.channel, "thread_ts": stream.thread_ts,
                   "task_display_mode": "plan"}
        md = metadata or {}
        recipients = (
            ("recipient_team_id", ("recipient_team_id", "team_id", "slack_team_id")),
            ("recipient_user_id", ("recipient_user_id", "user_id")))
        for key, sources in recipients:
            value = next((md[source] for source in sources if md.get(source)), None)
            if value:
                payload[key] = value
        # The SDK retries connection failures by default, and a start can take effect before its
        # response is lost. Start on a copy without retry handlers so injected clients cannot
        # replay an ambiguous start; production clients also disable SDK retries at admission.
        start_client = copy.copy(client)
        start_client.retry_handlers = []
        result = await start_client.api_call("chat.startStream", json=payload)
        if hasattr(result, "get"):
            stream.stream_ts = str(result.get("ts") or result.get("message_ts") or "")
        if not stream.stream_ts:
            raise RuntimeError("Slack startStream returned no stream timestamp")
        stream.message_timestamps.append(stream.stream_ts)
        # Metadata may omit the workspace. Persist the authenticated client identity
        # so a future process can recover the card without channel-routing memory.
        stream.team_id = next((team_id for team_id, team_client
                               in self.adapter._team_clients.items() if team_client is client),
                              stream.team_id)
        self.record(stream)

    def record(self, stream) -> None:
        if self.enabled():
            entries = self._read()
            entries.append({"team_id": stream.team_id, "channel": stream.channel,
                            "ts": stream.stream_ts, "created_at": time.time()})
            self._write(entries)

    def forget(self, team_id: str, channel: str, ts: str) -> None:
        entries = self._read()
        self._write([entry for entry in entries
                     if (entry["team_id"], entry["channel"], entry["ts"]) != (team_id, channel, ts)])

    async def sweep(self) -> None:
        if not self.enabled():
            return
        try:
            active = {(stream.team_id, stream.channel, ts)
                      for stream in self.adapter._native_task_card_streams.values()
                      for ts in stream.message_timestamps}
            for entry in self._read():
                key = (entry["team_id"], entry["channel"], entry["ts"])
                client = self.adapter._team_clients.get(entry["team_id"])
                if key in active or client is None:
                    continue
                await _stop_card(client, entry["channel"], entry["ts"])
                deleted = await _delete_card(client, entry["channel"], entry["ts"])
                if deleted:
                    self.forget(*key)
        except Exception as exc:
            logger.warning("[Slack] Native task-card sweep failed: %s", exc, exc_info=True)

    def start(self, *, immediate: bool = False) -> None:
        if not self.enabled() or not self._read():
            return
        if immediate:
            self.wake.set()
        if self.task is None or self.task.done():
            self.task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        try:
            while self.enabled() and self._read():
                if not self.wake.is_set():
                    try:
                        await asyncio.wait_for(self.wake.wait(), timeout=RETRY_SECONDS)
                    except TimeoutError:
                        pass
                self.wake.clear()
                await self.sweep()
        except Exception as exc:
            logger.warning("[Slack] Native task-card retry worker failed: %s", exc, exc_info=True)

    async def close(self) -> None:
        if self.task is not None:
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
            self.task = None
