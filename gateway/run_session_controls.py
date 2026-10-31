"""Gateway delivery for durable session-control records."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from hermes_cli.session_controls import REPLACEMENT_MAX_CHARS
from hermes_cli.goal_notices import lifecycle_cards_hidden, notice_level

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
    def _clip(text: Any, limit: int) -> str:
        """Bound one free-text field so a card or notice always fits one Telegram message."""
        text = str(text or "")
        return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"

    # A resolved notice is informational. Keep it retryable briefly, then settle it so an
    # unavailable target or requester route cannot keep waking the gateway until the record's
    # 24h outbox TTL. Busy sessions are not affected: the session authority queues the notice.
    _NOTICE_RETRY_SECONDS = 15 * 60

    @classmethod
    def _notice_is_stale(cls, record: dict) -> bool:
        resolved_at = record.get("resolved_at") or record.get("created_at")
        try:
            return bool(resolved_at and time.time() - float(resolved_at) >= cls._NOTICE_RETRY_SECONDS)
        except (TypeError, ValueError):
            return False

    @classmethod
    def _control_text(cls, record: dict) -> str:
        # Keep the card scannable on mobile: decision first, then the affected session, the actor,
        # the exact change and the reason. Every free-text field remains bounded so one Telegram
        # message can carry the complete proposal with its buttons.
        kind, action = record.get("kind", ""), record.get("action", "")
        requester_title = cls._clip(record.get("requester_title") or "Untitled session", 120)
        target_title = cls._clip(record.get("target_title") or "Untitled session", 120)
        reason = cls._clip(record.get("reason") or "session-control", 600)
        # Keyed by (kind, action): goals and loops share pause/resume, and the card must name
        # the thing approval actually changes.
        decision = {
            ("goal", "clear"): "Clear this goal?",
            ("goal", "pause"): "Pause this goal?",
            ("goal", "resume"): "Resume this goal?",
            ("goal", "replace"): "Replace this goal?",
            ("loop", "pause"): "Pause this loop?",
            ("loop", "resume"): "Resume this loop?",
            ("loop", "stop"): "Stop this loop?",
        }.get((kind, action), f"Apply {kind} {action}?")
        effect = {
            ("goal", "clear"): "The target goal will become cleared.",
            ("goal", "pause"): "The target goal will stop progressing.",
            ("goal", "resume"): "The target goal will become active again.",
            ("goal", "replace"): "The target goal will be replaced by the proposal below.",
            ("loop", "pause"): "The target loop will stop waking until resumed.",
            ("loop", "resume"): "The target loop will start waking again.",
            ("loop", "stop"): "The target loop will stop.",
        }.get((kind, action), f"The target {kind} will be changed.")
        guidance = {
            ("goal", "clear"): "Approval clears the current goal. It does not replace or restore its text.",
        }.get((kind, action), "Approve only if this still matches the target's current state.")
        return (
            f"◆ {decision}\n\n"
            f"Target: {target_title}\n"
            f"Requested by: {requester_title}\n\n"
            f"{effect}\n"
            f"{cls._change_text(record)}\n\n"
            f"Reason: {reason}\n\n"
            f"{guidance}"
        )

    @classmethod
    def _change_text(cls, record: dict) -> str:
        """What changes, with a replacement's new objective shown whole.

        The current definition and the proposed one are bounded separately: clipping them together
        could cut the entire proposal off the card while Approve still applied it. Requests whose
        proposal cannot fit are refused when created (``replacement_too_long``)."""
        affected = str(record.get("affected_text") or "")
        payload = record.get("payload") or {}
        new = str(payload.get("goal") or "")
        if record.get("action") == "replace" and new:
            from hermes_cli.goals import _CONTRACT_LABELS
            from hermes_cli.session_controls import replacement_contract
            suffix = f" -> {new}"
            current = affected.removesuffix(suffix)
            lines = [f"Affected: {cls._clip(current or '(state unavailable)', 600)}",
                     f"Proposed goal: {cls._clip(new, REPLACEMENT_MAX_CHARS)}"]
            # Show the complete proposed delta. Omission preserves existing requirements,
            # while an explicit empty field removes that field through the same approval.
            contract = replacement_contract(payload)
            if contract:
                lines.append("Proposed contract edits (omitted fields unchanged):")
                lines.extend(f"- {_CONTRACT_LABELS[f]}: {v or '(remove)'}" for f, v in contract.items())
            else:
                lines.append("Proposed contract: unchanged")
            lines.append("Subgoals: unchanged")
            max_turns = payload.get("max_turns")
            if not (max_turns in (None, 0) and record.get("current_max_turns") == 0):
                budget = {None: "unchanged", 0: "unlimited"}.get(max_turns, max_turns)
                lines.append(f"Max turns: {budget}")
            return "\n".join(lines)
        return f"Affected: {cls._clip(affected or '(state unavailable)', 2400)}"

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
        """Drain every served profile's store, each under that profile's own runtime scope.

        Session-control records live in the state.db of the profile whose session created them, so
        an unscoped drain would never post a secondary profile's approval card or outcome notice
        (same shape as the loop wakeup and handoff watchers)."""
        from gateway.run import _async_profile_runtime_scope, _resolve_handoff_watch_scopes

        for _profile_name, profile_home in await _resolve_handoff_watch_scopes(self):
            if profile_home is None:
                from tui_gateway.launch_profile_policy import async_launch_profile_scope_if_multiplexed
                scope = async_launch_profile_scope_if_multiplexed()
            else:
                scope = _async_profile_runtime_scope(profile_home)
            async with scope:
                await self._drain_session_controls_in_scope()

    async def _drain_session_controls_in_scope(self) -> None:
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

    async def _drain_target_notice(self, record: dict, session_controls, source, adapter):
        if record.get("target_notice_sent") or record.get("target_notice_skipped"):
            return record
        request_id = record["id"]
        if self._notice_is_stale(record):
            logger.warning("session-control target notice stale for %s; settling as skipped", request_id)
            return await self._mark_and_finish(
                session_controls, record, "target_notice_skipped"
            )
        if source is None or adapter is None:
            return await self._mark_and_finish(
                session_controls, record, "target_notice_skipped"
            )

        metadata = dict(self._thread_metadata_for_source(source) or {})
        metadata["session_control_request_id"] = request_id
        from agent.notice_style import notice_metadata
        message = self._control_notice(record)
        metadata = notice_metadata(metadata, platform=source.platform, style=getattr(message, "notice_style", None))
        from gateway.platforms.base import OUTBOUND_NOTICE, outbound_class
        with outbound_class(OUTBOUND_NOTICE):
            result = await adapter.send(source.chat_id, message, metadata=metadata)
        if _send_succeeded(result):
            return await self._mark_and_finish(
                session_controls, record, "target_notice_sent"
            )
        if getattr(result, "retryable", False):
            logger.info("session-control target notice delivery failed for %s; retrying", request_id)
            return None
        logger.warning(
            "session-control target notice not retryable for %s; settling as skipped: %s",
            request_id, getattr(result, "error", None) or "delivery_failed",
        )
        return await self._mark_and_finish(
            session_controls, record, "target_notice_skipped"
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
            elif (not record.get("request_posted") and not record.get("request_skipped")
                  and lifecycle_cards_hidden(record.get("kind", ""))):
                # Created before cards were hidden: nobody would see it, so fail it now and let
                # the requester's outcome notice tell the agent to ask in chat.
                failed = await self._run_in_executor_with_context(
                    session_controls.fail_request, request_id, "approval_cards_hidden"
                )
                if failed is None:
                    return
                record = failed
                status = record.get("status")
            elif not record.get("request_posted") and not record.get("request_skipped"):
                source, adapter = await self._session_control_route(target_entry)
                if source is not None and adapter is None:
                    # Persisted gateway origin, adapter offline: keep the request pending and retry
                    # after reconnect; its own expiry bounds the wait.
                    logger.info("session-control request %s waiting for target adapter", request_id)
                    return
                if source is None:
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

        record = await self._settle_quiet_autonomy_notices(session_controls, record)

        # A target with no persisted gateway origin is a CLI/TUI session, not a retryable route.
        source, adapter = await self._session_control_route(target_entry)
        # Any outcome whose gateway origin persists but whose adapter is offline is retryable:
        # target-side flags stay unset until reconnect, supersession or the record's TTL.
        target_offline = source is not None and adapter is None
        if not target_offline and not record.get("continuations_cleared"):
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
                    # Adapter slots/FIFOs belong to the event loop, not the DB executor.
                    self._clear_goal_pending_continuations(
                        target_entry.session_key, adapter, record.get("resolved_at", record["created_at"]),
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
            elif target_offline:
                # continuation_is_current persists the discard receipt when the goal moved on, so a
                # stale wake is never delivered after reconnect; reload that durable record.
                if not await self._run_in_executor_with_context(
                    session_controls.continuation_is_current, request_id,
                ):
                    record = (await self._run_in_executor_with_context(
                        session_controls._load_record, request_id,
                    )) or record
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

                is_busy = await self._run_in_executor_with_context(busy)
                # The routing/idle probes may yield; persist the same definition fence afterwards.
                current = await self._run_in_executor_with_context(
                    session_controls.continuation_is_current, request_id
                )
                if not current:
                    record = await self._mark_and_finish(
                        session_controls, record, "continuation_enqueued"
                    )
                elif is_busy:
                    return  # Unrelated/stale queued work is not proof that this continuation landed.
                else:
                    record = await self._admit_control_continuation(
                        record, session_controls, source, adapter, key, prompt
                    )
                    if not record.get("continuation_enqueued"):
                        return

        if not target_offline:
            record = await self._drain_target_notice(record, session_controls, source, adapter)
            if record is None:
                return

        if not record.get("requester_notified") and not record.get("requester_notification_skipped"):
            requester_source, _requester_adapter = await self._session_control_route(requester_entry)
            if requester_source is None:
                record = await self._mark_and_finish(
                    session_controls, record, "requester_notification_skipped"
                )
            else:
                from hermes_constants import get_hermes_home
                content = self._requester_notice(record)
                try:
                    accepted = await self._dispatch_plugin_message_injection(
                        session_key=requester_entry.session_key,
                        expected_session_id=record["requester_session_id"],
                        content=content,
                        plugin_id="session-controls",
                        # The drain runs in the scope of the profile whose store holds this record,
                        # and the requester is that profile's session. Without an explicit home a
                        # local (CLI/TUI/Desktop) requester is always refused, never resolved.
                        plugin_home=get_hermes_home(),
                    )
                except Exception:
                    logger.warning("requester outcome injection failed for %s", request_id, exc_info=True)
                    accepted = False
                if accepted:
                    record = await self._mark_and_finish(
                        session_controls, record, "requester_notified"
                    )
                elif self._notice_is_stale(record):
                    # Delivery is always attempted first, so a requester that comes back is still
                    # told; one that never does (a retired or rebound session) settles here.
                    logger.warning(
                        "session-control requester notice for %s undeliverable for %d min; settling as skipped",
                        request_id, self._NOTICE_RETRY_SECONDS // 60,
                    )
                    record = await self._mark_and_finish(
                        session_controls, record, "requester_notification_skipped"
                    )
                else:
                    logger.info("requester outcome injection not accepted for %s; retrying", request_id)
                    return

        if self._outbox_complete(record) and not record.get("outbox_done"):
            await self._mark_control(session_controls, request_id, "outbox_done")

    async def _settle_quiet_autonomy_notices(self, session_controls, record):
        """Settle only cosmetic delivery of goal and loop outcomes that their notices hide.

        Runs even offline. Continuation admission and cleanup still own progress, supersession
        and recovery. With notices off, only autonomous outcomes are settled, and explicit
        authorities (quote, button, session) keep their replies. With ``notices: none``, the
        target's chat receipt is settled for every authority, while the requester's outcome
        still reaches it unless the control was autonomous (its tool result already reported it).
        """
        kind = record.get("kind")
        if kind not in {"goal", "loop"}:
            return record
        level = notice_level(f"{kind}s")
        autonomous = (record.get("authority") or {}).get("via") == "autonomy"
        if level == "all" or (level == "quiet" and not autonomous):
            return record
        flags = [("target_notice_sent", "target_notice_skipped")]
        if autonomous:
            flags.append(("requester_notified", "requester_notification_skipped"))
        for sent, skipped in flags:
            if not record.get(sent) and not record.get(skipped):
                record = await self._mark_and_finish(session_controls, record, skipped)
        return record

    async def _admit_control_continuation(self, record, session_controls, source, adapter, key, prompt):
        from gateway.wake import WakeNotAccepted, WakeSuperseded, admit_internal_event

        event = self._synthetic_prompt_event(
            source, prompt, reply_expected=False, goal_continuation=True,
            goal_session_id=record["target_session_id"],
            goal_fingerprint=record.get("continuation_fingerprint", ""),
            goal_instance=record.get("continuation_instance"),
        )
        event.metadata["gateway_session_key"] = key
        event.metadata["gateway_session_id"] = record["target_session_id"]
        event.metadata["gateway_session_strict"] = True
        event.metadata["session_control_continuation_id"] = record["id"]
        try:
            await admit_internal_event(adapter, event)
        except WakeSuperseded:
            await self._run_in_executor_with_context(session_controls.continuation_is_current, record["id"])
            return await self._mark_and_finish(session_controls, record, "continuation_enqueued")
        except WakeNotAccepted:
            logger.info("session-control continuation for %s not accepted; retrying", record["id"])
            return record
        except Exception:
            logger.warning("session-control continuation admission failed for %s", record["id"], exc_info=True)
            return record
        if not getattr(event, "_gateway_accepted", False):
            logger.info("session-control continuation for %s was not accepted; retrying", record["id"])
            return record
        return await self._mark_and_finish(session_controls, record, "continuation_enqueued")

    @staticmethod
    def _approval_notice(authority: dict) -> tuple[str, str]:
        """Target/requester wording must claim approval only for a recorded approval source."""
        return {
            "autonomy": ("applied autonomously without approval", "autonomously without approval"),
            "button": ("approved in Telegram", "after Telegram approval"),
        }.get(str(authority.get("via") or ""),
              ("authority source not recorded", "(authority source not recorded)"))

    @classmethod
    def _control_notice(cls, record: dict) -> str:
        kind, action = record.get("kind", ""), record.get("action", "")
        requester_id = record.get("requester_session_id", "")
        requester_title = cls._clip(record.get("requester_title") or "Untitled session", 120)
        requester = f"{requester_title} ({requester_id})"
        quote = (record.get("authority") or {}).get("quote")
        status = record.get("status")
        if status == "denied":
            return f"✗ Request to {action} this {kind} was denied"
        if status == "expired":
            return f"⌛ Request to {action} this {kind} expired"
        if status == "failed":
            return f"✗ Request to {action} this {kind} failed: {record.get('error') or 'unknown error'}"
        if status == "applied" and record.get("outcome") == "timed_wait":
            return f"◷ {record['notice']}. Requested by {requester}."
        verb = {
            "pause": "paused", "resume": "resumed", "clear": "cleared", "stop": "stopped",
            "replace": "replaced",
        }.get(action, action)
        change = (f"\n{cls._change_text(record)}"
                  if action == "replace" and record.get("affected_text") else "")
        from agent.notice_style import ContextNotice
        # Only committed no-action control outcomes opt in. Pending, pause and failures stay full.
        receipt = ContextNotice if status == "applied" and (kind, action) in {
            ("goal", "replace"), ("goal", "resume"), ("goal", "clear"),
            ("loop", "resume"), ("loop", "stop"),
        } else str
        authority = record.get("authority") or {}
        if authority.get("via") == "session":
            session_id = cls._clip(str(authority.get("session_id") or requester_id), 120)
            return receipt(
                f"⊘ {kind.title()} {verb} by {requester} "
                f"(applied by session {session_id} without approval){change}"
            )
        if quote and authority.get("via") != "autonomy":
            message = str(authority.get("message") or "")
            full = (f"\nFull message: \"{cls._clip(message, 1200)}\""
                    if message and message != quote else "")
            quote = cls._clip(quote, 300)
            return receipt(f"⊘ {kind.title()} {verb} by {requester} (your words: \"{quote}\"){change}{full}")
        how, _ = cls._approval_notice(authority)
        return receipt(f"⊘ {kind.title()} {verb} by {requester} ({how}){change}")

    @classmethod
    def _requester_notice(cls, record: dict) -> str:
        target_id = record.get("target_session_id", "")
        target_title = record.get("target_title") or "Untitled session"
        target = f"{target_title} ({target_id})"
        action = f"{record.get('kind', '')} {record.get('action', '')}"
        if record.get("status") == "applied":
            if record.get("outcome") == "timed_wait":
                return f"✓ {record['notice']} in {target}."
            authority = record.get("authority") or {}
            if authority.get("via") == "quote":
                how = "using your quoted words"
            elif authority.get("via") == "session":
                session_id = cls._clip(str(authority.get("session_id") or ""), 120)
                how = f"by session authority{f' from {session_id}' if session_id else ''}"
            else:
                _, how = cls._approval_notice(authority)
            change = (f"\n{cls._change_text(record)}"
                      if record.get("action") == "replace" and record.get("affected_text") else "")
            return f"✓ Your request to {action} in {target} was applied {how}.{change}"
        if record.get("status") == "denied":
            return f"✗ Your request to {action} in {target} was denied."
        if record.get("status") == "expired":
            return f"⌛ Your request to {action} in {target} expired without approval."
        return f"✗ Your request to {action} in {target} failed: {record.get('error') or 'unknown error'}"
