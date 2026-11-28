"""Process notification payloads and terminal-watcher delivery phases.

Inherited methods keep GatewayNotificationsMixin/GatewayRunner call and patch targets.
New phases late-import facade globals so module clock and logger patches still apply.
"""

from __future__ import annotations

import asyncio


class GatewayProcessNotificationsMixin:
    @staticmethod
    def _event_route_key(evt: dict, fields: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(str(evt.get(field) or "") for field in fields)

    @staticmethod
    def _format_coalesced_process_completions(entries: list[tuple[str, dict, asyncio.Future]]) -> str:
        """Build one bounded synthetic event from several redacted completions."""
        from gateway.run import _redact_gateway_user_facing_secrets
        from tools.process_registry_notifications import PROCESS_NOTICE_OPEN, PROCESS_NOTIFICATION_END
        lines = [
            f"{PROCESS_NOTICE_OPEN}{len(entries)} background processes completed for this session.",
            "Treat these results as one completion batch and send at most one "
            "consolidated user-facing response.",
        ]
        shown = entries[:10]
        for _text, evt, _future in shown:
            session_id = str(evt.get("session_id") or "unknown")
            exit_code = evt.get("exit_code")
            reason = str(evt.get("completion_reason") or "exited")
            # Unconditional gateway redaction floor (the producer-seam redactor is configurable). Redact
            # BEFORE slicing: truncating first can leave a credential fragment the patterns miss.
            output = _redact_gateway_user_facing_secrets(str(evt.get("output") or "")).strip()
            if len(output) > 800:
                output = f"[… truncated …]\n{output[-800:]}"
            lines.append(f"\n- {session_id}: exit_code={exit_code}, reason={reason}")
            if output:
                lines.append(output)
        omitted = len(entries) - len(shown)
        if omitted:
            lines.append(
                f"\n- … and {omitted} more completion(s); inspect them with "
                "the process tool if they affect the conclusion."
            )
        lines.append("If a result does not change the current conclusion, absorb it silently.]")
        return "\n".join((*lines, PROCESS_NOTIFICATION_END))

    def _record_coalesced_completion_siblings(self, events: list[dict]) -> None:
        """Extend a successful primary delivery claim to its batched siblings."""
        identities = [i for i in map(self._completion_delivery_identity, events) if i is not None]
        with self._completion_delivery_lock:
            self._mark_completions_delivered_locked(identities)

    @staticmethod
    def _redacted_output_tail(session, limit: int) -> str:
        """Last ``limit`` chars of process output through the secret redactors (unconditional floor)."""
        from gateway.run import _redact_gateway_user_facing_secrets
        from tools.ansi_strip import strip_ansi
        from tools.process_registry import transform_process_output
        new_output = strip_ansi(session.output_buffer[-limit:]) if session.output_buffer else ""
        if new_output:
            from agent.redact import redact_terminal_output
            _command = getattr(session, "command", "") or ""
            new_output = transform_process_output(new_output, command=_command, returncode=session.exit_code,
                                                  task_id=getattr(session, "task_id", "") or "")
            new_output = redact_terminal_output(new_output, _command)
            # redact_terminal_output() is unforced (raw when security.redact_secrets is off); this goes
            # straight to the adapter, so apply the same unconditional floor as agent-notify.
            new_output = _redact_gateway_user_facing_secrets(new_output)
        return new_output

    async def _process_watcher_surface(self, watcher: dict, session, silent: bool, session_id: str) -> bool | None:
        """True renders; False waits for another tick; None ends this watcher."""
        from tools.process_registry import process_registry
        from tools.process_registry_notifications import acknowledge_survivor_notifications, should_surface_notification
        from tools.process_registry_survival import is_restart_safe

        if getattr(session, "_completion_delivered", False):
            return None
        async with self._completion_event_scope(watcher):
            surface = should_surface_notification({
                "owner_task_id": getattr(session, "owner_task_id", ""),
                "task_id": getattr(session, "task_id", ""),
            })
        if not surface:
            # Recheck live ownership next tick: handoff retains notification intent.
            if session.exited:
                if is_restart_safe(session):
                    if process_registry._claim_survivor_delivery(session):
                        process_registry._mark_survivor_delivered(session)
                else:
                    await asyncio.to_thread(acknowledge_survivor_notifications, process_registry,
                                            (({"session_id": session_id}, ""),))
                return None
            return False
        if silent:
            # Still wait for the process to exit so we can log it, but don't push any messages.
            if session.exited:
                if is_restart_safe(session):
                    if process_registry._claim_survivor_delivery(session):
                        process_registry._mark_survivor_delivered(session)
                else:
                    await asyncio.to_thread(acknowledge_survivor_notifications, process_registry,
                                            (({"session_id": session_id}, ""),))
                return None
            return False
        return True

    async def _process_completion_committed(self, completion_evt: dict, session_id: str) -> bool:
        """Recheck a possibly lost admission ACK before falling back to a plain notice."""
        from gateway.run_notifications import logger
        if (getattr(self, 'session_authority', None) is not None
                and self._completion_delivery_identity(completion_evt) is not None):
            from gateway.session_automation import completion_admission
            try:
                # The owning profile's authority, as on the delivery path.
                async with self._completion_event_scope(completion_evt):
                    committed = await asyncio.to_thread(completion_admission, self, completion_evt) is not None
            except Exception:  # health: allow BLE001 -- fail-open recheck boundary logs and falls back to the notice
                # Failing open sends the notice; a dead watcher would send nothing.
                logger.debug("Process watcher: admission recheck failed for %s", session_id,
                             exc_info=True)
                committed = False
            return committed
        return False

    async def _finish_process_text_notification(
        self, session_id: str, session, notify_mode: str, non_success: bool, watcher: dict, *, route: tuple,
    ) -> bool:
        """Settle a plain notice; False retains ownership for the watcher's retry path."""
        from gateway.run_notifications import logger
        from tools.process_registry import process_registry
        from tools.process_registry_notifications import acknowledge_survivor_notifications
        from tools.process_registry_survival import is_restart_safe

        platform_name, chat_id, thread_id = route
        # Text-only notification; skip when already consumed via wait/log (the agent_notify branch
        # FALLS THROUGH here, hence the re-check).
        if process_registry.is_completion_consumed(session_id):
            logger.debug(
                "Process watcher: completion for %s already consumed "
                "via wait/log — skipping raw notification (#65379)", session_id,
            )
            if is_restart_safe(session):
                process_registry._mark_survivor_delivered(session)
            return True
        if notify_mode in {"concise", "all", "result"} or (
            notify_mode == "error" and non_success
        ):
            message_text = self._format_process_final_message(session_id, session, notify_mode)
            from gateway.warning_notifications import present_notification
            sent = True  # policy suppression is deliberate consumption, not transport failure

            async def send_final():
                nonlocal sent
                sent = await self._send_watcher_message(
                    platform_name, chat_id, thread_id, message_text, watcher)

            async with self._completion_event_scope(watcher):
                # Non-zero exit is the automatic diagnostic; a clean completion is the requested result.
                await present_notification(send_final, platform=platform_name,
                                           diagnostic=non_success)
            if sent is False:
                if is_restart_safe(session):
                    process_registry.release_survivor_completion(session_id)
                return False
        # Presented, or deliberately suppressed by the notification mode: either way the
        # claimed survivor completion is settled and must not replay on a successor.
        # The acknowledgement can load retained results, whose ownership check opens state.db.
        await asyncio.to_thread(acknowledge_survivor_notifications, process_registry,
                                (({"session_id": session_id}, ""),))
        return True

    async def _run_process_watcher(self, watcher: dict) -> None:
        """Run :meth:`_watch_process` inside the owning profile's scope, start to finish. A watcher starts
        in an empty context (live-armed from a tool thread, or recovered at startup), and the output it
        prepares runs that profile's own ``transform_terminal_output`` hooks before any delivery.

        A process can outlive its profile route.  That is a terminal per-watcher condition, not a
        failed poll: drop the watcher without retaining it in the live watcher set or retrying it.
        """
        from gateway.profile_routing import ProfileRouteRejected
        live_watchers = getattr(self, "_live_process_watchers", None)
        if live_watchers is None:
            live_watchers = self._live_process_watchers = set()
        watcher_id = watcher.get("session_id") or watcher.get("process_id")
        if watcher_id:
            live_watchers.add(watcher_id)
        try:
            async with self._completion_event_scope(watcher):
                await self._watch_process(watcher)
        except ProfileRouteRejected:
            from gateway.run_notifications import logger
            logger.info("Process watcher %s dropped: profile route is no longer served",
                        watcher.get("session_id", ""))
        finally:
            if watcher_id:
                live_watchers.discard(watcher_id)

    async def _watch_process(self, watcher: dict) -> None:
        """Poll a background process and push updates until it exits. Mode
        (``display.background_process_notifications``): concise (default one-liner; failures append
        the output tail) / all (running updates + final raw) / result (final raw) / error (final raw
        if exit != 0) / off."""
        from gateway.run_notifications import (
            _COMPLETION_INJECTION_RETRY_MAX_DELAY, _MAX_COMPLETION_INJECTION_ATTEMPTS,
            Platform, asyncio, jittered_backoff, logger,
        )
        from tools.process_registry import process_registry
        from tools.process_registry_notifications import acknowledge_survivor_notifications, format_process_notification
        from tools.process_registry_survival import is_restart_safe
        session_id = watcher["session_id"]
        interval = watcher["check_interval"]
        platform_name = watcher.get("platform", "")
        chat_id = watcher.get("chat_id", "")
        thread_id = watcher.get("thread_id", "")
        agent_notify = watcher.get("notify_on_complete", False)
        notify_mode = self._load_background_notifications_mode()  # the owning profile's (scope bound above)
        logger.debug("Process watcher started: %s (every %ss, notify=%s, agent_notify=%s)",
                      session_id, interval, notify_mode, agent_notify)
        # Off hides public receipts, not an explicitly requested messaging wake.
        silent = notify_mode == "off" and (not agent_notify or platform_name == Platform.LOCAL.value)
        last_output_len = 0
        busy_receipt_sent = False
        text_retry_interval = min(max(interval, 1.0), 30.0)
        completion_attempts = 0
        retry_interval = interval
        while True:
            await asyncio.sleep(retry_interval)
            # A finished process falls back to retained results, whose ownership check reads state.db.
            session = await asyncio.to_thread(process_registry.get, session_id)
            if session is None:
                break
            # A retiring topic or a subagent handoff can transfer a live process. Its mutable
            # registry route is authoritative, not this watcher's launch-time snapshot.
            if self._follow_process_route(watcher, session):
                platform_name = watcher.get("platform", "")
                chat_id, thread_id = watcher.get("chat_id", ""), watcher.get("thread_id", "")
            surface = await self._process_watcher_surface(watcher, session, silent, session_id)
            if surface is None:
                break
            if not surface:
                continue
            current_output_len = len(session.output_buffer)
            has_new_output = current_output_len > last_output_len
            last_output_len = current_output_len
            if session.exited:
                from tools.process_registry_notifications import process_exit_unknown
                non_success = session.exit_code != 0 or process_exit_unknown(
                    session.exit_code, getattr(session, "completion_reason", ""),
                    getattr(session, "termination_source", ""))
                # Durable delivery suppresses replay, not a consumer's render/wait/log snapshot.
                # Claim before admission; while a live peer gateway holds the claim, wait so
                # this watcher can reclaim the completion if that peer dies before delivering.
                if is_restart_safe(session) and not process_registry._claim_survivor_delivery(session):
                    if process_registry._survivor_delivery_pending(session):
                        continue
                    break
                # Agent-notify: inject a synthetic message unless the agent already consumed the result via
                # wait/log (poll() is read-only and deliberately does NOT mark consumed).
                if agent_notify and not process_registry.is_completion_consumed(session_id):
                    completion_evt = self._build_process_completion_event(watcher, session, session_id)
                    synth_text = format_process_notification(completion_evt)
                    if not synth_text:
                        await asyncio.to_thread(acknowledge_survivor_notifications, process_registry, ((completion_evt, synth_text),))
                        break
                    # Captured before injection: afterwards the key is busy either way (the injected
                    # turn itself installs the guard).
                    turn_busy = await self._launching_turn_active(platform_name, watcher)
                    delivery = asyncio.ensure_future(self._enqueue_process_completion_notification(
                        synth_text, completion_evt, session_busy=turn_busy,
                    ))
                    # The agent normally reports the result itself, so the chat gets no separate receipt.
                    # While the launching turn is still running the agent hears of it only later (a
                    # queued follow-up, or a held fan-in batch), and the chat would stay mute that long
                    # (#112033): send the concise receipt now, once, without waiting for the wake.
                    if turn_busy and not busy_receipt_sent and (notify_mode in {"concise", "all", "result"} or (
                        notify_mode == "error" and non_success
                    )):
                        busy_receipt_sent = True
                        message_text = self._format_process_final_message(session_id, session, "concise")
                        await self._send_watcher_message(platform_name, chat_id, thread_id, message_text, watcher)
                    admitted = await delivery
                    if admitted is not False:
                        await asyncio.to_thread(acknowledge_survivor_notifications, process_registry, ((completion_evt, synth_text),))
                        break
                    # A refusal may clear (adapter reconnecting, store draining), so retry with backoff
                    # for a few minutes, then fall through to the plain notice instead of dropping it.
                    completion_attempts += 1
                    if completion_attempts < _MAX_COMPLETION_INJECTION_ATTEMPTS:
                        # The process remains terminal; only this claim owner may return it to pending.
                        if is_restart_safe(session):
                            process_registry.release_survivor_completion(session_id)
                        retry_interval = jittered_backoff(
                            completion_attempts, base_delay=max(interval, 1) * 2,
                            max_delay=_COMPLETION_INJECTION_RETRY_MAX_DELAY, jitter_ratio=0.0)
                        continue
                    if await self._process_completion_committed(completion_evt, session_id):
                        # The last refusal was a lost ACK: the turn is committed.
                        await asyncio.to_thread(acknowledge_survivor_notifications, process_registry, ((completion_evt, synth_text),))
                        break
                    logger.warning(
                        "Process watcher: completion for %s was refused %d times; falling back to the "
                        "notify_mode=%s notice", session_id, completion_attempts, notify_mode,
                    )
                if not await self._finish_process_text_notification(
                    session_id, session, notify_mode, non_success, watcher,
                    route=(platform_name, chat_id, thread_id),
                ):
                    # Keep one offer alive until shutdown cancels the watcher, even if
                    # transport recovery takes hours. A capped scalar bounds retry state.
                    interval = retry_interval = text_retry_interval
                    text_retry_interval = min(text_retry_interval * 2, 15 * 60.0)
                    continue
                break
            elif has_new_output and notify_mode == "all" and not agent_notify:
                # New output — deliver a status update (only in "all" mode; agent_notify watchers
                # only care about completion).
                await self._send_watcher_message(
                    platform_name, chat_id, thread_id, self._format_process_running_message(session), watcher,
                )
        logger.debug("Process watcher ended%s: %s", " (silent)" if silent else "", session_id)
