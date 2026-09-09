"""Gateway-owned delegation cards; child callbacks outlive their spawning turn.

Only observed lifecycle/tool events enter this projection. Delivery receipts, not
child return or synthetic-message acceptance, retire cards. No timer emits activity.
"""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import re
import threading
import time
from pathlib import Path

from agent.display import get_tool_emoji
from gateway.config import Platform
from gateway.session import SessionSource
from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)
_MANAGER_LOCK = threading.Lock()
_TERMINAL = {"completed", "failed", "error", "timeout", "cancelled", "interrupted", "budget_exhausted"}


def _label(value, default, limit=60):
    # Labels are explicit display data, never goals, tool args or child text.
    text = re.sub(r"[\x00-\x1f\x7f*_`\[\]<>]", "", str(value or ""))
    return " ".join(text.split())[:limit] or default


def render_card(card, now=None):
    elapsed = max(0, int(((time.time() if now is None else now) - card["started_at"]) / 60))
    lines = [f"🧵 **Delegating · {elapsed} min**"]
    for row in card["rows"].values():
        lines.append(f"\n**{row['thread_ref']}. {_label(row.get('task_label'), 'Run delegated task')}** · {_label(row.get('role'), 'Worker', 24)}")
        state = row.get("state")
        if state == "completed":
            activity = "Returned · awaiting parent"
        elif state in _TERMINAL:
            activity = f"{state.replace('_', ' ').capitalize()} · awaiting parent"
        elif state == "unknown":
            activity = "Interrupted / unknown · gateway restarted"
        elif row.get("last_tool"):
            tool = row["last_tool"]
            activity = f"Last tool: {get_tool_emoji(tool)} {_label(tool, 'tool', 40)}"
        else:
            activity = "Started · awaiting activity"
        lines.append(activity)
    # Keep a single Telegram message, never split into a second card.
    return "\n".join(lines)[:3500]


