"""Gateway-owned delegation cards; child callbacks outlive their spawning turn.

Only observed lifecycle/tool events enter this projection. Delivery receipts, not
child return or synthetic-message acceptance, retire cards. No timer emits activity.
"""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import math
import re
import threading
import time
import uuid
from pathlib import Path

from agent.delegation_disposition import DEFER_REASON_GUIDANCE
from agent.display import get_tool_emoji
from agent.delegation_activity import WAIT_LABELS, TERMINAL_LABELS
from gateway.config import Platform
from gateway import delegation_card_anchor as anchoring
from gateway import delegation_card_batches as batches
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


def render_card(card, now=None, *, max_visible_roots=5, selected_identities=None):
    # Plain rich text: cards must never render as a native quote or fake border.
    lines = []
    # Legacy rows precede newly admitted identities; stable sort preserves stored
    # card/row order for old records and ties, without relying on wall clocks.
    rows = dict(sorted(card["rows"].items(), key=lambda item: presentation.row_order(item[1])))
    identities = set(rows)
    children = {}
    for identity, row in rows.items():
        parent = row.get("card_parent_identity")
        if parent in identities and parent != identity:
            children.setdefault(parent, []).append(identity)
    ordered = []
    roots = []
    visited = set()

    def add(identity, depth=0, ancestry=()):
        if identity in visited:
            return
        visited.add(identity)
        row = rows[identity]
        actual_depth = depth
        if depth == 0:
            roots.append(identity)
        row = {**row, "_display_identity": identity, "_display_depth": min(actual_depth, 2),
               "_display_root": ancestry[0] if ancestry else identity}
        ordered.append(row)
        for child in children.get(identity, ()):
            if child not in ancestry:
                add(child, actual_depth + 1, ancestry + (identity,))

    for identity in rows:
        if rows[identity].get("card_parent_identity") not in identities:
            add(identity)
    for identity in rows:
        add(identity)

    cap = max_visible_roots if type(max_visible_roots) is int and max_visible_roots > 0 else 5
    selected = set(roots[-cap:])
    if selected_identities is not None:
        selected &= {row["_display_root"] for row in ordered if row["_display_identity"] in selected_identities}
    for row in ordered:
        if (row["_display_root"] not in selected
                or (selected_identities is not None and row["_display_identity"] not in selected_identities)):
            continue
        depth = row["_display_depth"]
        prefix = _row_prefix(depth)
        named_type = _label(row.get("subagent_type"), "", 10_000)
        role_suffix = f" · {named_type.capitalize()}" if named_type else ""
        # New labels are validated at admission, never shortened at rendering.
        # Historical labels stay intact; proportional fonts do not imply fixed width.
        # Refs remain stable in the lifecycle/tool API, never in visible text.
        label = _label(row.get("task_label"), "Task", 10_000)
        state = row.get("state")
        symbol, activity = {
            "running": ("○", None),
            "queued": ("◌", "Queued"),
            "completed": ("✓", None),
            "failed": ("!", None),
            "error": ("!", None),
            "timeout": ("!", "Timeout"),
            "cancelled": ("Ⅱ", "Cancelled"),
            "interrupted": ("Ⅱ", None),
            "budget_exhausted": ("Ⅱ", "Budget exhausted"),
            "unknown": ("Ⅱ", None),
        }.get(state, ("Ⅱ", "Status unknown"))
        lines.append(f"{prefix}{symbol} {label}{role_suffix}")
        disposition = row.get("disposition") or {}
        reason_field, reason_labels = {
            "running": ("activity_reason", WAIT_LABELS),
            "failed": ("terminal_reason", TERMINAL_LABELS),
            "error": ("terminal_reason", TERMINAL_LABELS),
        }.get(state, ("", {}))
        reason = row.get(reason_field)
        if state in _TERMINAL | {"unknown"} and disposition.get("reason") == "deferred":
            activity = disposition["detail"]
        elif isinstance(reason, str) and reason in reason_labels:
            activity = reason_labels[reason]
        elif state == "running" and (tool := row.get("last_tool")):
            # Tool rows retain the compact icon plus canonical identifier only:
            # no previews, arguments, usage summaries, or stale terminal tool.
            activity = f"{get_tool_emoji(tool)} {_tool_label(tool)}"
        if activity:
            lines.append(f"{prefix}\u00a0\u00a0↳ {activity}")
    # Root groups alone are windowed. Complete descendants and authored labels
    # may still exceed the platform limit; report that transport error honestly.
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
    def __init__(self, runner, *, home=None, interval=3.0, clock=None):
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
        self._clock = clock or time.time
        self._expiry_timers = {}
        self._expiry_tokens = {}
        self._shutdown = False
        self._inflight = {}
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
                for key, card in self.cards.items():
                    anchoring.adopt_receipt(self, card)
                    for row in card["rows"].values():
                        if row["state"] not in _TERMINAL:
                            row["state"] = "unknown"
                            row.pop("activity_reason", None)
                        elif not row.get("original_call_id") and self._valid_number(row.get("terminal_at")) and not self._valid_number(row.get("display_expires_at")):
                            row["display_expires_at"] = row["terminal_at"] + presentation.terminal_ttl_seconds(self, key)
                            changed = True
                    batches.refresh(self, key)
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

    @staticmethod
    def _valid_number(value):
        return type(value) in (int, float) and not isinstance(value, bool) and math.isfinite(value)

    def _now(self):
        return float(self._clock())

    def _display_projection(self, key):
        projection = self._projection(key, retain_batches=True)
        selected = presentation.select_rows(projection, now=self._now(),
                                            max_visible_roots=presentation.max_visible_roots(self, key))
        projection["rows"] = {identity: row for identity, row in projection["rows"].items() if identity in selected}
        return projection

    def _schedule_expiry(self, key):
        if self._shutdown or key not in self.cards:
            return
        anchor = self._anchor(key)
        scope = self._scope(self.cards[anchor])
        nearest = None
        for row in self._display_projection(anchor)["rows"].values():
            deadline = row.get("display_expires_at")
            if row.get("state") in _TERMINAL and self._valid_number(deadline) and deadline > self._now():
                nearest = deadline if nearest is None else min(nearest, deadline)
        old = self._expiry_timers.pop(scope, None)
        if old is not None:
            old.cancel()
        if nearest is None:
            self._expiry_tokens.pop(scope, None)
            return
        token = uuid.uuid4().hex
        self._expiry_tokens[scope] = token
        loop = asyncio.get_running_loop()
        self._expiry_timers[scope] = loop.call_later(max(0.0, nearest - self._now()), self._expiry_callback,
                                                     scope, anchor, token)

    def _expiry_callback(self, scope, anchor, token):
        if self._shutdown or self._expiry_tokens.get(scope) != token:
            return
        if anchor not in self.cards or self._anchor(anchor) != anchor or self._scope(self.cards[anchor]) != scope:
            return
        self._expiry_timers.pop(scope, None)
        self._queue(anchor)

    async def shutdown(self):
        self._shutdown = True
        for timer in self._expiry_timers.values():
            timer.cancel()
        self._expiry_timers.clear()
        self._expiry_tokens.clear()

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

    def _owner_matches(self, original, current):
        from tools.delegation_owner import delegation_owner_matches
        if original == current:
            return True
        store = getattr(self.runner, "session_store", None)
        resolve_db = getattr(store, "_db_for_key", None)
        db = resolve_db(original.get("session_key")) if callable(resolve_db) else None
        return delegation_owner_matches(original, current, db)

    def _actor_matches(self, card, session_id, owner):
        original = card.get("delegation_owner", card["owner"])
        # Preserve exact-session callbacks. Crossing a compression boundary
        # additionally requires the complete trusted owner scope.
        if original.get("session_id") == session_id:
            return True
        if owner is None:
            return False
        return (isinstance(owner, dict) and owner.get("session_id") == session_id
                and self._owner_matches(original, owner))

    def _anchor(self, key):
        return self.cards[key].get("presentation_key", key)

    def _members(self, key):
        return [(k, c) for k, c in self.cards.items() if self._anchor(k) == key]

    def _projection(self, key, *, retain_batches=False):
        anchor = self.cards[key]
        rows = {}
        hidden = set()
        for task_key, card in self._members(key):
            for ref, row in card["rows"].items():
                identity = _row_identity(task_key, ref)
                batch_fields = batches.display_fields(card, row) if retain_batches else {}
                if not batch_fields and (card.get("retired") or (ref in (card.get("handled") or ()) and row["state"] in _TERMINAL | {"unknown"})):
                    hidden.add(identity)
                rows[identity] = {
                    **row,
                    **batch_fields,
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
                        if not self._projection(key, retain_batches=True)["rows"]:
                            await self._delete_obsolete(key)
                            await self._delete(key, card)
                        else:
                            self._queue(key)
                            self._schedule_expiry(key)

    def _queue(self, key):
        if self._shutdown:
            return
        key = self._anchor(key)
        if key not in self.pending:
            self.pending[key] = asyncio.create_task(self._flush(key))

    async def observe(self, source, session_key, session_id, generation, event_type, tool_name, data, preview=None):
        # Kept for the runner's lifecycle callback compatibility; cards intentionally
        # project canonical tool names only and never persist/render preview detail.
        scope = presentation.scope(data.get("owner") or {}, dict(
            profile=getattr(source, "profile", None), platform=source.platform.value,
            chat_id=source.chat_id, thread_id=source.thread_id))
        observed_at = self._now()
        async with self.locks.setdefault(scope, asyncio.Lock()):
            await self._observe(source, session_key, session_id, generation, event_type, tool_name, data, observed_at)

    async def _observe(self, source, session_key, session_id, generation, event_type, tool_name, data, observed_at=None):
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
                or not self._owner_matches(card_owner, {**card_owner, "session_id": str(session_id)})
                or str(card_owner.get("session_key", "")) != str(session_key)
                or str(card_owner.get("chat_id", "")) != str(source.chat_id)
                or str(card_owner.get("thread_id", "")) != str(source.thread_id or "")
                or str(owner.get("profile", "")) != str(card_owner.get("profile", ""))
                or str(owner.get("session_key", "")) != str(card_owner.get("session_key", ""))
                or str(owner.get("chat_id", "")) != str(card_owner.get("chat_id", ""))
                or str(owner.get("thread_id", "")) != str(card_owner.get("thread_id", ""))):
            return
        card = self.cards.get(key)
        late_member = bool(card and event_type == "subagent.start"
                           and batches.late_member(card, key, ref, data.get("original_call")))
        if card and (card["owner"] != card_owner
                     or card.get("delegation_owner", card["owner"]) != owner or (card.get("retired") and event_type != "subagent.admitted" and not late_member)):
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
            row.pop("activity_reason", None)
            row.pop("activity_sequence", None)
            row.pop("terminal_reason", None)
            row.pop("terminal_at", None)
            row.pop("display_expires_at", None)
            card.get("handling", {}).pop(ref, None)
            card["handled"] = [r for r in card.get("handled", ()) if r != ref]
            card["retired"] = False
            batches.refresh(self, key)
            card["generation"] += 1
            self._bind(key)
            anchor = self.cards[self._anchor(key)]
            if (anchor.get("message_deleted") and not anchor.get("message_id")
                    and not anchor.get("reanchor")):
                # Only confirmed retirement plus this validated admission grants
                # a fresh send. Bind first: a live topic survivor needs only edit.
                anchor.update(send_attempts=0, rendered="", delete_attempts=0,
                              recoveries=0, rejections=0)
                anchor.pop("message_deleted", None)
                anchor.pop("delete_retry_at", None)
                anchor.pop("retry_at", None)
            if anchoring.pending(anchor):
                # Only the validated new attempt above grants another burst.
                anchor["reanchor"]["new_work_pending"] = True
            card["revision"] = card.get("revision", 0) + 1
            self._save()
            self._queue(key)
            self._schedule_expiry(key)
            return
        if row and data.get("attempt", 0) != row.get("attempt", 0):
            return  # stale late tool/completion from a superseded execution
        if row and row["state"] in _TERMINAL | {"unknown"}:
            recovered_completion = (row["state"] == "unknown" and event_type == "subagent.complete"
                and row.get("resume_claim_id") and row.get("resume_claim_id") == data.get("resume_claim_id")
                and row.get("child_session_id") == data.get("child_session_id"))
            if not recovered_completion:
                return
        sequence = data.get("activity_sequence")
        if sequence is not None:
            if type(sequence) is not int or sequence < 1 or (row and sequence <= row.get("activity_sequence", 0)):
                return
        elif event_type == "subagent.activity":
            return  # reasons require a runtime-owned sequence; legacy tool/lifecycle still work
        if event_type == "subagent.start":
            if row:
                return
            row = card["rows"][ref] = {"thread_ref": ref, "task_label": data.get("task_label"),
                "presentation_order": presentation.next_row_order(self, key),
                "role": data.get("role"), "subagent_type": data.get("subagent_type"),
                "card_parent_task_id": data.get("card_parent_task_id"),
                "card_parent_thread_ref": data.get("card_parent_thread_ref"),
                "state": "running", "last_tool": None, "attempt": data.get("attempt", 0),
                "child_session_id": data.get("child_session_id")}
            batches.register(self, key, ref, data.get("original_call"))
            if late_member:
                card["retired"] = False  # existing roster member first observed after sibling delivery
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
            status = data.get("status", "completed")  # legacy completion events omitted status
            terminal_state = status if isinstance(status, str) and status in _TERMINAL | {"unknown"} else "unknown"
            if row.get("state") not in _TERMINAL:
                terminal_at = self._now() if observed_at is None else observed_at
                row["terminal_at"] = terminal_at
                if not row.get("original_call_id"):
                    row["display_expires_at"] = terminal_at + presentation.terminal_ttl_seconds(self, key)
            row["state"] = terminal_state
            row.pop("activity_reason", None)
            row["last_tool"] = None
            if isinstance(data.get("terminal_reason"), str) and data["terminal_reason"] in TERMINAL_LABELS:
                row["terminal_reason"] = data["terminal_reason"]
            row["result_turn_id"] = data.get("result_turn_id")
        elif row and event_type == "subagent.activity":
            reason = data.get("activity_reason")
            if reason is not None and (not isinstance(reason, str) or reason not in WAIT_LABELS):
                return
            if reason is None:
                row.pop("activity_reason", None)
            else:
                row["activity_reason"] = reason
        elif row and event_type == "subagent.tool" and tool_name:
            row["last_tool"] = _tool_label(tool_name, "tool", 60)
            if sequence is not None and "activity_reason" in data:
                if isinstance(data["activity_reason"], str) and data["activity_reason"] in WAIT_LABELS:
                    row["activity_reason"] = data["activity_reason"]
                elif data["activity_reason"] is None:
                    row.pop("activity_reason", None)
        else:
            return
        if sequence is not None:
            row["activity_sequence"] = sequence
        batches.refresh(self, key)
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
        self._schedule_expiry(key)

    async def _transport(self, key, operation):
        scope = self._scope(self.cards[key])
        lock = self.locks[scope]
        self._inflight[scope] = key
        lock.release()
        try:
            return await operation
        finally:
            await lock.acquire()
            self._inflight.pop(scope, None)

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
                revision = card.get("revision", 0)
                projection = self._display_projection(key)
                if not projection["rows"]:
                    # An empty display can mean TTL expiry or completed lifecycle
                    # cleanup. Only retained rows need display-only bookkeeping
                    # reset; deferred retirement must keep its deletion receipts.
                    ttl = bool(self._projection(key, retain_batches=True)["rows"])
                    await self._delete(key, card, ttl=ttl)
                    await self._delete_obsolete(key)
                    return
                text = presentation.render(self, key, projection)
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
                sent_text = text

                def latest():
                    nonlocal sent_text
                    if self._shutdown or self._anchor(key) != key or card.get("message_id") != message_id:
                        return None
                    sent_text = presentation.render(self, key, self._projection(key, retain_batches=True))
                    return sent_text or None

                fresh_edit = getattr(type(adapter), "edit_delegation_card", None)
                if message_id:
                    operation = (fresh_edit(adapter, source, message_id, latest) if fresh_edit else
                                 adapter.edit_message(source.chat_id, message_id, text, finalize=True,
                                                      metadata={"hermes_status": True}))
                    result = await self._transport(key, operation)
                    missing = "message to edit not found" in str(getattr(result, "error", "")).lower()
                    if (missing and card["recoveries"] < 1 and not card.get("reanchor")
                            and not presentation.fenced(self, key)):
                        card["recoveries"] += 1
                        card["message_id"] = None
                        card["send_attempts"] = 0
                        message_id = None
                        result = None
                else:
                    result = None
                if (result is None and card["send_attempts"] < 1
                        and not presentation.fenced(self, key)):
                    card["send_attempts"] += 1  # ambiguous sends must not spam retries
                    self._save()  # persist the attempt BEFORE an ambiguous transport await
                    result = await self._transport(key, adapter.send_delegation_card(source, latest if fresh_edit else text))
                    if getattr(result, "success", False):
                        card["message_id"] = str(result.message_id)
                        card.pop("message_deleted", None)
                        card["delete_attempts"] = 0
                        card["anchored_at"] = time.time()
                        self.displacement.pop(key, None)
                    elif (getattr(result, "raw_response", None) or {}).get("definite_rejection") and card.get("rejections", 0) < 1:
                        card["rejections"] = card.get("rejections", 0) + 1
                        card["send_attempts"] = 0  # one retry, on a subsequent observed event only
                if getattr(result, "success", False):
                    card.pop("retry_at", None)
                    card["rendered"] = sent_text
                    if sent_text != presentation.render(self, key, self._projection(key, retain_batches=True)):
                        card["revision"] = card.get("revision", 0) + 1
                    presentation.published(self, key)
                    await self._delete_obsolete(key)
                elif (getattr(result, "raw_response", None) or {}).get("cancelled_before_send"):
                    if not card.get("message_id"):
                        card["send_attempts"] = 0
                    elif not self._display_projection(key)["rows"]:
                        card["revision"] = card.get("revision", 0) + 1
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
            if key in self.cards:
                self._schedule_expiry(key)
            if (self.cards[key].get("delete_retry_at") or self.cards[key].get("retry_at")
                    or (revision is not None and revision != self.cards[key].get("revision", 0))) and not asyncio.current_task().cancelling():
                self._queue(key)
        # Events arriving during transport awaits are coalesced, not dropped.
        card = self.cards[key]
        if revision is not None and (revision != card.get("revision", 0) or card.get("retry_at")):
            self._queue(key)

    async def handling(self, source, session_key, session_id, generation, *,
                       actor_session_id, parent_task_id, refs, reason, detail=None, turn_id=None, actor_owner=None):
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
            if (not self._actor_matches(card, actor_session_id, actor_owner)
                    or not self._owner_matches(card["owner"], {**card["owner"], "session_id": session_id})
                    or card["owner"].get("session_key") != session_key
                    or str(card["source"]["chat_id"]) != str(source.chat_id)
                    or str(card["source"].get("thread_id") or "") != str(source.thread_id or "")
                    or any(ref not in card["rows"] or card["rows"][ref]["state"] not in _TERMINAL | {"unknown"} for ref in refs)):
                raise ValueError("Handling requires this exact parent owner's terminal rows")
            # Record the immutable identities; compression grants authority,
            # not a new owner or a nested parent's root-delivery privilege.
            actor_session_id = owner["session_id"]
            session_id = card["owner"]["session_id"]
            if reason == "validate_replacement":
                if len(refs) != 1 or (detail is not None and (not isinstance(detail, str) or not detail)):
                    raise ValueError("Replacement requires one ref and a caller reservation identity")
                ref = refs[0]
                attempt = card["rows"][ref].get("attempt", 0)
                if detail in card.get("replacement_cancellations", {}).get(ref, []):
                    raise ValueError("Replacement validation was cancelled before launch")
                if card.get("replacement_claims", {}).get(ref):
                    raise ValueError("Replacement already reserved or launch outcome unresolved; reconcile before retrying")
                claim_id = detail or uuid.uuid4().hex
                card.setdefault("replacement_claims", {})[ref] = {"id": claim_id, "attempt": attempt}
                self._save()
                return {"validated": True, "claim_id": claim_id, "attempt": attempt}
            if reason == "release_replacement":
                if not isinstance(detail, str) or not detail:
                    raise ValueError("Replacement release requires the exact caller reservation identity")
                for ref in refs:
                    claim = card.get("replacement_claims", {}).get(ref)
                    if claim and claim["id"] == detail:
                        if claim.get("launched"):
                            continue
                        card["replacement_claims"].pop(ref)
                    # Cancellation must also precede a validation callback that
                    # timed out while still queued. Never let it resurrect later.
                    cancelled = card.setdefault("replacement_cancellations", {}).setdefault(ref, [])
                    if detail not in cancelled:
                        cancelled.append(detail)
                self._save()
                return {"released": True}
            if actor_session_id != session_id and reason == "blocker_report":
                raise ValueError("A nested parent cannot attest a root user-facing delivery; incorporate its result instead")
            if turn_id is not None:
                presented = card.get("result_turns", {}).get(turn_id, {})
                if any(ref not in presented or presented[ref] != card["rows"][ref].get("attempt", 0) for ref in refs):
                    raise ValueError("Disposition requires the exact terminal attempt delivered to this processing turn; the result is absent or superseded")
            if reason == "deferred":
                if (not isinstance(detail, str) or not 1 <= len(detail.split()) <= 2
                        or len(detail.strip()) > 160):
                    raise ValueError(DEFER_REASON_GUIDANCE)
                # Normalize separators before display sanitization so tabs/newlines
                # cannot join words. Reject empty markup rather than recording it.
                detail = _label(" ".join(detail.split()), "")
                if not detail:
                    raise ValueError(DEFER_REASON_GUIDANCE)
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

    async def result_turn(self, *, actor_session_id, turn_id, results=None, actor_owner=None, include_deferred=False):
        """Exact terminal attempts presented to this turn, never historical visibility.

        The trusted runtime supplies results; model text never enters this method.
        Persist presentation identity so recovery cannot turn a later attempt into
        an acknowledgement of this one. Query only the supplied opaque turn id.
        """
        if not isinstance(turn_id, str) or not turn_id:
            raise ValueError("Missing result-processing turn identity")
        nested_deliveries = {}
        accepted = False
        for item in results or ():
            card = self.cards.get(item.get("parent_task_id"))
            if not card or not self._actor_matches(card, actor_session_id, actor_owner):
                continue
            async with self.locks.setdefault(self._scope(card), asyncio.Lock()):
                for ref in item.get("thread_refs") or ():
                    row = card["rows"].get(ref)
                    if not row or row["state"] not in _TERMINAL | {"unknown"}:
                        continue
                    attempt = (item.get("attempts") or {}).get(ref, 0)
                    if attempt != row.get("attempt", 0):
                        continue
                    accepted = True
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
            if not self._actor_matches(card, actor_session_id, actor_owner):
                continue
            for ref, attempt in card.get("result_turns", {}).get(turn_id, {}).items():
                row = card["rows"].get(ref, {})
                prior = card.get("attempt_history", {}).get(ref, {}).get(str(attempt), {})
                intent = (row.get("disposition") or card.get("handling", {}).get(ref, {})) if row.get("attempt", 0) == attempt else prior.get("disposition", {})
                if not intent.get("reason") or (intent.get("reason") == "deferred" and intent.get("turn_id") != turn_id):
                    missing.append({"parent_task_id": key, "thread_ref": ref, "attempt": attempt,
                                    "task_label": row.get("task_label", "Task")})
        answer = {"missing": missing}
        if include_deferred and accepted:
            # An arrival is a reconciliation trigger, not proof that old work is
            # accepted. Return only locators: the caller must retrieve the result
            # under its immutable owner before gaining this turn's authority.
            deferred = []
            for key, card in self.cards.items():
                if not self._actor_matches(card, actor_session_id, actor_owner):
                    continue
                for ref, row in card["rows"].items():
                    intent = row.get("disposition") or card.get("handling", {}).get(ref, {})
                    if (row.get("state") in _TERMINAL | {"unknown"}
                            and ref not in card.get("handled", ())
                            and ref not in card.get("result_turns", {}).get(turn_id, {})
                            and intent.get("reason") == "deferred"):
                        deferred.append({"parent_task_id": key, "thread_ref": ref,
                                         "attempt": row.get("attempt", 0),
                                         "task_label": row.get("task_label", "Task"),
                                         "detail": intent.get("detail", "Deferred")})
            if deferred:
                deferred.sort(key=lambda item: self.cards[item["parent_task_id"]]["rows"][item["thread_ref"]].get("followthrough_offered_at", 0))
                answer["deferred"] = deferred[:8]
                for item in answer["deferred"]:
                    self.cards[item["parent_task_id"]]["rows"][item["thread_ref"]]["followthrough_offered_at"] = time.time()
                self._save()
        return answer

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
                if self._projection(anchor_key, retain_batches=True)["rows"] or self._scope(anchor) in self._inflight:
                    self._queue(anchor_key)
                else:
                    await self._delete_obsolete(anchor_key)
                    await self._delete(anchor_key, anchor)

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
        empty = not self._projection(key, retain_batches=True)["rows"]
        display_empty = not self._display_projection(key)["rows"]
        for entry in presentation.pending(self, key):
            # Exact final-delivery receipts can retire every row before the first
            # union edit, or between a failed delete and restart. Then there is
            # no survivor to preserve; retain that distinct retirement evidence.
            if empty:
                entry.update(state="ready", projection_retired=True,
                             survivor_message_id=anchor.get("message_id"))
                self._save()
            elif display_empty:
                entry.update(state="ready", projection_expired=True,
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
        for member_key, card in self._members(key):
            message_id = card.get("obsolete_message_id")
            adapter = self._adapter(card)
            if message_id and adapter:
                if not presentation.cleanup_allowed(self, member_key, message_id):
                    continue
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

    async def _delete(self, key, card, *, ttl=False):
        """Keep the tombstone; bounded restart retries may finish failed deletion."""
        adapter = self._adapter(card)
        if not adapter or not card.get("message_id") or card.get("delete_attempts", 0) >= 3:
            return
        if not presentation.cleanup_allowed(self, key, card["message_id"]):
            return
        if self._defer_delete(card, adapter):
            return
        card["delete_attempts"] = card.get("delete_attempts", 0) + 1
        self._save()
        try:
            status_delete = getattr(type(adapter), "_delete_status_message", None)
            message_id = card["message_id"]

            def current():
                return (not self._shutdown and self._anchor(key) == key
                        and card.get("message_id") == message_id and not self._display_projection(key)["rows"])

            kwargs = {"guard": current} if ttl and getattr(type(adapter), "edit_delegation_card", None) else {}
            operation = (status_delete(adapter, card["source"]["chat_id"], message_id, **kwargs)
                         if status_delete is not None else adapter.delete_message(card["source"]["chat_id"], message_id))
            deleted = await self._transport(key, operation) if ttl else await operation
            if card.get("message_id") != message_id:
                return  # an obsolete receipt must never clear a newer anchor
            if deleted is None and status_delete is not None:
                # Explicit proof the local gate issued no request, not an API failure.
                card["delete_attempts"] -= 1
                if not ttl or current():
                    self._defer_delete(card, adapter, minimum_delay=1.0)
            elif deleted:
                card["message_id"] = None
                card["message_deleted"] = True
                if ttl:
                    # A confirmed display-only expiry is not a lifecycle retirement. Clear only
                    # per-message transport bookkeeping so a later visible attempt can create a
                    # fresh card; never touch rows, handling, results or approvals.
                    card["rendered"] = ""
                    card["send_attempts"] = 0
                    card["recoveries"] = 0
                    card["delete_attempts"] = 0
                    card.pop("rejections", None)
                    self.displacement.pop(key, None)
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
