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


def _tool_label(value, default="tool", limit=40):
    """Bound visible tool names without rewriting their canonical identifier."""
    text = str(value or default).replace("\n", " ").replace("\r", " ").strip()
    return text[:limit] or default


def render_card(card, now=None):
    elapsed = max(0, int(((time.time() if now is None else now) - card["started_at"]) / 60))
    # Plain rich text: cards must never render as a native quote or fake border.
    lines = [f"🧵 **Delegating · {elapsed} min**"]
    for row in card["rows"].values():
        named_type = _label(row.get("subagent_type"), "", 24)
        role_suffix = f" · {named_type.capitalize()}" if named_type else ""
        lines.append(f"{row['thread_ref']}. {_label(row.get('task_label'), 'Task ' + row['thread_ref'])}{role_suffix}")
        state = row.get("state")
        if state == "completed":
            activity = "Returned · awaiting parent"
        elif state in _TERMINAL:
            activity = f"{state.replace('_', ' ').capitalize()} · awaiting parent"
        elif state == "unknown":
            activity = "Interrupted / unknown · gateway restarted"
        elif row.get("last_tool"):
            tool = row["last_tool"]
            # Show the full stored name excerpt; never ingest previews or args.
            activity = f"{get_tool_emoji(tool)} {_tool_label(tool, 'tool', 60)}"
        else:
            activity = "Started · awaiting activity"
        lines.append(f"↳ {activity}")
    # Keep a single Telegram message, never split into a second card.
    return "\n".join(lines)[:3500]


