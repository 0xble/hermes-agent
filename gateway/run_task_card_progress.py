"""Slack-native progress state, retry policy and clean text fallback for a turn."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import queue
import re
import time
from typing import Any, Optional

from agent.i18n import t

logger = logging.getLogger("gateway.run")

# Exact refusals retained for older adapters/connectors without destination preflight.
# Substring matching would also silence transient thread-resolution errors.
_CARD_DESTINATION_REFUSALS = {
    "No Slack thread target",
    "slack task_card requires a thread anchor",
    "slack task_card requires a thread anchor (Slack streams are thread replies)",
}



class TaskCardProgressMixin:
    # ── Slack-native task cards (progress-queue drain) ──────────────────────────────────────

    @dataclasses.dataclass
    class _TaskCardState:
        """Task-card rail state for ``_send_native_task_card_progress``."""
        adapter: Any
        tasks: dict[str, dict[str, str]] = dataclasses.field(default_factory=dict)
        task_order: list[str] = dataclasses.field(default_factory=list)
        fallback_msg_id: Optional[str] = None
        native_failed: bool = False
        native_retry_at: float = 0.0
        native_retry_delay: float = 2.0
        # TERMINAL for the turn, distinct from native_failed: no later publication
        # in this turn may deliver task text through the native lane OR the text
        # fallback. Two causes, both properties of the destination rather than of
        # one attempt: the connector's egress guard refused the chat, or the chat
        # cannot host a card (no thread anchor). Declared rather than set
        # dynamically so the state is visible where it lives.
        publication_suppressed: bool = False
        anonymous_seq: int = 0

        @staticmethod
        def _compact(value: Any, limit: int = 120) -> str:
            text = re.sub(r"\s+", " ", str(value or "")).strip()
            return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."

        def visible_tasks(self) -> list[dict[str, str]]:
            return [self.tasks[task_id] for task_id in self.task_order[-8:]]

        def fallback_text(self) -> str:
            labels = {"in_progress": t("gateway.progress.task_status_running"),
                      "complete": t("gateway.progress.task_status_complete"),
                      "error": t("gateway.progress.task_status_error")}
            lines = [t("gateway.progress.task_line", title=task["title"], status=labels.get(task["status"], task["status"]))
                     for task in self.visible_tasks()]
            return t("gateway.progress.task_card_title") + "\n" + "\n".join(lines)

        def _upsert(self, call_id: str, title: str) -> dict[str, str]:
            if call_id not in self.tasks:
                self.task_order.append(call_id)
            self.tasks[call_id] = {"id": call_id, "title": self._compact(title), "status": "in_progress"}
            return self.tasks[call_id]

        @staticmethod
        def _title(tool_name: str) -> str:
            from agent.display import get_tool_verb, progress_tool_label
            # Executable previews are source code, not a useful activity description.
            if tool_name in {"terminal", "execute_code"}:
                return t(f"display.verb.{tool_name}")
            return get_tool_verb(tool_name) or progress_tool_label(tool_name)[1]

        def apply_event(self, raw: Any) -> bool:
            event_type = raw.get("type") if isinstance(raw, dict) else None
            if event_type not in {"tool.started", "tool.completed"}:
                return False
            call_id = str(raw.get("tool_call_id") or "")
            if not call_id:
                self.anonymous_seq += 1
                call_id = f"anonymous_{self.anonymous_seq}"
            tool_name = str(raw.get("tool_name") or "tool")
            if event_type == "tool.started":
                self._upsert(call_id, self._title(tool_name))
                return True
            # Completion-only events are rare but valid on some runtimes; keep their real ID instead
            # of guessing a same-name pending call.
            task = self.tasks.get(call_id) or self._upsert(call_id, self._title(tool_name))
            task["status"] = "error" if raw.get("is_error") else "complete"
            return True

    async def _task_card_send_or_edit_fallback(self, st) -> None:
        ctx = self._ctx
        text = st.fallback_text()
        from gateway.relay.egress import declined_send

        if st.publication_suppressed:
            return
        if st.fallback_msg_id:
            result = await st.adapter.edit_message(
                chat_id=ctx.source.chat_id, message_id=st.fallback_msg_id, content=text, metadata=ctx._progress_metadata,
            )
            if getattr(result, "success", False):
                return
            # P5(b): R5-4 made a declined native CARD terminal but left this
            # editable-text fallback: a declined edit fell through to
            # _send_progress_text and re-sent the same task text to the refused
            # chat. The decline must set the terminal state here too.
            if declined_send(result):
                logger.warning(
                    "Task-card fallback edit DECLINED by the connector's egress "
                    "guard; suppressing progress delivery for the rest of this "
                    "turn (the destination is not approved)"
                )
                st.publication_suppressed = True
                return
        result = await self._send_progress_text(st, text)
        if getattr(result, "success", False) and getattr(result, "message_id", None):
            st.fallback_msg_id = str(result.message_id)

    async def _task_card_publish(self, st) -> None:
        ctx = self._ctx
        if not st.tasks:
            return
        if st.publication_suppressed:
            # Publication was suppressed earlier in the turn (egress refusal or a chat
            # that cannot host a card); every later publication would re-deliver the
            # same task text there.
            return
        # Resolve eligibility in the owning adapter BEFORE transport I/O: a first
        # timeout/disconnect must not turn an un-cardable chat into text fallback.
        # Optional for older adapters; only an explicit False refuses publication.
        destination_supported = getattr(st.adapter, "native_task_card_destination_supported", None)
        if callable(destination_supported) and destination_supported(
            ctx.source.chat_id, reply_to=ctx._progress_reply_to, metadata=ctx._progress_metadata,
        ) is False:
            self._task_card_uncardable_destination(st)
            if st.publication_suppressed:
                return
        if not st.native_failed:
            if time.monotonic() < st.native_retry_at:
                return
            result = await st.adapter.send_native_task_card_progress(
                chat_id=ctx.source.chat_id, tasks=st.visible_tasks(), title=t("gateway.progress.task_card_title"),
                reply_to=ctx._progress_reply_to, metadata=ctx._progress_metadata, fallback_text=st.fallback_text(),
            )
            if getattr(result, "success", False):
                st.native_retry_at, st.native_retry_delay = 0.0, 2.0
                return
            # P5(b): an AUTHORIZATION decline is not a broken card lane. The
            # fallback below sends the same task text to the same chat, which
            # turns a refused card into delivered plain text. Stop the lane
            # without re-delivering; the refusal is already logged.
            from gateway.relay.egress import declined_send

            if declined_send(result):
                # TERMINAL, and stored SEPARATELY from native_failed. Reusing
                # native_failed suppressed exactly ONE update: the next progress
                # event skipped this branch (the lane is already "failed") and
                # went straight to the text fallback. A refusal does not expire
                # after one tick.
                st.publication_suppressed = True
                st.native_failed = True
                logger.warning(
                    "Slack native task-card progress DECLINED by the connector's "
                    "egress guard — suppressing the text fallback for the rest "
                    "of this turn (the destination is not approved)"
                )
                return
            if getattr(result, "held", False):
                # An unacknowledged start may already have created a card. Never duplicate it.
                st.publication_suppressed = True
                logger.warning("Slack task-card delivery is ambiguous; suppressing retries and text fallback: %s", result.error)
                return
            if getattr(result, "retryable", False):
                delay = max(st.native_retry_delay, getattr(result, "retry_after", None) or 0.0)
                st.native_retry_at = time.monotonic() + delay
                st.native_retry_delay = min(30.0, st.native_retry_delay * 2)
                logger.warning("Slack native task-card progress temporarily unavailable; retrying in %.1fs without text fallback: %s", delay, result.error)
                return
            st.native_retry_at = 0.0
            st.native_failed = True
            if getattr(result, "error", None) in _CARD_DESTINATION_REFUSALS:
                self._task_card_uncardable_destination(st, getattr(result, "error", "unknown error"))
                if st.publication_suppressed:
                    return
            else:
                logger.warning(
                    "Slack native task-card progress failed; falling back "
                    "to an editable text update: %s", getattr(result, "error", "unknown error"),
                )
        # Once the native rail fails, every later lifecycle event edits the same fallback message.
        await self._task_card_send_or_edit_fallback(st)

    def _task_card_uncardable_destination(self, st, reason: str = "no thread anchor") -> None:
        """An unthreaded chat cannot host a card. Keep Slack's default quiet, but honor an
        operator's explicit text-progress request through the editable fallback."""
        if self._ctx.tool_progress_enabled:
            st.native_failed = True
            logger.info(
                "Slack native task cards are unsupported for this destination (%s); "
                "tool progress continues as an editable text update", reason,
            )
            return
        st.publication_suppressed = True
        logger.info(
            "Slack native task cards are unsupported for this destination (%s); "
            "tool progress stays off for this turn", reason,
        )

    def _task_card_drain(self, st) -> bool:
        changed = False
        try:
            while True:
                changed = st.apply_event(self._ctx.progress_queue.get_nowait()) or changed
        except queue.Empty:
            pass
        except Exception:
            logger.debug("Slack native progress queue drain failed", exc_info=True)
        return changed

    async def _send_native_task_card_progress(self, adapter) -> None:
        """Drain progress into native cards; supported destinations retain editable fallback.
        Unsupported destinations and egress refusals suppress publication, never finalization.

        See #29483.
        """
        ctx = self._ctx
        st = self._TaskCardState(adapter)
        try:
            while ctx._run_still_current():
                try:
                    raw = ctx.progress_queue.get_nowait()
                except queue.Empty:
                    if st.native_retry_at and time.monotonic() >= st.native_retry_at and not self._agent_interrupted():
                        await self._task_card_publish(st)
                    await asyncio.sleep(0.1)
                    continue
                if not self._agent_interrupted() and st.apply_event(raw):
                    await self._task_card_publish(st)
        except asyncio.CancelledError:
            if self._task_card_drain(st) and ctx._run_still_current() and not self._agent_interrupted():
                await self._task_card_publish(st)
        finally:
            if hasattr(adapter, "stop_native_task_card_progress"):
                # Best-effort on the turn-cleanup path: an escaping transport exception would skip
                # final-delivery logic (cleanup awaits catch only CancelledError).
                try:
                    await adapter.stop_native_task_card_progress(
                        ctx.source.chat_id, reply_to=ctx._progress_reply_to, metadata=ctx._progress_metadata,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.debug("task-card stop failed during turn cleanup", exc_info=True)

