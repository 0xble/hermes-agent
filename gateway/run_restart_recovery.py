"""Admission, completion and observable outcomes for interrupted gateway turns."""

import asyncio
import logging
import time

from gateway.platforms.event import MessageEvent, MessageType

logger = logging.getLogger("gateway.run")
_NOT_SUPPLIED = object()


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

    def _interruption_note_delivered(self, session_key: str) -> bool:
        return session_key in getattr(self, "_s2_note_delivered_keys", ())

    async def _finish_restart_recovery(self, session_key: str, reason: str) -> None:
        """A failed dispatch must not be mistaken for a completed turn; never replay work here."""
        if not self._interruption_note_delivered(session_key):
            await self._send_interrupted_turn_notes(
                [session_key], reclaim_pending=True, cancel_on_timeout=True,
            )
        outcome = "noted" if self._interruption_note_delivered(session_key) else f"skipped:{reason}"
        self._record_restart_resume_outcome(session_key, outcome)

    async def _run_startup_resume_event(self, adapter, event: MessageEvent, session_key: str) -> None:
        """Wait for the admitted turn, not merely the adapter's quick task-spawn return."""
        from gateway.run import _AGENT_PENDING_SENTINEL

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
                if task is not None:
                    await asyncio.wait({task}, timeout=_STARTUP_RESUME_OPENING_S)
            if task is not None:
                await asyncio.shield(task)
            # An adapter may decline admission without raising (no handler, wrong identity,
            # plugin veto). Inline adapters can demonstrate completion by clearing the marker.
            entry = self.session_store._entries.get(session_key)
            completed_inline = entry is not None and not entry.resume_pending
            if event._gateway_accepted is True or completed_inline:
                outcome = "resumed"
            else:
                outcome = "skipped:not-admitted"
        except asyncio.CancelledError:
            outcome = "skipped:cancelled"
            raise
        except Exception:
            logger.warning("Startup auto-resume failed for %s", session_key, exc_info=True)
            raise
        finally:
            state = self._peek_session_state(session_key)
            if state is not None and state.turn.agent is _AGENT_PENDING_SENTINEL:
                self._release_running_agent_state(session_key)
            if outcome == "resumed":
                self._record_restart_resume_outcome(session_key, outcome)
            elif outcome == "skipped:cancelled":
                # Shutdown will retain the marker for the next boot. Do not attempt network I/O
                # while cancellation is unwinding a gateway that may already have disconnected.
                decided = "noted" if self._interruption_note_delivered(session_key) else outcome
                self._record_restart_resume_outcome(session_key, decided)
            else:
                await self._finish_restart_recovery(session_key, outcome.removeprefix("skipped:"))

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
