"""Clarification and execution-approval callbacks for a gateway turn."""

from __future__ import annotations

import logging
from typing import Any


logger = logging.getLogger('gateway.run')


class TurnRunner:
    async def _send_shared_clarify(self, entry, **kwargs):
        result = await self._ctx._status_adapter.send_clarify(**kwargs)
        if self._approval_owner is not None and result.success:
            authority, session_id, generation = self._approval_owner
            from gateway import local_viewer_events
            authority.register_clarify(session_id, generation, entry,
                                       timeout_s=local_viewer_events.prompt_timeout_s(self, "clarify"))
        return result


    def _clarify_callback_sync(self, questions) -> dict:
        """Answer the clarify tool's questions (clarify_tool's synchronous contract): one card per
        question, stop at the first the user never answers. The stream/typing re-arm waits for the
        last question — between two cards it only opens a bubble the next boundary closes."""
        from gateway.run_turn_runner import Platform, _finite_viewer_turn, t
        from gateway.run_turn_runner_clarify_delivery import (
            UNDELIVERED, UNDELIVERED_DECLINED, UNDELIVERED_NO_SURFACE,
        )
        from tools.clarify_gateway import CANCELLED, SKIPPED
        if _finite_viewer_turn():
            # `chat -q` / `-z`: the viewer detaches on the first prompt, so nobody can answer.
            # Answer headless at once, as main's `_single_query_clarify_callback` did in process.
            from gateway.session_finite import finite_clarify_reply
            return finite_clarify_reply()
        from gateway.session_cli_turn import clarify_batch_turn
        if clarify_batch_turn(self):
            # Main's batch panel (classic CLI, Ink TUI) shows every question at once: one request.
            from gateway.session_clarify_batch import ask_clarify_batch
            return ask_clarify_batch(self, questions)
        answers: dict[str, Any] = {}
        reply: dict[str, Any] = {"answers": answers, "outcome": "submitted"}
        last = len(questions) - 1
        # The typed-"skip" hint is for messaging adapters, where a chat reply answers the card. A local
        # viewer (TUI / Desktop / CLI) renders main's modal with its own skip control, unhinted.
        platform = getattr(getattr(self._ctx, "source", None), "platform", None)
        hint = "" if platform == Platform.LOCAL else f"\n{t('gateway.clarify.skip_hint')}"
        for index, entry in enumerate(questions):
            question = f"{entry['question']}{hint}"
            raw, answered = self._ask_clarify_question(
                question, entry["choices"], bool(entry["multi_select"]), rearm=index == last)
            if raw == CANCELLED:
                reply["outcome"] = "cancelled"
                break
            if not answered:
                # The surface's own no-answer text ("could not be delivered", "did not respond
                # within Nm") rides along as ``notice``: blank answers alone read as user
                # inactivity, which is the misreport #112684 describes for an undelivered card.
                undelivered = raw in (UNDELIVERED, UNDELIVERED_DECLINED, UNDELIVERED_NO_SURFACE)
                reply.update(outcome="undelivered" if undelivered else "timed_out", notice=raw)
                break
            answers[entry["qid"]] = None if raw == SKIPPED else raw
        return reply


    def _ask_clarify_question(self, question, choices, multi_select, rearm: bool = True) -> tuple[str, bool]:
        """One card: register, send, wait, then retire it (no answer) or re-arm (answer).
        Returns ``(response, answered)``; the caller decides what "no answer" means."""
        from gateway.run_turn_runner import (
            _clarify_expired_notice, pause_typing_for_prompt, resume_typing_after_prompt, suppress, t,
            typing_metadata_for_source,
        )
        from gateway.run_turn_runner_clarify_delivery import (
            UNDELIVERED_NO_SURFACE, _clarify_send_then_wait, text_fallback_coro,
        )
        from tools import clarify_gateway as clarify_mod
        import uuid
        ctx = self._ctx
        if not ctx._status_adapter:
            # Nothing can render the question: say so, or the batch's blank answers read as
            # user inactivity (#112684).
            return UNDELIVERED_NO_SURFACE, False
        session_key = ctx.session_key or ""
        clarify_id = uuid.uuid4().hex[:10]
        choices = list(choices) if choices else None
        send_kwargs = dict(
            chat_id=ctx._status_chat_id, question=question, choices=choices, clarify_id=clarify_id,
            session_key=session_key, metadata=ctx._status_thread_metadata,
        )

        def _text_fallback():
            """Schedule the plain-text prompt when the native card cannot render; None = no such path."""
            coro = text_fallback_coro(ctx._status_adapter, **send_kwargs)
            return None if coro is None else self._schedule(coro, "Clarify text fallback failed to schedule")
        entry = clarify_mod.register(
            clarify_id=clarify_id, session_key=session_key, question=question, choices=choices,
            multi_select=bool(multi_select),
        )
        # Unlike approval, clarify passes reopen=True so the continuation re-opens a native stream
        # below the question; if the re-seed fails the consumer degrades to send() automatically.
        self._close_native_stream_boundary("Clarify", t("gateway.clarify.native_stream_placeholder"), reopen=True)
        # Pause typing: a "thinking..." status must not obscure the prompt or block an "Other" reply
        # on platforms that disable input while typing (Slack Assistant).
        with suppress(Exception):
            pause_typing_for_prompt(
                ctx._status_adapter, ctx.session_key, ctx._status_chat_id,
                typing_metadata_for_source(ctx.source, ctx.event_message_id))
        # Ordering barrier: flush buffered assistant prose BEFORE the poll, which goes out on a
        # separate agent-thread-blocking path and would otherwise render ABOVE its own explanation.
        # Best-effort + short timeout so the agent thread never hangs if the consumer isn't running.
        flush = getattr(self._stream_consumer(), "flush_pending_sync", None)
        try:
            if callable(flush):
                flush(timeout=3.0)
        except Exception:
            logger.debug("Stream-consumer flush before clarify prompt failed", exc_info=True)
        fut = self._schedule(
            self._send_shared_clarify(entry, **send_kwargs),
            "Clarify send failed to schedule",
        )
        # Boundary rule (see _approval_send_outcome): a send timeout is AMBIGUOUS — the card may
        # have posted with a late ack. Only a definitive failure tears down the registration;
        # ambiguous falls through to the bounded wait so a late reply resolves. A definitive
        # failure — immediate or late — retries once as plain text before giving up.
        response, answered = _clarify_send_then_wait(
            fut, clarify_id=clarify_id, session_key=session_key, clarify_mod=clarify_mod,
            fallback=_text_fallback)
        # Branch on the explicit flag, never on the text: a real answer can start with '[' (a
        # "[A] staging" label, "[urgent] ..." free text) and must not be mistaken for a sentinel.
        if not answered:
            # No answer arrived (timeout, /new, run end): retire the native card so it stops
            # looking answerable. Adapters without a persistent card have no such method.
            retire = getattr(type(ctx._status_adapter), "retire_clarify_card", None)
            if callable(retire):
                self._schedule(
                    retire(ctx._status_adapter, clarify_id, _clarify_expired_notice()),
                    "Clarify card retire failed to schedule")
        elif rearm:
            # Reopen typing IMMEDIATELY, not on the LLM's first post-answer token (native streaming
            # otherwise re-seeds lazily on the first delta: ~48s of dead air). request_reopen_seed is
            # a no-op outside the reopen-pending native state.
            sc = self._stream_consumer()
            if sc is not None:
                try:
                    sc.request_reopen_seed()
                except Exception:
                    logger.debug("request_reopen_seed after clarify answer failed", exc_info=True)
            try:
                resume_typing_after_prompt(
                    ctx._status_adapter, ctx.session_key, ctx._status_chat_id,
                    typing_metadata_for_source(ctx.source, ctx.event_message_id))
            except Exception:
                logger.debug("resume_typing_for_chat after clarify answer failed", exc_info=True)
        return response, answered


    def _approval_notify_sync(self, approval_data: dict) -> None:
        owner = self._approval_owner
        if owner is not None:
            authority, session_id, generation = owner
            authority.check_approval_generation(session_id, generation)
        # A native decline must finish before any observer can authorize work.
        self._render_approval_sync(approval_data)
        if owner is not None:
            from gateway import local_viewer_events
            authority.register_approval(
                session_id, generation, self._ctx.session_key, approval_data,
                timeout_s=local_viewer_events.prompt_timeout_s(self, "approval"),
            )


    def _render_approval_sync(self, approval_data: dict) -> None:
        """Send the approval request from the agent thread: the adapter's interactive button
        approvals (``send_exec_approval``) when available, else plain text with ``/approve`` steps."""
        from gateway.run_turn_runner import (
            _ExecApprovalDeclined, _renders_exec_approval_buttons, ea_default_reason_text,
            pause_typing_for_prompt, typing_metadata_for_source,
        )
        from gateway.run import (
            _approval_send_outcome, _format_exec_approval_fallback, _interim_metadata,
            _redact_approval_command,
        )
        from gateway.run_turn_runner_approval_settle import register_timeout_notice
        ctx = self._ctx
        adapter = ctx._status_adapter
        # Slack's assistant_threads_setStatus disables the compose box, so the user can't type
        # /approve while "is thinking..." shows. Pausing stops _keep_typing re-setting it; resumed
        # in approve/deny.
        pause_typing_for_prompt(
            adapter, ctx.session_key, ctx._status_chat_id,
            typing_metadata_for_source(ctx.source, ctx.event_message_id))
        self._close_native_stream_boundary("Approval")
        # Redact credentials before display: the raw command string can carry secrets. Both the button and plain-text paths use this value.
        cmd = _redact_approval_command(approval_data.get("command", ""))
        desc = _redact_approval_command(approval_data.get("description")) or ea_default_reason_text()
        flags = {k: approval_data.get(k, d) for k, d in (("allow_permanent", True), ("allow_session", True), ("smart_denied", False))}
        # Check the *class*, not the instance — MagicMock auto-creates attributes in tests.
        if _renders_exec_approval_buttons(type(adapter)):
            try:
                fut = self._schedule(
                    adapter.send_exec_approval(
                        chat_id=ctx._status_chat_id, command=cmd, session_key=ctx.session_key or "",
                        description=desc, metadata=ctx._status_thread_metadata,
                        request_id=approval_data.get("request_id"), **flags,
                    ),
                    "send_exec_approval scheduling error",
                )
                if fut is None:
                    raise RuntimeError("send_exec_approval: loop unavailable")
                outcome = _approval_send_outcome(fut, timeout=15)
                if outcome == "sent":
                    # Without this, a card whose timer runs out keeps live buttons and nobody
                    # learns the command did NOT run (only the TUI registered a settle hook).
                    register_timeout_notice(
                        self, approval_data, command=cmd,
                        card_message_id=getattr(fut.result(timeout=0), "message_id", None))
                    return
                if outcome == "ambiguous":
                    # Timeout ≠ failure: the card may have posted with a late ack. The prompt
                    # registration stays alive so a tap still resolves; re-sending made duplicate
                    # cards + orphaned "/approve: nothing pending".
                    logger.warning(
                        "Button-based approval send timed out — treating "
                        "as possibly-delivered (no re-send; the prompt "
                        "stays armed for a late tap)"
                    )
                    return
                if outcome == "declined":
                    # P5(b): the connector AUTHORIZED this destination and
                    # refused it. The text fallback below re-sends the same
                    # content to the same chat, which would turn a refused
                    # button card into a delivered plain-text one — the exact
                    # leak the egress guard exists to stop. A decline is
                    # definitive, so unlike `ambiguous` the registration is
                    # torn down; unlike `failed`, nothing is re-sent.
                    logger.warning(
                        "Button-based approval DECLINED by the connector's "
                        "egress guard — not falling back to text (the "
                        "destination is not approved for this connection)"
                    )
                    # RAISE, do not return. This function is the notify_cb for
                    # `_await_gateway_decision`, which already has a correct
                    # undeliverable path: a raising notify drops the queue entry
                    # and returns `notify_failed`, unblocking the tool. Returning
                    # quietly suppressed the text fallback (right) but left the
                    # CENTRAL approval entry pending (wrong) — the dangerous
                    # command then blocked until the approval timeout. My earlier
                    # comment claimed the registration was torn down; only the
                    # adapter's private prompt map was.
                    raise _ExecApprovalDeclined(
                        "exec approval undeliverable: connector egress declined "
                        "this destination"
                    )
                logger.warning("Button-based approval failed (send returned error), falling back to text")
            except _ExecApprovalDeclined:
                # Must escape this handler: the fallback below is a text send to
                # the destination the connector just refused.
                raise
            except Exception as e:
                logger.warning("Button-based approval failed, falling back to text: %s", e, exc_info=True)
        # Plain-text prompt with the adapter's typed prefix (e.g. `!approve`): typed "/" is blocked
        # in Slack threads and reserved by Matrix clients.
        msg = _format_exec_approval_fallback(cmd, desc, getattr(adapter, "typed_command_prefix", "/"), **flags)
        try:
            # Mark as approval prompt: WeCom routes it through the control lane and Telegram pushes it
            # in "important" mode (#132516). Never ``notify`` — A2A reads that as the turn-final reply.
            metadata = {**(ctx._status_thread_metadata or {}), "is_approval_prompt": True}
            fut = self._schedule(
                adapter.send(ctx._status_chat_id, msg, metadata=_interim_metadata(metadata)), "Approval text-send scheduling error",
            )
            if fut is not None:
                fut.result(timeout=15)
                # No card to edit on the text path: the prompt has no buttons to drop and carries
                # the /approve instructions, so the timeout notice is posted as a new message.
                register_timeout_notice(self, approval_data, command=cmd, card_message_id=None)
        except Exception as e:
            logger.error("Failed to send approval request: %s", e, exc_info=True)
