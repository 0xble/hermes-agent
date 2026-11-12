"""Cancellation settlement for runtime delivery-ledger claims."""
from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from collections.abc import Awaitable
from typing import Optional, TypeVar

from gateway.config import Platform
from gateway.platforms.event import MessageEvent
from gateway.run_shutdown import _log_suppressed
from hermes_cli.checkins import DISPLAY_KIND

logger = logging.getLogger("gateway.run")

_T = TypeVar("_T")


async def send_obligation_payloads(adapter, chat_id: str, payloads: list, obligation_id: str):
    """Preserve any root fallback across copy parts, but return a later refusal verbatim."""
    result = None
    fallback_result = None
    for payload, metadata in payloads:
        try:
            result = await adapter.send(chat_id=chat_id, content=payload, metadata=metadata)
        except Exception as send_err:
            logger.warning("obligation %s: redelivery send raised: %s", obligation_id, send_err, exc_info=True)
            return None
        if result is None or not getattr(result, "success", False):
            return result
        raw = getattr(result, "raw_response", None)
        if isinstance(raw, dict) and raw.get("thread_fallback"):
            fallback_result = result
    return fallback_result or result


class RuntimeDeliverySettlement:
    """Defer teardown cancellation until the same claim/send/refund operation settles.

    Adapted from NousResearch/hermes-agent#107135 without its older runner. A DB
    thread may commit after its awaiting task is cancelled: never restart that
    operation, or a repeated refund could alter a successor's newer claim.
    """

    def __init__(self) -> None:
        self.cancelled = False
        self.blocked_chats: dict = {}

    async def wait(self, operation: Awaitable[_T]) -> _T:
        task = asyncio.ensure_future(operation)
        while True:
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                if task.cancelled():
                    raise
                self.cancelled = True


async def _recover_obligation_policy(runner, row, chat_key, blocked_chats):
    """Retain unreadable policy for retry, retire only a proven disarm."""
    from gateway.checkin_delivery import DISARMED, permits_metadata, recovery_metadata
    try:
        policy = await recovery_metadata(
            runner, row["session_key"], row.get("resume_turn_id"),
            known_supervisor=bool(row.get("supervisor_checkin")),
        )
        permitted = await permits_metadata(runner, policy, retry_on_read_error=True)
    except (OSError, sqlite3.Error):
        logger.warning("Delivery policy unreadable for %s; retaining owed reply", row["session_key"], exc_info=True)
        blocked_chats[chat_key] = row.get("last_error") or "send_path_degraded"
        await runner._release_runtime_claim_quiet(
            row["obligation_id"], "failed to release unreadable-policy obligation %s",
            error=blocked_chats[chat_key])
        return None
    if not permitted:
        from gateway.delivery_ledger import abandon_obligation
        await asyncio.to_thread(abandon_obligation, row["obligation_id"], DISARMED)
        return None
    return policy


