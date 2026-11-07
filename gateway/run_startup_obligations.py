"""Startup claim, redelivery and resume of durable gateway obligations."""

from __future__ import annotations

import logging
from typing import Optional
from gateway.platforms.event import MessageEvent
from gateway.session import SessionSource


logger = logging.getLogger('gateway.run')


class GatewayStartupMixin:
    async def _clear_resume_pending_for_claimed_obligations(
        self, claimed: list, *, require_success: bool = False
    ) -> list:
        """Clear only the resume owner answered by each row and return rows safe to redeliver.
        Startup recovery is best-effort, so an ownership mismatch leaves a successor marker in place
        while its predecessor remains sendable. Runtime reconnect recovery (``require_success``) is
        stricter: if the session-store compare-and-swap fails the response must not be sent."""
        from gateway.run_startup import (
            inspect,
        )
        sendable = []
        for row in claimed:
            session_key = row.get("session_key") or ""
            if not session_key:
                sendable.append(row)
                continue
            try:
                expected_marker = row.get("resume_marker")
                expected_turn_id = row.get("resume_turn_id")
                # Rows written before ownership fields existed get a marker snapshot immediately
                # before the compare-and-swap. This keeps a successor marked after the snapshot from
                # being cleared, while new rows carry the exact owner captured at record time.
                if expected_marker is None and expected_turn_id is None:
                    marker_reader = getattr(self.async_session_store, "get_resume_pending_marker", None)
                    if callable(marker_reader):
                        marker = marker_reader(session_key)
                        if inspect.isawaitable(marker):
                            marker = await marker
                        if isinstance(marker, tuple) and len(marker) == 3:
                            expected_marker = marker
                clear_kwargs = {}
                if expected_marker is not None:
                    clear_kwargs["expected_marker"] = expected_marker
                if expected_turn_id is not None:
                    clear_kwargs["expected_turn_id"] = expected_turn_id
                cleared = await self.async_session_store.clear_resume_pending(session_key, **clear_kwargs)
                if require_success and not cleared:
                    # False is also the normal result when this row's session has no resume marker.
                    # Re-read after the CAS attempt so a successor marker still blocks delivery,
                    # while an already-cleared session remains sendable on reconnect.
                    marker_reader = getattr(self.async_session_store, "get_resume_pending_marker", None)
                    marker = marker_reader(session_key) if callable(marker_reader) else None
                    if inspect.isawaitable(marker):
                        marker = await marker
                    if marker is not None:
                        continue
            except Exception:
                logger.debug("clear_resume_pending failed for %s", session_key, exc_info=True)
                if require_success:
                    continue
            sendable.append(row)
        return sendable


    async def _claim_pending_obligations(self) -> list:
        """Claim recoverable delivery-ledger rows and clear their ``resume_pending`` flags (pure DB
        work, no sends). Must run INLINE BEFORE ``_schedule_resume_pending_sessions`` and the
        abandonable boot-send task: these sessions already produced their answer, so the resume path
        must not re-run (re-pay for) the turn however long the sends take. Mid-send / rejected rows
        carry a visible recovered-reply marker (gateway/delivery_ledger.py). Returns the rows.

        A session with a recoverable obligation already produced its answer — the turn completed and only
        delivery is owed — so clearing ``resume_pending`` here prevents the resume path from re-running (and
        re-paying for) a turn whose output we hold, regardless of how long the sends ahead of redelivery
        take (#91969). Flood-refused rows adopted inside their wait come back flagged ``adopted``: their
        flags are cleared here too, and ``_redeliver_claimed_obligations`` leaves them to the timer.
        """
        from gateway.run_startup import (
            asyncio,
        )
        try:
            from gateway.delivery_ledger import ledger_enabled, sweep_recoverable
            if not await asyncio.to_thread(ledger_enabled):
                return []
            # Claim only rows whose exact transport owner is connected: platform-only filtering would spend
            # a disconnected bot's retry budget because another bot on that platform is online.
            _profile_adapters = getattr(self, "_profile_adapters", None) or {}
            _pval = lambda p: getattr(p, "value", str(p))
            _deliverable_targets = {(_pval(p), "default") for p in self.adapters}
            # Legacy rows (no adapter_profile) are unambiguous only without multiplexing; else fail closed.
            if not _profile_adapters:
                _deliverable_targets.update((_pval(p), None) for p in self.adapters)
            for _profile, _adapters in _profile_adapters.items():
                _deliverable_targets.update((_pval(p), _profile) for p in _adapters)
            claimed = await asyncio.to_thread(
                sweep_recoverable, None,
                deliverable_platforms={platform for platform, _ in _deliverable_targets},
                deliverable_targets=_deliverable_targets,
            )
        except Exception:
            logger.debug("delivery ledger sweep failed", exc_info=True)
            return []
        if not claimed:
            return []
        # Clear resume_pending for EVERY claimed row before any send: the answer is in the ledger.
        # Claiming already spent one of the row's redelivery attempts — the answer is in the ledger, so the
        # resume path must never re-run these turns (#91969).
        await self._clear_resume_pending_for_claimed_obligations(claimed)
        return claimed


    @staticmethod
    async def _release_runtime_claim_quiet(obligation_id, log_fmt: str, error: str = "send_path_degraded") -> None:
        """Release an unsent runtime delivery-ledger claim; log-only on failure. ``error`` is what the row
        goes back to ``failed`` with: the claim's own pre-claim error when the caller knows it, so a
        flood-refused row keeps its ``flood_control:<seconds>`` and stays on the flood timer's list (the
        release re-stamps ``updated_at``, so it waits the platform's figure once more); else
        ``send_path_degraded``."""
        from gateway.run_startup import (
            asyncio,
        )
        from gateway.delivery_ledger import release_runtime_claim
        try:
            await asyncio.to_thread(release_runtime_claim, obligation_id, error)
        except Exception:
            logger.debug(log_fmt, obligation_id, exc_info=True)


    def _schedule_flood_redelivery(self, platform, *, profile: Optional[str] = None) -> None:
        """Wake one deadline-driven ledger worker per bot identity, never sleep in a send."""
        from gateway.run_startup import Platform, asyncio, time
        from gateway.delivery_ledger import flood_retry_delay, pending_retries
        target = platform if isinstance(platform, Platform) else Platform(str(platform))
        key = (target.value, profile or "default")
        pending = getattr(self, "_flood_redelivery_tasks", None)
        if pending is None:
            pending = self._flood_redelivery_tasks = {}
            self._flood_redelivery_wakes = {}
        wakes = self._flood_redelivery_wakes
        if key in pending:
            # Remember refusals arriving during a threaded SELECT or a send. An empty stale
            # snapshot cannot retire this worker until it has observed the wake.
            wakes[key].set()
            return
        wake = wakes[key] = asyncio.Event()

        async def _redeliver_after_wait():
            try:
                while getattr(self, "_running", False):
                    wake.clear()
                    waiting = await asyncio.to_thread(pending_retries)
                    deadlines = [r["not_before"] for r in waiting
                                 if (r["platform"], r["profile"]) == key]
                    if not deadlines:
                        if wake.is_set():
                            continue
                        return
                    delay = flood_retry_delay(min(deadlines) - time.time())
                    try:
                        await asyncio.wait_for(wake.wait(), timeout=delay)
                        continue  # A shorter sibling may now be due first.
                    except TimeoutError:
                        pass
                    if getattr(self, "_running", False):
                        await self._redeliver_failed_obligations_for_platform(target, profile=profile)
            finally:
                pending.pop(key, None)
                wakes.pop(key, None)

        task = asyncio.create_task(_redeliver_after_wait(), name="flood-redelivery:{}:{}".format(*key))
        pending[key] = task
        # The gateway's ordinary shutdown drain must cancel sleeping timers too.
        background = getattr(self, "_background_tasks", None)
        if background is not None:
            self._track_task_in(background, task)


    async def _arm_flood_timers_for_waiting_rows(self) -> None:
        """Recover adopted, newly refused/rejected and unsent released rows without blocking the loop."""
        from gateway.run_startup import asyncio
        from gateway.delivery_ledger import pending_retries
        for row in await asyncio.to_thread(pending_retries):
            self._schedule_flood_redelivery(row["platform"], profile=row["profile"])


    async def _redelivery_restart_note_event(self, row: dict) -> Optional[MessageEvent]:
        """Snapshot the note owned by this delivery before its network await.

        A resumed final may have failed both its note deletion and answer send. Ledger recovery must
        reconcile that note too, but not a successor interruption arriving while the send is in flight.
        Lookup failures are best-effort and never prevent redelivery.
        """
        from gateway.run_startup import MessageEvent, MessageType
        session_key = row.get("session_key") or ""
        if not session_key:
            return None
        try:
            entry = getattr(getattr(self, "session_store", None), "_entries", {}).get(session_key)
            source = getattr(entry, "origin", None)
            if (source is None or str(source.chat_id) != str(row.get("chat_id", ""))
                    or source.platform.value != row.get("platform")
                    or source.thread_id != row.get("thread_id")):
                return None
            note = await self.async_session_store.get_restart_note(session_key)
            if not note or not note[3] or note[0] != entry.session_id:
                return None
            marker_reader = getattr(self.async_session_store, "get_resume_pending_marker", None)
            marker_available = callable(marker_reader)
            marker = await marker_reader(session_key) if marker_available else None
            event = MessageEvent(text="", message_type=MessageType.TEXT, source=source, internal=True)
            event._restart_note_expected = note
            event._restart_note_marker_api_available = marker_available
            # A recovered turn normally cleared resume_pending before its ledger row was written;
            # retain the note's owner marker for the cleared-state CAS fallback.
            event._restart_note_expected_marker = marker or tuple(note[:3])
            return event
        except Exception:
            logger.debug("Restart-note snapshot failed for recovered %s", session_key, exc_info=True)
            return None


    async def _redeliver_claimed_obligations(self, claimed: list, *, runtime_settlement=None) -> int:
        """Redeliver final responses for claimed rows (network half of the split): runs inside the
        bounded boot-send task, so a flood-limited send can be abandoned by the restore gate without
        reopening the turn-replay window. Returns the redelivered count."""
        from gateway.run_startup import _log_suppressed, asyncio, logging
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
            note_event = await self._redelivery_restart_note_event(row)
            from gateway.copy_blocks import split_copy_blocks_for
            # Separate copy messages only where the adapter sends them plain; elsewhere the
            # recovered reply renders its blocks inline, as normal delivery does.
            content, copy_blocks = split_copy_blocks_for(adapter, row["content"], inline_quotes=False)
            if row.get("needs_marker"):
                content = row.get("marker", RECOVERED_MARKER) + content
            metadata = {"thread_id": row["thread_id"]} if row.get("thread_id") else {}
            payloads = [(content, metadata)] if content.strip() else []
            payloads.extend((block, {**metadata, "copy_block": True, "plain": True}) for block in copy_blocks)
            if runtime_settlement is not None and runtime_settlement.cancelled:
                # Cancellation during adapter/note admission must not begin a send.
                await self._release_runtime_claim_quiet(
                    row["obligation_id"], "failed to release cancelled runtime delivery claim %s",
                    error=row.get("last_error") or "send_path_degraded",
                )
                continue
            result = None
            for payload, payload_metadata in payloads:
                try:
                    result = await adapter.send(chat_id=row["chat_id"], content=payload, metadata=payload_metadata)
                except Exception as send_err:
                    logger.warning("obligation %s: redelivery send raised: %s", row["obligation_id"], send_err)
                    result = None
                if result is None or not getattr(result, "success", False):
                    break
            with _log_suppressed(logging.DEBUG, "delivery ledger update failed", exc_info=True):
                if result is not None and getattr(result, "success", False):
                    await asyncio.to_thread(mark_delivered, row["obligation_id"])
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


    async def _obligation_adapter(self, row: dict):
        """Resolve the adapter for a claimed ledger row, or None when it cannot be delivered now."""
        from gateway.run_startup import (
            Platform,
        )
        try:
            platform = Platform(row["platform"])
        except Exception:
            logger.debug("obligation %s: unknown platform %r", row["obligation_id"], row.get("platform"))
            return None
        if "profile" in row:
            adapter = self._authorization_adapter(platform, row.get("profile"))
        else:
            # Startup rows preserve the historical default-adapter route.
            adapter = self.adapters.get(platform)
        # A claim (runtime or boot) whose adapter vanished before dispatch was never sent: release it
        # without spending an attempt so the reconnect sweep, which only takes 'failed' rows, delivers it
        # instead of it waiting 'attempting' for the next restart.
        # Only a flood row keeps its error (the platform's wait must be honoured); any other row
        # becomes reconnect-only, or the redelivery timer would claim and release it until the
        # adapter is back.
        if adapter is None:
            from gateway.delivery_ledger import is_flood_error

            last_error = row.get("last_error")
            await self._release_runtime_claim_quiet(
                row["obligation_id"], "failed to release undispatched runtime obligation %s",
                error=last_error if is_flood_error(last_error) else "send_path_degraded",
            )
        return adapter


    async def _redeliver_pending_obligations(self) -> int:
        """Claim + redeliver in one call. Stable shape for tests/external callers; the startup path
        calls the halves separately so the DB half runs inline before the abandonable send task."""
        return await self._redeliver_claimed_obligations(await self._claim_pending_obligations())


    def _resume_pending_candidates(self, platform=None, *, record_boot=True) -> Optional[list]:
        """Snapshot resume-pending entries; only the boot path spends breaker budget."""
        from gateway.run_startup import (
            ResumeCandidates,
        )
        try:
            with self.session_store._lock:  # noqa: SLF001 — snapshot under lock
                self.session_store._ensure_loaded_locked()  # noqa: SLF001
                candidates = ResumeCandidates([
                    entry for entry in self.session_store._entries.values()  # noqa: SLF001
                    if entry.resume_pending
                    and not entry.suspended
                    and entry.origin is not None
                    and entry.resume_reason in self._AUTO_RESUME_REASONS
                    and (platform is None or getattr(entry.origin, "platform", None) == platform)
                ])
        except Exception as exc:
            logger.warning("Failed to enumerate resume-pending sessions: %s", exc)
            return None
        # Canonical admissions, including paused unknown outcomes, own their restart decisions.
        # This snapshot already runs off-loop, so its FIFO ownership probes do too.
        from gateway.session_authorities import all_authorities, holds_admission
        authorities = all_authorities(self)
        candidates[:] = [entry for entry in candidates
                         if not any(holds_admission(authority, entry.session_id) for authority in authorities)]
        # Only boots with restart-interrupted sessions count. Shutdown stamps one-shot planned
        # intent after draining; recent chat/home notification markers remain a fallback.
        # The guard gives the first planned boot one chain-local allowance; repeated planned boots count.
        candidates.breaker_tripped = bool(getattr(self, "_restart_resume_breaker_tripped", False))
        if record_boot:
            try:
                from gateway import restart_loop_guard as _rlg
                from gateway.run import _hermes_home, _planned_restart_notification_path
                _max_restarts, _window, _max_gap = self._restart_loop_guard_config()
                _planned_exit = _rlg.consume_planned_exit(_window, max_gap_seconds=_max_gap)
                if not candidates:
                    return candidates
                _planned_restart = _planned_exit or _rlg.planned_marker_is_recent(
                    _hermes_home / ".restart_notify.json", window_seconds=_window,
                    max_gap_seconds=_max_gap, use_mtime=True,
                ) or _rlg.planned_marker_is_recent(
                    _planned_restart_notification_path(), window_seconds=_window, max_gap_seconds=_max_gap,
                )
                candidates.breaker_tripped = _rlg.check_and_record(
                    _max_restarts, _window, max_gap_seconds=_max_gap, planned_restart=_planned_restart,
                )
            except Exception as exc:  # noqa: BLE001 — breaker must fail OPEN
                logger.debug("Restart-loop guard check skipped: %s", exc)
        return candidates


    async def _resume_pending_candidates_async(self, platform=None, *, record_boot=True) -> Optional[list]:
        """Snapshot resume-pending entries without running the state.db load on the gateway loop."""
        from gateway.run_startup import asyncio
        return await asyncio.to_thread(
            self._resume_pending_candidates, platform, record_boot=record_boot,
        )


    @staticmethod
    def _build_startup_resume_event(source: SessionSource, resume_marker: Optional[str]) -> MessageEvent:
        """Build the exact internal event used to auto-continue one restart marker."""
        from gateway.run_startup import MessageEvent, MessageType
        # This turn continues the original platform message, not a synthetic producer identity.
        event = MessageEvent(text="", message_type=MessageType.TEXT, source=source,
                             message_id=getattr(source, "message_id", None), internal=True,
                             reply_anchor_override=getattr(source, "message_id", None))
        if resume_marker:
            # Keep the transport reply anchor for threading, but admit each interruption marker
            # under its own outbox identity so one marker resumes at most once.
            setattr(event, "_outbox_admission_id", f"resume:{resume_marker}")
        return event


    async def _drop_queued_resume(self, session_key: str) -> None:
        """Release a shutdown-dropped resume sentinel and keep the durable marker recoverable."""
        from gateway.run import _AGENT_PENDING_SENTINEL
        getattr(self, "_startup_resume_inflight", set()).discard(session_key)
        state = self._peek_session_state(session_key)
        if state is None or state.turn.agent is not _AGENT_PENDING_SENTINEL:
            return
        # No turn was admitted, so the original reason, turn ID and marker token still own recovery.
        self._release_running_agent_state(session_key)


    def _schedule_auto_resume_delegations(self) -> int:
        """Queue bounded parent notices for eligible abandoned single-task rows.

        This is intentionally only a queueing step. The async-delegation watcher checks
        the parent session and current route, takes the separate one-shot boot-notice
        claim, and injects a synthetic turn; no child process is recreated here.
        """
        from gateway.run_startup import (
            Path,
        )
        from tools.delegation_resume import build_auto_resume_notice, list_boot_candidates
        if not getattr(self.config, "auto_resume_on_boot", True):
            return 0
        from tools.process_registry import process_registry
        from hermes_cli.profiles import get_active_profile_name
        scheduled = 0

        def _queue_current_profile() -> None:
            nonlocal scheduled
            profile_name = get_active_profile_name() or "default"
            for record in list_boot_candidates():
                process_registry.completion_queue.put({
                    "type": "delegation_auto_resume",
                    "delegation_id": record["delegation_id"],
                    "session_key": record["session_key"],
                    "profile": profile_name,
                    "origin_ui_session_id": record.get("origin_ui_session_id", ""),
                    "origin_session_id": record.get("origin_session_id", ""),
                    "parent_session_id": record.get("parent_session_id", ""),
                    "text": build_auto_resume_notice(record),
                    "message_id": f"auto-resume:{record['delegation_id']}",
                    **{key: record["task"].get(key, "") for key in ("scope_id", "user_id", "user_name")},
                })
                scheduled += 1

        try:
            _queue_current_profile()
            from gateway.run import _multiplex_profile_homes, _profile_runtime_scope
            active = get_active_profile_name() or "default"
            for profile_name, profile_home in _multiplex_profile_homes(self.config):
                if profile_name == active:
                    continue
                try:
                    with _profile_runtime_scope(Path(profile_home), {}):
                        _queue_current_profile()
                except Exception:
                    logger.warning("Could not queue boot auto-resume notices for profile %r", profile_name, exc_info=True)
        except Exception:
            logger.warning("Could not queue boot auto-resume notices", exc_info=True)
        if scheduled:
            logger.info("Queued %d parent-facing boot auto-resume notice(s)", scheduled)
        return scheduled
