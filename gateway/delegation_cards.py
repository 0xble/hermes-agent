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
import uuid
from pathlib import Path

from agent.display import get_tool_emoji
from gateway.config import Platform
from gateway import delegation_card_anchor as anchoring
from gateway import delegation_card_presentation as presentation
from gateway.session import SessionSource
from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)
_MANAGER_LOCK = threading.Lock()
_TERMINAL = {"completed", "failed", "error", "timeout", "cancelled", "interrupted", "budget_exhausted"}



def _label(value, default, limit=60):
    # Labels are explicit display data, never goals, tool args or child text.
    text = re.sub(r"[\x00-\x1f\x7f*_`\[\]<>]", "", str(value or ""))
    return " ".join(text.split()) or default


def _tool_label(value, default="tool", limit=40):
    """Bound visible tool names without rewriting their canonical identifier."""
    text = str(value or default).replace("\n", " ").replace("\r", " ").strip()
    return text[:limit] or default


def _row_identity(task_key, ref):
    return f"{task_key}:{ref}"


def _row_prefix(depth):
    # Telegram's MarkdownV2 client collapses leading ASCII spaces in ordinary
    # rich-text paragraphs. NBSP stays plain text; four per rendered depth
    # makes the existing maximum of three visible layers scannable.
    return "\u00a0" * (4 * min(max(0, depth), 2))


def render_card(card, now=None):
    # Plain rich text: cards must never render as a native quote or fake border.
    lines = ["🧵 **Delegating tasks**"]
    rows = card["rows"]
    identities = set(rows)
    children = {}
    for identity, row in rows.items():
        parent = row.get("card_parent_identity")
        if parent in identities and parent != identity:
            children.setdefault(parent, []).append(identity)
    ordered = []
    visited = set()

    def add(identity, depth=0, ancestry=()):
        if identity in visited:
            return
        visited.add(identity)
        row = rows[identity]
        actual_depth = depth
        row = {**row, "_display_depth": min(actual_depth, 2)}
        ordered.append(row)
        for child in children.get(identity, ()):
            if child not in ancestry:
                add(child, actual_depth + 1, ancestry + (identity,))

    for identity in rows:
        if rows[identity].get("card_parent_identity") not in identities:
            add(identity)
    for identity in rows:
        add(identity)

    for row in ordered:
        depth = row["_display_depth"]
        prefix = _row_prefix(depth)
        named_type = _label(row.get("subagent_type"), "", 10_000)
        role_suffix = f" · {named_type.capitalize()}" if named_type else ""
        # The model receives a 24-character row-budget guideline, but authored
        # labels are never truncated or rejected here. Telegram's proportional
        # fonts likewise cannot guarantee physical width.
        # Refs remain stable in the lifecycle/tool API, never in visible text.
        label = _label(row.get("task_label"), "Task", 10_000)
        state = row.get("state")
        symbol, activity = {
            "running": ("○", None),
            "queued": ("◌", "Queued"),
            "completed": ("✓", "Awaiting parent"),
            "failed": ("!", "Failed · awaiting parent"),
            "error": ("!", "Error · awaiting parent"),
            "timeout": ("!", "Timeout · awaiting parent"),
            "cancelled": ("Ⅱ", "Cancelled · awaiting parent"),
            "interrupted": ("Ⅱ", "Interrupted · awaiting parent"),
            "budget_exhausted": ("Ⅱ", "Budget exhausted · awaiting parent"),
            "unknown": ("Ⅱ", "Interrupted · awaiting parent"),
        }.get(state, ("Ⅱ", "Status unknown · awaiting parent"))
        lines.append(f"{prefix}{symbol} {label}{role_suffix}")
        disposition = row.get("disposition") or {}
        if disposition.get("reason") == "deferred":
            activity = "Deferred · " + disposition["detail"]
        if activity is None:
            # Tool rows retain the compact icon plus canonical identifier only:
            # no previews, arguments, usage summaries, or stale terminal tool.
            tool = row.get("last_tool")
            activity = (f"{get_tool_emoji(tool)} {_tool_label(tool)}" if tool
                        else "Started · awaiting activity")
        lines.append(f"{prefix}\u00a0\u00a0↳ {activity}")
    # Do not invent a row/card truncation policy. The platform adapter reports
    # an over-limit send honestly rather than silently hiding authored labels.
    return "\n".join(lines)


