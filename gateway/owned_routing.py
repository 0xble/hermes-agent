"""Opt-in, generation-fenced Telegram admission through the owning native adapter."""
from __future__ import annotations

import asyncio
import json
import time
from contextlib import closing
from contextvars import ContextVar
from dataclasses import fields
from datetime import datetime
from pathlib import Path

from gateway.config import Platform
from gateway.deadline import connect_sqlite
from gateway.platforms.event import MessageEvent, MessageType
from gateway.session import SessionSource
from gateway.session_identity import identity_of


_owned_callback_replay: ContextVar[bool] = ContextVar("owned_callback_replay", default=False)


def _source_payload(source):
    return {field.name: (getattr(source, field.name).value if field.name == "platform"
                         else getattr(source, field.name)) for field in fields(SessionSource)
            if field.init and field.name != "profile_route_rejected"}


def _event_payload(event):
    # The normalized event, not a reconstruction from Telegram's prompt message.
    # Cached Telegram media paths are shared under HERMES_HOME across generations.
    if event.platform_update_id is None:
        raise RuntimeError("event cannot be handed to another generation")
    return {field.name: (getattr(event, field.name).value if field.name == "message_type"
                         else getattr(event, field.name).isoformat() if field.name == "timestamp"
                         else _source_payload(event.source) if field.name == "source"
                         else getattr(event, field.name))
            for field in fields(MessageEvent) if field.init and field.name != "raw_message"}


def _restore_event(payload, adapter):
    data = json.loads(payload)
    source = data.pop("source")
    source.pop("platform")
    source.pop("profile", None)
    source = adapter.build_source(**{name: value for name, value in source.items()
                                     if name in {"chat_id", "chat_name", "chat_type", "user_id",
                                                 "user_name", "thread_id", "chat_topic", "user_id_alt",
                                                 "chat_id_alt", "is_bot", "scope_id", "guild_id",
                                                 "parent_chat_id", "message_id", "role_authorized",
                                                 "auto_thread_created", "auto_thread_initial_name"}})
    event_fields = {field.name for field in fields(MessageEvent) if field.init and field.name != "raw_message"}
    event = MessageEvent(**{name: value for name, value in {
        **data, "source": source,
        "message_type": MessageType(data["message_type"]),
        "timestamp": datetime.fromisoformat(data["timestamp"]),
    }.items() if name in event_fields})
    adapter._canonicalize(event.source)
    setattr(event, "_owned_replay", True)
    return event


