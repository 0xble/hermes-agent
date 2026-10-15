"""Admission, completion and observable outcomes for interrupted gateway turns."""

import asyncio
import logging
import time
from contextlib import nullcontext, suppress

from agent.i18n import t
from gateway.platforms.event import MessageEvent, MessageType

logger = logging.getLogger("gateway.run")
_NOT_SUPPLIED = object()
_INPUT_DURABLE_POLL_S = 0.25


class ResumeCandidates(list):
    """Keep the boot breaker decision attached to its recoverable snapshot."""

    breaker_tripped = False


class RestartRecoveryMixin:
    def _record_restart_resume_outcome(self, session_key: str, outcome: str) -> None:
        """Emit one terminal decision per session per boot, including reconnect retries."""
        recorded = getattr(self, "_restart_resume_outcomes", None)
        if recorded is None:
            recorded = self._restart_resume_outcomes = set()
        if session_key not in recorded:
            recorded.add(session_key)
            logger.info("Restart recovery outcome: session=%s outcome=%s", session_key, outcome)

    def _release_restart_followup_key(self, session_key: str) -> None:
        """Allow a later reconnect scan to handle a recovered follow-up after it settles."""
        keys = getattr(self, "_restart_followup_keys", None)
        if isinstance(keys, set):
            keys.discard(session_key)

    @staticmethod
    def _retire_recovered_followup_spool(event) -> None:
        spool = getattr(event, "_hermes_recovery_spool", None)
        if spool is not None:
            spool.unlink(missing_ok=True)

    @staticmethod
    def _recovery_probe_events(event):
        """Yield the recovered event and any merged successor/absorbed events once each."""
        pending = [event]
        seen = set()
        while pending:
            candidate = pending.pop()
            marker = id(candidate)
            if marker in seen:
                continue
            seen.add(marker)
            yield candidate
            successor = getattr(candidate, "_hermes_absorbed_into", None)
            if successor is not None:
                pending.append(successor)
            pending.extend(getattr(candidate, "_hermes_absorbed_events", ()) or ())

    async def _recovered_input_durable(self, event) -> bool:
        """Whether this replayed input or its merged successor has written a durable user row."""
        from gateway.run_pending_recovery import input_already_durable
        recovery_session_id = getattr(event, "_hermes_recovery_session_id", None)
        recovery_spool_ts = getattr(event, "_hermes_recovery_spool_ts", None)
        recovery_id = getattr(event, "_hermes_recovery_id", None)
        for candidate in self._recovery_probe_events(event):
            session_id = getattr(candidate, "_hermes_recovery_session_id", None)
            if session_id is None:
                session_id = recovery_session_id
            spool_ts = getattr(candidate, "_hermes_recovery_spool_ts", None)
            if spool_ts is None:
                spool_ts = recovery_spool_ts
            candidate_ids = {
                candidate_id for candidate_id in (
                    getattr(candidate, "_hermes_recovery_id", None),
                    getattr(candidate, "message_id", None),
                ) if candidate_id is not None
            }
            if not candidate_ids and recovery_id is not None:
                candidate_ids.add(recovery_id)
            for candidate_id in candidate_ids:
                if await asyncio.to_thread(
                    input_already_durable, getattr(self, "session_store", None), session_id,
                    candidate_id, getattr(candidate, "text", None), spool_ts,
                ):
                    return True
        return False

    async def _retire_spool_once_input_durable(self, event) -> None:
        """Retire the replay copy at the turn's durable start, not at adapter admission.

        ``handle_message`` returns once the session task exists, before the turn has leased the
        session or persisted anything, so the spool is the only copy until the turn writes its
        input row (``_persist_turn_start`` stamps the inbound or recovery id before the first model call). After
        that row exists, a restart falls to the turn's ``resume_pending`` marker and boot recovery
        drops the spool instead of replaying a prompt that may have had effects. Legacy payloads
        without a recovery id use the content/timestamp fallback and retire when their turn reports
        a result.
        """
        while not await self._recovered_input_durable(event):
            await asyncio.sleep(_INPUT_DURABLE_POLL_S)
        self._retire_recovered_followup_spool(event)

    def _interruption_note_delivered(self, session_key: str) -> bool:
        return session_key in getattr(self, "_s2_note_delivered_keys", ())

    def _release_startup_preclaim(self, session_key: str) -> None:
        """Drop the boot pre-claim sentinel; a real agent or a successor's claim is left alone."""
        from gateway.run import _AGENT_PENDING_SENTINEL
        state = self._peek_session_state(session_key)
        if state is not None and state.turn.agent is _AGENT_PENDING_SENTINEL:
            self._release_running_agent_state(session_key)

    @staticmethod
    async def _follow_startup_resume_turn(adapter, event, session_key: str, task, *, on_task_done=None) -> None:
        """Await the task that runs *event*. A busy or bounced event is parked and run by a
        successor session task, so follow ``_session_tasks`` until the event reports a result.

        The adapter may consume a recovered event by merging it into an already pending slot. In
        that case the successor owns the result and the parked recovery event is not the object on
        which ``run_turn`` writes ``_agent_turn_succeeded``. Session tasks are tagged with their
        event by the shared background runner; copy a terminal result only when that task consumed
        this event directly or recorded it as an absorbed pending event. Never borrow a result from
        an unrelated predecessor that happened to own the session while this event was parked."""
        seen = set()
        while isinstance(task, asyncio.Task) and task not in seen:
            seen.add(task)
            await asyncio.shield(task)
            if on_task_done is not None:
                on_task_done(task)
            task_event = getattr(task, "_hermes_event", None)
            task_result = getattr(task_event, "_agent_turn_succeeded", None)
            from gateway.platforms.base import _task_event_covers_recovered_event
            if (
                _task_event_covers_recovered_event(task_event, event)
                and isinstance(task_result, bool)
                and not isinstance(getattr(event, "_agent_turn_succeeded", None), bool)
            ):
                event._agent_turn_succeeded = task_result
            if isinstance(getattr(event, "_agent_turn_succeeded", None), bool):
                return
            task = getattr(adapter, "_session_tasks", {}).get(session_key)

    async def _finish_restart_recovery(self, session_key: str, reason: str) -> None:
        """A failed dispatch must not be mistaken for a completed turn; never replay work here."""
        if not self._interruption_note_delivered(session_key):
            await self._send_interrupted_turn_notes(
                [session_key], reclaim_pending=True, cancel_on_timeout=True,
            )
        outcome = "noted" if self._interruption_note_delivered(session_key) else f"skipped:{reason}"
        self._release_restart_followup_key(session_key)
        self._record_restart_resume_outcome(session_key, outcome)

    async def _run_startup_resume_event(self, adapter, event: MessageEvent, session_key: str) -> None:
        """Wait for the admitted turn, not merely the adapter's quick task-spawn return."""
        from gateway.run_startup import _STARTUP_RESUME_CONCURRENCY, _STARTUP_RESUME_OPENING_S
        slots = getattr(self, "_startup_resume_slots", None)
        if slots is None:
            slots = self._startup_resume_slots = asyncio.Semaphore(_STARTUP_RESUME_CONCURRENCY)
        outcome = "skipped:dispatch-failed"
        task = None
        try:
            async with slots:
                await adapter.handle_message(event)
                session_tasks = getattr(adapter, "_session_tasks", {})
                task = session_tasks.get(session_key) if isinstance(session_tasks, dict) else None
                # Hand the pre-claim to the adapter guard before its task reaches the runner: a
                # held sentinel bounces the resume into the pending slot, and every drain of it
                # would bounce again until this coroutine released it.
                self._release_startup_preclaim(session_key)
                if task is not None:
                    await asyncio.wait({task}, timeout=_STARTUP_RESUME_OPENING_S)
            # A busy or bounced event still runs, later, in a successor task: follow it.
            await self._follow_startup_resume_turn(adapter, event, session_key, task)
            # An adapter may decline admission without raising (no handler, wrong identity,
            # plugin veto). Inline adapters can demonstrate completion by clearing the marker.
            store = getattr(self, "session_store", None)
            entry = getattr(store, "_entries", {}).get(session_key)
            completed_inline = entry is not None and not entry.resume_pending
            succeeded = getattr(event, "_agent_turn_succeeded", None)
            outcome = "resumed" if succeeded is True or completed_inline else (
                "skipped:turn-failed" if succeeded is False else "skipped:not-admitted")
        except asyncio.CancelledError:
            outcome = "skipped:cancelled"
            raise
        except Exception:
            logger.warning("Startup auto-resume failed for %s", session_key, exc_info=True)
            raise
        finally:
            self._release_startup_preclaim(session_key)
            getattr(self, "_startup_resume_inflight", set()).discard(session_key)
            if outcome in ("resumed", "skipped:turn-failed"):
                # The turn ran and replied (or reported its failure) itself; no interruption note.
                self._release_restart_followup_key(session_key)
                self._record_restart_resume_outcome(session_key, outcome)
            elif outcome == "skipped:cancelled":
                # Shutdown will retain the marker for the next boot. Do not attempt network I/O
                # while cancellation is unwinding a gateway that may already have disconnected.
                decided = "noted" if self._interruption_note_delivered(session_key) else outcome
                self._release_restart_followup_key(session_key)
                self._record_restart_resume_outcome(session_key, decided)
            else:
                await self._finish_restart_recovery(session_key, outcome.removeprefix("skipped:"))

    async def _finish_recovered_followup(self, event, session_key: str, reason: str) -> None:
        """A future turn can outlive its predecessor's marker; its refusal still needs a note."""
        from gateway.run import _async_profile_runtime_scope, _startup_restore_drain_timeout_secs

        declined = getattr(event, "_hermes_recovered_declined", False) is True
        # A declined input never ran, so the predecessor's interruption note does not cover it.
        noted = False if declined else self._interruption_note_delivered(session_key)
        if not noted:
            try:
                # Restore observers run outside any profile scope; a multiplexed secondary
                # profile's notice must resolve its own locale, metadata and config.
                home = self._resolve_profile_home_for_source(event.source)
                async with _async_profile_runtime_scope(home) if home else nullcontext():
                    send = self._deliver_platform_notice(
                        event.source,
                        t("gateway.restart.followup_declined_notice" if declined
                          else "gateway.restart.followup_recovery_notice"),
                    )
                    timeout = _startup_restore_drain_timeout_secs()
                    noted = await asyncio.wait_for(send, timeout=timeout if timeout > 0 else None)
            except Exception:
                logger.warning("Restored follow-up recovery note failed for %s", session_key, exc_info=True)
        suffix = ":noted" if noted else ":note-unavailable"
        if declined and noted:
            # The user now knows to resend it: keep the bytes for manual recovery, but never
            # replay or re-announce this input on a later boot.
            from gateway.shutdown_flush import quarantine_pending_spool
            spool = getattr(event, "_hermes_recovery_spool", None)
            if spool is not None:
                quarantine_pending_spool(spool, f"declined shutdown follow-up: {reason}")
        if declined:
            # This input is its own recovery item: decide it once per spool, independently of the
            # predecessor turn's outcome for the same session (and never release that turn's claim).
            decided = getattr(self, "_declined_followup_outcomes", None)
            if decided is None:
                decided = self._declined_followup_outcomes = set()
            marker = str(getattr(event, "_hermes_recovery_spool", None) or id(event))
            if marker not in decided:
                decided.add(marker)
                logger.info("Restart recovery outcome: session=%s outcome=%s",
                            session_key, f"skipped:{reason}{suffix}")
            return
        self._release_restart_followup_key(session_key)
        self._record_restart_resume_outcome(session_key, f"skipped:{reason}{suffix}")

    async def _observe_recovered_followup(self, adapter, event, session_key: str, *, task=None) -> None:
        """Observe the actual turn, following a busy adapter's successor rather than its receipt."""
        reason = "not-admitted" if event._gateway_accepted is not True else "accepted-without-turn"
        watcher = asyncio.create_task(self._retire_spool_once_input_durable(event))
        try:
            await self._follow_startup_resume_turn(adapter, event, session_key, task)
            succeeded = getattr(event, "_agent_turn_succeeded", None)
            if isinstance(succeeded, bool):
                # A failed real turn may have tool effects; never replay it just because it did
                # not produce a successful final. Its request remains in the durable transcript.
                self._retire_recovered_followup_spool(event)
                # A real turn replied or reported its own failure; the "still preserved"
                # notice would be false once the spool is gone, so neither path sends it.
                self._release_restart_followup_key(session_key)
                self._record_restart_resume_outcome(
                    session_key, "resumed" if succeeded else "skipped:turn-failed")
                return
        except asyncio.CancelledError:
            self._release_restart_followup_key(session_key)
            self._record_restart_resume_outcome(session_key, "skipped:cancelled")
            raise
        except Exception:
            reason = "turn-failed"
            logger.warning("Restored follow-up failed for %s", session_key, exc_info=True)
        finally:
            watcher.cancel()
        await self._finish_recovered_followup(event, session_key, reason)

    async def _drain_startup_restore_queue(self, keys=None, *, owned_keys=None) -> int:
        """Replay ready inbound, leaving sessions owned by another restore in order."""
        drained = 0
        queue = getattr(self, "_startup_restore_queue", None) or []
        # Topic recovery behind the session key reads state.db: resolve keys off the loop, once per
        # event (by identity; the queue keeps each event alive while it is listed).
        resolved: dict[int, tuple] = {}

        def session_keys(events):
            return [self._session_key_for_source(self._normalize_source_for_session_key(event.source))
                    for event in events]

        async def resolve_keys():
            # Repeat until no await is left between resolution and the scan: inbound queued while a
            # batch resolved is resolved too, so ``ready`` below always has every key.
            while pending := [event for event in queue if resolved.get(id(event), (None,))[0] is not event]:
                for event, key in zip(pending, await asyncio.to_thread(session_keys, pending)):
                    resolved[id(event)] = (event, key)

        def ready(event):
            key = resolved[id(event)][1]
            if keys is not None and key not in keys:
                return False
            # Our own gate permits one claimant; every other owner's gate must be fully open.
            limit = 1 if owned_keys and key in owned_keys else 0
            return getattr(self, "_reconnect_restore_keys", {}).get(key, 0) <= limit

        while True:
            await resolve_keys()
            index = next((i for i, event in enumerate(queue) if ready(event)), None)
            if index is None:
                break
            event = queue.pop(index)
            recovery_key = resolved[id(event)][1]
            if await self._dispatch_startup_restore_event(event, recovery_key):
                drained += 1
        if queue and keys is None:
            logger.warning("Startup-restore drain left %d queued message(s) awaiting a reconnect owner", len(queue))
        return drained

    async def _dispatch_startup_restore_event(self, event, recovery_key: str) -> bool:
        """Separate adapter admission from a recovered follow-up's actual turn completion."""
        recovered = getattr(event, "_hermes_recovery_session_key", None) is not None
        try:
            if getattr(event, "_hermes_recovered_declined", False) is True:
                # Never auto-run an input recovery could not attribute or still trust: send the
                # visible note (retaining the spool if no adapter can carry it yet).
                await self._finish_recovered_followup(
                    event, recovery_key, getattr(event, "_hermes_recovery_decline_reason", "declined"))
                return False
            source = getattr(event, "source", None)
            adapter = self._intake_adapter_for(source)
            if adapter is None and getattr(event, "_hermes_recovered_followup", False):
                adapter = self._delivery_adapter_for(source)
            if adapter is None:
                logger.debug(
                    "Dropping startup-restore queued message: adapter unavailable for %s",
                    getattr(getattr(source, "platform", None), "value", None),
                )
                if recovered:
                    await self._finish_recovered_followup(event, recovery_key, "adapter-offline")
                return False
            # Mark the replay so _handle_message does not re-queue it while the restore gate is closed.
            with suppress(Exception):
                setattr(event, "_hermes_startup_restore_replay", True)
            # A normal return can be an admission refusal. Retire the only
            # durable copy only after this replay acquires an explicit receipt.
            event._gateway_accepted = False
            await adapter.handle_message(event)
            if recovered:
                owner = getattr(adapter, "_session_tasks", {}).get(recovery_key)
                self._retain_background_task(asyncio.create_task(
                    self._observe_recovered_followup(adapter, event, recovery_key, task=owner)))
            if getattr(event, "_gateway_accepted", False) is not True:
                return False
            spool = getattr(event, "_hermes_recovery_spool", None)
            if spool is not None and not recovered:
                spool.unlink(missing_ok=True)
        except Exception:
            # One bad replay must not abort the drain: the remaining queued
            # events still deserve their turn, and a raise here used to skip
            # the gate release in _finish_startup_restore entirely.
            logger.warning("Startup-restore queued replay failed; continuing drain", exc_info=True)
            if recovered:
                await self._finish_recovered_followup(event, recovery_key, "dispatch-failed")
            return False
        return True

    def _resume_owner_authorized(self, session_key: str, source) -> bool | None:
        """True for an authorized owner, False for denial, None when checking failed."""
        try:
            if self._is_user_authorized_for_source(source):
                return True
            logger.warning(
                "Skipping auto-resume for %s: session owner is no "
                "longer authorized under the current allowlist", session_key,
            )
        except Exception as exc:
            logger.warning("Skipping auto-resume for %s: authorization check failed: %s", session_key, exc)
            return None
        return False

    def _auto_resume_ready(self, entry, *, require_adapter=True) -> tuple | None:
        """Shared admission for deferred follow-ups and synthetic resume turns."""
        ready, _reason = self._resume_admission(entry, require_adapter=require_adapter)
        return ready

    def _resume_admission(self, entry, *, require_adapter=True) -> tuple:
        from gateway.run import (
            _auto_continue_freshness_window, _is_fresh_gateway_interruption,
            _resume_pending_marker_timestamp,
        )
        if not getattr(entry, "resume_pending", True):
            return None, "already-completed"
        if getattr(entry, "suspended", False):
            return None, "suspended"
        if getattr(entry, "origin", None) is None:
            return None, "missing-origin"
        marker = _resume_pending_marker_timestamp(entry)
        if not _is_fresh_gateway_interruption(marker, window_secs=_auto_continue_freshness_window()):
            return None, "stale"
        if self._is_session_running(entry.session_key):
            return None, "already-running"
        source = self._restored_source(entry)
        adapter = self._delivery_adapter_for(source)
        authorization = self._resume_owner_authorized(entry.session_key, source)
        if authorization is not True:
            return None, "unauthorized" if authorization is False else "authorization-unavailable"
        if require_adapter and adapter is None:
            return None, "adapter-offline"
        return (adapter, source), None

    def _startup_interrupted_note_candidates(self, candidates) -> list[str]:
        """Admit current-version interruption notes even when the breaker vetoes model work."""
        return [
            entry.session_key for entry in (candidates or [])
            if getattr(entry, "resume_turn_id", None) and self._auto_resume_ready(entry) is not None
        ]

    def _schedule_resume_pending_sessions(self, platform=None, *, restore_tasks=None, restore_keys=None,
                                          candidates=_NOT_SUPPLIED, allow_resume=True) -> int:
        """Claim eligible interrupted sessions before spawning; record every terminal decision."""
        from gateway.run import _AGENT_PENDING_SENTINEL
        candidates = self._resume_pending_candidates(platform) if candidates is _NOT_SUPPLIED else candidates
        if candidates is None:
            return 0
        scheduled = 0
        allow_resume = (allow_resume and not getattr(candidates, "breaker_tripped", False)
                        and not getattr(self, "_restart_resume_breaker_tripped", False))
        for entry in candidates:
            if entry.session_key in getattr(self, "_restart_followup_keys", ()):
                # The restored prompt owns this recovery. Do not admit an empty synthetic turn
                # against it or log the predecessor's decision before its real follow-up finishes.
                continue
            if entry.session_key in getattr(self, "_startup_resume_inflight", ()):
                # Its resume already runs; the pre-claim was handed to the adapter guard, so the
                # running-session check below cannot see it.
                continue
            if platform is not None and getattr(entry.origin, "platform", None) != platform:
                continue
            if not allow_resume:
                outcome = "noted" if self._interruption_note_delivered(entry.session_key) else "skipped:restart-loop-breaker"
                if outcome != "noted" and entry.session_key in getattr(self, "_s2_note_ambiguous_keys", ()):
                    outcome += ":ambiguous-note"
                self._record_restart_resume_outcome(entry.session_key, outcome)
                continue
            ready = self._auto_resume_ready(entry)
            if ready is None:
                _ready, reason = self._resume_admission(entry)
                outcome = "noted" if self._interruption_note_delivered(entry.session_key) else f"skipped:{reason}"
                self._record_restart_resume_outcome(entry.session_key, outcome)
                continue
            adapter, source = ready
            inflight = getattr(self, "_startup_resume_inflight", None)
            if inflight is None:
                inflight = self._startup_resume_inflight = set()
            inflight.add(entry.session_key)
            state = self._session_state(entry.session_key)
            state.turn.agent = _AGENT_PENDING_SENTINEL
            state.turn.started_ts = time.time()
            self._persist_active_agents()
            event = MessageEvent(text="", message_type=MessageType.TEXT, source=source,
                                 message_id=getattr(source, "message_id", None), internal=True)
            resume_marker = getattr(entry, "resume_turn_id", None) or getattr(entry, "resume_marker_token", None)
            if resume_marker:
                # Admission identity names the interruption, not the original Telegram reply anchor.
                event._outbox_admission_id = f"resume:{resume_marker}"
            task = self._retain_background_task(
                asyncio.create_task(self._run_startup_resume_event(adapter, event, entry.session_key))
            )
            if restore_tasks is not None:
                restore_tasks.append(task)
            if restore_keys is not None:
                restore_keys.add(entry.session_key)
            if getattr(self, "_startup_restore_in_progress", False):
                tasks = getattr(self, "_startup_restore_tasks", None)
                if tasks is None:
                    tasks = self._startup_restore_tasks = []
                tasks.append(task)
            scheduled += 1
        if scheduled:
            logger.info("Scheduled auto-resume for %d restart-interrupted session(s)", scheduled)
        return scheduled