def _handled_terminal(card):
    """True only for the exact refs a parent final-delivery receipt handled."""
    rows = card.get("rows") or {}
    return bool(rows) and set(rows) <= set(card.get("handled") or ()) and all(
        row.get("state") in _TERMINAL | {"unknown"} for row in rows.values())


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
        self._diagnostics = set()
        if self.path.exists():
            try:
                self.cards = json.loads(self.path.read_text(encoding="utf-8"))
                changed = False
                for card in self.cards.values():
                    for row in card["rows"].values():
                        if row["state"] not in _TERMINAL:
                            row["state"] = "unknown"
                    card["generation"] += 1
                    card["receipt_epoch"] = card.get("receipt_epoch", 0) + 1
                    # A crash can happen after the successful parent-final receipt is
                    # saved and before delivered() writes the retirement fence.  Recover
                    # only that exact receipt; an unhandled terminal/failed-send card
                    # remains visible and retryable.
                    if not card.get("retired") and _handled_terminal(card):
                        card["retired"] = True
                        changed = True
                if changed:
                    self._save()
            except (OSError, ValueError, KeyError):
                logger.exception("Cannot recover delegation cards")

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.cards, ensure_ascii=False), encoding="utf-8")
        temporary.replace(self.path)

    def _warn_once(self, card, reason):
        key = (id(card), reason)
        if key not in self._diagnostics:
            self._diagnostics.add(key)
            logger.warning(reason)

    def _source(self, card):
        source_data = card.get("source")
        if not isinstance(source_data, dict):
            self._warn_once(card, "Delegation card source is invalid; skipping delivery")
            return None
        source_data = dict(source_data)
        platform = source_data.get("platform")
        if isinstance(platform, str):
            try:
                source_data["platform"] = Platform(platform)
            except ValueError:
                self._warn_once(card, "Delegation card source platform is invalid; skipping delivery")
                return None
        elif not isinstance(platform, Platform):
            self._warn_once(card, "Delegation card source platform is invalid; skipping delivery")
            return None
        try:
            return SessionSource(**source_data)
        except (TypeError, ValueError):
            self._warn_once(card, "Delegation card source is invalid; skipping delivery")
            return None

    def _adapter(self, card, source=None):
        source = source if source is not None else self._source(card)
        if source is None:
            return None
        adapter = self.runner._adapter_for_source(source)
        if adapter is None:
            self._warn_once(card, "Delegation card adapter unavailable; skipping delivery")
        return adapter

    @staticmethod
    def _scope(card):
        source = card["source"]
        return (card["owner"].get("profile", ""), source.get("profile"),
                source.get("platform"), str(source.get("chat_id")), str(source.get("thread_id") or ""))

    def _anchor(self, key):
        return self.cards[key].get("presentation_key", key)

    def _members(self, key):
        return [(k, c) for k, c in self.cards.items() if self._anchor(k) == key]

    def _projection(self, key):
        anchor = self.cards[key]
        rows = {}
        for task_key, card in self._members(key):
            if card.get("retired"):
                continue
            for ref, row in card["rows"].items():
                if ref in (card.get("handled") or ()) and row["state"] in _TERMINAL | {"unknown"}:
                    continue
                rows[task_key + ref] = {**row, "thread_ref": row.get("display_ref", ref)}
        return {"started_at": anchor["started_at"], "rows": rows}

    def _bind(self, key):
        card = self.cards[key]
        if "presentation_key" in card:
            self._assign_refs(key)
            return
        # Presentation identity deliberately excludes execution session/task identity.
        # Owners and receipts remain on the original task records.
        anchor = next((self._anchor(k) for k, c in self.cards.items()
                       if k != key and "presentation_key" in c
                       and (not c.get("retired") or c.get("message_id")
                            or (c.get("send_attempts") and not c.get("message_deleted")))
                       and self._scope(c) == self._scope(card)), key)
        card["presentation_key"] = anchor
        self.cards[anchor]["delete_attempts"] = 0
        if anchor != key and card.get("message_id"):
            # Exact linkage, not age-based dismissal or an invented handled receipt.
            card["obsolete_message_id"] = card.pop("message_id")
            card["message_id"] = None
        self._assign_refs(key)

    def _assign_refs(self, key):
        card = self.cards[key]
        anchor = self._anchor(key)
        used = {r.get("display_ref", ref) for k, c in self._members(anchor) if k != key
                for ref, r in c["rows"].items()}
        for ref, row in card["rows"].items():
            display = ref
            suffix = 2
            while display in used:
                display = f"{ref}·{suffix}"
                suffix += 1
            row.setdefault("display_ref", display)
            used.add(row["display_ref"])

    async def reconcile(self):
        # Bind legacy task cards before any transport work, persisting replacement
        # links before removing redundant messages. No task outcomes are inferred.
        for key, card in sorted(self.cards.items(), key=lambda item: not bool(item[1].get("message_id"))):
            if not card.get("retired"):
                self._bind(key)
        self._save()
        for key, card in list(self.cards.items()):
            if self._anchor(key) != key:
                continue
            if not self._projection(key)["rows"]:
                async with self.locks.setdefault(self._scope(card), asyncio.Lock()):
                    await self._delete_obsolete(key)
                    await self._delete(card)
            else:
                self._queue(key)

    def _queue(self, key):
        key = self._anchor(key)
        if key not in self.pending:
            self.pending[key] = asyncio.create_task(self._flush(key))

    async def observe(self, source, session_key, session_id, generation, event_type, tool_name, data):
        scope = ((data.get("owner") or {}).get("profile", ""), getattr(source, "profile", None),
                 source.platform.value, str(source.chat_id), str(source.thread_id or ""))
        async with self.locks.setdefault(scope, asyncio.Lock()):
            await self._observe(source, session_key, session_id, generation, event_type, tool_name, data)

    async def _observe(self, source, session_key, session_id, generation, event_type, tool_name, data):
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
                "role": data.get("role"), "subagent_type": data.get("subagent_type"),
                "state": "running", "last_tool": None}
            card["generation"] += 1
            if data.get("background") is False:
                self.turn_tasks.setdefault((session_key, generation), {}).setdefault(key, set()).add(ref)
        elif row and event_type == "subagent.complete":
            row["state"] = data.get("status") if data.get("status") in _TERMINAL else "completed"
        elif row and event_type == "subagent.tool" and tool_name:
            row["last_tool"] = _tool_label(tool_name, "tool", 60)
        else:
            return
        self._bind(key)
        if _handled_terminal(card):
            card["retired"] = True
        anchor = self.cards[self._anchor(key)]
        anchor["revision"] = anchor.get("revision", 0) + 1
        self._save()
        self._queue(key)

    async def _flush(self, key):
        revision = None
        try:
            await asyncio.sleep(max(0, self.interval - (time.monotonic() - self.last_edit.get(key, 0)),
                                    self.cards[key].get("retry_at", 0) - time.time(),
                                    self.cards[key].get("delete_retry_at", 0) - time.time()))
            async with self.locks.setdefault(self._scope(self.cards[key]), asyncio.Lock()):
                card = self.cards[key]
                card.pop("delete_retry_at", None)
                projection = self._projection(key)
                if not projection["rows"]:
                    await self._delete(card)
                    await self._delete_obsolete(key)
                    return
                text = render_card(projection)
                if text == card["rendered"]:
                    await self._delete_obsolete(key)
                    return
                source = self._source(card)
                adapter = self._adapter(card, source)
                if adapter is None:
                    return
                revision = card.get("revision", 0)
                message_id = card["message_id"]
                if message_id:
                    result = await adapter.edit_message(source.chat_id, card["message_id"], text, finalize=True,
                                                        metadata={"hermes_status": True})
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
                    result = await adapter.send_delegation_card(source, text)
                    if getattr(result, "success", False):
                        card["message_id"] = str(result.message_id)
                    elif (getattr(result, "raw_response", None) or {}).get("definite_rejection") and card.get("rejections", 0) < 1:
                        card["rejections"] = card.get("rejections", 0) + 1
                        card["send_attempts"] = 0  # one retry, on a subsequent observed event only
                if getattr(result, "success", False):
                    card.pop("retry_at", None)
                    card["rendered"] = text
                    await self._delete_obsolete(key)
                elif getattr(result, "retryable", False) and getattr(result, "retry_after", None) is not None:
                    # Only explicit flood/cooldown rejection may reset an initial
                    # send attempt. Ambiguous network sends never enter this path.
                    if not card.get("message_id"):
                        card["send_attempts"] = 0
                    card["retry_at"] = time.time() + max(0.05, float(result.retry_after))
                self.last_edit[key] = time.monotonic()
                self._save()

        except Exception:
            logger.exception("Delegation card update failed")
        finally:
            self.pending.pop(key, None)
            if self.cards[key].get("delete_retry_at") and not asyncio.current_task().cancelling():
                self._queue(key)
        # Events arriving during transport awaits are coalesced, not dropped.
        card = self.cards[key]
        if revision is not None and self._projection(key)["rows"] and (revision != card.get("revision", 0) or card.get("retry_at")):
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
                receipt[key] = {"generation": card["generation"], "epoch": card.get("receipt_epoch", 0),
                                "refs": sorted(refs & set(card["rows"]))}
        return receipt

    async def delivered(self, receipt):
        for key, proof in receipt.items():
            anchor_key = self._anchor(key) if key in self.cards else key
            if key not in self.cards:
                continue
            async with self.locks.setdefault(self._scope(self.cards[key]), asyncio.Lock()):
                card = self.cards.get(key)
                if (not card or card.get("retired")
                        or proof.get("epoch", 0) != card.get("receipt_epoch", 0)
                        or proof["generation"] > card["generation"]):
                    continue
                handled = set(card.get("handled") or []) | (set(proof["refs"]) & set(card["rows"]))
                card["handled"] = sorted(handled)
                self._save()
                if _handled_terminal(card):
                    card["retired"] = True
                anchor = self.cards[anchor_key]
                anchor["revision"] = anchor.get("revision", 0) + 1
                self._save()
                if self._projection(anchor_key)["rows"]:
                    self._queue(anchor_key)
                else:
                    await self._delete_obsolete(anchor_key)
                    await self._delete(anchor)

    def _defer_delete(self, card, adapter, minimum_delay=0):
        delay = getattr(adapter, "deletion_retry_after", lambda _: 0)(card["source"]["chat_id"])
        key = next((k for k, c in self.cards.items() if c is card), None)
        if key is None:
            return False
        anchor_key = self._anchor(key)
        anchor = self.cards[anchor_key]
        delay = max(minimum_delay, delay if isinstance(delay, (int, float)) else 0, anchor.get("delete_retry_at", 0) - time.time())
        if delay <= 0:
            return False
        anchor["delete_retry_at"] = max(anchor.get("delete_retry_at", 0), time.time() + delay)
        self._save()
        self._queue(anchor_key)
        return True

    async def _delete_obsolete(self, key):
        for _, card in self._members(key):
            message_id = card.get("obsolete_message_id")
            adapter = self._adapter(card)
            if message_id and adapter:
                if self._defer_delete(card, adapter):
                    continue
                if await adapter.delete_message(card["source"]["chat_id"], message_id):
                    card["obsolete_message_id"] = None
                    self._save()
                else:
                    self._defer_delete(card, adapter)

    async def _delete(self, card):
        """Keep the tombstone; bounded restart retries may finish failed deletion."""
        adapter = self._adapter(card)
        if not adapter or not card.get("message_id") or card.get("delete_attempts", 0) >= 3:
            return
        if self._defer_delete(card, adapter):
            return
        card["delete_attempts"] = card.get("delete_attempts", 0) + 1
        self._save()
        try:
            status_delete = getattr(type(adapter), "_delete_status_message", None)
            if status_delete is not None:
                deleted = await status_delete(adapter, card["source"]["chat_id"], card["message_id"])
            else:
                deleted = await adapter.delete_message(card["source"]["chat_id"], card["message_id"])
            if deleted is None and status_delete is not None:
                # Explicit proof the local gate issued no request, not an API failure.
                card["delete_attempts"] -= 1
                self._defer_delete(card, adapter, minimum_delay=1.0)
            elif deleted:
                card["message_id"] = None
                card["message_deleted"] = True
                self._save()
            elif self._defer_delete(card, adapter):
                self._save()  # an actual failed request still spends its bounded attempt
        except Exception:
            logger.exception("Delegation card deletion deferred until reconciliation")


def cards_for(runner):
    # Concurrent child callbacks can precede startup-watcher initialization.
    with _MANAGER_LOCK:
        cards = getattr(runner, "_delegation_cards", None)
        if cards is None:
            cards = runner._delegation_cards = DelegationCards(runner)
        return cards
