"""Async-delegation groups, retry delivery and orphan recovery for GatewayRunner."""

from __future__ import annotations

import logging
import asyncio
from typing import Optional


logger = logging.getLogger('gateway.run')


class GatewayNotificationsMixin:
    def _enrich_async_delegation_routing(self, evt: dict) -> None:
        """Fill platform/chat_id/thread_id/chat_type on an async-delegation event.

        Such events only carry ``session_key`` (the daemon worker lacks per-message routing
        metadata). Best-effort: a CLI-origin event (empty session_key) is left as-is and won't route.
        """
        from gateway.run import _parse_session_key
        if evt.get("platform"):
            return  # already enriched
        parsed = _parse_session_key(evt.get("session_key", "") or "")
        if not parsed:
            return
        evt["platform"] = parsed.get("platform", "")
        evt["chat_type"] = parsed.get("chat_type", "")
        evt["chat_id"] = parsed.get("chat_id", "")
        if parsed.get("thread_id"):
            evt["thread_id"] = parsed["thread_id"]


    async def _flush_async_delegation_batch(
        self, key: tuple[str, ...], delay: float, release: asyncio.Event,
    ) -> None:
        """Deliver one route's held delegation results as one turn; requeue them on failure."""
        from gateway.run_notifications import (
            asyncio,
        )
        current_task = asyncio.current_task()
        tasks, releases = self._async_delegation_batch_tasks, self._async_delegation_batch_releases
        group: list[dict] = []
        try:
            await self._wait_batch_window(delay, release)
            group = self._async_delegation_batches.pop(key, [])
            self._detach_batch_flush(tasks, releases, key, current_task, release)
            if group and await self._deliver_async_delegation_group(group) is False:
                self._requeue_completion_events(group)
        except asyncio.CancelledError:
            self._requeue_completion_events(group or self._async_delegation_batches.pop(key, []))
            raise
        except Exception:
            logger.exception("Coalesced async delegation delivery failed")
            self._requeue_completion_events(group or self._async_delegation_batches.pop(key, []))
        finally:
            self._detach_batch_flush(tasks, releases, key, current_task, release)


    async def _enqueue_async_delegation_group(self, group: list[dict]) -> Optional[bool]:
        """Deliver a same-tick group now, or hold routine results per the batch window.

        True while held: the rows stay unclaimed and pending in the durable ledger until delivery.
        """
        self._ensure_completion_batch_state()
        if self._completion_notification_batches_stopping:
            return False
        hold = min([await self._completion_hold_seconds(evt) for evt in group] or [0.0])
        key = self._event_route_key(group[0], self._ASYNC_GROUP_KEY_FIELDS)
        if hold <= 0:
            self._release_held_completions(str(group[0].get("session_key") or ""))
        if hold <= 0 and key not in self._async_delegation_batch_tasks:
            return await self._deliver_async_delegation_group(group)
        self._async_delegation_batches.setdefault(key, []).extend(group)
        self._schedule_batch_flush(
            self._async_delegation_batch_tasks, self._async_delegation_batch_releases,
            self._async_delegation_batch_flush_tasks, key, hold, self._flush_async_delegation_batch,
        )
        return True


    async def _deliver_async_delegation_group(self, group: list[dict]) -> Optional[bool]:
        """Deliver a same-session batch of async completions as ONE turn: the primary carries the
        consolidated text of every sibling THIS runner claimed (siblings owned elsewhere are excluded;
        their claims are acked only after adapter acceptance). True after acceptance, False to requeue
        the group, None when nothing is deliverable here (retry siblings requeued)."""
        from gateway.run_notifications import asyncio
        # The group shares one session_key, hence one profile: scope the pre-checks and sibling claims too.
        # A retirement can move a subset while this batch waits. Re-resolve the
        # durable routes and regroup before taking any delivery claim.
        from tools.async_delegation import refresh_retirement_route
        buckets = {}
        for evt in group:
            async with self._completion_event_scope(evt):
                await asyncio.to_thread(refresh_retirement_route, evt)
            self._enrich_async_delegation_routing(evt)
            route = self._event_route_key(evt, self._ASYNC_GROUP_KEY_FIELDS)
            buckets.setdefault(route, []).append(evt)
        if len(buckets) > 1:
            retry = []
            for subgroup in buckets.values():
                async with self._completion_event_scope(subgroup[0]):
                    result = await self._deliver_async_delegation_group_scoped(subgroup)
                if result is False:
                    retry.extend(subgroup)
            group[:] = retry
            return False if retry else True
        original = list(group)
        async with self._completion_event_scope(group[0]):
            outcome = await self._deliver_async_delegation_group_scoped(group)
        if outcome is not False:
            group[:] = original  # Only a failed/aborted batch becomes the narrowed retry payload.
        return outcome


    async def _deliver_completion_with_sibling_cleanup(
        self, consolidated: str, primary_evt: dict, siblings: list[tuple[dict, str]],
    ) -> Optional[bool]:
        from gateway.run_notifications import asyncio
        sibling_cleanup = {"owner": "batch"}
        try:
            # The delivery coroutine takes over the sibling claims once it starts. If shutdown
            # cancellation lands in the gap before that, this caller still owns and refunds them.
            return await self._deliver_completion_notification(
                consolidated, primary_evt, sibling_claims=siblings, cleanup_state=sibling_cleanup,
            )
        except asyncio.CancelledError:
            if sibling_cleanup["owner"] == "batch":
                await self._settle_durable_claims([
                    ("defer", event["delegation_id"], claim_id) for event, claim_id in siblings])
            raise


    async def _deliver_async_delegation_group_scoped(self, group: list[dict]) -> Optional[bool]:
        from gateway.run_notifications import asyncio, claim_siblings_off_loop
        from gateway.run import _format_gateway_process_notification
        from gateway.session_local_recovery import restore_completion_route
        restore_completion_route(self, str(group[0].get("session_key") or ""))
        if any(self._completion_held_by_stop(evt) for evt in group):
            return False  # requeued whole; no sibling claim is taken while the session is stopped

        def forget(evt):
            group[:] = [pending for pending in group if pending is not evt]

        async def deliver_one(text, evt):
            outcome = await self._deliver_completion_notification(text, evt)
            if outcome is not False:
                forget(evt)
            return outcome

        # API delivery does not start a model turn, so there is nothing to coalesce.
        # Keep each unit's stable identity with its row across partial delivery/retry.
        if group and group[0].get("origin_session_id"):
            outcomes = []
            for evt in list(group):
                text = _format_gateway_process_notification(evt)
                if text:
                    outcomes.append(await deliver_one(text, evt))
                else:
                    forget(evt)
            return False if False in outcomes else True
        deliverable: list[tuple[dict, str]] = []
        acknowledged = False
        retry_needed = False
        for evt in list(group):
            synth_text = _format_gateway_process_notification(evt)
            if not synth_text:
                forget(evt)
                continue
            if getattr(self, 'session_authority', None) is not None:
                from gateway.session_automation import completion_admission
                if await asyncio.to_thread(completion_admission, self, evt) is not None:
                    outcome = await deliver_one(synth_text, evt)
                    acknowledged = outcome is True or acknowledged
                    retry_needed = outcome is False or retry_needed
                    continue
            readiness = await self._completion_delivery_ready(evt)
            parent = str(evt.get("parent_session_id") or "")
            if readiness is None or (parent and await self._classify_completion_target(parent, str(evt.get("session_key") or "")) == "terminal"):
                # Classification is not settlement: a failed claim/drop must stay queued.
                if await deliver_one(synth_text, evt) is False:
                    retry_needed = True
                continue
            identity = self._completion_delivery_identity(evt)
            if identity is not None and self._completion_identity_state(identity):
                forget(evt)
                continue
            deliverable.append((evt, synth_text))
        if not deliverable:
            return False if retry_needed else (True if acknowledged else None)
        if len(deliverable) == 1:
            evt, synth_text = deliverable[0]
            outcome = await deliver_one(synth_text, evt)
            return False if retry_needed else outcome
        # Check the entire group before claiming ANY row: an unavailable sibling must
        # not exhaust its budget just because the primary has a usable route.
        for evt, _text in deliverable:
            if await self._completion_delivery_ready(evt) is False:
                return False
        primary_evt, primary_text = deliverable[0]
        claimed = await claim_siblings_off_loop(deliverable[1:], f"gateway-batch:{id(self)}")
        blocks = [primary_text]
        siblings: list[tuple[dict, str]] = []
        unclaimed: list[dict] = []
        for evt, synth_text, claim_id in claimed:
            if claim_id is None:
                # Another consumer owns this row, or its ownership is unproven: keep it out of our text so
                # it is never double-injected. It stays in a failed batch's retry payload only.
                unclaimed.append(evt)
                continue
            siblings.append((evt, claim_id))
            blocks.append(synth_text)
        if not siblings:
            outcome = await deliver_one(primary_text, primary_evt)
            if outcome is not False:
                for evt in unclaimed:
                    forget(evt)
            return False if retry_needed or group else outcome
        from tools.process_registry_notifications import PROCESS_NOTICE_OPEN
        header = (
            f"{PROCESS_NOTICE_OPEN}{len(blocks)} background subagent delegations "
            "completed for this session. Treat these results as one "
            "completion batch and send at most one consolidated user-facing "
            "response. If a result does not change the current conclusion, absorb it silently.]"
        )
        consolidated = "\n\n".join([header, *blocks])
        admission_evt = primary_evt
        if getattr(self, 'session_authority', None) is not None:
            from gateway.session_automation import producer_identity
            admission_evt = dict(primary_evt, _automation_identities=[
                producer_identity(self, event) for event in [primary_evt, *(e for e, _ in siblings)]])
        delivered = await self._deliver_completion_with_sibling_cleanup(consolidated, admission_evt, siblings)
        if delivered is not False:
            forget(primary_evt)
            for evt in unclaimed:
                forget(evt)
            if delivered is True:
                for evt, _claim_id in siblings:
                    forget(evt)
            # A dropped/owned primary refunded its unadmitted siblings in the delivery finally.
            # The watcher requeues only the remaining group, never an already-settled sibling.
        return False if group else delivered


    def _restore_secondary_completion_ledgers(self, profile_homes) -> None:
        """Re-queue undelivered async completions from every SECONDARY profile's ledger. The process
        registry's ``restore_completions()`` covers only the launch profile's ``state.db``; a
        secondary's rows would otherwise never be replayed after a restart."""
        from tools.async_delegation import restore_undelivered_completions
        from tools.process_registry import process_registry as _pr
        self._each_secondary_ledger(profile_homes, lambda: restore_undelivered_completions(_pr.completion_queue),
                                    "Restored")


    def _sweep_orphaned_completion_ledgers(self) -> None:
        """Offer completions whose owner process died while this gateway runs (#97202): the launch
        ledger in the launch scope, each served secondary under its own. Startup replay only covers
        owners that were already gone when the gateway started."""
        from gateway.run_notifications import _log_suppressed, logging
        from tools.async_delegation import sweep_orphaned_completions
        from tools.process_registry import process_registry as _pr
        sweep = lambda: sweep_orphaned_completions(_pr.completion_queue)
        with _log_suppressed(logging.DEBUG, "Orphaned async completion sweep failed: %s"):
            if count := sweep():
                logger.info("Re-offered %d orphaned async completion(s)", count)
        self._each_secondary_ledger((getattr(self, "_served_profile_homes", None) or {}).items(), sweep,
                                    "Re-offered orphaned")


    def _each_secondary_ledger(self, profile_homes, fn, verb: str) -> None:
        """Run ``fn`` (returns a completion count) once per SECONDARY profile, bound to that profile."""
        from gateway.run_notifications import (
            Path,
        )
        from gateway.run import _profile_runtime_scope
        primary = getattr(self, "_primary_profile_name", None)
        for profile_name, profile_home in profile_homes:
            if profile_name == primary:
                continue
            try:
                with _profile_runtime_scope(Path(profile_home), {}):
                    count = fn()
            except Exception:
                logger.warning("Could not replay async completions for profile %r", profile_name, exc_info=True)
                continue
            if count:
                logger.info("%s %d undelivered async completion(s) for profile %r", verb, count, profile_name)


    def _collect_delegation_retry_actions(self) -> list:
        """Claim due retry notices / terminal settlements from every served profile's ledger (atomic claims)."""
        from gateway.run_notifications import Path, _log_suppressed, logging
        from tools.delegation_resume import sweep_retries
        from hermes_cli.profiles import get_active_profile_name
        actions = []
        with _log_suppressed(logging.WARNING, "Delegation retry sweep failed: %s"):
            primary = get_active_profile_name() or "default"
            actions += [{**a, "profile": primary} for a in sweep_retries()]
        profile_homes = (getattr(self, "_served_profile_homes", None) or {}).items()

        def _one(name):
            found = sweep_retries()
            actions.extend({**a, "profile": name} for a in found)
            return len(found)

        from gateway.run import _profile_runtime_scope
        for profile_name, profile_home in profile_homes:
            if profile_name in {primary, getattr(self, "_primary_profile_name", None)}:
                continue
            with _log_suppressed(logging.WARNING, "Delegation retry sweep failed: %s"):
                with _profile_runtime_scope(Path(profile_home), {}):
                    _one(profile_name)
        return actions


    async def _deliver_delegation_retry_action(self, action: dict) -> None:
        """Settle one claimed retry action. A notice wakes the parent to dispatch the retry. Routine
        terminal actions settle quietly; only a completed result whose parent session is gone is sent
        straight to the user's chat, after the live authorization gate."""
        from gateway.run_notifications import (
            asyncio,
        )
        from tools import delegation_resume as dr
        delegation_id, claim = action["delegation_id"], action["claim"]
        async with self._completion_event_scope(action):
            if not await asyncio.to_thread(dr.retry_action_is_current, action):
                return  # /stop invalidated this collected action before delivery
            if action["kind"] == "terminal":
                from gateway.run import _redact_gateway_user_facing_secrets

                parent_session_id = str(action.get("parent_session_id") or "").strip()
                if not action.get("text"):
                    # Routine stops are durable ledger state, not user notifications.
                    await asyncio.to_thread(dr.mark_terminal_reported, delegation_id, claim)
                    return
                # A completed result is user-visible only when no parent conversation can receive it.
                verdict = (await self._classify_completion_target(parent_session_id, settle_ended_tip=True)
                           if parent_session_id else "terminal")
                if verdict != "terminal":
                    settle = dr.mark_terminal_reported if verdict == "deliver" else dr.release_terminal_report
                    await asyncio.to_thread(settle, delegation_id, claim)
                    return
                sent = suppressed = False
                try:
                    source, authorized = await self._authorized_completion_notice_target(action)
                    suppressed = authorized is None
                    if source is not None and authorized is True:
                        # Scrub before any display cut; failure must leave the report owed, never send raw text.
                        line = _redact_gateway_user_facing_secrets(action["text"])
                        line = " ".join(line.split())[:360]
                        sent = await self._deliver_platform_notice(source, line)
                except Exception:
                    logger.warning("Delegation %s: undeliverable-result notice send failed", delegation_id, exc_info=True)
                settle = (dr.suppress_terminal_report if suppressed else
                          dr.mark_terminal_reported if sent else dr.release_terminal_report)
                await asyncio.to_thread(settle, delegation_id, claim)
                if sent:
                    logger.info("Delegation %s: reported undeliverable result to the user", delegation_id)
                return
            parent_session_id = str(action.get("parent_session_id") or "").strip()
            if parent_session_id and await self._classify_completion_target(parent_session_id) == "terminal":
                # The owning conversation is gone (/new, closed): nobody can dispatch; settle it terminal.
                await asyncio.to_thread(dr.mark_retry_terminal, delegation_id, claim,
                                        "its conversation ended before the retry could run")
                return
            accepted = suppressed = False
            try:
                # Internal wakes bypass the inbound authorization gate, so re-check it live here.
                _, authorized = await self._authorized_completion_notice_target(action, check_delivery_ready=True)
                suppressed = authorized is None
                if authorized is True:
                    accepted = await self._inject_watch_notification(
                        action["text"], {**action, "type": "delegation_retry", "retry_notice_claim": claim}) is True
            except Exception:
                logger.debug("Delegation %s: retry notice injection failed", delegation_id, exc_info=True)
            settle = (dr.suppress_retry_notice if suppressed else
                      dr.mark_notice_accepted if accepted else dr.release_notice)
            await asyncio.to_thread(settle, delegation_id, claim)


    async def _drive_delegation_retries(self) -> None:
        from gateway.run_notifications import (
            asyncio,
        )
        for action in await asyncio.to_thread(self._collect_delegation_retry_actions):
            try:
                await self._deliver_delegation_retry_action(action)
            except Exception:
                logger.warning("Delegation retry action failed for %s", action.get("delegation_id"), exc_info=True)


    async def _authorized_completion_notice_target(self, evt: dict, *, check_delivery_ready: bool = False):
        """Shared boot/terminal route and live authorization gate: None means terminal suppression;
        False means a disconnected transport (still owed). Terminal reports can outlive the parent session."""
        from gateway.run_notifications import (
            asyncio,
        )
        source = await asyncio.to_thread(self._build_process_event_source, evt)
        if source is not None:
            platform = source.platform.value if hasattr(source.platform, "value") else str(source.platform)
            if self._resolve_injection_adapter(platform, source) is None:
                return source, False
        if check_delivery_ready:
            readiness = await self._completion_delivery_ready(evt)
            if readiness is None:
                logger.warning("Dropping %s %s: no messaging route or raw API session.",
                               evt.get("type"), evt.get("delegation_id"))
                return source, None
            if readiness is False:
                return source, False
        if source is not None:
            try:
                authorized = self._is_user_authorized_for_source(source)
            except Exception:
                logger.warning("Could not authorize completion notice target; suppressing notice", exc_info=True)
                authorized = False
            if not authorized:
                logger.warning("Suppressing completion notice to an unauthorized target")
                return source, None
        return source, True


    async def _deliver_auto_resume_notice(self, evt: dict) -> Optional[bool]:
        """Deliver one boot recovery notice without claiming explicit resume or spawning a child."""
        from gateway.run_notifications import asyncio, claim_off_loop
        async with self._completion_event_scope(evt):
            parent_session_id = str(evt.get("parent_session_id") or "").strip()
            from tools.delegation_resume import (
                claim_auto_resume_trigger, complete_auto_resume_trigger, inspect_resumable,
                release_auto_resume_trigger,
            )

            delegation_id = str(evt.get("delegation_id") or "")

            def _claim_and_consume() -> None:
                record, reason = claim_auto_resume_trigger(delegation_id)
                if reason is None and record is not None:
                    complete_auto_resume_trigger(delegation_id, str(record.get("auto_resume_claim") or ""))

            async def _suppress_claimed_notice() -> bool:
                """Terminally consume a notice that must not be retried (claim and consume in one hop)."""
                await asyncio.to_thread(_claim_and_consume)
                return True

            if parent_session_id:
                verdict = await self._classify_completion_target(parent_session_id)
                if verdict == "terminal":
                    # Closed owners are terminal even if their transport is disconnected.
                    return await _suppress_claimed_notice()
                if verdict != "deliver":
                    return False
            _, authorized = await self._authorized_completion_notice_target(evt, check_delivery_ready=True)
            if authorized is False:
                return False
            if authorized is None:
                return await _suppress_claimed_notice()
            def _release_abandoned(claimed) -> None:
                record, reason = claimed
                if reason is None and record is not None:
                    release_auto_resume_trigger(delegation_id, str(record.get("auto_resume_claim") or ""))

            record, reason = await claim_off_loop(lambda: claim_auto_resume_trigger(delegation_id),
                                                  _release_abandoned)
            if reason is not None or record is None:
                # Includes an explicit resume claim racing this notice, or another gateway
                # already owning/finishing the boot notice.
                return True
            claim_id = str(record.get("auto_resume_claim") or "")
            accepted = False
            try:
                # Cheap guard for a row resumed or pruned between the claim above and
                # injection. It narrows that window but cannot close it; a notice that
                # still slips through is refused safely by claim_resume. Inside the try so
                # the finally releases the claim on this path and on cancellation alike.
                _, eligibility_reason = await asyncio.to_thread(inspect_resumable, delegation_id)
                if eligibility_reason is not None:
                    return True
                injected = await self._inject_watch_notification(
                    str(evt.get("text") or ""), evt, raise_not_accepted=True,
                )
                accepted = injected is True
                return injected
            except Exception:
                logger.debug("Boot auto-resume notice injection failed", exc_info=True)
                return False
            finally:
                settle = complete_auto_resume_trigger if accepted else release_auto_resume_trigger
                await asyncio.to_thread(settle, delegation_id, claim_id)


    async def _async_delegation_watcher(self, interval: float = 2.0) -> None:
        """Drain async completions and pattern notifications even while sessions are idle.

        Background subagents and process pattern events have no per-process notification
        consumer; both must progress without a later foreground turn.
        """
        from gateway.run_notifications import _log_suppressed, asyncio, logging, time
        await asyncio.sleep(3)  # let platforms finish connecting
        from tools.async_delegation import ORPHAN_SWEEP_INTERVAL_S
        from tools.process_registry import process_registry as _pr
        last_orphan_sweep = None
        while self._running:
            with _log_suppressed(logging.DEBUG, "Async delegation watcher error: %s"):
                # Completions whose owner process died while this gateway runs (#97202).
                if last_orphan_sweep is None or time.monotonic() - last_orphan_sweep >= ORPHAN_SWEEP_INTERVAL_S:
                    last_orphan_sweep = time.monotonic()
                    await asyncio.to_thread(self._sweep_orphaned_completion_ledgers)
                    # Durable delegation retries share the cadence: due notices, re-prompts and
                    # terminal settlements are claimed atomically from state.db, so restarts lose nothing.
                    await self._drive_delegation_retries()
                # Pattern events also need an idle consumer; foreground turns are optional.
                await self._drain_watch_notifications(_pr.completion_queue)
                # Process completions remain owned by their per-process watchers.
                requeue = []
                async_events = []
                auto_resume_events = []
                while not _pr.completion_queue.empty():
                    try:
                        evt = _pr.completion_queue.get_nowait()
                    except Exception:
                        break
                    if evt.get("type") == "async_delegation":
                        async_events.append(evt)
                    elif evt.get("type") == "delegation_auto_resume":
                        auto_resume_events.append(evt)
                    else:
                        requeue.append(evt)
                for evt in requeue:
                    _pr.completion_queue.put(evt)
                # A fan-out finishing together yields N completions for one session; group by full route +
                # parent session so each group becomes ONE consolidated turn.
                # A same-tick drain often carries several completions for the SAME originating session (a
                # fan-out of background subagents finishing together). Events for different sessions never
                # coalesce. See #70300.
                groups: dict[tuple[str, ...], list[dict]] = {}
                for evt in async_events:
                    self._enrich_async_delegation_routing(evt)
                    groups.setdefault(self._event_route_key(evt, self._ASYNC_GROUP_KEY_FIELDS), []).append(evt)
                for group in groups.values():
                    if any(evt.get("_watch_next_retry_at", 0) > time.monotonic() for evt in group):
                        for evt in group:
                            _pr.completion_queue.put(evt)
                        continue
                    try:
                        delivered = await self._enqueue_async_delegation_group(group)
                        if delivered is False:
                            self._back_off_refused_completions(group)
                            for evt in group:
                                _pr.completion_queue.put(evt)
                    except Exception as e:
                        for evt in group:
                            _pr.completion_queue.put(evt)
                        logger.error("Async delegation injection error: %s", e)
                for evt in auto_resume_events:
                    try:
                        delivered = await self._deliver_auto_resume_notice(evt)
                        if delivered is False:
                            _pr.completion_queue.put(evt)
                    except Exception:
                        _pr.completion_queue.put(evt)
                        logger.debug("Boot auto-resume notice error", exc_info=True)
            await asyncio.sleep(interval)