class RuntimeDeliveryRecoveryMixin:
    """Runtime reconnect sweep and settlement methods for GatewayRunner."""

    async def _redeliver_failed_obligations_for_platform(
        self, platform: Platform, *, profile: Optional[str] = None,
    ) -> int:
        """Settle each runtime claim before honoring adapter teardown cancellation.

        The claim/refund worker can commit after its caller is cancelled. Shield
        the same operation until it completes, refund only undispatched rows,
        and settle an in-flight send rather than treating it as safely unsent.
        """
        settlement = RuntimeDeliverySettlement()
        try:
            from gateway.delivery_ledger import ledger_enabled, sweep_failed_for_runtime
            if not await asyncio.to_thread(ledger_enabled):
                return 0
            claimed = await settlement.wait(asyncio.to_thread(
                sweep_failed_for_runtime, platform.value, profile=profile,
            ))
        except Exception:
            logger.debug(
                "runtime delivery ledger sweep failed after %s reconnect", platform.value, exc_info=True,
            )
            if settlement.cancelled:
                raise asyncio.CancelledError()
            return 0
        sendable = []
        if claimed and not settlement.cancelled:
            # Clear before dispatch so recovery cannot redeliver AND resume a turn.
            sendable = await settlement.wait(
                self._clear_resume_pending_for_claimed_obligations(claimed, require_success=True)
            )
        sendable_ids = {row["obligation_id"] for row in sendable}
        redelivered = 0
        for row in claimed or []:
            if settlement.cancelled or row["obligation_id"] not in sendable_ids:
                await settlement.wait(self._release_runtime_claim_quiet(
                    row["obligation_id"], "failed to release runtime delivery claim %s",
                    error=row.get("last_error") or "send_path_degraded",
                ))
                continue
            # Each shielded row includes the Bot API call and its ledger settlement.
            # Once cancelled, do not start another row, and never refund this one.
            delivered = await settlement.wait(self._redeliver_claimed_obligations(
                [row], runtime_settlement=settlement,
            ))
            redelivered += delivered
        if not settlement.cancelled:
            with _log_suppressed(logging.DEBUG, "arming flood redelivery timers failed", exc_info=True):
                await settlement.wait(self._arm_flood_timers_for_waiting_rows())
        if settlement.cancelled:
            raise asyncio.CancelledError
        return redelivered

    async def _redeliver_claimed_obligations(self, claimed: list, *, runtime_settlement=None) -> int:
        """Redeliver final responses for claimed rows (network half of the split): runs inside the
        bounded boot-send task, so a flood-limited send can be abandoned by the restore gate without
        reopening the turn-replay window. Returns the redelivered count."""
        # No early return on an empty claim: the boot sweep may have ADOPTED flood-refused rows that are
        # not due yet, and those still need their timer armed below.
        try:
            from gateway.delivery_ledger import RECOVERED_MARKER, mark_delivered, mark_failed
        except Exception:
            logger.debug("delivery ledger import failed", exc_info=True)
            return 0
        redelivered = 0
        blocked_chats = runtime_settlement.blocked_chats if runtime_settlement is not None else {}
        for row in claimed:
            # Ordering is per bot identity: one bot's refusal never holds another bot's messages.
            chat_key = (row.get("platform"), row.get("profile") or "default", row.get("chat_id"), row.get("thread_id"))
            if chat_key in blocked_chats and not row.get("adopted"):
                # An earlier message to this chat was refused: sending this one now would put it
                # ahead of that one. It goes back to failed and is retried after it, in order.
                await self._release_runtime_claim_quiet(
                    row["obligation_id"], "failed to release held obligation %s", error=blocked_chats[chat_key])
                continue
            if row.get("adopted"):
                # Later messages to this chat must wait for it, so they are released behind it.
                blocked_chats.setdefault(chat_key, row.get("last_error") or "send_path_degraded")
                # Adopted at boot inside its flood wait: its resume flag is cleared with the others, and
                # the timer armed below sends it once the platform's deadline has passed.
                continue
            adapter = await self._obligation_adapter(row)
            if adapter is None:
                continue
            policy = await _recover_obligation_policy(self, row, chat_key, blocked_chats)
            if policy is None:
                continue
            note_event = await self._redelivery_restart_note_event(row)
            from gateway.copy_blocks import split_copy_blocks_for
            # Separate copy messages only where the adapter sends them plain; elsewhere the
            # recovered reply renders its blocks inline, as normal delivery does.
            content, copy_blocks = split_copy_blocks_for(adapter, row["content"], inline_quotes=False)
            if row.get("needs_marker"):
                content = row.get("marker", RECOVERED_MARKER) + content
            metadata = {**policy, **({"thread_id": row["thread_id"]} if row.get("thread_id") else {})}
            payloads = [(content, metadata)] if content.strip() else []
            payloads.extend((block, {**metadata, "copy_block": True, "plain": True}) for block in copy_blocks)
            if runtime_settlement is not None and runtime_settlement.cancelled:
                # Cancellation during adapter/note admission must not begin a send.
                await self._release_runtime_claim_quiet(
                    row["obligation_id"], "failed to release cancelled runtime delivery claim %s",
                    error=row.get("last_error") or "send_path_degraded",
                )
                continue
            result = await send_obligation_payloads(
                adapter, row["chat_id"], payloads, row["obligation_id"]
            )
            with _log_suppressed(logging.DEBUG, "delivery ledger update failed", exc_info=True):
                from gateway.checkin_delivery import DISARMED
                if result is not None and getattr(result, "error", None) == DISARMED:
                    from gateway.delivery_ledger import abandon_obligation
                    await asyncio.to_thread(abandon_obligation, row["obligation_id"], DISARMED)
                    continue
                if result is not None and getattr(result, "success", False):
                    await asyncio.to_thread(mark_delivered, row["obligation_id"], result)
                    if note_event is not None:
                        reconcile = getattr(adapter, "_reconcile_restart_note_after_delivery", None)
                        if callable(reconcile):
                            try:
                                await reconcile(note_event, row["session_key"])
                            except Exception:
                                logger.debug(
                                    "Recovered obligation %s: restart-note reconciliation failed",
                                    row["obligation_id"], exc_info=True,
                                )
                    redelivered += 1
                    logger.info(
                        "Redelivered recovered final response to %s:%s (obligation %s, attempt %d)",
                        row["platform"], row["chat_id"], row["obligation_id"], row["attempts"],
                    )
                else:
                    blocked_chats[chat_key] = str(getattr(result, "error", "") or "send failed")
                    await asyncio.to_thread(
                        mark_failed, row["obligation_id"], blocked_chats[chat_key]
                    )
        # Whatever is still waiting on a flood penalty or a retry backoff (adopted at boot, skipped as not
        # yet due, refused again just now) gets a timer, so no rejected reply waits for the next restart.
        if runtime_settlement is None:
            with _log_suppressed(logging.DEBUG, "arming flood redelivery timers failed", exc_info=True):
                await self._arm_flood_timers_for_waiting_rows()
        return redelivered

    async def _ledger_crash_left_replies(self, max_age_seconds: int) -> int:
        """Settle every marked turn whose final reply was persisted and clear its marker, so
        auto-resume does not regenerate it: a reply live delivery would have suppressed is owed
        nothing, any other goes to the delivery ledger for the boot sweep. Without the ledger a
        presentable reply stays marked and resumes."""
        from gateway.delivery_ledger import compute_obligation_id, ledger_enabled, record_crash_left_reply
        ledger_on = await asyncio.to_thread(ledger_enabled)
        cutoff = time.time() - max_age_seconds  # older markers are cleared, never acted on
        marked = await asyncio.to_thread(self._snapshot_active_turns_for_recovery)
        ledgered = 0
        for key, session_id, token, started_at, origin, profile in marked:
            started = started_at.timestamp()  # aware UTC marker; a pre-upgrade naive one reads as local
            if started < cutoff:
                continue
            from gateway.checkin_delivery import permits_recovery
            text = None
            supervisor_checkin = False
            try:
                history = await self.async_session_store.load_transcript(session_id)
                visible = [message for message in history if isinstance(message, dict)
                           and message.get("role") not in ("session_meta", "system")]
                prompt = next((message for message in reversed(visible[:-1])
                               if message.get("role") == "user"), {})
                supervisor_checkin = prompt.get("display_kind") == DISPLAY_KIND
                text = self._crash_left_reply(history, started, origin)
                if text is None or (text and not ledger_on):
                    continue  # no final reply to deliver: the turn resumes
                if text and not await permits_recovery(
                        self, key, token, known_supervisor=supervisor_checkin):
                    text = ""
            except (OSError, sqlite3.Error):
                # A saved final reply is still owed. Falling back to the base recovery path
                # clears the marker and ledgers it, so startup cannot turn it into a fresh resume.
                logger.warning("Crash-left delivery policy unreadable for %s; ledgering saved reply", key, exc_info=True)
                if text is None:
                    continue
            if text:
                for ref, content in await asyncio.to_thread(
                        self._crash_left_parts, key, token, text, started, origin):
                    await asyncio.to_thread(
                        record_crash_left_reply,
                        obligation_id=compute_obligation_id(key, ref, content), session_key=key,
                        platform=str(getattr(origin.platform, "value", origin.platform)), chat_id=origin.chat_id,
                        thread_id=origin.thread_id, content=content, since=started, adapter_profile=profile,
                        # Parts were matched against the ledger above; only a single whole-reply
                        # row still needs the content-based duplicate check.
                        resume_turn_id=token, part=ref != f"crash:{token}" or text != content,
                        supervisor_checkin=supervisor_checkin)
            if await self.async_session_store.clear_turn_active(key, token) and text:
                ledgered += 1
        return ledgered
