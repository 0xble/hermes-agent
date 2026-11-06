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

        async with self._completion_event_scope(watcher):
            surface = should_surface_notification({
                "owner_task_id": getattr(session, "owner_task_id", ""),
                "task_id": getattr(session, "task_id", ""),
            })
        if not surface:
            # Recheck live ownership next tick: handoff retains notification intent.
            if session.exited:
                return None
            return False
        if silent:
            # Still wait for the process to exit so we can log it, but don't push any messages.
            if session.exited:
                if is_restart_safe(session):
                    if process_registry._claim_survivor_delivery(session):
                        process_registry._mark_survivor_delivered(session)
                else:
                    acknowledge_survivor_notifications(process_registry, (({"session_id": session_id}, ""),))
                return None
            return False
        return True

    async def _finish_process_text_notification(
        self, session_id: str, session, notify_mode: str, non_success: bool, watcher: dict, *, route: tuple,
    ) -> bool:
        """Settle a plain notice; False retains the existing survivor retry path."""
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
        settled = True
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
            settled = sent is not False
            if sent is False and is_restart_safe(session):
                process_registry.release_survivor_completion(session_id)
                return False
        # Presented, or deliberately suppressed by the notification mode: either way the
        # claimed survivor completion is settled and must not replay on a successor.
        if settled:
            acknowledge_survivor_notifications(process_registry, (({"session_id": session_id}, ""),))
        return True
