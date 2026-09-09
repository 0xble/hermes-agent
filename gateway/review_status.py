"""Native Telegram projection for a parent-owned review lifecycle.

This deliberately has no delegation-card rows or generic progress fallback.  Its
one message belongs to one exact review delegation and is retired only after the
parent's successful final delivery handles that exact delegation.
"""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import threading
import time
from pathlib import Path

from gateway.config import Platform
from gateway.session import SessionSource
from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)
_MANAGER_LOCK = threading.Lock()
_TERMINAL = {"returned", "unknown"}


def render(state: str, started_at: float | None = None, *, now: float | None = None) -> str:
    """One ordinary Telegram line; never a quoted/card-shaped status."""
    if state == "dispatched":
        return "⚖ **Review dispatched**"
    if state == "reviewing":
        current = time.time() if now is None else now
        started = current if started_at is None else started_at
        elapsed = max(0, int((current - started) / 60))
        return f"⚖ **Reviewing · {elapsed} min**"
    if state == "returned":
        return "⚖ **Review returned**"
    return "⚖ **Review interrupted / unknown**"


class ReviewStatuses:
    def __init__(self, runner, *, home=None):
        self.runner = runner
        self.path = Path(home or get_hermes_home()) / "cache" / "review-statuses.json"
        self.items = {}
        self.locks = {}
        self.delete_pending = {}
        self._load()

    def _load(self):
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            self.items = raw if isinstance(raw, dict) else {}
        except (OSError, ValueError):
            self.items = {}
            return
        # A gateway restart cannot know whether an in-flight edit/send completed.
        # Keep a known message only for an honest unknown edit; never resend one.
        changed = False
        for item in self.items.values():
            if not isinstance(item, dict) or item.get("retired"):
                continue
            if item.get("state") not in _TERMINAL:
                item["state"] = "unknown"
                changed = True
        if changed:
            self._save()

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.items, ensure_ascii=False), encoding="utf-8")
        temporary.replace(self.path)

    @staticmethod
    def _source_data(source):
        data = {key: getattr(source, key) for key in source.__dataclass_fields__}
        data["platform"] = source.platform.value
        return data

    @staticmethod
    def _matches(item, source, session_key, session_id, generation=None):
        owner = item.get("owner") or {}
        return (
            str(owner.get("session_key", "")) == str(session_key or "")
            and str(owner.get("session_id", "")) == str(session_id or "")
            and str(owner.get("chat_id", "")) == str(source.chat_id or "")
            and str(owner.get("thread_id", "")) == str(source.thread_id or "")
            and (generation is None or int(item.get("generation", -1)) == int(generation))
        )

    def _adapter(self, source):
        return self.runner._adapter_for_source(source) if source is not None else None

    async def dispatch(self, source, session_key, session_id, generation, delegation_id):
        if source.platform != Platform.TELEGRAM or not isinstance(delegation_id, str) or not delegation_id:
            return False
        lock = self.locks.setdefault(delegation_id, asyncio.Lock())
        async with lock:
            prior = self.items.get(delegation_id)
            if prior is not None:
                # A callback retry may only reuse the exact same owner/generation;
                # it cannot send a second ambiguous initial status.
                return bool(self._matches(prior, source, session_key, session_id, generation) and prior.get("message_id"))
            adapter = self._adapter(source)
            if adapter is None:
                return False
            owner = dict(session_key=str(session_key or ""), session_id=str(session_id or ""),
                         chat_id=str(source.chat_id or ""), thread_id=str(source.thread_id or ""))
            item = self.items[delegation_id] = {
                "owner": owner, "source": self._source_data(source), "generation": generation,
                "state": "dispatched", "started_at": None, "message_id": None,
                "send_attempted": True, "retired": False,
            }
            # Persist attempt before awaiting: after an interruption, a new process must
            # not create a second visually indistinguishable review notice.
            self._save()
            try:
                result = await adapter.send(source.chat_id, render("dispatched"),
                                            metadata={**(self.runner._thread_metadata_for_source(source) or {}), "hermes_status": True})
            except Exception:
                logger.debug("Native review status send failed", exc_info=True)
                return False
            if not getattr(result, "success", False) or not getattr(result, "message_id", None):
                return False
            item["message_id"] = str(result.message_id)
            self._save()
            return True

    def owns(self, source, session_key, session_id, generation, delegation_id):
        item = self.items.get(delegation_id)
        return bool(item and not item.get("retired") and self._matches(
            item, source, session_key, session_id, generation,
        ))

    async def lifecycle(self, source, session_key, session_id, generation, delegation_id, event_type):
        # Typed native-review progress may beat the parent dispatch callback.
        await self.dispatch(source, session_key, session_id, generation, delegation_id)
        return await self.observe(source, session_key, session_id, generation, delegation_id, event_type)

    async def observe(self, source, session_key, session_id, generation, delegation_id, event_type):
        item = self.items.get(delegation_id)
        if (not item or item.get("retired") or not self._matches(item, source, session_key, session_id, generation)
                or event_type not in {"subagent.start", "subagent.complete"}):
            return False
        lock = self.locks.setdefault(delegation_id, asyncio.Lock())
        async with lock:
            item = self.items.get(delegation_id)
            if not item or item.get("retired") or not self._matches(item, source, session_key, session_id, generation):
                return False
            if event_type == "subagent.start":
                if item.get("state") != "dispatched":
                    return False
                item["state"], item["started_at"] = "reviewing", time.time()
            elif item.get("state") != "returned":
                item["state"] = "returned"
            else:
                return False
            self._save()
            adapter = self._adapter(source)
            if not adapter or not item.get("message_id"):
                return False
            try:
                result = await adapter.edit_message(source.chat_id, item["message_id"],
                    render(item["state"], item.get("started_at")), finalize=True,
                    metadata={**(self.runner._thread_metadata_for_source(source) or {}), "hermes_status": True})
            except Exception:
                logger.debug("Native review status edit failed", exc_info=True)
                return False
            return bool(getattr(result, "success", False))

    def receipt(self, event, session_key, generation):
        """Return only a matching parent-completion proof for a returned review."""
        metadata = event.metadata or {}
        delegation_id = metadata.get("delegation_id") if getattr(event, "internal", False) else None
        item = self.items.get(delegation_id)
        if not item or item.get("retired") or item.get("state") not in {"returned", "unknown"}:
            return {}
        # The parent continuation is necessarily a later turn, so its run
        # generation must not be confused with the review-dispatch generation.
        if not self._matches(item, event.source, session_key, metadata.get("gateway_session_id")):
            return {}
        return {delegation_id: {"generation": item.get("generation"), "owner": copy.deepcopy(item.get("owner"))}}

    async def delivered(self, receipt):
        for delegation_id, proof in (receipt or {}).items():
            lock = self.locks.setdefault(delegation_id, asyncio.Lock())
            async with lock:
                item = self.items.get(delegation_id)
                if (not item or item.get("retired") or item.get("state") not in {"returned", "unknown"}
                        or item.get("generation") != proof.get("generation")
                        or item.get("owner") != proof.get("owner")):
                    continue
                # Fence first: late lifecycle events cannot overwrite or recreate it.
                item["retired"] = True
                self._save()
                await self._delete(item)

    def _defer_delete(self, item, adapter):
        delay = getattr(adapter, "deletion_retry_after", lambda _: 0)(item["source"]["chat_id"])
        delay = max(delay if isinstance(delay, (int, float)) else 0, item.get("delete_retry_at", 0) - time.time())
        if delay <= 0:
            return False
        item["delete_retry_at"] = time.time() + delay
        self._save()
        key = next(k for k, value in self.items.items() if value is item)
        if key not in self.delete_pending:
            self.delete_pending[key] = asyncio.create_task(self._retry_delete(key))
        return True

    async def _retry_delete(self, key):
        try:
            await asyncio.sleep(max(0, self.items[key].get("delete_retry_at", 0) - time.time()))
            async with self.locks.setdefault(key, asyncio.Lock()):
                self.items[key].pop("delete_retry_at", None)
                await self._delete(self.items[key])
        finally:
            self.delete_pending.pop(key, None)
            if self.items[key].get("delete_retry_at") and not asyncio.current_task().cancelling():
                self.delete_pending[key] = asyncio.create_task(self._retry_delete(key))

    async def _delete(self, item):
        if not item.get("message_id") or item.get("delete_attempts", 0) >= 3:
            return
        adapter = self._adapter(SessionSource(**{**item["source"], "platform": Platform(item["source"]["platform"])}))
        if adapter:
            if self._defer_delete(item, adapter):
                return
            item["delete_attempts"] = item.get("delete_attempts", 0) + 1
            self._save()
            try:
                if await adapter.delete_message(item["source"]["chat_id"], item["message_id"]):
                    item["message_id"] = None
                    self._save()
                elif self._defer_delete(item, adapter):
                    item["delete_attempts"] -= 1
                    self._save()
            except Exception:
                logger.debug("Native review status deletion deferred", exc_info=True)

    async def reconcile(self):
        for key, item in self.items.items():
            async with self.locks.setdefault(key, asyncio.Lock()):
                if item.get("retired"):
                    await self._delete(item)


def statuses_for(runner):
    with _MANAGER_LOCK:
        statuses = getattr(runner, "_review_statuses", None)
        if statuses is None:
            statuses = runner._review_statuses = ReviewStatuses(runner)
        return statuses