class OwnedRouting:
    def __init__(self, generation):
        self.generation = generation
        self._task: asyncio.Task | None = None
        self._last_warning = 0.0
        self._last_probe: dict[str, float] = {}
        self._last_prune = float("-inf")

    def bind(self, runner):
        self.generation.runner = runner
        for adapter in runner.adapters.values():
            if getattr(adapter, "platform", None) == Platform.TELEGRAM:
                adapter._owned_routing = self

    def _adapters(self):
        return [adapter for adapter in self.generation.runner.adapters.values()
                if getattr(adapter, "platform", None) == Platform.TELEGRAM]

    def _home(self, source):
        return str(self.generation.runner._resolve_profile_home_for_source(source))

    def _live_keys(self):
        keys = set()
        for adapter in self._adapters():
            keys.update(getattr(adapter, "_active_sessions", {}))
            keys.update(getattr(adapter, "_pending_messages", {}))
            keys.update(getattr(adapter, "_pending_text_batches", {}))
            for attr in ("_pending_photo_batches", "_media_group_events"):
                for event in getattr(adapter, attr, {}).values():
                    try:
                        keys.add(adapter._event_session_key(event))
                    except Exception:
                        continue
        approvals = getattr(self.generation.runner, "_pending_approvals", None)
        if isinstance(approvals, dict):
            keys.update(approvals)
        return keys

    def validate_live(self):
        keys = self._live_keys() | self._delegation_keys()
        if any(not key.startswith("agent:") for key in keys):
            raise RuntimeError("unscoped session obligation during transfer")
        return keys

    def _delegation_keys(self):
        # A child finishing is not its result being admitted. Keep the spawning
        # session on A through durable completion delivery, including that gap.
        from gateway.status import get_process_start_time, start_time_fingerprints_match
        pid = self.generation.identity.pid
        try:
            started = get_process_start_time(pid)
        except Exception:
            started = None
        served = getattr(self.generation.runner, '_served_profile_homes', None)
        homes = {self.generation.coordinator.home,
                 *(served.values() if isinstance(served, dict) else ())}
        keys = set()
        for home in homes:
            path = Path(home) / 'state.db'
            if not path.exists():
                continue
            with closing(connect_sqlite(f'file:{path}?mode=ro', uri=True)) as conn:
                if conn.execute("SELECT 1 FROM sqlite_master WHERE name='async_delegations'").fetchone():
                    rows = conn.execute(
                        "SELECT origin_session, owner_started_at FROM async_delegations "
                        "WHERE owner_pid=? AND delivery_state='pending'", (pid,))
                    for origin, recorded in rows:
                        if not origin:
                            continue
                        # Unknown ownership metadata must stay with this process. Only a
                        # clearly mismatched fingerprint proves PID reuse.
                        if recorded is None or started is None:
                            keys.add(origin)
                            continue
                        try:
                            matches = start_time_fingerprints_match(recorded, started)
                        except Exception:
                            matches = True
                        if matches:
                            keys.add(origin)
        return keys

    def claim_live(self):
        from gateway.session import profile_from_session_key_namespace
        from hermes_cli.profiles import get_profile_dir
        for key in self.validate_live():
            profile = profile_from_session_key_namespace(key.split(":", 2)[1])
            home = str(self.generation.home if profile == "default" else get_profile_dir(profile))
            self.generation.coordinator.freeze_session(
                home, "telegram", key, self.generation.identity.id, self.generation.epoch)

    async def route_message(self, adapter, event, key):
        identity = identity_of(event.source)
        if event.internal:
            return False
        if identity is None:
            return True
        if event.source.user_id is not None and adapter._is_sender_authorized(
                event.source.user_id, event.source.chat_type, event.source.chat_id,
                thread_id=event.source.thread_id) is not True:
            return False  # The native runner owns pairing and unauthorized-DM replies.
        if event.platform_update_id is None:
            return False  # Synthetic prompts have no transport ID for deduplication.
        home = self._home(event.source)
        payload = lambda: json.dumps({"event": _event_payload(event)}, ensure_ascii=False).encode()
        journal = getattr(adapter, "_controlled_journal", None)
        envelope = json.dumps({"version": 1, "authorized": True,
                               "token_hash": journal.token_hash if journal is not None else None,
                               "sender": event.source.user_id, "chat": event.source.chat_id,
                               "thread": event.source.thread_id,
                               "is_bot": bool(getattr(event.source, "is_bot", False)),
                               "profile": identity.runtime_profile,
                               "transport_profile": identity.transport_profile,
                               "home": home}, ensure_ascii=False).encode()
        row, fresh = await asyncio.to_thread(
            self.generation.coordinator.enqueue_owned, home, "telegram", key,
            str(event.platform_update_id), "message", envelope, payload,
            self.generation.identity.id, self.generation.epoch)
        if row["owner_id"] == self.generation.identity.id:
            scope = (self.generation.identity.id, home, identity.runtime_profile,
                     identity.transport_profile, key, event.platform_update_id,
                     event.source.user_id, event.source.chat_id, event.source.thread_id)
            if not fresh:
                return not (row["payload"] == b"{}" and
                            getattr(event, "_owned_local_scope", None) == scope and
                            getattr(event, "_owned_local_pending", None) == row["id"])
            if row["payload"] != b"{}":
                return True  # An earlier replay row owns this lane; drain in sequence.
            event._owned_local_pending = row["id"]
            event._owned_local_scope = scope
            return False
        return True

    async def route_callback(self, adapter, update, source, key):
        identity = identity_of(source)
        if identity is None:
            await update.callback_query.answer(text="This action is unavailable.")
            return True
        home = self._home(source)
        journal = getattr(adapter, "_controlled_journal", None)
        envelope = json.dumps({"version": 1, "authorized": True,
                               "token_hash": journal.token_hash if journal is not None else None,
                               "sender": source.user_id, "chat": source.chat_id,
                               "thread": source.thread_id, "profile": identity.runtime_profile,
                               "transport_profile": identity.transport_profile,
                               "home": home}, ensure_ascii=False).encode()
        payload = lambda: json.dumps({"callback": update.to_dict()}, ensure_ascii=False).encode()
        row, fresh = await asyncio.to_thread(
            self.generation.coordinator.enqueue_owned, home, "telegram", key,
            str(update.update_id), "callback", envelope, payload,
            self.generation.identity.id, self.generation.epoch)
        if row["owner_id"] == self.generation.identity.id:
            if not fresh:
                return True
            if row["payload"] != b"{}":
                return True
            return False
        return True

    async def drain(self):
        delay = .1
        while not self.generation._drain_stopping:
            try:
                active = await self._drain_once()
                delay = .1 if active or getattr(self.generation.runner, "_overlap_draining", False) else min(2., delay * 2)
            except Exception:
                from gateway.run_generation import logger
                now = time.monotonic()
                if now - self._last_warning >= 30:
                    logger.warning("owned admission drain failed; retaining rows", exc_info=True)
                    self._last_warning = now
                delay = min(2., delay * 2)
            await asyncio.sleep(delay)

    async def _drain_once(self):
        store = self.generation.coordinator
        owner = self.generation.identity.id

        def read_inbox():
            with store._transaction() as db:
                rows = [dict(row) for row in db.execute(
                    "SELECT * FROM inbox WHERE owner_id=? AND state='pending' ORDER BY id", (owner,))]
                foreign = [row[0] for row in db.execute(
                    "SELECT DISTINCT owner_id FROM inbox WHERE owner_id!=? AND state='pending' "
                    "UNION SELECT generation_id FROM sessions WHERE generation_id!=? AND outstanding_work>0",
                    (owner, owner))]
            return rows, foreign

        rows, foreign = await asyncio.to_thread(read_inbox)
        now = time.monotonic()
        for foreign_owner in foreign:
            if now - self._last_probe.get(foreign_owner, float("-inf")) >= 2:
                await asyncio.to_thread(store.hold_dead_owner, foreign_owner)
                self._last_probe[foreign_owner] = now
        for row in rows:
            # Locally admitted events are already entering the native handler on
            # this generation; the placeholder is not a cross-owner replay.
            if row["payload"] == b"{}":
                continue
            try:
                adapter = next((a for a in self._adapters() if a._owner_transport_profile() in
                                (None, json.loads(row["authorized_source"])["transport_profile"])), None)
                if adapter is None:
                    continue
                envelope = json.loads(row["authorized_source"])
                data = json.loads(row["payload"])
                if (envelope.get("authorized") is not True or envelope.get("version") != 1
                        or envelope.get("home") != row["profile_home"]):
                    await asyncio.to_thread(store.disposition, row["id"], owner, row["owner_epoch"], "refused")
                    continue
                if row["kind"] == "message":
                    event = _restore_event(json.dumps(data["event"]), adapter)
                    source = event.source
                else:
                    from telegram import Update
                    update = Update.de_json(data["callback"], adapter._bot)
                    query = update.callback_query
                    cb = adapter._callback_ctx(query)
                    source = adapter.build_source(chat_id=str(cb["chat_id"]),
                        chat_type="dm" if cb["chat_type"] == "private" else "group",
                        user_id=str(query.from_user.id), thread_id=str(cb["thread_id"]) if cb["thread_id"] else None)
                    adapter._canonicalize(source)
                if (source.user_id != envelope.get("sender") or source.chat_id != envelope.get("chat")
                        or bool(getattr(source, "is_bot", False)) != bool(envelope.get("is_bot", False))
                        or (source.user_id is not None and adapter._is_sender_authorized(
                            source.user_id, source.chat_type, source.chat_id,
                            thread_id=source.thread_id) is not True)
                        or self._home(source) != row["profile_home"]
                        or adapter._source_session_key(source) != row["session_key"]):
                    await asyncio.to_thread(store.disposition, row["id"], owner, row["owner_epoch"], "refused")
                    continue
                if row["kind"] == "message":
                    await adapter.handle_message(event)
                    accepted = event._gateway_accepted
                else:
                    token = _owned_callback_replay.set(True)
                    try:
                        await adapter._handle_callback_query(update, None)
                    finally:
                        _owned_callback_replay.reset(token)
                    accepted = True
                if not await asyncio.to_thread(store.disposition, row["id"], owner, row["owner_epoch"],
                                               "accepted" if accepted else "refused"):
                    raise RuntimeError("owned dispatch disposition refused after handler returned")
            except Exception:
                from gateway.run_generation import logger
                logger.warning("owned dispatch failed; interrupting row %s", row["id"], exc_info=True)
                await asyncio.to_thread(store.interrupt_row, row["id"], owner, row["owner_epoch"])
        if getattr(self.generation.runner, "_overlap_draining", False):
            def read_claims():
                with store._transaction() as db:
                    return [dict(row) for row in db.execute(
                        "SELECT * FROM sessions WHERE generation_id=?", (owner,))]

            claims = await asyncio.to_thread(read_claims)
            live = self._live_keys() | await asyncio.to_thread(self._delegation_keys)
            from tools.process_registry import process_registry
            # A claim is retained only for work belonging to its own session.
            lease_rows = await asyncio.to_thread(store.leases)
            lease = next((x for x in lease_rows if x["resource"] == "active_generation"), None)
            if lease and lease["generation_id"] != owner:
                for claim in claims:
                    key = claim["session_key"]
                    count = int(key in live or key in self.generation.runner._pending_approvals or
                                process_registry.has_active_for_session(key) or
                                any(w.get("session_key") == key for w in process_registry.pending_watchers))
                    await asyncio.to_thread(store.set_outstanding, claim["profile_home"], claim["transport"], key,
                                             owner, claim["epoch"], count)
                    if not count:
                        await asyncio.to_thread(store.transfer_session, claim["profile_home"], claim["transport"], key,
                                                 owner, claim["epoch"], lease["generation_id"], lease["epoch"])
        if now - self._last_prune >= 60:
            await asyncio.to_thread(store.prune_settled_inbox, owner, self._live_keys())
            self._last_prune = now
        return bool(rows or foreign)
