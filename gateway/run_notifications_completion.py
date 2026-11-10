"""Completion claims, batched delivery and shutdown spool preservation for GatewayRunner."""

from __future__ import annotations

import logging
import asyncio
import contextlib
import dataclasses
from typing import Optional


logger = logging.getLogger('gateway.run')


class GatewayNotificationsMixin:
    @dataclasses.dataclass
    class _CompletionClaim:
        """Pre-flight outcome for one completion delivery."""

        delegation_id: str = ""
        claim_id: str = ""
        proceed: bool = True
        early_result: Optional[bool] = None
        settled: bool = False
        owner_session_id: str = ""  # owning session the pre-flight proved for the completion's pin
        proven_session_id: str = ""  # session the pre-flight proved delivery reaches (a compression tip included)
        defer: bool = False  # unadmitted retry: settle without spending a delivery attempt


    def _completion_held_by_stop(self, evt: dict) -> bool:
        """True while the event's session is stopped (``/stop``) and the user has not sent a turn."""
        from gateway.run_notifications import suppress
        metadata_key = ""
        with suppress(Exception):
            metadata_key = str((evt.get("metadata") or {}).get("gateway_session_key") or "")
        return self._user_stop_latched(str(evt.get("session_key") or "").strip(), metadata_key.strip())


    @staticmethod
    def _completion_delivery_identity(evt: dict) -> Optional[tuple[str, str, object]]:
        """Return a producer-stable identity when one is available.

        Delegation UUIDs identify one producer completion. Process session IDs include the
        persisted spawn epoch so a reused ID is a distinct incarnation; legacy events without
        ``started_at`` are delivered undeduplicated rather than risk suppressing a real completion.
        """
        evt_type = str(evt.get("type") or "")
        if evt_type in {"delegation_retry", "delegation_auto_resume"}:
            producer_id = str(evt.get("delegation_id") or "").strip()
            claim = (str(evt.get("retry_notice_claim") or "").strip()
                     if evt_type == "delegation_retry" else "boot")
            if not producer_id or not claim:
                return None
            return (evt_type, producer_id, claim)
        if evt_type == "async_delegation":
            producer_id = str(evt.get("delegation_id") or "")
            if not producer_id:
                return None
            if evt.get("task_failure_notice"):
                # An interim per-task notice is its own producer event: it must not mark the
                # batch's final result as already delivered, nor a sibling's notice.
                task_idx = ((evt.get("results") or [{}])[0] or {}).get("task_index", "")
                return (evt_type, producer_id, f"task_failure:{task_idx}")
            return (evt_type, producer_id, "")
        # A liveness tick is one watch event like a match: without an identity the session authority
        # refuses it and the drain requeues it forever.
        if evt_type in {"watch_match", "watch_disabled", "heartbeat"} and evt.get("event_id"):
            return (evt_type, str(evt["session_id"]), str(evt["event_id"]))
        if evt_type == "completion":
            producer_id = str(evt.get("session_id") or "")
            started_at = evt.get("started_at")
            if producer_id and started_at is not None:
                return (evt_type, producer_id, started_at)
        return None


    def _mark_completions_delivered_locked(self, identities) -> None:
        """Move identities inflight -> delivered and trim retention. Caller holds ``_completion_delivery_lock``."""
        for identity in identities:
            self._completion_deliveries_inflight.discard(identity)
            self._completion_deliveries_delivered[identity] = None
        while len(self._completion_deliveries_delivered) > self._completion_delivery_retention:
            self._completion_deliveries_delivered.popitem(last=False)


    def _completion_identity_state(self, identity, *, claim: bool = False) -> str | None:
        """``"delivered"`` (admitted earlier this gateway lifecycle), ``"inflight"`` (another delivery
        holds it), else ``None``. With ``claim`` an unseen identity is atomically marked inflight under
        the same lock hold and ``"claimed"`` is returned, so two callers never both own it."""
        with self._completion_delivery_lock:
            if identity in self._completion_deliveries_delivered:
                return "delivered"
            if identity in self._completion_deliveries_inflight:
                return "inflight"
            if claim:
                self._completion_deliveries_inflight.add(identity)
            return "claimed" if claim else None


    async def _settle_durable_claims(self, operations, *, on_settled=None) -> bool:
        from gateway.run_notifications import settle_durable_claims
        return await settle_durable_claims(operations, self._settle_durable_claim, on_settled=on_settled)


    async def _drop_completion_claim(self, claim, reason: str) -> None:
        # Mark FIRST: cancellation must not release a claim whose shielded drop is still running.
        claim.proceed, claim.settled = False, True
        if await self._settle_durable_claims([("drop", claim.delegation_id, claim.claim_id)]) is False:
            claim.settled, claim.defer, claim.early_result = False, True, False
        else:
            logger.warning("Dropping async delegation %s: %s (result remains queryable).",
                           claim.delegation_id or "<legacy>", reason)


    async def _completion_delivery_ready(self, evt: dict) -> Optional[bool]:
        """True: routable, False: retry, None: no route exists to recover.

        Unavailable owners/transports must not spend a durable delivery attempt.
        """
        from gateway.run_notifications import (
            Platform, _held_reason, _raw_process_event_session_id, asyncio,
        )
        from gateway.wake import adapter_supports_push

        parent_session_id = str(evt.get("parent_session_id") or "").strip()
        if parent_session_id:
            verdict = await self._classify_completion_target(parent_session_id, str(evt.get("session_key") or ""))
            if verdict != "deliver":
                # Definitively closed targets still need the normal terminal disposition.
                return verdict == "terminal"
        source = await asyncio.to_thread(self._build_process_event_source, evt)
        if source is not None:
            platform = source.platform.value if hasattr(source.platform, "value") else str(source.platform)
            adapter = self._resolve_injection_adapter(platform, source)
        else:
            raw_sid = _raw_process_event_session_id(evt)
            profile = str(evt.get("profile") or "").strip() or None
            adapters = self._adapters_for_profile(profile)
            adapter = adapters.get(Platform.API_SERVER) if raw_sid else None
            if adapter is not None and adapter_supports_push(adapter):
                return False
            if not raw_sid and not any(str(evt.get(key) or "").strip() for key in (
                "session_key", "parent_session_id", "platform", "chat_id", "user_id",
            )):
                return None
        warned = self.__dict__.setdefault("_completion_no_transport_warned", set())
        key = (getattr(source, "profile", None), platform) if source is not None else None
        if adapter is None:
            # The watcher requeues silently every tick; say once per outage which lane has no transport.
            if key is not None and key not in warned:
                warned.add(key)
                logger.warning("Async completion held: %s", _held_reason(
                    source, platform, getattr(getattr(self, "config", None), "multiplex_profiles", False)))
            return False
        warned.discard(key)
        if not adapter_supports_push(adapter):
            ensure = getattr(adapter, "_ensure_session_db", None)
            try:
                if not callable(ensure) or await asyncio.to_thread(ensure) is None:
                    return False
            except Exception:
                logger.debug("Async-completion delivery DB unavailable", exc_info=True)
                return False
        return True


    async def _preflight_completion_delivery(self, evt: dict, claim=None) -> _CompletionClaim:
        """Claim the durable row (async delegations) and verify the target before adapter acceptance.

        Adapter acceptance is not proof of delivery: the inner resolver can still fail closed inside
        the pipeline after acceptance, falsely acking the durable row. Verifying first gives drops an
        honest durable disposition. ``claim`` is the caller's record, filled in place so the caller's
        cleanup sees a claim taken before a cancellation interrupts the pre-flight.
        """
        from gateway.run_notifications import (
            claim_off_loop,
        )
        claim = self._CompletionClaim() if claim is None else claim
        evt_type = evt.get("type")
        readiness = await self._completion_delivery_ready(evt) if evt_type == "async_delegation" else True
        if readiness is False:
            claim.proceed, claim.early_result = False, False
            return claim
        terminal_reason = "no messaging route or raw API session" if readiness is None else ""
        # Every tagged async event owns its delivery identity. Outbox events (interim notices and
        # terminal fallbacks) must claim that exact event row; legacy untagged finals retain their
        # delegation lifecycle-row claim for compatibility. An untagged legacy interim notice returns
        # an empty claim and remains process-local, as it predates durable outbox delivery.
        if evt_type == "async_delegation":
            claim.delegation_id = str(evt.get("delegation_id") or "")
            if claim.delegation_id:
                try:
                    from tools.async_delegation import claim_event_delivery, defer_completion_delivery
                    claim_id = f"gateway:{id(self)}:{__import__('uuid').uuid4().hex}"
                    def _defer_claim(token):
                        if token:
                            defer_completion_delivery(claim.delegation_id, token)
                    claimed = await claim_off_loop(
                        lambda: claim_event_delivery(evt, claim_id),
                        _defer_claim,
                    )
                    if claimed is None:
                        claim.proceed = False
                        return claim
                    # Empty tokens are the compatibility result for legacy in-memory notices; only
                    # a real claim needs durable settlement in the caller's finally block.
                    claim.claim_id = claimed or ""
                except Exception as exc:
                    logger.warning("Could not claim durable async completion %s: %s", claim.delegation_id, exc)
                    claim.proceed, claim.early_result = False, False
                    return claim
        elif evt_type != "completion":
            return claim
        # Background completions carry only session_key, so after /new the OLD session's notification
        # would land in the NEW one. Stamped events get the async-delegation pre-flight; unstamped deliver.
        if terminal_reason:
            await self._drop_completion_claim(claim, terminal_reason)
            return claim
        parent_session_id = str(evt.get("parent_session_id") or "").strip()
        if not parent_session_id:
            return claim
        # Pre-flight (#65838-class): adapter acceptance is NOT proof of delivery — the inner #55578 resolver
        # can still fail closed inside the message pipeline AFTER the adapter accepted, which would falsely
        # acknowledge the durable row as delivered. Verify the target here, before acceptance, and give
        # drops an honest durable disposition.
        verdict, claim.owner_session_id, claim.proven_session_id = await self._classify_completion_owner(
            parent_session_id, str(evt.get("session_key") or ""))
        if verdict == "terminal":
            if evt_type == "async_delegation":
                await self._drop_completion_claim(claim, f"permanently-gone session {parent_session_id}")
            else:
                logger.warning(
                    "Background process %s completion targets "
                    "permanently-gone session %s (user boundary such as "
                    "/new); dropping notification (output remains available via process(action='log')).",
                    evt.get("session_id") or "<unknown>", parent_session_id,
                )
            claim.proceed = False
        elif verdict == "retry":
            # Transient uncertainty before admission: re-poll without spending a delivery attempt.
            claim.proceed, claim.early_result, claim.defer = False, False, True
        return claim


    @contextlib.asynccontextmanager
    async def _completion_event_scope(self, evt: dict):
        # Scope setup reads the routing store too; entering the context itself must
        # stay on-loop so the target profile's contextvars cover the awaited body.
        from gateway.run_notifications import asyncio
        source = await asyncio.to_thread(self._build_process_event_source, evt)
        async with self._completion_event_context(evt, source):
            yield


    def _completion_event_context(self, evt: dict, source):
        """Profile runtime scope of the session a completion event targets (a no-op context when the
        event is the default profile's or the scope is already installed).

        The pre-flight (``_classify_completion_target`` → ``_session_db``) and every durable-ledger op
        (``tools.async_delegation`` → ``get_hermes_home()/state.db``) resolve from the ambient scope.
        The supervised ``_async_delegation_watcher`` and startup-recovered process watchers run under
        the ROOT scope, so a secondary profile's completion was looked up in the DEFAULT profile's
        state.db — classified ``terminal`` and dropped, its ledger row stranded ``pending`` forever."""
        from gateway.run_notifications import _raw_process_event_session_id, contextlib
        from gateway.run import _async_profile_runtime_scope
        from hermes_constants import get_hermes_home_override
        profile_name = getattr(source, "profile", None) if source is not None else None
        profile_name = str(profile_name or evt.get("profile") or "").strip() or None
        if source is None and profile_name and profile_name != "default":
            try:
                from hermes_cli.profiles import get_profile_dir, profile_exists
                if profile_exists(profile_name):
                    profile_home = get_profile_dir(profile_name)
                else:
                    logger.warning("Completion event references unknown profile %r; leaving ambient scope", profile_name)
                    return contextlib.nullcontext()
            except Exception:
                logger.warning("Could not resolve completion event profile %r", profile_name, exc_info=True)
                return contextlib.nullcontext()
        elif source is None or not profile_name:
            raw_sid = _raw_process_event_session_id(evt)
            if raw_sid and getattr(getattr(self, "config", None), "multiplex_profiles", False):
                return self._served_api_server_event_scope(evt, raw_sid)
            from tui_gateway.launch_profile_policy import async_launch_profile_scope_if_multiplexed
            return async_launch_profile_scope_if_multiplexed()
        else:
            profile_home = self._resolve_profile_home_for_source(source)
        if get_hermes_home_override() == str(profile_home):
            return contextlib.nullcontext()  # already inside this profile's scope
        return _async_profile_runtime_scope(profile_home)


    @contextlib.asynccontextmanager
    async def _served_api_server_event_scope(self, evt: dict, raw_sid: str):
        """Scope of the served profile whose own store holds a raw api_server session (its events name no
        profile), else the launch profile's, as for any unrouted event."""
        from gateway.run_notifications import Platform, SessionSource, asyncio
        from gateway.run import _async_profile_runtime_scope
        from tui_gateway.launch_profile_policy import async_launch_profile_scope_if_multiplexed
        served = await asyncio.to_thread(self._served_api_server_wake_profile, evt, raw_sid)
        if served:
            source = SessionSource(platform=Platform.API_SERVER, chat_id=raw_sid, profile=served)
            scope = _async_profile_runtime_scope(self._resolve_profile_home_for_source(source))
        else:
            scope = async_launch_profile_scope_if_multiplexed()
        async with scope:
            yield


    async def _deliver_completion_notification(
        self, synth_text: str, evt: dict, *, sibling_claims=(), cleanup_state=None,
    ) -> Optional[bool]:
        """Acknowledge one admitted batch, refund refusals, or release failed deliveries.

        True means adapter admission, not model execution; None means deduplicated or
        terminal. False remains retryable. Claims are settled together for every sibling.
        """
        async with self._completion_event_scope(evt):
            return await self._deliver_completion_notification_scoped(
                synth_text, evt, sibling_claims=sibling_claims, cleanup_state=cleanup_state)


    async def _deliver_completion_notification_scoped(
        self, synth_text: str, evt: dict, *, sibling_claims=(), cleanup_state=None,
    ) -> Optional[bool]:
        from gateway.run_notifications import asyncio
        from gateway.session_local_recovery import restore_completion_route
        from gateway.wake import WakeNotAccepted
        from tools.process_registry_notifications import should_surface_notification
        if not should_surface_notification(evt):
            return None  # Suppressed noise is terminal, not a consumed process result.
        if cleanup_state is not None:
            # Transfer responsibility synchronously, before the first await. If cancellation is
            # delivered before this coroutine starts, the batching caller still owns the claims.
            cleanup_state["owner"] = "delivery"
        await restore_completion_route(self, str(evt.get("session_key") or ""))
        if self._completion_held_by_stop(evt):
            # Before the primary's durable claim, so holding spends no delivery attempt; refund any
            # sibling the batch already claimed so it is not stranded until its lease expires.
            await self._settle_durable_claims(
                [("defer", sibling["delegation_id"], claim_id) for sibling, claim_id in sibling_claims])
            return False
        from gateway.session_detached import follow_detached_pin
        evt = await follow_detached_pin(self, evt)
        identity = self._completion_delivery_identity(evt)
        claim = self._CompletionClaim()
        accepted = identity_claimed = refused = cancelled = False
        ack_claims: list[tuple[str, str]] = []
        try:
            claim = await self._preflight_completion_delivery(evt, claim)
            if not claim.proceed:
                return claim.early_result
            if getattr(self, 'session_authority', None) is not None:
                from gateway.session_automation import completion_admission
                if await asyncio.to_thread(completion_admission, self, evt) is not None:
                    accepted = True  # Lost ACK: finish durable claims, never infer again.
                    return True
            if identity is not None:
                if self._completion_identity_state(identity, claim=True) != "claimed":
                    return None
                identity_claimed = True
            if claim.owner_session_id:
                # Admission settles the durable claim, so the resolver starts from the owner proven here.
                evt = {**evt, "parent_session_id": claim.owner_session_id,
                       "proven_session_id": claim.proven_session_id}
            # The rows this admission settles: a turn that cannot confirm the owner offers them again.
            completion_ids = [claim.delegation_id] if claim.claim_id else []
            completion_ids += [sibling["delegation_id"] for sibling, claim_id in sibling_claims if claim_id]
            evt = {**evt, "completion_ids": completion_ids}
            # Publish the admission claims before the turn can start; it may resolve ownership while
            # the acknowledgement below is still in flight (see ``_requeue_uncertain_completion``).
            ack_claims = [(claim.delegation_id, claim.claim_id)] if claim.claim_id else []
            ack_claims += [(sibling["delegation_id"], claim_id) for sibling, claim_id in sibling_claims if claim_id]
            with self._completion_delivery_lock:
                self._pending_completion_acks().update(ack_claims)
            injection_result = await self._inject_watch_notification(synth_text, evt, raise_not_accepted=True)
            if injection_result is not True:
                return injection_result
            accepted = True
            # Before the awaited acknowledgement: a turn that requeues meanwhile must find these
            # marks to clear, not have them restored after it ran.
            if identity is not None:
                with self._completion_delivery_lock:
                    self._mark_completions_delivered_locked((identity,))
            if sibling_claims:
                self._record_coalesced_completion_siblings([event for event, _claim_id in sibling_claims])
            return True
        except asyncio.CancelledError:
            # Cancellation is a shutdown/retry boundary, not a failed delivery. Refund every
            # claim taken by this delivery so repeated shutdowns cannot spend sibling attempts.
            cancelled = True
            raise
        except WakeNotAccepted as exc:
            # A proven-permanent refusal spends its durable attempt, so the row converges to
            # ``dropped`` across restarts too (#141153); any other (restart wait, full FIFO, an owner
            # not yet proven) is refunded and never drops a completion the owner will take later.
            refused = not exc.permanent
            return False
        finally:
            if identity_claimed and not accepted:
                with self._completion_delivery_lock:
                    self._completion_deliveries_inflight.discard(identity)
            operations = []
            operation = "complete" if accepted else "defer" if refused or cancelled or claim.defer else "release"
            if not claim.settled:
                operations.append((operation, claim.delegation_id, claim.claim_id))
            # A primary that never proceeds past pre-flight with no result (its claim was lost, or it
            # was dropped) makes the batching caller requeue every sibling unattempted: refund their
            # attempts. Siblings of an attempted delivery keep the ordinary failed-delivery release.
            sibling_operation = operation
            if not accepted and not claim.proceed and claim.early_result is None:
                sibling_operation = "defer"
            operations.extend(
                (sibling_operation, sibling["delegation_id"], claim_id) for sibling, claim_id in sibling_claims
            )
            # Unpublish only once the settle worker has actually finished: cancellation of this
            # coroutine must not orphan an acknowledgement that is still running, or an owner
            # retry meanwhile would lose the claim it needs to reopen the pending row.
            if not ack_claims:
                await self._settle_durable_claims(operations)
            else:
                await self._settle_durable_claims(
                    operations, on_settled=lambda: self._unpublish_completion_acks(ack_claims))


    async def _flush_process_completion_batch(
        self, key: tuple[str, ...], delay: float, release: asyncio.Event,
    ) -> None:
        """Deliver one route's held completions as one turn and resolve every waiter."""
        from gateway.run_notifications import (
            asyncio,
        )
        current_task = asyncio.current_task()
        tasks, releases = self._completion_notification_batch_tasks, self._completion_notification_batch_releases
        entries: list[tuple[str, dict, asyncio.Future]] = []
        claimed_identities: list = []
        delivered: Optional[bool] = False
        try:
            await self._wait_batch_window(delay, release)
            entries = self._completion_notification_batches.pop(key, [])
            # Detach before delivery so a completion arriving mid-flight can schedule the next flush.
            self._detach_batch_flush(tasks, releases, key, current_task, release)
            # The agent may have read a held result with process(wait/log) during the window: that
            # consumed it, so it no longer wakes anyone (the watcher's own pre-enqueue rule).
            from tools.process_registry import process_registry
            consumed = [e for e in entries if process_registry.is_completion_consumed(str(e[1].get("session_id") or ""))]
            if consumed:
                self._settle_batch_waiters(consumed, None)
                entries = [e for e in entries if e not in consumed]
            # Claim every identity, primary included, before the slow delivery: a copy arriving meanwhile
            # is deduplicated, and an already-delivered one (True) never stands in for a fresh sibling.
            for entry in list(entries):
                identity = self._completion_delivery_identity(entry[1])
                if identity is None:
                    continue
                identity_state = self._completion_identity_state(identity, claim=True)
                if identity_state != "claimed":
                    self._settle_batch_waiters([entry], True if identity_state == "delivered" else None)
                    entries.remove(entry)
                else:
                    claimed_identities.append(identity)
            if not entries:
                return
            synth_text = entries[0][0] if len(entries) == 1 else self._format_coalesced_process_completions(entries)
            # None is now a terminal/routeless carrier, not a delivered duplicate: try the next entry.
            delivered = None
            for _text, candidate_evt, _future in entries:
                candidate_identity = self._completion_delivery_identity(candidate_evt)
                if candidate_identity in claimed_identities:
                    # Promoted to carrier: it claims its own identity in the delivery seam.
                    claimed_identities.remove(candidate_identity)
                    with self._completion_delivery_lock:
                        self._completion_deliveries_inflight.discard(candidate_identity)
                delivered = await self._deliver_completion_notification(synth_text, candidate_evt)
                if delivered is not None:
                    break
            if delivered is True:  # only a real adapter admission covers the coalesced siblings
                self._record_coalesced_completion_siblings([evt for _text, evt, _future in entries])
        except asyncio.CancelledError:
            # Shutdown cancellation: recover undetached entries and resolve every waiter as retryable.
            delivered = False
            if not entries:
                entries = self._completion_notification_batches.pop(key, [])
            raise
        except Exception:
            logger.exception("Coalesced process completion delivery failed")
            delivered = False
        finally:
            if claimed_identities and delivered is not True:
                with self._completion_delivery_lock:
                    for identity in claimed_identities:
                        self._completion_deliveries_inflight.discard(identity)
            # A failure while stopping has no watcher retry left: spool it rather than drop it.
            if delivered is False and self._completion_notification_batches_stopping:
                self._preserve_undelivered_batch(entries)
            # Never strand watcher futures: False = watcher retry path; None = ordinary dedupe result.
            self._settle_batch_waiters(entries, delivered)
            self._detach_batch_flush(tasks, releases, key, current_task, release)


    @staticmethod
    def _settle_batch_waiters(entries, result) -> None:
        for _text, _evt, future in entries:
            if not future.done():
                future.set_result(result)


    def _spool_completion_for_restart(self, synth_text: str, evt: dict) -> bool:
        """Last resort while stopping: a process completion has no durable copy and its watcher
        stops retrying, so write its wake straight to the drain spool (replayed on next boot)."""
        from gateway.run_notifications import MessageEvent, MessageType, _mark_internal_notification
        try:
            source = self._build_process_event_source(evt)
            if source is None:
                return False
            if getattr(source, "message_id", None):
                from gateway.session_identity import replace_source
                source = replace_source(source, message_id=None)
            metadata = {"notification_origin": "process_registry_synthetic"}
            session_key = str(evt.get("session_key") or "").strip()
            if session_key.startswith("agent:"):
                metadata["gateway_session_key"] = session_key
            if str(evt.get("parent_session_id") or "").strip():
                metadata["gateway_session_id"] = str(evt["parent_session_id"]).strip()
            event = MessageEvent(text=_mark_internal_notification(synth_text), message_type=MessageType.TEXT,
                                 source=source, internal=True, metadata=metadata)
            return bool(self._preserve_drain_event(self._session_key_for_source(source), event))
        except Exception:
            logger.warning("Could not spool completion %s for restart", evt.get("session_id"), exc_info=True)
            return False


    async def _deliver_completion_while_stopping(self, synth_text: str, evt: dict) -> Optional[bool]:
        """Teardown has started and the watcher gets no further retries: spool the completion, or
        when it has no spool form (raw API-server routes) try direct delivery while adapters live."""
        if self._spool_completion_for_restart(synth_text, evt):
            return True
        try:
            return await self._deliver_completion_notification(synth_text, evt)
        except Exception:
            logger.warning("Completion %s could not be delivered during shutdown", evt.get("session_id"), exc_info=True)
            return False


    def _preserve_undelivered_batch(self, entries) -> None:
        """Stopping: spool each undelivered entry and settle its waiter; True once preserved."""
        for synth_text, evt, future in entries:
            if future.done():
                continue
            preserved = self._spool_completion_for_restart(synth_text, evt)
            if not preserved:
                logger.error("Background completion %s could not be preserved across shutdown",
                             evt.get("session_id"))
            future.set_result(bool(preserved))


    def _preserve_held_process_completion_batches(self) -> None:
        """Synchronously spool held process completions when cancellable teardown was interrupted."""
        self._ensure_completion_batch_state()
        self._completion_notification_batches_stopping = True
        for release in self._completion_notification_batch_releases.values():
            release.set()
        for entries in list(self._completion_notification_batches.values()):
            self._preserve_undelivered_batch(entries)
        self._completion_notification_batches.clear()


    @staticmethod
    def _requeue_completion_events(events: list[dict]) -> None:
        from tools.process_registry import process_registry
        for evt in events:
            process_registry.completion_queue.put(evt)


    async def _cancel_process_completion_batch_tasks(self) -> None:
        """Before adapter teardown: release every held batch for delivery now, then settle the rest.

        Process completions have no durable copy, so a held batch is delivered (into the drain
        spool while stopping) rather than cancelled. Delegation rows stay pending in their ledger
        and are requeued if their flush cannot finish within the bound.
        """
        from gateway.run_notifications import asyncio
        self._ensure_completion_batch_state()
        self._completion_notification_batches_stopping = True
        for releases in (self._completion_notification_batch_releases, self._async_delegation_batch_releases):
            for release in list(releases.values()):
                release.set()
        tasks = {
            task for task in (self._completion_notification_batch_flush_tasks
                              | self._async_delegation_batch_flush_tasks)
            if not task.done()
        }
        if tasks:
            _done, stuck = await asyncio.wait(tasks, timeout=self._COMPLETION_SHUTDOWN_FLUSH_S)
            for task in stuck:
                task.cancel()
            if stuck:
                await asyncio.gather(*stuck, return_exceptions=True)
        # Defensive cleanup for an orphaned queue with no live flush task.
        for entries in self._completion_notification_batches.values():
            self._preserve_undelivered_batch(entries)
        for group in self._async_delegation_batches.values():
            self._requeue_completion_events(group)
        for attr in ("_completion_notification_batches", "_completion_notification_batch_tasks",
                     "_completion_notification_batch_releases", "_completion_notification_batch_flush_tasks",
                     "_async_delegation_batches", "_async_delegation_batch_tasks",
                     "_async_delegation_batch_releases", "_async_delegation_batch_flush_tasks"):
            getattr(self, attr).clear()


    async def _enqueue_process_completion_notification(
        self, synth_text: str, evt: dict, *, session_busy: bool = False,
    ) -> Optional[bool]:
        """Fan in one conversation's completions; hold routine successes per the batch window."""
        from gateway.run_notifications import asyncio
        # Filter each event before it can contaminate (or suppress) a foreground batch.
        # The shared queue may be drained outside the owning profile's runtime scope.
        async with self._completion_event_scope(evt):
            from tools.process_registry_notifications import should_surface_notification
            if not should_surface_notification(evt):
                return None
        self._ensure_completion_batch_state()
        if self._completion_notification_batches_stopping:
            return await self._deliver_completion_while_stopping(synth_text, evt)
        hold = await self._completion_hold_seconds(evt, session_busy=session_busy)
        if self._completion_notification_batches_stopping:
            return await self._deliver_completion_while_stopping(synth_text, evt)
        if hold <= 0:
            self._release_held_completions(str(evt.get("session_key") or ""))
        # A route key alone is not ownership: /new can leave old and new pins
        # in the same queue. Never settle one pin on another pin's acceptance.
        key = self._event_route_key(
            evt, (*self._COMPLETION_BATCH_KEY_FIELDS, "parent_session_id", "origin_session_id"),
        )
        future = asyncio.get_running_loop().create_future()
        self._completion_notification_batches.setdefault(key, []).append((synth_text, evt, future))
        self._schedule_batch_flush(
            self._completion_notification_batch_tasks, self._completion_notification_batch_releases,
            self._completion_notification_batch_flush_tasks, key, hold, self._flush_process_completion_batch,
        )
        return await future
