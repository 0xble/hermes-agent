"""Topical phases split from gateway/run_turn_runner.py; original module bindings remain late-bound."""

from __future__ import annotations

from logging import Logger

class TurnRunner:
    def _status_callback_sync(self, event_type: str, message: str) -> None:
        from gateway.run_turn_runner import logger, local_viewer_turn, _viewer_status_kind, notice_metadata
        from gateway.run import _prepare_gateway_status_message, _redact_gateway_user_facing_secrets
        from gateway.run_status_filter import status_chat_type
        from gateway.warning_notifications import is_warning_status, render_notification
        from gateway.bot_peer_notices import automatic_notices_allowed
        ctx = self._ctx
        if ctx.mute_notification_reply or not self._status_live() or not automatic_notices_allowed(ctx.source):
            return
        prepared = _prepare_gateway_status_message(
            ctx.source.platform, event_type, message, status_chat_type(ctx.source))
        if prepared is None:
            logger.debug(
                "status_callback suppressed for %s/%s: %s",
                ctx.source.platform.value if ctx.source.platform else "unknown", event_type,
                _redact_gateway_user_facing_secrets(str(message or ""))[:160],
            )
            return
        def present():
            if local_viewer_turn(self):
                self._publish_execution("status.update", {"kind": _viewer_status_kind(event_type, message),
                                                          "text": prepared})
                return
            self._schedule(
                self._status_delivery.send(ctx._status_adapter, ctx._status_chat_id, event_type, prepared, notice_metadata(
                    ctx._status_thread_metadata, platform=ctx.source.platform, style=getattr(message, "notice_style", None))),
                f"status_callback ({event_type}) scheduling error",
            )
        render_notification(present, platform=ctx.source.platform, user_config=ctx.user_config,
                            diagnostic=is_warning_status(event_type, message))


    def _notice_callback_sync(self, notice) -> None:
        """Credits / out-of-band notices (usage bands, depletion, restored) fire from the agent's
        sync worker thread; hop onto the gateway loop. Fired-once latch lives on the cached agent."""
        from gateway.run_turn_runner import logger
        from gateway.run import render_notice_line
        from gateway.warning_notifications import is_diagnostic_notice, render_notification
        from gateway.bot_peer_notices import automatic_notices_allowed
        if self._ctx.mute_notification_reply or not self._status_live() or not automatic_notices_allowed(self._ctx.source):
            return
        # A usage-window limit line repeats the turn's own reply, which carries the sentence and the
        # switch button (a chat message cannot be cleared, so it would only pile up). The 90% heads-up
        # is a desktop / terminal nudge; in a chat it is noise (/usage has the numbers).
        key = str(getattr(notice, "key", "") or "")
        if key == "credits.window_limit" or key.startswith("credits.window."):
            return
        diagnostic = is_diagnostic_notice(notice)
        def present(log: Logger = logger):
            try:
                line = render_notice_line(notice)
                if key.startswith("credits."):
                    # Window plans (e.g. a paused plan's line): a chat's /topup has no credits settings.
                    from agent.plan_limits import portal_wording
                    line = portal_wording(line)
            except Exception:
                log.error("render_notice_line failed", exc_info=True)
                return
            if line:
                self._schedule(self._runner._deliver_platform_notice(self._ctx.source, line, notice_style=getattr(notice, "notice_style", None)), "notice_callback delivery scheduling error")
        render_notification(present, platform=self._ctx.source.platform,
                            user_config=self._ctx.user_config, diagnostic=diagnostic)


    def _make_bg_review_callbacks(self):
        """(send, release): background-review messages ("💾 Memory updated") are held until the
        adapter's post-delivery hook releases them after the main response lands."""
        from gateway.run_turn_runner import threading, time, Platform, notice_metadata
        from gateway.run import _interim_metadata, _non_conversational_metadata
        ctx = self._ctx
        release_evt = threading.Event()
        pending: list[str] = []
        pending_lock = threading.Lock()

        owner = self._approval_owner
        local_viewer = owner is not None and getattr(ctx.source, "platform", None) == Platform.LOCAL

        def deliver(message: str) -> None:
            if local_viewer and owner is not None:
                authority, session_id, _generation = owner
                live = authority.sessions.get(session_id)
                if live is not None:
                    live.event_stream.publish(session_id, {"text": str(message)}, event_type="review.summary")
                return
            if self._status_live():
                self._send_status_text(
                    message,
                    notice_metadata(_interim_metadata(_non_conversational_metadata(ctx._status_thread_metadata, platform=ctx.source.platform)), platform=ctx.source.platform, style="context"),
                    "background_review_callback scheduling error",
                )

        def release() -> None:
            release_evt.set()
            with pending_lock:
                queued = list(pending)
                pending.clear()
            for message in queued:
                deliver(message)

        def send(message: str) -> None:
            if local_viewer and owner is not None:
                authority, session_id, generation = owner
                live = authority.sessions.get(session_id)
                deadline = time.monotonic() + 120
                while (live is not None and time.monotonic() < deadline
                       and live.event_stream.execution.get("execution_generation") == generation):
                    time.sleep(0.05)
                deliver(message)
                return
            if not self._status_live():
                return
            if not release_evt.is_set():
                with pending_lock:
                    if not release_evt.is_set():
                        pending.append(message)
                        return
            deliver(message)

        return send, release


    def _emit_and_clear_attended_notice(self, state, attended_notice):
        """Emit the captured switch notice before consuming its queued marker."""
        if attended_notice:
            self._emit_persistent_attended_notice(attended_notice)
            if state is not None:
                state.conversation.pending_attended_notice = None
