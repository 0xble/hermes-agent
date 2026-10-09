"""Gateway delivery for durable session-control records."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

logger = logging.getLogger(__name__)


class GatewaySessionControlsMixin:
    """Drain session-control approval/outcome records without coupling tools to a live runner."""

    async def _session_control_watcher(self, interval: float = 15.0) -> None:
        await asyncio.sleep(1.0)
        while getattr(self, "_running", True):
            try:
                await self._drain_session_controls()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("session-control outbox drain failed", exc_info=True)
            await asyncio.sleep(interval)

    @staticmethod
    def _control_text(record: dict) -> str:
        kind, action = record.get("kind", ""), record.get("action", "")
        target = record.get("target_session_id", "")
        requester = record.get("requester_session_id", "")
        reason = record.get("reason") or "session-control"
        payload = record.get("payload") or {}
        detail = ""
        if kind == "goal":
            try:
                from hermes_cli.goals import GoalManager
                detail = GoalManager(target).status_line()
            except Exception:
                pass
            if action == "replace":
                detail = f"replacement: {payload.get('goal', '')}"
        elif kind == "loop":
            try:
                from hermes_cli.loops import LoopManager
                detail = LoopManager(target).status_line()
            except Exception:
                pass
        return (f"Session control request\nRequester: {requester}\nAction: {kind} {action}\n"
                f"Target: {target}\nReason: {reason}\n{detail}")

    def _control_entry(self, session_id: str):
        store = getattr(self, "session_store", None)
        return store.lookup_by_session_id(session_id) if store is not None else None

    async def _drain_session_controls(self) -> None:
        from hermes_cli import session_controls
        for record in session_controls.pending_outbox():
            status = record.get("status")
            target_entry = self._control_entry(record.get("target_session_id", ""))
            requester_entry = self._control_entry(record.get("requester_session_id", ""))
            if status == "pending" and not record.get("request_posted"):
                if target_entry is None or getattr(target_entry, "origin", None) is None:
                    continue
                source = target_entry.origin
                adapter = self._delivery_adapter_for(source)
                if adapter is None:
                    continue
                metadata = {"thread_id": getattr(source, "thread_id", None),
                            "session_control_request_id": record["id"]}
                await adapter.send_control_request(source.chat_id, self._control_text(record), record["id"], metadata=metadata)
                session_controls.mark_outbox(record["id"], "request_posted")
                continue
            if status == "pending":
                if float(record.get("expires_at") or 0) <= time.time():
                    # Marking expired is deliberately done through the same CAS path as a button press.
                    db = session_controls._db()
                    if db is not None:
                        key = session_controls._record_key(record["id"])
                        def expire(conn):
                            current = db.get_meta(key)
                            if not current:
                                return
                            import json
                            item = json.loads(current)
                            if item.get("status") == "pending":
                                item["status"] = "expired"
                                item["resolved_at"] = time.time()
                                db.set_meta(key, json.dumps(item), cursor=conn)
                        db._execute_write(expire)
                continue
            if status in {"applied", "denied", "failed", "expired"}:
                notice = self._control_notice(record)
                if not record.get("target_notice_sent") and target_entry is not None and getattr(target_entry, "origin", None) is not None:
                    source = target_entry.origin
                    adapter = self._delivery_adapter_for(source)
                    if adapter is not None:
                        if (record.get("kind"), record.get("action")) in {("goal", "pause"), ("goal", "clear")} and not record.get("continuations_cleared"):
                            try:
                                self._clear_goal_pending_continuations(target_entry.session_key, adapter)
                            except Exception:
                                logger.debug("goal continuation cleanup failed", exc_info=True)
                            session_controls.mark_outbox(record["id"], "continuations_cleared")
                        await adapter.send(source.chat_id, notice, metadata={"thread_id": getattr(source, "thread_id", None)})
                        session_controls.mark_outbox(record["id"], "target_notice_sent")
                if not record.get("requester_notified") and requester_entry is not None:
                    try:
                        await self._dispatch_plugin_message_injection(
                            session_key=requester_entry.session_key, content=notice,
                            plugin_id="session-controls")
                        session_controls.mark_outbox(record["id"], "requester_notified")
                    except Exception:
                        logger.debug("requester outcome injection failed", exc_info=True)

    @staticmethod
    def _control_notice(record: dict) -> str:
        authority = record.get("authority") or {}
        if authority.get("via") == "quote":
            via = f'quote: "{authority.get("quote", "")}"'
        elif authority.get("via") == "button":
            via = "approved by button" if record.get("status") == "applied" else "button"
        else:
            via = str(authority.get("via") or "session")
        action = f"{record.get('kind', '')} {record.get('action', '')}"
        return f"⊘ {action} by session {record.get('requester_session_id', '')} ({via})"