class DelegationCards:
    def __init__(self, runner, *, home=None, interval=1.5):
        self.runner = runner
        self.path = Path(home or get_hermes_home()) / "cache" / "delegation" / "cards.json"
        self.interval = interval
        self.cards = {}
        self.pending = {}
        self.locks = {}
        self.last_edit = {}
        self.turn_tasks = {}
        if self.path.exists():
            try:
                self.cards = json.loads(self.path.read_text(encoding="utf-8"))
                for card in self.cards.values():
                    for row in card["rows"].values():
                        if row["state"] not in _TERMINAL:
                            row["state"] = "unknown"
                    card["generation"] += 1
            except (OSError, ValueError, KeyError):
                logger.exception("Cannot recover delegation cards")

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.cards, ensure_ascii=False), encoding="utf-8")
        temporary.replace(self.path)

    def _source(self, card):
        return SessionSource(**card["source"])

    def _adapter(self, card):
        return self.runner._adapter_for_source(self._source(card))

    async def reconcile(self):
        for key, card in list(self.cards.items()):
            if card.get("retired"):
                await self._delete(card)
            else:
                self._queue(key)
        self._save()

    def _queue(self, key):
        if key not in self.pending:
            self.pending[key] = asyncio.create_task(self._flush(key))

    async def observe(self, source, session_key, session_id, generation, event_type, tool_name, data):
        key, ref = data.get("parent_task_id"), data.get("thread_ref")
        owner = data.get("owner") or {}
        if (source.platform != Platform.TELEGRAM or not isinstance(key, str)
                or not re.fullmatch(r"[a-f0-9]{32}", key)
                or not isinstance(ref, str) or not re.fullmatch(r"[A-Z]+", ref)
                or str(owner.get("session_id", "")) != str(session_id)
                or str(owner.get("session_key", "")) != str(session_key)
                or str(owner.get("chat_id", "")) != str(source.chat_id)
                or str(owner.get("thread_id", "")) != str(source.thread_id or "")):
            return
        card = self.cards.get(key)
        if card and (card["owner"] != owner or card.get("retired")):
            return
        if not card:
            if event_type != "subagent.start":
                return  # no resurrection from a late tool/completion
            source_data = {k: getattr(source, k) for k in source.__dataclass_fields__}
            source_data["platform"] = source.platform.value
            card = self.cards[key] = dict(owner=copy.deepcopy(owner), source=source_data,
                started_at=time.time(), generation=0, rows={}, message_id=None,
                rendered="", recoveries=0, send_attempts=0, retired=False)
        row = card["rows"].get(ref)
        if row and row["state"] in _TERMINAL | {"unknown"}:
            return
        if event_type == "subagent.start":
            if row:
                return
            row = card["rows"][ref] = {"thread_ref": ref, "task_label": data.get("task_label"),
                "role": data.get("role"), "state": "running", "last_tool": None}
            card["generation"] += 1
            if data.get("background") is False:
                self.turn_tasks.setdefault((session_key, generation), {}).setdefault(key, set()).add(ref)
        elif row and event_type == "subagent.complete":
            row["state"] = data.get("status") if data.get("status") in _TERMINAL else "completed"
        elif row and event_type == "subagent.tool" and tool_name:
            row["last_tool"] = _label(tool_name, "tool", 60)
        else:
            return
        card["revision"] = card.get("revision", 0) + 1
        self._save()
        self._queue(key)

    async def _flush(self, key):
        revision = None
        try:
            await asyncio.sleep(max(0, self.interval - (time.monotonic() - self.last_edit.get(key, 0))))
            async with self.locks.setdefault(key, asyncio.Lock()):
                card = self.cards[key]
                if card.get("retired"):
                    return
                text = render_card(card)
                if text == card["rendered"]:
                    return
                adapter = self._adapter(card)
                if adapter is None:
                    return
                revision = card.get("revision", 0)
                message_id = card["message_id"]
                if message_id:
                    result = await adapter.edit_message(card["source"]["chat_id"], message_id, text)
                    missing = "message to edit not found" in str(getattr(result, "error", "")).lower()
                    if missing and card["recoveries"] < 1:
                        card["recoveries"] += 1
                        card["message_id"] = None
                        card["send_attempts"] = 0
                        result = None
                else:
                    result = None
                if result is None and card["send_attempts"] < 1:
                    card["send_attempts"] += 1  # ambiguous sends must not spam retries
                    self._save()  # persist the attempt BEFORE an ambiguous transport await
                    result = await adapter.send_delegation_card(self._source(card), text)
                    if getattr(result, "success", False):
                        card["message_id"] = str(result.message_id)
                    elif (getattr(result, "raw_response", None) or {}).get("definite_rejection") and card.get("rejections", 0) < 1:
                        card["rejections"] = card.get("rejections", 0) + 1
                        card["send_attempts"] = 0  # one retry, on a subsequent observed event only
                if getattr(result, "success", False):
                    card["rendered"] = text
                self.last_edit[key] = time.monotonic()
                self._save()

        except Exception:
            logger.exception("Delegation card update failed")
        finally:
            self.pending.pop(key, None)
        # Events arriving during transport awaits are coalesced, not dropped.
        card = self.cards[key]
        if not card.get("retired") and revision is not None and revision != card.get("revision", 0):
            self._queue(key)

    def receipt(self, event, session_key, generation):
        metadata = event.metadata or {}
        keys = set(self.turn_tasks.get((session_key, generation), ()))
        if event.internal and metadata.get("delegation_parent_task_id"):
            keys.add(metadata["delegation_parent_task_id"])
        receipt = {}
        for key in keys:
            card = self.cards.get(key)
            if (card and not card.get("retired") and card["rows"]
                    and str(event.source.chat_id) == str(card["source"]["chat_id"])
                    and str(event.source.thread_id or "") == str(card["source"].get("thread_id") or "")):
                refs = set(self.turn_tasks.get((session_key, generation), {}).get(key, ()))
                if (event.internal and metadata.get("delegation_parent_task_id") == key
                        and metadata.get("delegation_owner") == card["owner"]):
                    refs.update(metadata.get("delegation_thread_refs", []))
                if not refs:
                    continue
                receipt[key] = {"generation": card["generation"], "refs": sorted(refs)}
        return receipt

    async def delivered(self, receipt):
        for key, proof in receipt.items():
            async with self.locks.setdefault(key, asyncio.Lock()):
                card = self.cards.get(key)
                if not card or card["generation"] != proof["generation"] or card.get("retired"):
                    continue
                handled = set(card.get("handled", [])) | set(proof["refs"])
                card["handled"] = sorted(handled)
                self._save()
                if not (set(card["rows"]) <= handled and all(
                        row["state"] in _TERMINAL | {"unknown"} for row in card["rows"].values())):
                    continue
                # Fence first, including outstanding coalesced updates and late child events.
                card["retired"] = True
                self._save()
                await self._delete(card)

    async def _delete(self, card):
        """Keep the tombstone; bounded restart retries may finish failed deletion."""
        adapter = self._adapter(card)
        if not adapter or not card.get("message_id") or card.get("delete_attempts", 0) >= 3:
            return
        card["delete_attempts"] = card.get("delete_attempts", 0) + 1
        self._save()
        try:
            if await adapter.delete_message(card["source"]["chat_id"], card["message_id"]):
                card["message_id"] = None
                self._save()
        except Exception:
            logger.exception("Delegation card deletion deferred until reconciliation")


def cards_for(runner):
    # Concurrent child callbacks can precede startup-watcher initialization.
    with _MANAGER_LOCK:
        cards = getattr(runner, "_delegation_cards", None)
        if cards is None:
            cards = runner._delegation_cards = DelegationCards(runner)
        return cards