def _handled_terminal(card):
    """True only for the exact refs a parent final-delivery receipt handled."""
    rows = card.get("rows") or {}
    return bool(rows) and set(rows) <= set(card.get("handled") or ()) and all(
        row.get("state") in _TERMINAL | {"unknown"} for row in rows.values())


class DelegationCards:
    # 3.0s matches the transport's per-chat edit floor (Telegram: _edit_min_interval_seconds).
    # Editing faster does not surface state sooner: the extra edits queue behind that floor inside
    # the chat's send lock, and every one of them still counts against the chat's flood budget.
    def __init__(self, runner, *, home=None, interval=3.0):
        self.runner = runner
        self.path = Path(home or get_hermes_home()) / "cache" / "delegation" / "cards.json"
        self.interval = interval
        self.cards = {}
        self.pending = {}
        self.locks = {}
        self.last_edit = {}
        self.turn_tasks = {}
        self.displacement = {}
        self.observed_messages = {}
        self.tracking_started = time.time()
        self._diagnostics = set()
        if self.path.exists():
            try:
                self.cards = json.loads(self.path.read_text(encoding="utf-8"))
                from gateway.delegation_card_reconciliation import apply_pending
                try:
                    apply_pending(self)
                except Exception:
                    logger.exception("Delegation presentation dismissal request reconciliation failed")
                changed = False
                for card in self.cards.values():
                    anchoring.adopt_receipt(self, card)
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
        if adapter is not None:
            adapter._delegation_conversation_observer = (
                lambda chat, thread, message: anchoring.observe_conversation(self, adapter, chat, thread, message))
        return adapter

    @staticmethod
    def _scope(card):
        return presentation.scope(card["owner"], card["source"])

    def _anchor(self, key):
        return self.cards[key].get("presentation_key", key)

    def _members(self, key):
        return [(k, c) for k, c in self.cards.items() if self._anchor(k) == key]

    def _projection(self, key):
        anchor = self.cards[key]
        rows = {}
        hidden = set()
        for task_key, card in self._members(key):
            for ref, row in card["rows"].items():
                identity = _row_identity(task_key, ref)
                if card.get("retired") or (ref in (card.get("handled") or ()) and row["state"] in _TERMINAL | {"unknown"}):
                    hidden.add(identity)
                rows[identity] = {
                    **row,
                    "thread_ref": row.get("display_ref", ref),
                    "card_parent_identity": _row_identity(row["card_parent_task_id"], row["card_parent_thread_ref"])
                    if row.get("card_parent_task_id") and row.get("card_parent_thread_ref") else None,
                }
        visible = set(rows) - hidden
        for identity in list(visible):
            parent = rows[identity].get("card_parent_identity")
            seen = {identity}
            while parent in rows and parent not in seen:
                visible.add(parent)
                seen.add(parent)
                parent = rows[parent].get("card_parent_identity")
        return {"started_at": anchor["started_at"], "rows": {k: v for k, v in rows.items() if k in visible}}

    def _bind(self, key):
        presentation.bind(self, key)

    def _assign_refs(self, key):
        card = self.cards[key]
        anchor = self._anchor(key)
        used = {r.get("display_ref", ref) for k, c in self._members(anchor) if k != key
                for ref, r in c["rows"].items()}
        for ref, row in card["rows"].items():
            if row.get("display_ref"):
                used.add(row["display_ref"])
                continue
            parent_card = self.cards.get(row.get("card_parent_task_id"))
            parent_row = parent_card and parent_card.get("rows", {}).get(row.get("card_parent_thread_ref"))
            if parent_row and parent_row.get("display_ref"):
                child_index = parent_row.get("next_child_display_index", 1)
                display = f"{parent_row['display_ref']}.{child_index}"
                while display in used:
                    child_index += 1
                    display = f"{parent_row['display_ref']}.{child_index}"
                parent_row["next_child_display_index"] = child_index + 1
                row["display_ref"] = display
                used.add(display)
                continue
            display = ref
            suffix = 2
            while display in used:
                display = f"{ref}·{suffix}"
                suffix += 1
            row.setdefault("display_ref", display)
            used.add(row["display_ref"])

    async def reconcile(self):
        from gateway.delivery_ledger import delivered_delegation_receipts
        try:
            for receipt in await asyncio.to_thread(delivered_delegation_receipts):
                await self.delivered(receipt)
        except Exception:
            logger.warning("Delegation delivery receipt reconciliation failed", exc_info=True)
        # Recovery uses the same normalized scope lock as dispatch and transport.
        for scope in dict.fromkeys(self._scope(c) for c in self.cards.values()):
            async with self.locks.setdefault(scope, asyncio.Lock()):
                for key, card in list(self.cards.items()):
                    if self._scope(card) == scope and not card.get("retired"):
                        self._bind(key)
                self._save()
                for key, card in list(self.cards.items()):
                    if self._scope(card) == scope and self._anchor(key) == key:
                        if not self._projection(key)["rows"]:
                            await self._delete_obsolete(key)
                            await self._delete(card)
                        else:
                            self._queue(key)

    def _queue(self, key):
        key = self._anchor(key)
        if key not in self.pending:
            self.pending[key] = asyncio.create_task(self._flush(key))

    async def observe(self, source, session_key, session_id, generation, event_type, tool_name, data, preview=None):
        # Kept for the runner's lifecycle callback compatibility; cards intentionally
        # project canonical tool names only and never persist/render preview detail.
        scope = presentation.scope(data.get("owner") or {}, dict(
            profile=getattr(source, "profile", None), platform=source.platform.value,
            chat_id=source.chat_id, thread_id=source.thread_id))
        async with self.locks.setdefault(scope, asyncio.Lock()):
            await self._observe(source, session_key, session_id, generation, event_type, tool_name, data)

    async def _observe(self, source, session_key, session_id, generation, event_type, tool_name, data):
        key, ref = data.get("parent_task_id"), data.get("thread_ref")
        owner = data.get("owner") or {}
        # ``owner`` is the child delegation's actual ownership. ``card_owner``
        # is a trusted identity copied from its spawning card lineage solely for
        # gateway display routing; it never replaces durable delegation ownership.
        card_owner = data.get("card_owner") or owner
        if (source.platform != Platform.TELEGRAM or not isinstance(key, str)
                or not re.fullmatch(r"[a-f0-9]{32}", key)
                or not isinstance(ref, str) or not re.fullmatch(r"[A-Z]+", ref)
                or not isinstance(owner, dict) or not isinstance(card_owner, dict)
                or str(card_owner.get("session_id", "")) != str(session_id)
                or str(card_owner.get("session_key", "")) != str(session_key)
                or str(card_owner.get("chat_id", "")) != str(source.chat_id)
                or str(card_owner.get("thread_id", "")) != str(source.thread_id or "")
                or str(owner.get("profile", "")) != str(card_owner.get("profile", ""))
                or str(owner.get("session_key", "")) != str(card_owner.get("session_key", ""))
                or str(owner.get("chat_id", "")) != str(card_owner.get("chat_id", ""))
                or str(owner.get("thread_id", "")) != str(card_owner.get("thread_id", ""))):
            return
        card = self.cards.get(key)
        if card and (card["owner"] != card_owner
                     or card.get("delegation_owner", card["owner"]) != owner or (card.get("retired") and event_type != "subagent.admitted")):
            return
        if not card:
            if event_type != "subagent.start":
                return  # no resurrection from a late tool/completion
            source_data = {k: getattr(source, k) for k in source.__dataclass_fields__}
            source_data["platform"] = source.platform.value
            card = self.cards[key] = dict(owner=copy.deepcopy(card_owner), delegation_owner=copy.deepcopy(owner), source=source_data,
                started_at=time.time(), generation=0, rows={}, message_id=None,
                rendered="", recoveries=0, send_attempts=0, retired=False)
        row = card["rows"].get(ref)
        if event_type == "subagent.admitted" and data.get("resume_claim_id"):
            attempt = data.get("attempt")
            if (not row or not isinstance(attempt, int) or attempt != row.get("attempt", 0) + 1
                    or row["state"] not in _TERMINAL | {"unknown"}
                    or row.get("child_session_id") not in (None, data.get("child_session_id"))):
                if row and attempt == row.get("attempt") and row.get("resume_claim_id") == data.get("resume_claim_id"):
                    return  # exact replay of already linked admission
                raise ValueError("Continuation admission does not match the exact prior terminal attempt")
            from tools.async_delegation import release_result_retention
            release_result_retention(owner=owner, parent_task_id=key,
                                     attempts={ref: row.get("attempt", 0)})
            old = copy.deepcopy(row)
            old["disposition"] = {"reason": "revision_requested", "actor_session_id": owner["session_id"],
                                  "next_attempt": attempt, "resume_claim_id": data["resume_claim_id"]}
            card.setdefault("attempt_history", {}).setdefault(ref, {})[str(row.get("attempt", 0))] = old
            row.update(state="running", last_tool=None, attempt=attempt, resume_claim_id=data["resume_claim_id"])
            row.pop("disposition", None)
            card.get("handling", {}).pop(ref, None)
            card["handled"] = [r for r in card.get("handled", ()) if r != ref]
            card["retired"] = False
            card["generation"] += 1
            self._bind(key)
            anchor = self.cards[self._anchor(key)]
            if anchoring.pending(anchor):
                # Only the validated new attempt above grants another burst.
                anchor["reanchor"]["new_work_pending"] = True
            card["revision"] = card.get("revision", 0) + 1
            self._save()
            self._queue(key)
            return
        if row and data.get("attempt", 0) != row.get("attempt", 0):
            return  # stale late tool/completion from a superseded execution
        if row and row["state"] in _TERMINAL | {"unknown"}:
            recovered_completion = (row["state"] == "unknown" and event_type == "subagent.complete"
                and row.get("resume_claim_id") and row.get("resume_claim_id") == data.get("resume_claim_id")
                and row.get("child_session_id") == data.get("child_session_id"))
            if not recovered_completion:
                return
        if event_type == "subagent.start":
            if row:
                return
            row = card["rows"][ref] = {"thread_ref": ref, "task_label": data.get("task_label"),
                "role": data.get("role"), "subagent_type": data.get("subagent_type"),
                "card_parent_task_id": data.get("card_parent_task_id"),
                "card_parent_thread_ref": data.get("card_parent_thread_ref"),
                "state": "running", "last_tool": None, "attempt": data.get("attempt", 0),
                "child_session_id": data.get("child_session_id")}
            if data.get("replaces"):
                row["replaces"] = copy.deepcopy(data["replaces"])
            card["generation"] += 1
            if data.get("background") is False:
                self.turn_tasks.setdefault((session_key, generation), {}).setdefault(key, set()).add(ref)
        elif row and event_type == "subagent.admitted":
            replacement = row.get("replaces")
            old = self.cards.get(replacement.get("parent_task_id")) if isinstance(replacement, dict) else None
            old_ref = replacement.get("thread_ref") if isinstance(replacement, dict) else None
            if (old and old.get("delegation_owner", old["owner"]) == owner
                    and old["owner"] == card_owner and self._scope(old) == self._scope(card)
                    and old_ref in old["rows"] and old["rows"][old_ref]["state"] in _TERMINAL | {"unknown"}
                    and old.get("replacement_claims", {}).get(old_ref, {}).get("id") == replacement.get("claim_id")
                    and replacement.get("claim_id")
                    and old["rows"][old_ref].get("attempt", 0) == replacement.get("attempt")):
                from tools.async_delegation import release_result_retention
                release_result_retention(owner=owner, parent_task_id=replacement["parent_task_id"],
                                         attempts={old_ref: replacement["attempt"]})
                old["replacement_claims"][old_ref]["launched"] = {"parent_task_id": key, "thread_ref": ref}
                row["replaces"] = copy.deepcopy(replacement)
                old.setdefault("handling", {})[old_ref] = {"reason": "revision_requested",
                    "replacement": {"parent_task_id": key, "thread_ref": ref}, "actor_session_id": owner.get("session_id")}
                old["rows"][old_ref]["disposition"] = copy.deepcopy(old["handling"][old_ref])
                old["handled"] = sorted({*old.get("handled", ()), old_ref})
                # Bind the replacement to the existing anchor before retiring
                # the old task, otherwise presentation.bind chooses a new bubble.
            card["generation"] += 1
            if data.get("background") is False:
                self.turn_tasks.setdefault((session_key, generation), {}).setdefault(key, set()).add(ref)
        elif row and event_type == "subagent.complete":
            row["state"] = data.get("status") if data.get("status") in _TERMINAL else "completed"
            row["result_turn_id"] = data.get("result_turn_id")
        elif row and event_type == "subagent.tool" and tool_name:
            row["last_tool"] = _tool_label(tool_name, "tool", 60)
        else:
            return
        self._bind(key)
        for _, member in self._members(key):
            if _handled_terminal(member):
                member["retired"] = True
        if _handled_terminal(card):
            card["retired"] = True
        anchor = self.cards[self._anchor(key)]
        if event_type == "subagent.start" and anchoring.pending(anchor):
            # Only an admitted new row grants recovery, never a replay or tool tick.
            # Keep this through an in-flight send; its outcome may still be unknown.
            anchor["reanchor"]["new_work_pending"] = True
        anchor["revision"] = anchor.get("revision", 0) + 1
        self._save()
        self._queue(key)

    async def _flush(self, key):
        revision = None
        try:
            await asyncio.sleep(max(0, self.interval - (time.monotonic() - self.last_edit.get(key, 0)),
                                    self.cards[key].get("retry_at", 0) - time.time(),
                                    self.cards[key].get("delete_retry_at", 0) - time.time(),
                                    (self.cards[key].get("reanchor") or {}).get("retry_not_before", 0) - time.time()))
            # Replacement releases the lifecycle lock across transport, unlike
            # ordinary edits. Its durable phases fence parallel/restarted sends.
            if anchoring.pending(self.cards[key]) or anchoring.eligible(self, key):
                revision = self.cards[key].get("revision", 0)
                self.cards[key].pop("retry_at", None)
                self.cards[key].pop("delete_retry_at", None)
                await anchoring.replace(self, key)
                self.last_edit[key] = time.monotonic()
                return
            async with self.locks.setdefault(self._scope(self.cards[key]), asyncio.Lock()):
                # A queued legacy anchor may have been rebound while waiting.
                if not self.cards[key].get("retired"):
                    self._bind(key)
                if self._anchor(key) != key:
                    self._queue(key)
                    return
                card = self.cards[key]
                card.pop("delete_retry_at", None)
                card.pop("retry_at", None)
                projection = self._projection(key)
                if not projection["rows"]:
                    await self._delete(card)
                    await self._delete_obsolete(key)
                    return
                text = render_card(projection)
                if (text == card["rendered"]
                        and not any(e["state"] == "pending" for e in presentation.pending(self, key))):
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
                    if (missing and card["recoveries"] < 1 and not card.get("reanchor")
                            and not presentation.fenced(self, key)):
                        card["recoveries"] += 1
                        card["message_id"] = None
                        card["send_attempts"] = 0
                        result = None
                else:
                    result = None
                if (result is None and card["send_attempts"] < 1
                        and not presentation.fenced(self, key)):
                    card["send_attempts"] += 1  # ambiguous sends must not spam retries
                    self._save()  # persist the attempt BEFORE an ambiguous transport await
                    result = await adapter.send_delegation_card(source, text)
                    if getattr(result, "success", False):
                        card["message_id"] = str(result.message_id)
                        card["anchored_at"] = time.time()
                        self.displacement.pop(key, None)
                    elif (getattr(result, "raw_response", None) or {}).get("definite_rejection") and card.get("rejections", 0) < 1:
                        card["rejections"] = card.get("rejections", 0) + 1
                        card["send_attempts"] = 0  # one retry, on a subsequent observed event only
                if getattr(result, "success", False):
                    card.pop("retry_at", None)
                    card["rendered"] = text
                    presentation.published(self, key)
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
            if (self.cards[key].get("delete_retry_at") or self.cards[key].get("retry_at")
                    or (revision is not None and revision != self.cards[key].get("revision", 0))) and not asyncio.current_task().cancelling():
                self._queue(key)
        # Events arriving during transport awaits are coalesced, not dropped.
        card = self.cards[key]
        if revision is not None and (revision != card.get("revision", 0) or card.get("retry_at")):
            self._queue(key)

    async def handling(self, source, session_key, session_id, generation, *,
                       actor_session_id, parent_task_id, refs, reason, detail=None, turn_id=None):
        """Explicit parent attestation, not result arrival or prose classification.

        Root attestations await their final delivery. Nested incorporation has
        no user-facing send dependency; its own parent may consume it silently.
        """
        card = self.cards.get(parent_task_id)
        if (not card or not isinstance(refs, list) or not refs
                or not all(isinstance(ref, str) for ref in refs)
                or len(set(refs)) != len(refs)
                or reason not in {"incorporated", "blocker_report", "deferred", "validate_replacement", "release_replacement"}):
            raise ValueError("Expected exact task identity, refs and handling reason")
        async with self.locks.setdefault(self._scope(card), asyncio.Lock()):
            owner = card.get("delegation_owner", card["owner"])
            if (owner.get("session_id") != actor_session_id
                    or card["owner"].get("session_id") != session_id
                    or card["owner"].get("session_key") != session_key
                    or str(card["source"]["chat_id"]) != str(source.chat_id)
                    or str(card["source"].get("thread_id") or "") != str(source.thread_id or "")
                    or any(ref not in card["rows"] or card["rows"][ref]["state"] not in _TERMINAL | {"unknown"} for ref in refs)):
                raise ValueError("Handling requires this exact parent owner's terminal rows")
            if reason == "validate_replacement":
                if len(refs) != 1 or card.get("replacement_claims", {}).get(refs[0]):
                    raise ValueError("Replacement already reserved or launch outcome unresolved; reconcile before retrying")
                claim_id = uuid.uuid4().hex
                card.setdefault("replacement_claims", {})[refs[0]] = {
                    "id": claim_id, "attempt": card["rows"][refs[0]].get("attempt", 0)}
                self._save()
                return {"validated": True, "claim_id": claim_id,
                        "attempt": card["rows"][refs[0]].get("attempt", 0)}
            if reason == "release_replacement":
                for ref in refs:
                    claim = card.get("replacement_claims", {}).get(ref)
                    if claim and claim["id"] == detail:
                        card["replacement_claims"].pop(ref)
                self._save()
                return {"released": True}
            if actor_session_id != session_id and reason == "blocker_report":
                raise ValueError("A nested parent cannot attest a root user-facing delivery; incorporate its result instead")
            if turn_id is not None:
                presented = card.get("result_turns", {}).get(turn_id, {})
                if any(ref not in presented or presented[ref] != card["rows"][ref].get("attempt", 0) for ref in refs):
                    raise ValueError("Disposition requires the exact terminal attempt delivered to this processing turn; the result is absent or superseded")
            if reason == "deferred":
                if not isinstance(detail, str) or not detail.strip() or len(detail.strip()) > 160:
                    raise ValueError("Deferred requires a short nonempty reason (at most 160 characters)")
                detail = _label(detail, "")
            for ref in refs:
                prior = card.get("handling", {}).get(ref, {})
                card.setdefault("handling", {})[ref] = dict(
                    # A queued outbound obligation already owns this exact ID.
                    # Re-attesting the same immutable terminal ref must not revoke it.
                    id=(prior.get("id") if (prior.get("reason") == reason or {prior.get("reason"), reason} <= {"incorporated", "blocker_report"}) else None) or uuid.uuid4().hex,
                    reason=reason, detail=detail, turn_id=turn_id, actor_session_id=actor_session_id,
                    session_key=session_key, generation=generation,
                    epoch=card.get("receipt_epoch", 0))
                card["rows"][ref]["disposition"] = copy.deepcopy(card["handling"][ref])
            card["revision"] = card.get("revision", 0) + 1
            self._save()
            if reason == "deferred":
                self._queue(parent_task_id)
            proof = {parent_task_id: self._proof(card, refs)}
        return {"recorded": True, "parent_task_id": parent_task_id, "refs": refs,
                "awaiting_delivery": reason != "deferred" and (actor_session_id == session_id or reason == "blocker_report")}

    async def result_turn(self, *, actor_session_id, turn_id, results=None):
        """Exact terminal attempts presented to this turn, never historical visibility.

        The trusted runtime supplies results; model text never enters this method.
        Persist presentation identity so recovery cannot turn a later attempt into
        an acknowledgement of this one. Query only the supplied opaque turn id.
        """
        if not isinstance(turn_id, str) or not turn_id:
            raise ValueError("Missing result-processing turn identity")
        nested_deliveries = {}
        for item in results or ():
            card = self.cards.get(item.get("parent_task_id"))
            if not card or card.get("delegation_owner", card["owner"]).get("session_id") != actor_session_id:
                continue
            async with self.locks.setdefault(self._scope(card), asyncio.Lock()):
                for ref in item.get("thread_refs") or ():
                    row = card["rows"].get(ref)
                    if not row or row["state"] not in _TERMINAL | {"unknown"}:
                        continue
                    attempt = (item.get("attempts") or {}).get(ref, 0)
                    if attempt != row.get("attempt", 0):
                        continue
                    card.setdefault("result_turns", {}).setdefault(turn_id, {})[ref] = attempt
                    child_session = row.get("child_session_id")
                    if child_session and row["state"] == "completed":
                        # Nested incorporation is delivered only when this exact
                        # parent's composed result actually reaches its owner.
                        for nested_key, nested in self.cards.items():
                            if nested.get("delegation_owner", nested["owner"]).get("session_id") != child_session:
                                continue
                            refs = [r for r, intent in nested.get("handling", {}).items()
                                    if intent.get("reason") == "incorporated"
                                    and intent.get("actor_session_id") == child_session
                                    and row.get("result_turn_id") is not None
                                    and intent.get("turn_id") == row.get("result_turn_id")
                                    and r not in nested.get("handled", ())]
                            if refs:
                                nested_deliveries[nested_key] = self._proof(nested, refs)
                self._save()
        if nested_deliveries:
            await self.delivered(nested_deliveries)
        missing = []
        for key, card in self.cards.items():
            if card.get("delegation_owner", card["owner"]).get("session_id") != actor_session_id:
                continue
            for ref, attempt in card.get("result_turns", {}).get(turn_id, {}).items():
                row = card["rows"].get(ref, {})
                prior = card.get("attempt_history", {}).get(ref, {}).get(str(attempt), {})
                intent = (row.get("disposition") or card.get("handling", {}).get(ref, {})) if row.get("attempt", 0) == attempt else prior.get("disposition", {})
                if not intent.get("reason"):
                    missing.append({"parent_task_id": key, "thread_ref": ref, "attempt": attempt,
                                    "task_label": row.get("task_label", "Task")})
        return {"missing": missing}

    @staticmethod
    def _proof(card, refs):
        return {"generation": card["generation"], "epoch": card.get("receipt_epoch", 0),
                "refs": sorted(refs), "owner": copy.deepcopy(card.get("delegation_owner", card["owner"])),
                "handling_ids": {ref: card["handling"][ref]["id"] for ref in refs}}

    def receipt(self, event, session_key, generation):
        receipt = {}
        for key, card in self.cards.items():
            refs = [ref for ref, intent in card.get("handling", {}).items()
                    if intent.get("reason") in {"incorporated", "blocker_report"}
                    and intent.get("actor_session_id") == card["owner"].get("session_id")
                    and ref not in card.get("handled", ())
                    and intent.get("session_key") == session_key
                    and intent.get("generation") == generation
                    and intent.get("epoch") == card.get("receipt_epoch", 0)
                    and str(event.source.chat_id) == str(card["source"]["chat_id"])
                    and str(event.source.thread_id or "") == str(card["source"].get("thread_id") or "")]
            if refs:
                receipt[key] = self._proof(card, refs)
        return receipt

    async def delivered(self, receipt):
        for key, proof in (receipt or {}).items():
            if key not in self.cards:
                continue
            async with self.locks.setdefault(self._scope(self.cards[key]), asyncio.Lock()):
                anchor_key = self._anchor(key)
                card = self.cards.get(key)
                if not card or card.get("retired"):
                    continue
                if proof.get("owner") != card.get("delegation_owner", card["owner"]):
                    continue
                refs = {ref for ref in proof.get("refs", ()) if ref in card["rows"]
                        and card["rows"][ref]["state"] in _TERMINAL | {"unknown"}
                        and card.get("handling", {}).get(ref, {}).get("reason") in {"incorporated", "blocker_report"}
                        and proof.get("handling_ids", {}).get(ref)
                        and proof["handling_ids"][ref] == card.get("handling", {}).get(ref, {}).get("id")}
                if not refs:
                    continue
                from tools.async_delegation import release_result_retention
                release_result_retention(
                    owner=card.get("delegation_owner", card["owner"]), parent_task_id=key,
                    attempts={ref: card["rows"][ref].get("attempt", 0) for ref in refs},
                )
                handled = set(card.get("handled") or []) | refs
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

    def _defer_delete(self, card, adapter, minimum_delay=0.0):
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
        anchor = self.cards[key]
        adapter = self._adapter(anchor)
        empty = not self._projection(key)["rows"]
        for entry in presentation.pending(self, key):
            # Exact final-delivery receipts can retire every row before the first
            # union edit, or between a failed delete and restart. Then there is
            # no survivor to preserve; retain that distinct retirement evidence.
            if empty:
                entry.update(state="ready", projection_retired=True,
                             survivor_message_id=anchor.get("message_id"))
                self._save()
            if (entry["state"] != "ready" or entry["attempts"] >= 3 or not adapter
                    or not presentation.cleanup_allowed(self, key, entry["message_id"])
                    or entry.get("survivor_message_id") != anchor.get("message_id")):
                continue
            if self._defer_delete(anchor, adapter):
                continue
            entry["attempts"] += 1
            self._save()
            status_delete = getattr(type(adapter), "_delete_status_message", None)
            if status_delete is not None:
                deleted = await status_delete(adapter, anchor["source"]["chat_id"], entry["message_id"])
            else:
                deleted = await adapter.delete_message(anchor["source"]["chat_id"], entry["message_id"])
            if deleted is None and status_delete is not None:
                entry["attempts"] -= 1
                self._defer_delete(anchor, adapter, minimum_delay=1.0)
            elif deleted:
                entry["state"] = "deleted"
            else:
                self._defer_delete(anchor, adapter)
            self._save()
        for _, card in self._members(key):
            message_id = card.get("obsolete_message_id")
            adapter = self._adapter(card)
            if message_id and adapter:
                if self._defer_delete(card, adapter):
                    continue
                if card.get("obsolete_delete_attempts", 0) >= 3:
                    continue
                card["obsolete_delete_attempts"] = card.get("obsolete_delete_attempts", 0) + 1
                self._save()
                status_delete = getattr(type(adapter), "_delete_status_message", None)
                if status_delete is not None:
                    deleted = await status_delete(adapter, card["source"]["chat_id"], message_id)
                else:
                    deleted = await adapter.delete_message(card["source"]["chat_id"], message_id)
                if deleted is None and status_delete is not None:
                    card["obsolete_delete_attempts"] -= 1
                    self._defer_delete(card, adapter, minimum_delay=1.0)
                elif deleted:
                    card["obsolete_message_id"] = None
                    card.pop("obsolete_delete_attempts", None)
                    if (card.get("reanchor") or {}).get("state") == "sent":
                        card["last_reanchor"] = card.pop("reanchor")
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
