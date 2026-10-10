"""Gateway delivery for durable session-control records."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)


def _send_succeeded(result: Any) -> bool:
    """Only a successful SendResult counts as delivery acceptance."""
    return bool(getattr(result, "success", False))


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
        requester_title = record.get("requester_title") or "Untitled session"
        target_title = record.get("target_title") or "Untitled session"
        reason = record.get("reason") or "session-control"
        detail = record.get("affected_text") or "(state unavailable)"
        return (
            "Session control request\n"
            f"Requester: {requester_title} ({requester})\n"
            f"Target: {target_title} ({target})\n"
            f"Action: {kind} {action}\n"
            f"Reason: {reason}\n"
            f"Affected: {detail}"
        )

    def _control_entry(self, session_id: str):
        store = getattr(self, "session_store", None)
        return store.lookup_by_session_id(session_id) if store is not None else None

    async def _control_entry_off_loop(self, session_id: str):
        return await self._run_in_executor_with_context(self._control_entry, session_id)

    async def _mark_control(self, session_controls, request_id: str, flag: str, value: Any = True):
        return await self._run_in_executor_with_context(session_controls.mark_outbox, request_id, flag, value)

    @staticmethod
    def _outbox_complete(record: dict) -> bool:
        if record.get("status") == "pending":
            return bool(record.get("request_skipped"))
        target_done = record.get("target_notice_sent") or record.get("target_notice_skipped")
        requester_done = record.get("requester_notified") or record.get("requester_notification_skipped")
        continuation_done = not record.get("continuation_prompt") or record.get("continuation_enqueued")
        cleanup_done = (
            record.get("kind"), record.get("action")
        ) not in {("goal", "pause"), ("goal", "clear")} or record.get("continuations_cleared")
        return bool(target_done and requester_done and continuation_done and cleanup_done)

    async def _mark_and_finish(self, session_controls, record: dict, flag: str, value: Any = True) -> dict:
        updated = await self._mark_control(session_controls, record["id"], flag, value)
        updated = updated or record
        if self._outbox_complete(updated) and not updated.get("outbox_done"):
            updated = (await self._mark_control(session_controls, record["id"], "outbox_done")) or updated
        return updated

    async def _session_control_route(self, entry):
        if entry is None or getattr(entry, "suspended", False):
            return None, None
        source = self._restored_source(entry)
        if source is None:
            return None, None
        return source, self._delivery_adapter_for(source)

    async def _drain_session_controls(self) -> None:
        from hermes_cli import session_controls

        await self._warm_goals_session_db("session controls")
        records = await self._run_in_executor_with_context(session_controls.pending_outbox)
        for record in records:
            try:
                await self._drain_session_control_record(record, session_controls)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning(
                    "session-control record drain failed for %s",
                    record.get("id"),
                    exc_info=True,
                )

    async def _drain_session_control_record(self, record: dict, session_controls) -> None:
        request_id = record.get("id")
        if not request_id:
            return
        status = record.get("status")
        target_entry = await self._control_entry_off_loop(record.get("target_session_id", ""))
        requester_entry = await self._control_entry_off_loop(record.get("requester_session_id", ""))

        if status == "pending":
            if float(record.get("expires_at") or 0) <= time.time():
                expired = await self._run_in_executor_with_context(
                    session_controls.expire_request, request_id
                )
                if expired is None:
                    return
                record = expired
                status = record.get("status")
            elif not record.get("request_posted") and not record.get("request_skipped"):
                source, adapter = await self._session_control_route(target_entry)
                if source is None or adapter is None:
                    failed = await self._run_in_executor_with_context(
                        session_controls.fail_request, request_id, "target_unroutable"
                    )
                    if failed is None:
                        return
                    record = failed
                    status = record.get("status")
                else:
                    metadata = dict(self._thread_metadata_for_source(source) or {})
                    metadata["session_control_request_id"] = request_id
                    from gateway.platforms.base import OUTBOUND_NOTICE, outbound_class
                    with outbound_class(OUTBOUND_NOTICE):
                        result = await adapter.send_control_request(
                            source.chat_id, self._control_text(record), request_id, metadata=metadata
                        )
                    if not _send_succeeded(result):
                        logger.info("session-control request delivery failed for %s; retrying", request_id)
                        return
                    await self._mark_and_finish(session_controls, record, "request_posted")
                    return

        if status not in {"applied", "denied", "failed", "expired"}:
            return

        # A target with no persisted gateway origin is a CLI/TUI session, not a retryable route.
        source, adapter = await self._session_control_route(target_entry)
        if not record.get("continuations_cleared"):
            # Only an applied pause/clear stops the goal; a denied, expired or failed request
            # must leave the target's queued continuation alone.
            needs_cleanup = status == "applied" and (record.get("kind"), record.get("action")) in {
                ("goal", "pause"), ("goal", "clear")
            }
            if not needs_cleanup or source is None or adapter is None:
                record = await self._mark_and_finish(
                    session_controls, record, "continuations_cleared"
                )
            else:
                try:
                    await self._run_in_executor_with_context(
                        self._clear_goal_pending_continuations, target_entry.session_key, adapter
                    )
                except Exception:
                    logger.debug("goal continuation cleanup failed", exc_info=True)
                record = await self._mark_and_finish(
                    session_controls, record, "continuations_cleared"
                )

        if not record.get("continuation_enqueued"):
            prompt = record.get("continuation_prompt")
            if not prompt:
                record = await self._mark_and_finish(
                    session_controls, record, "continuation_enqueued"
                )
            elif source is None or adapter is None:
                record = await self._mark_and_finish(
                    session_controls, record, "continuation_enqueued"
                )
            else:
                key = target_entry.session_key

                def busy():
                    return (
                        self._is_session_running(key)
                        or key in getattr(adapter, "_active_sessions", {})
                        or self._queue_depth(key, adapter=adapter) > 0
                    )

                if await self._run_in_executor_with_context(busy):
                    record = await self._mark_and_finish(
                        session_controls, record, "continuation_enqueued"
                    )
                else:
                    event = self._synthetic_prompt_event(
                        source, prompt, reply_expected=False, goal_continuation=True
                    )
                    event.metadata["gateway_session_key"] = key
                    try:
                        from gateway.wake import WakeNotAccepted, admit_internal_event
                        await admit_internal_event(adapter, event)
                    except WakeNotAccepted:
                        logger.info(
                            "session-control continuation for %s not accepted; retrying",
                            request_id,
                        )
                        return
                    except Exception:
                        logger.warning(
                            "session-control continuation admission failed for %s",
                            request_id,
                            exc_info=True,
                        )
                        return
                    if not getattr(event, "_gateway_accepted", False):
                        logger.info(
                            "session-control continuation for %s was not accepted; retrying",
                            request_id,
                        )
                        return
                    record = await self._mark_and_finish(
                        session_controls, record, "continuation_enqueued"
                    )

        notice = self._control_notice(record)
        if not record.get("target_notice_sent") and not record.get("target_notice_skipped"):
            if source is None or adapter is None:
                record = await self._mark_and_finish(
                    session_controls, record, "target_notice_skipped"
                )
            else:
                metadata = dict(self._thread_metadata_for_source(source) or {})
                metadata["session_control_request_id"] = request_id
                from gateway.platforms.base import OUTBOUND_NOTICE, outbound_class
                with outbound_class(OUTBOUND_NOTICE):
                    result = await adapter.send(source.chat_id, notice, metadata=metadata)
                if not _send_succeeded(result):
                    logger.info("session-control target notice delivery failed for %s; retrying", request_id)
                    return
                record = await self._mark_and_finish(
                    session_controls, record, "target_notice_sent"
                )

        if not record.get("requester_notified") and not record.get("requester_notification_skipped"):
            requester_source, _requester_adapter = await self._session_control_route(requester_entry)
            if requester_source is None:
                record = await self._mark_and_finish(
                    session_controls, record, "requester_notification_skipped"
                )
            else:
                content = self._requester_notice(record)
                try:
                    accepted = await self._dispatch_plugin_message_injection(
                        session_key=requester_entry.session_key,
                        content=content,
                        plugin_id="session-controls",
                    )
                except Exception:
                    logger.warning("requester outcome injection failed for %s", request_id, exc_info=True)
                else:
                    if not accepted:
                        logger.info("requester outcome injection not accepted for %s; retrying", request_id)
                        return
                    record = await self._mark_and_finish(
                        session_controls, record, "requester_notified"
                    )

        if self._outbox_complete(record) and not record.get("outbox_done"):
            await self._mark_control(session_controls, request_id, "outbox_done")

    @staticmethod
    def _control_notice(record: dict) -> str:
        kind, action = record.get("kind", ""), record.get("action", "")
        requester_id = record.get("requester_session_id", "")
        requester_title = record.get("requester_title") or "Untitled session"
        requester = f"{requester_title} ({requester_id})"
        quote = (record.get("authority") or {}).get("quote")
        status = record.get("status")
        if status == "denied":
            return f"✗ Request to {action} this {kind} was denied"
        if status == "expired":
            return f"⌛ Request to {action} this {kind} expired"
        if status == "failed":
            return f"✗ Request to {action} this {kind} failed: {record.get('error') or 'unknown error'}"
        verb = {
            "pause": "paused", "resume": "resumed", "clear": "cleared", "stop": "stopped",
            "replace": "replaced",
        }.get(action, action)
        change = f"\n{record['affected_text']}" if action == "replace" and record.get("affected_text") else ""
        if quote:
            message = str((record.get("authority") or {}).get("message") or "")
            full = f"\nFull message: \"{message}\"" if message and message != quote else ""
            return f"⊘ {kind.title()} {verb} by {requester} (your words: \"{quote}\"){change}{full}"
        return f"⊘ {kind.title()} {verb} by {requester} (approved in Telegram){change}"

    @classmethod
    def _requester_notice(cls, record: dict) -> str:
        target_id = record.get("target_session_id", "")
        target_title = record.get("target_title") or "Untitled session"
        target = f"{target_title} ({target_id})"
        action = f"{record.get('kind', '')} {record.get('action', '')}"
        if record.get("status") == "applied":
            if record.get("authority", {}).get("via") == "quote":
                how = "using your quoted words"
            else:
                how = "after Telegram approval"
            change = (f" ({record['affected_text']})"
                      if record.get("action") == "replace" and record.get("affected_text") else "")
            return f"✓ Your request to {action} in {target} was applied {how}.{change}"
        if record.get("status") == "denied":
            return f"✗ Your request to {action} in {target} was denied."
        if record.get("status") == "expired":
            return f"⌛ Your request to {action} in {target} expired without approval."
        return f"✗ Your request to {action} in {target} failed: {record.get('error') or 'unknown error'}"
