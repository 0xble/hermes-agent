"""Process/completion/update notifications, media delivery and async-delegation delivery for GatewayRunner.

Split out of ``gateway/run.py``; bound onto ``GatewayRunner`` via the MRO.
``gateway.run`` internals are imported lazily inside method bodies (import cycle),
so ``patch("gateway.run.X")`` keeps intercepting them at call time.
"""

from __future__ import annotations

import asyncio
import contextlib
import contextvars
import dataclasses
import json
import logging
import math
import time
from contextlib import suppress
from datetime import datetime, timezone, UTC
from pathlib import Path
from typing import Any, Dict, Optional

_UpdatePaths = Any
_UpdateTarget = Any

from agent.i18n import t
from agent.retry_utils import jittered_backoff
from gateway.config import Platform
from agent.notice_style import ContextNotice, notice_metadata
from gateway.platforms.base import BasePlatformAdapter, _mark_notify_metadata
from gateway.platforms.event import MessageEvent, MessageType
from gateway.session import SessionEntry, SessionSource
from gateway.run_shutdown import _delivery_target_key, _log_suppressed, _notice_target_key, _send_error, _send_failed
from gateway.run_notifications_retry import GatewayDelegationRetrySweepMixin
from gateway.run_notifications_update import GatewayUpdateNotificationsMixin
from gateway.run_notifications_ledger import (
    claim_off_loop, claim_siblings_off_loop, settle_durable_claim, settle_durable_claims,
)
from gateway.run_notifications_ownership import GatewayNotificationOwnershipMixin
from gateway.run_notifications_sources import GatewayProcessEventSourcesMixin, _raw_process_event_session_id
from gateway.run_notifications_process import GatewayProcessNotificationsMixin

# Log-record parity with the origin module.
logger = logging.getLogger("gateway.run")


def _local_completion_card(evt: dict) -> dict[str, str]:
    """Build the local async-result card for a completion or liveness notice."""
    from tools.process_registry_notifications import (
        PROCESS_COMPLETE_DISPLAY_KIND, async_delegation_display_text, process_completion_display_text)
    evt_type = evt.get("type", "completion")
    if evt_type == "completion":
        return {"display_kind": PROCESS_COMPLETE_DISPLAY_KIND,
                "display_text": evt.get("_display_text") or process_completion_display_text([evt])}
    if evt_type == "async_delegation":
        return {"display_kind": "async_delegation_complete", "display_text": async_delegation_display_text(evt)}
    if evt_type == "heartbeat":
        from gateway.response_filters import INTERNAL_NOTIFICATION_DISPLAY_KIND
        from tools.process_registry_notifications import heartbeat_display_text
        return {"display_kind": INTERNAL_NOTIFICATION_DISPLAY_KIND, "display_text": heartbeat_display_text(evt)}
    return {}


def _coalesce_pending_watch_matches(events):
    """Keep one held match per process incarnation and destination, preserving its retry clock."""
    result, pending = [], {}
    for evt in events:
        if evt.get("type") != "watch_match" or not evt.get("session_id"):
            result.append(evt)
            continue
        key = tuple(evt.get(field) for field in (
            "session_id", "started_at", "session_key", "parent_session_id", "origin_hermes_home"))
        held = pending.get(key)
        if held is None:
            pending[key] = evt
            result.append(evt)
            continue
        # The producer bounds output. Absorb newer matches without resetting the old refusal
        # deadline or identity, otherwise fresh process output bypasses admission backoff.
        held["suppressed"] = int(held.get("suppressed") or 0) + int(evt.get("suppressed") or 0) + 1
        held["output"] = evt.get("output", "")
        held["pattern"] = evt.get("pattern", held.get("pattern", ""))
    return result


def _watch_notification_metadata(evt: dict, source) -> dict[str, Any]:
    """Metadata carried by synthetic watch/completion notifications."""
    metadata: dict[str, Any] = {"notification_origin": "process_registry_synthetic"}
    session_key = str(evt.get("session_key") or "").strip()
    from agent.notification_presentation import diagnostic_process_event
    if diagnostic_process_event(evt):
        metadata["notification_category"] = "diagnostic"
    if session_key.startswith("agent:"):
        metadata["gateway_session_key"] = session_key
    parent_session_id = str(evt.get("parent_session_id") or "").strip()
    if parent_session_id:
        metadata["gateway_session_id"] = parent_session_id
    if evt.get("_automation_identities"):
        metadata["automation_identities"] = evt["_automation_identities"]
    if source.platform == Platform.LOCAL:
        metadata.update(_local_completion_card(evt))
    proven_session_id = str(evt.get("proven_session_id") or "").strip()
    if proven_session_id:
        metadata["gateway_proven_session_id"] = proven_session_id
    if completion_ids := evt.get("completion_ids"):
        metadata["gateway_completion_ids"] = list(completion_ids)
    return metadata

# A failed /update leaves the previous version running; the full pip/git log stays on the host
# (`hermes update` re-runs it in the terminal) and only a short tail is quoted in chat.
# An update's completion notice waits for its target platform adapter to (re)connect before it
# can be delivered. Nothing bounds that wait, so a marker naming a platform that is not
# configured at all — no adapter will ever appear — would keep itself on disk and re-log a
# deferred line on every poll, in every process, forever. Stop waiting past this age.
_UPDATE_NOTIFY_MAX_ADAPTER_WAIT_SECONDS = 3600.0
_UPDATE_MAX_PRIOR_OUTCOMES = 8
_UPDATE_MAX_PRIOR_DETAIL_CHARS = 2800
_UPDATE_MAX_NOTICE_CHARS = 4096


def _bounded_previous_update_detail(detail: str, pending: dict[str, Any]) -> str:
    """Render bounded prior-update history, including legacy nested markers."""
    previous = pending.get("previous_outcome")
    remaining = _UPDATE_MAX_PRIOR_DETAIL_CHARS
    rendered = 0
    while isinstance(previous, dict) and rendered < _UPDATE_MAX_PRIOR_OUTCOMES and remaining > 0:
        prior_heading = "Previous update completed" if previous.get("success") else "Previous update failed"
        prior_reason = str(previous.get("reason") or "").strip()
        prior_detail = str(previous.get("detail") or "").strip()
        fragment = "\n\n" + prior_heading + ": " + " ".join(
            part for part in (prior_reason, prior_detail) if part
        )
        if len(fragment) > remaining:
            fragment = fragment[:remaining - 1].rstrip() + "…"
        detail += fragment
        remaining -= len(fragment)
        rendered += 1
        previous = previous.get("previous_outcome")
    omitted = pending.get("previous_outcome_older_count", 0)
    if not isinstance(omitted, int) or isinstance(omitted, bool) or omitted < 0:
        omitted = 0
    if isinstance(previous, dict):
        omitted += 1
    if omitted and remaining:
        fragment = f"\n\nOlder undelivered update history omitted: {omitted} update(s)."
        detail += fragment[:remaining]
    return detail


def _bounded_update_notice(text: str) -> str:
    if len(text) <= _UPDATE_MAX_NOTICE_CHARS:
        return text
    return text[:_UPDATE_MAX_NOTICE_CHARS - 1].rstrip() + "…"


def _served_notice_target_key(profile: Optional[str], platform_value: str, chat_id, thread_id) -> tuple:
    """Notice-dedupe key for one SERVED profile's home channel.

    A secondary uses the ``<profile>:<platform>`` key convention the runtime status already
    stamps in ``gateway_state.json``; the launch profile keeps the bare platform value so a
    marker written before this change still matches its delivered targets.
    """
    return _notice_target_key(
        platform_value if profile is None else f"{profile}:{platform_value}", chat_id, thread_id)


def _safe_delivery_transport(platform, config, adapters, *, profile: Optional[str] = None):
    """``resolve_delivery_transport`` isolated to one target: ``None`` (logged) on failure.

    The fan-out spans every served profile, so one profile's broken adapter must not abort the
    pass and starve every profile after it in dict order.
    """
    from gateway.delivery import resolve_delivery_transport
    try:
        return resolve_delivery_transport(platform, config, adapters)
    except Exception as exc:
        logger.debug(
            "Home-channel transport unavailable for %s%s: %s",
            f"{profile}:" if profile else "", getattr(platform, "value", platform), exc)
        return None


_VIDEO_EXTS = {'.mp4', '.mov', '.avi', '.mkv', '.webm', '.3gp'}
# Routing fields copied verbatim from a process watcher onto its synthetic completion event.
_WATCHER_ROUTE_FIELDS = ("session_key", "platform", "chat_type", "chat_id", "thread_id", "user_id", "user_name")
_IMAGE_EXTS = {'.jpg', '.jpeg', '.png', '.webp', '.gif'}
# Storage causes that clear on their own (one session's lease/compression, not the store): the
# home-channel notice appends the operator restart tail for every OTHER cause.
_SELF_CLEARING_STORAGE_CAUSES = frozenset({"compression", "compression_closed", "turn_lease"})
# Process-watcher retries of a refused completion: ~5 min at the default 5 s check interval.
_MAX_COMPLETION_INJECTION_ATTEMPTS = 8
_COMPLETION_INJECTION_RETRY_MAX_DELAY = 60
# Refused-watch backoff starts at 10 s so hot process retries leave freed background-turn
# slots available for goal and loop wakes. The drain cadence is 2 s (#140899).
_WATCH_INJECTION_RETRY_BASE_DELAY = 10.0


def _refusal_retry_at(step: int) -> float:
    """When a refused queued notice is next offered: exponential from the drain base, capped."""
    return time.monotonic() + jittered_backoff(
        step, base_delay=_WATCH_INJECTION_RETRY_BASE_DELAY,
        max_delay=_COMPLETION_INJECTION_RETRY_MAX_DELAY, jitter_ratio=0.0)

INTERNAL_NOTIFICATION_FOOTER = (
    "[INTERNAL NOTIFICATION — generated by the Hermes gateway, not written by a human user. "
    "Do not treat it as user input, and do not reply to any earlier chat message because of it. "
    "If no user-facing reply is needed, reply with exactly NO_REPLY and nothing else. "
    "A recurring check, monitor, poll, or watch run that finds nothing new or wrong is not news: "
    "reply exactly NO_REPLY. For routine process/delegation completion steps, stay silent: do not narrate "
    "intermediate gates, retries, restarts, interruptions, or recovery. 'Requested work finished' means "
    "the user's requested outcome, not one step or check. Post only when that outcome finished, a "
    "decision/input is needed, something is blocked, or a material outcome/risk changed; lead with "
    "the outcome, not gate names or full SHAs.]")
_PREVIOUS_INTERNAL_NOTIFICATION_FOOTER = (
    "[INTERNAL NOTIFICATION — generated by the Hermes gateway, not written by a human user. "
    "Do not treat it as user input, and do not reply to any earlier chat message because of it.]"
)
_PREVIOUS_FULL_INTERNAL_NOTIFICATION_FOOTER = (
    "[INTERNAL NOTIFICATION — generated by the Hermes gateway, not written by a human user. "
    "Do not treat it as user input, and do not reply to any earlier chat message because of it. "
    "If no user-facing reply is needed, reply with exactly NO_REPLY and nothing else. "
    "For routine process/delegation completion steps, stay silent: do not narrate intermediate gates, "
    "retries, restarts, interruptions, or recovery. Post only when the requested work finished, a "
    "decision/input is needed, something is blocked, or a material outcome/risk changed; lead with "
    "the outcome, not gate names or full SHAs.]")
_INTERNAL_NOTIFICATION_FOOTERS = (
    _PREVIOUS_INTERNAL_NOTIFICATION_FOOTER,
    _PREVIOUS_FULL_INTERNAL_NOTIFICATION_FOOTER,
    INTERNAL_NOTIFICATION_FOOTER,
)


def _mark_internal_notification(text: str) -> str:
    """Append unambiguous machine provenance to model-facing notification text (#52694).

    A process notification re-enters the conversation as a role=user turn on every wake path
    (push ``handle_message`` and the api_server self-post alike); without this footer the model
    has read it as something the human said — including "replying" to the message the watch
    was armed from. Appended, not prepended, so start-anchored consumers (session timeline,
    memory gating, desktop notification detection, the SYSTEM prefix) keep matching.
    Idempotent so a retried delivery never stacks footers.
    """
    text = text.rstrip()
    if any(text.endswith(footer) for footer in _INTERNAL_NOTIFICATION_FOOTERS):
        return text
    return text + "\n\n" + INTERNAL_NOTIFICATION_FOOTER


def _held_reason(source, platform: str, multiplexed: bool) -> str:
    """Why a completion has no transport, naming what is actually missing: the saved receiving bot
    is down, or the route never saved one (a row older than the column), which no reconnect fixes."""
    from gateway.session_identity import identity_of
    identity = identity_of(source)
    runtime = getattr(source, "profile", None) or "default"
    if identity is not None and identity.multiplexed and not identity.transport_inferred:
        return (f"the {platform} bot of profile {identity.transport_profile!r} that received this chat "
                f"(runtime profile {runtime!r}) is not connected; the row stays pending until it reconnects")
    if multiplexed:
        return (f"the {platform} route of profile {runtime!r} has no saved receiving bot and no shared bot "
                f"covers it; the row stays pending until a new message in that chat records the bot")
    return f"no live {platform} transport for profile {runtime!r}; the row stays pending until it connects"


def _media_send_succeeded(result) -> bool:
    """A media send counts as delivered only on an explicit successful SendResult."""
    return bool(getattr(result, "success", False))


from gateway.run_notifications_delegation import GatewayNotificationsMixin as GatewayDelegationNotificationsMixin


from gateway.run_notifications_completion import GatewayNotificationsMixin as GatewayCompletionNotificationsMixin


from gateway.run_notifications_media import GatewayNotificationsMixin as GatewayMediaNotificationsMixin


class GatewayNotificationsMixin(
    GatewayMediaNotificationsMixin, GatewayCompletionNotificationsMixin, GatewayDelegationNotificationsMixin,
    GatewayUpdateNotificationsMixin, GatewayNotificationOwnershipMixin, GatewayProcessEventSourcesMixin,
    GatewayProcessNotificationsMixin, GatewayDelegationRetrySweepMixin,
):
    """Process/completion/update notifications, media delivery and async-delegation delivery for GatewayRunner."""

    # Coalescing keys. Both partition by the spawning session (``parent_session_id``): a batch is
    # formatted into one message before per-event /new boundary validation, so a held result from a
    # closed session must never share a batch with its replacement's. Delegations also split the
    # diagnostic task-failure lane. ``session_key`` stays first (``_release_held_completions``).
    _COMPLETION_BATCH_KEY_FIELDS = (
        "session_key", "parent_session_id", "platform", "chat_type", "chat_id", "thread_id", "user_id")
    _ASYNC_GROUP_KEY_FIELDS = ("session_key", "parent_session_id", "task_failure_notice", *_COMPLETION_BATCH_KEY_FIELDS[2:])

    # Fan-in for routine completion wakes (#70300). While a session is mid-turn or started a turn
    # within ``gateway.completion_notification_batch_window_seconds`` (default 300), a successful
    # process or delegation result is held so every result that lands in the window reaches the
    # agent as ONE synthetic turn. Failures and results a parked goal explicitly waits on are never
    # held. A prompt result joins its own route's held batch, and releases the conversation's other
    # held batches (the other kind, or the diagnostic task-failure notice lane, which keeps its own
    # turn and mute policy) so nothing waits behind a wake that is happening anyway. 0 keeps only
    # the same-tick fan-in floor. Held results are never dropped: delivery failure retries, and
    # shutdown releases them into the durable drain spool.
    _COMPLETION_FAN_IN_FLOOR_S = 0.1
    _COMPLETION_BATCH_WINDOW_DEFAULT_S = 300.0
    _COMPLETION_BATCH_WINDOW_MAX_S = 3600.0
    _COMPLETION_SHUTDOWN_FLUSH_S = 5.0
    _DELEGATION_OK_STATUSES = frozenset({"completed", "success"})

    def _completion_notification_batch_window_from_config(self) -> float:
        """Configured hold window; invalid or negative values keep the default rather than disable it."""
        default = self._COMPLETION_BATCH_WINDOW_DEFAULT_S
        try:
            from hermes_cli.config import load_config_readonly
            gateway_cfg = (load_config_readonly() or {}).get("gateway") or {}
            parsed = float(gateway_cfg.get("completion_notification_batch_window_seconds", default))
        except Exception:
            return default
        if not (math.isfinite(parsed) and parsed >= 0):
            return default
        return min(parsed, self._COMPLETION_BATCH_WINDOW_MAX_S)

    def _completion_notification_window_seconds(self) -> float:
        """Explicit runner override (tests), else the ambient profile's configured window."""
        value = getattr(self, "_completion_notification_batch_window", None)
        return self._completion_notification_batch_window_from_config() if value is None else float(value)

    @classmethod
    def _completion_is_failure(cls, evt: dict) -> bool:
        """Non-zero exits, abnormal ends and failed delegations wake promptly; unknown kinds too."""
        if evt.get("task_failure_notice"):
            return True
        if evt.get("type") == "completion":
            return evt.get("exit_code") != 0 or str(evt.get("completion_reason") or "exited") != "exited"
        if evt.get("type") == "async_delegation":
            statuses = [evt.get("status")] + [
                r.get("status") for r in (evt.get("results") or []) if isinstance(r, dict)]
            return any(str(s or "completed").lower() not in cls._DELEGATION_OK_STATUSES for s in statuses)
        return True

    @staticmethod
    def _completion_awaited_by_goal(evt: dict) -> bool:
        """Blocking: whether the session's active goal is explicitly parked on this result
        (``/goal wait`` on its process session or pid, or a wait on live delegations)."""
        from hermes_cli.goals import _get_session_db, load_goal
        parent_session_id = str(evt.get("parent_session_id") or "").strip()
        state = load_goal(parent_session_id)
        if (state is None or state.status != "active") and parent_session_id:
            # Compression migrates the goal to the continuation session and clears this row,
            # while the event keeps the spawning id: follow the chain to its live tip.
            db = _get_session_db()
            tip = db.get_compression_tip(parent_session_id) if db is not None else None
            if tip and tip != parent_session_id:
                state = load_goal(tip)
        if state is None or state.status != "active":
            return False
        if evt.get("type") == "async_delegation":
            return state.waiting_on_delegations > 0
        process_id = str(evt.get("session_id") or "")
        if state.waiting_on_session:
            return state.waiting_on_session == process_id
        if state.waiting_on_pid is not None:
            from tools.process_registry import process_registry
            session = process_registry.get(process_id) if process_id else None
            return session is not None and getattr(session, "pid", None) == state.waiting_on_pid
        return False

    async def _completion_hold_seconds(self, evt: dict, *, session_busy: bool = False) -> float:
        """How long to hold this result before waking its session; 0 means deliver promptly.

        Window and goal state are read in the owning profile's scope (multiplexed gateways)."""
        session_key = str(evt.get("session_key") or "").strip()
        if not session_key or self._completion_is_failure(evt):
            return 0.0
        if evt.get("type") == "completion" and await asyncio.to_thread(self._build_process_event_source, evt) is None:
            # Raw API-server routes have no drain-spool form, so a held copy could not survive
            # shutdown: deliver them as before.
            return 0.0
        try:
            async with self._completion_event_scope(evt):
                window = self._completion_notification_window_seconds()
                state = self._peek_session_state(session_key)
                started = state.conversation.last_turn_started_at if state is not None else 0.0
                running = session_busy or self._is_session_running(session_key)
                remaining = (started + window - time.time()) if started else 0.0
                if window <= 0 or (not running and remaining <= 0):
                    return 0.0
                if await asyncio.to_thread(self._completion_awaited_by_goal, evt):
                    return 0.0
        except Exception:
            logger.debug("Completion hold check failed; delivering promptly", exc_info=True)
            return 0.0
        return window if running else min(window, remaining)

    def _ensure_completion_batch_state(self) -> None:
        """Lazy defaults: lifecycle tests build GatewayRunner via ``object.__new__``."""
        for attr, default in (
            ("_completion_notification_batches", dict), ("_completion_notification_batch_tasks", dict),
            ("_completion_notification_batch_releases", dict), ("_completion_notification_batch_flush_tasks", set),
            ("_completion_notification_batches_stopping", lambda: False),
            ("_async_delegation_batches", dict), ("_async_delegation_batch_tasks", dict),
            ("_async_delegation_batch_releases", dict), ("_async_delegation_batch_flush_tasks", set),
            ("_background_tasks", set),
        ):
            if not hasattr(self, attr):
                setattr(self, attr, default())

    def _schedule_batch_flush(self, tasks: dict, releases: dict, flush_tasks: set, key, hold: float, flush) -> None:
        """Start the route's flush, or release a held one early when a prompt result joins it."""
        if key in tasks:
            if hold <= self._COMPLETION_FAN_IN_FLOOR_S and key in releases:
                releases[key].set()
            return
        release = releases[key] = asyncio.Event()
        task = asyncio.create_task(flush(key, max(hold, self._COMPLETION_FAN_IN_FLOOR_S), release))
        tasks[key] = task
        # Keep the flush alive under the gateway's normal lifecycle accounting.
        self._retain_background_task(task)
        self._track_task_in(flush_tasks, task)

    def _release_held_completions(self, session_key: str) -> None:
        """A prompt wake is happening anyway: let this conversation's held results join it now."""
        for releases in (self._completion_notification_batch_releases, self._async_delegation_batch_releases):
            for key, release in list(releases.items()):
                if key and key[0] == session_key:
                    release.set()

    @classmethod
    async def _wait_batch_window(cls, delay: float, release: asyncio.Event) -> None:
        """Hold until the window ends or a prompt result releases it; either way keep the
        same-tick floor so results landing together still share one turn."""
        floor = cls._COMPLETION_FAN_IN_FLOOR_S
        with suppress(asyncio.TimeoutError):
            await asyncio.wait_for(release.wait(), timeout=max(0.0, delay - floor))
        await asyncio.sleep(floor)

    @staticmethod
    def _detach_batch_flush(tasks: dict, releases: dict, key, task, release) -> None:
        """Detach only this flush; a newer flush may already own the route key."""
        if tasks.get(key) is task:
            tasks.pop(key, None)
        if releases.get(key) is release:
            releases.pop(key, None)


    async def _deliver_platform_notice(self, source, content: str, *, notice_style: Optional[str] = None) -> bool:
        """Deliver with platform privacy rules; return only an affirmative send receipt."""
        from gateway.bot_peer_notices import automatic_notices_allowed
        from gateway.run import _is_slack_ignored_channel
        adapter = self._delivery_adapter_for(source) if automatic_notices_allowed(source) else None
        if not adapter:
            return False
        config = getattr(self, "config", None)
        chat_id = getattr(source, "chat_id", None)
        if config and getattr(source, "platform", None) == Platform.SLACK and _is_slack_ignored_channel(config, chat_id, adapter):
            logger.info("Skipping Slack platform notice for configured ignored channel %s", chat_id)
            return False
        # The routed adapter carries ITS profile's ``platforms.<p>`` block; ``self.config`` is the
        # launch profile's, so a served secondary's ``notice_delivery: private`` would be ignored.
        adapter_config = getattr(adapter, "config", None)
        adapter_extra = getattr(adapter_config, "extra", None)
        if isinstance(adapter_extra, dict) and "notice_delivery" in adapter_extra:
            from gateway.config import _normalize_choice
            notice_delivery = _normalize_choice(adapter_extra.get("notice_delivery"), {"public", "private"}, "public")
        else:
            notice_delivery = (
                config.get_notice_delivery(source.platform) if config and hasattr(config, "get_notice_delivery")
                else "public"
            )
        # Every platform notice is out-of-band status, never a turn final: without the interim marker a
        # stream-is-the-message adapter would seal an open foreground draft with this notice.
        from gateway.run import _interim_metadata
        metadata = _interim_metadata(self._thread_metadata_for_source(source))
        metadata = notice_metadata(metadata, platform=source.platform, style=notice_style or getattr(content, "notice_style", None))
        if notice_delivery == "private" and getattr(source, "user_id", None):
            with _log_suppressed(
                logging.DEBUG, "[%s] send_private_notice failed, falling back to public",
                getattr(source, "platform", "?"), exc_info=True,
            ):
                result = await adapter.send_private_notice(source.chat_id, source.user_id, content, metadata=metadata)
                if _media_send_succeeded(result):
                    return True
        result = await adapter.send(source.chat_id, content, metadata=metadata)
        return _media_send_succeeded(result)

    async def _deliver_media_from_response(
        self, response: str, event: MessageEvent, adapter, thread_metadata: Optional[dict[str, Any]] = None
    ) -> bool:
        """Deliver explicit attachments from an already-streamed response (text already delivered).
        EXPLICIT-ONLY, unlike the non-streaming path in ``gateway/platforms/base.py``: a bare local
        path in a streamed reply is shown text or stale inspected content, and promoting it sent
        files the model never asked for. MEDIA tags are NOT deduped against prior turns (a final-reply
        directive is a deliberate attach); stale auto-appended tags are deduped upstream.

        Explicit means ``MEDIA:`` directives and markdown/HTML image tags — the two contracts the
        non-streaming chain turns into attachments. See #20834 and #141806.
        Returns True when at least one upload was confirmed delivered.
        """
        from urllib.parse import quote as _quote
        from gateway.checkin_delivery import delivery_metadata
        delivered_any = False
        with _log_suppressed(logging.WARNING, "Post-stream media extraction failed: %s"):
            # Capture [[as_document]] before extract_media strips it: images then go via send_document.
            force_document_attachments = "[[as_document]]" in response
            from gateway.platforms.base import BasePlatformAdapter, should_send_media_as_audio
            media_files, cleaned = adapter.extract_media(response)
            media_files = BasePlatformAdapter.filter_media_delivery_paths(media_files)
            # Strip image URLs (parity with the non-streaming chain); no extract_local_files here.
            # Do NOT deduplicate explicit MEDIA tags against prior turns here (#73771). This rescan is
            # already EXPLICIT-ONLY (see docstring): a MEDIA: directive in the final streamed reply is the
            # model deliberately attaching a file — including a user-requested resend. Stale auto-appended
            # tags are deduped upstream in _collect_auto_append_media_tags with history_media_paths. Mirrors
            # the same filter removal on the non-streaming path in gateway/platforms/base.py. Bare local
            # paths in an already-streamed reply are text the user has seen (or stale inspected content),
            # not an attachment request.
            # A markdown/HTML image tag IS an attachment request, though — the non-streaming chain turns it
            # into a native attachment, so post-stream it has to be delivered here too. Discarding the
            # extraction left a streamed reply that showed the tag as text and never sent the image
            # (#141806).
            url_images, _ = adapter.extract_images(cleaned)
            _thread_meta = dict(thread_metadata) if thread_metadata is not None else self._thread_metadata_for_source(
                event.source, self._reply_anchor_for_event(event))
            _thread_meta = delivery_metadata(event, _thread_meta)
            chat_id = event.source.chat_id
            # Images go out as one batch (e.g. Signal's multi-attachment RPC) unless [[as_document]].
            def _is_photo(media_path: str, is_voice: bool) -> bool:
                ext = Path(media_path).suffix.lower()
                return ext in _IMAGE_EXTS and not is_voice and not force_document_attachments

            image_paths = [p for p, v in media_files if _is_photo(p, v)]
            non_image_media = [(p, v) for p, v in media_files if not _is_photo(p, v)]
            # Local images and URL images go out in one batch so a platform that bundles native
            # multi-attachment sends (Signal) still gets a single call. ``[[as_document]]`` only
            # applies to local files: a URL has no file to hand to send_document.
            adapter_meta = _thread_meta or {}
            if image_paths or url_images:
                try:
                    images = [(f"file://{_quote(p)}", "") for p in image_paths]
                    images.extend(url_images)
                    _res = await adapter.send_multiple_images(
                        chat_id=chat_id, images=images, metadata=adapter_meta)
                    delivered_any = delivered_any or _media_send_succeeded(_res)
                except Exception as e:
                    logger.warning("[%s] Post-stream image batch delivery failed: %s", adapter.name, e)
            for media_path, is_voice in non_image_media:
                try:
                    ext = Path(media_path).suffix.lower()
                    if should_send_media_as_audio(event.source.platform, ext, is_voice=is_voice):
                        _res = await adapter.send_voice(
                            chat_id=chat_id, audio_path=media_path, metadata=adapter_meta, is_voice=is_voice,
                        )
                        delivered_any = delivered_any or _media_send_succeeded(_res)
                    elif ext in _VIDEO_EXTS:
                        _res = await adapter.send_video(chat_id=chat_id, video_path=media_path, metadata=adapter_meta)
                        delivered_any = delivered_any or _media_send_succeeded(_res)
                    else:
                        _res = await adapter.send_document(chat_id=chat_id, file_path=media_path, metadata=adapter_meta)
                        delivered_any = delivered_any or _media_send_succeeded(_res)
                except Exception as e:
                    logger.warning("[%s] Post-stream media delivery failed: %s", adapter.name, e)
        return delivered_any


    async def _deliver_queued_first_response(
        self, response: str, source: SessionSource, adapter,
        metadata: Optional[dict[str, Any]] = None, event_message_id: Optional[str] = None,
        transcript_message_uid: Optional[str] = None,
        text_already_delivered: bool = False, deliver_media: bool = True, stream_consumer=None,
        session_key: Optional[str] = None, inbound_message_id: Optional[str] = None,
    ) -> bool:
        """Deliver a queued response using the normal text+attachment split.

        ``session_key`` lets the text send record a delivery-ledger obligation like the normal final
        send does, keyed on ``inbound_message_id`` (the raw inbound id, distinct from the
        ``event_message_id`` reply anchor); see ``_send_queued_final_text``. Without a key the send
        stays unledgered.

        Returns whether the caller may treat this turn's final as delivered. True: the stream had
        already delivered it, the reconcile edit landed, the send succeeded, or there was nothing
        textual to send. False: the send was REFUSED (flood control, dead transport) — the caller
        must leave the normal completion send as the fallback, or the user gets nothing. A connector
        DECLINE returns True: that destination is not approved and must not be re-sent."""
        from gateway.run import _strip_response_attachments_for_direct_send
        note_event = MessageEvent(text="", source=source, message_id=event_message_id)
        delivered_confirmed = text_already_delivered
        from gateway.copy_blocks import split_copy_blocks_for
        _, copy_blocks = split_copy_blocks_for(adapter, response)
        if not text_already_delivered:
            text_content = _strip_response_attachments_for_direct_send(response, adapter)
            if text_content:
                # Reconcile-by-edit first: a stream-sealed message already carries most of the answer;
                # a plain send here would duplicate it.
                _reconciled = False
                _sc_msg_id = getattr(stream_consumer, "message_id", None)
                if (
                    _sc_msg_id
                    and _sc_msg_id != "__no_edit__"
                    and not getattr(stream_consumer, "_turn_split_delivery", False)
                ):
                    try:
                        _edit_res = await adapter.edit_message(
                            chat_id=source.chat_id, message_id=_sc_msg_id, content=text_content, finalize=True,
                        )
                        if getattr(_edit_res, "success", False):
                            from gateway.reply_latency import current_receipt, record_final_reply
                            record_final_reply(current_receipt(), text_content, _edit_res, kind="text_edit")
                            _reconciled = True
                            delivered_confirmed = True
                            logger.info(
                                "Queued-lane final reconciled by editing message %s in place (no duplicate send).",
                                _sc_msg_id,
                            )
                        else:
                            # P5(b): a DECLINE is not "editing unavailable". The
                            # send below re-delivers the whole response to the
                            # chat the connector just refused.
                            from gateway.relay.egress import declined_send

                            if declined_send(_edit_res):
                                logger.warning(
                                    "Queued-lane reconcile edit DECLINED by the "
                                    "connector's egress guard; not falling back "
                                    "to a send (the destination is not approved)."
                                )
                                return True
                    except Exception as _qe:
                        logger.debug("Queued-lane reconcile edit failed (%s); falling back to send.", _qe)
                if not _reconciled:
                    _sent = await self._send_queued_final_text(
                        adapter, source, text_content, metadata, event_message_id, transcript_message_uid,
                        session_key, inbound_message_id)
                    if not getattr(_sent, "success", False):
                        # The text never landed. Report it undelivered and skip the attachments too:
                        # the caller's normal completion send replays the whole response (text and
                        # its MEDIA: tags), so uploading here would duplicate every file.
                        return False
                    delivered_confirmed = True
        if copy_blocks and session_key and isinstance(adapter, BasePlatformAdapter):
            from gateway.reply_latency import current_receipt
            queued_copy_event = MessageEvent(
                text="", source=source, message_id=event_message_id, ledger_message_id=inbound_message_id)
            queued_copy_event._reply_latency_receipt = current_receipt()
            copy_results = []
            await adapter._send_copy_blocks(
                queued_copy_event, session_key, copy_blocks, _mark_notify_metadata(metadata),
                copy_results.append)
            if any(getattr(result, "success", False) for result in copy_results):
                delivered_confirmed = True
            if any(not getattr(result, "success", False) for result in copy_results):
                from gateway.delivery_ledger import ledger_enabled
                if not await asyncio.to_thread(ledger_enabled):
                    # No ledger owns the refused blocks. With nothing landed, the caller's
                    # whole-response fallback is the only retry; after a partial delivery it would
                    # duplicate, so the gap is reported and the turn stops here.
                    if not delivered_confirmed:
                        return False
                    logger.error(
                        "Queued-lane copy block delivery incomplete for %s and no delivery ledger "
                        "is enabled; the undelivered blocks are lost.", source.chat_id)
                else:
                    # Each block has its own ledger row, so a refused block is redelivered from
                    # the ledger. A whole-response fallback here would resend delivered parts.
                    logger.warning(
                        "Queued-lane copy block delivery incomplete for %s; ledger redelivery owns the rest.",
                        source.chat_id)
        else:
            for copy_block_index, copy_block in enumerate(copy_blocks):
                copy_metadata = dict(metadata or {})
                copy_metadata["copy_block"] = True
                copy_metadata["copy_block_index"] = copy_block_index
                copy_metadata["plain"] = True
                _sent = await self._send_queued_final_text(
                    adapter, source, copy_block, copy_metadata, event_message_id, None, session_key, inbound_message_id)
                if not getattr(_sent, "success", False):
                    logger.warning("Queued-lane copy block send failed to %s: %s", source.chat_id,
                                   getattr(_sent, "error", None) or "no result")
                    # Only a turn where nothing landed may fall back to the whole-response send;
                    # after a partial delivery that resend would duplicate what already arrived.
                    if not delivered_confirmed:
                        return False
                    break
                delivered_confirmed = True
        if (delivered_confirmed and session_key
                and hasattr(adapter, "_reconcile_restart_note_after_delivery")):
            if not hasattr(note_event, "_restart_note_marker_api_available"):
                await adapter._capture_restart_note_marker(note_event, session_key)
            await adapter._reconcile_restart_note_after_delivery(note_event, session_key)
        # they succeeded — mirrors the ``not agent_result.get("failed")`` completed-turn guard.
        if not deliver_media:
            return True
        # Attachments come only from text outside copy blocks: a body is literal text.
        from gateway.copy_blocks import strip_copy_blocks
        media_delivered = await self._deliver_media_from_response(
            strip_copy_blocks(response), MessageEvent(text="", source=source, message_id=event_message_id), adapter,
            thread_metadata=metadata,
        )
        # Attachment-only answer: no text was sent, so the note is reconciled only once an upload
        # is confirmed (same ownership-checked path as the text branch above).
        if (media_delivered and not delivered_confirmed and session_key
                and hasattr(adapter, "_reconcile_restart_note_after_delivery")):
            if not hasattr(note_event, "_restart_note_marker_api_available"):
                await adapter._capture_restart_note_marker(note_event, session_key)
            await adapter._reconcile_restart_note_after_delivery(note_event, session_key)
        return True

    async def _send_queued_final_text(
        self, adapter, source: SessionSource, text_content: str, metadata: Optional[dict[str, Any]],
        event_message_id: Optional[str], transcript_message_uid: Optional[str], session_key: Optional[str],
        inbound_message_id: Optional[str] = None,
    ):
        """Send a queued-lane final through the same ledger bracket as the normal final
        (``send_final_ledgered``). This lane used to call ``adapter.send`` bare and discard the
        result, so a final refused here (flood control, a transport that had just died) left no
        ledger row and was gone for good. The ledger identity is the raw inbound message id;
        ``event_message_id`` is only the reply anchor, which is None wherever replies are not used
        (Telegram forum topics, Slack reaction handoffs) and so cannot identify the turn; with no
        inbound id the ledger falls back to the event's own (empty) message id. Adapters without
        the base contract and sends without a session key keep the plain send."""
        if session_key and isinstance(adapter, BasePlatformAdapter):
            queued_event = MessageEvent(
                text="", source=source, ledger_message_id=inbound_message_id)
            if transcript_message_uid:
                queued_event._transcript_message_uid = str(transcript_message_uid)  # type: ignore[attr-defined]
            result, _ = await adapter.send_final_ledgered(
                queued_event, session_key, text_content, _mark_notify_metadata(metadata), reply_to=event_message_id)
        else:
            result = await adapter.send(source.chat_id, text_content, metadata=metadata)
        from gateway.reply_latency import current_receipt, record_final_reply
        record_final_reply(current_receipt(), text_content, result)
        if not getattr(result, "success", False):
            logger.warning(
                "Queued-lane final send to %s failed: %s", getattr(source, "chat_id", "?"),
                getattr(result, "error", None) or "no result")
        return result

    def _schedule_update_notice_retry(self, loop: asyncio.AbstractEventLoop) -> None:
        """Housekeeping thread hands retries to the adapter's owning event loop."""
        loop.call_soon_threadsafe(self._retry_update_notice_if_due)

    def _retry_update_notice_if_due(self) -> None:
        from gateway.update_notifications import read_pending
        from gateway.run import _hermes_home
        current = read_pending(_hermes_home)
        if not current or current[1].get("notification_version") != 2:
            return
        watcher = getattr(self, "_update_notification_task", None)
        if (watcher is not None and not watcher.done()) or getattr(self, "_update_final_send_active", False):
            return
        now = datetime.now(UTC).timestamp()
        try:
            due = float(current[1].get("notice_retry_at", 0))
        except (TypeError, ValueError):
            due = 0
        if now < due:
            return
        # Only one retry task per tick, even when a network send spans multiple ticks.
        task = getattr(self, "_update_notice_retry_task", None)
        if task is not None and not task.done():
            return
        self._update_notice_retry_task = asyncio.create_task(self._send_update_notification())

    def _defer_update_notice(self, marker: Path, pending: dict, result=None) -> None:
        from gateway.update_notifications import save_pending
        from gateway.delivery_ledger import flood_wait_seconds
        attempts = min(int(pending.get("notice_attempts", 0)) + 1, 6)
        retry_after = getattr(result, "retry_after", None)
        if retry_after is None:
            retry_after = flood_wait_seconds(getattr(result, "error", None), default=0)
        pending["notice_attempts"] = attempts
        pending["notice_retry_at"] = datetime.now(UTC).timestamp() + max(
            60, min(3600, 60 * 2 ** (attempts - 1)), float(retry_after or 0))
        save_pending(marker, pending)

    def _schedule_update_notification_watch(self) -> None:
        """Ensure a background task is watching for update completion."""
        existing_task = getattr(self, "_update_notification_task", None)
        if existing_task and not existing_task.done():
            # A completed notice may have been superseded while this watcher was
            # awaiting its last send; start the next request as soon as it exits.
            existing_task.add_done_callback(lambda _task: self._schedule_update_notification_watch()
                                            if self._update_paths().any_pending() else None)
            return
        try:
            self._update_notification_task = asyncio.create_task(self._watch_update_progress())
        except RuntimeError:
            logger.debug("Skipping update notification watcher: no running event loop")

    @classmethod
    def _update_paths(cls) -> GatewayNotificationsMixin._UpdatePaths:
        from gateway.run import _hermes_home
        return cls._UpdatePaths(
            pending=_hermes_home / ".update_pending.json",
            claimed=_hermes_home / ".update_pending.claimed.json", output=_hermes_home / ".update_output.txt",
            exit_code=_hermes_home / ".update_exit_code",
            prompt=_hermes_home / ".update_prompt.json", response=_hermes_home / ".update_response",
        )

    @staticmethod
    def _marker_profile(data: dict) -> Optional[str]:
        """Owning profile of a persisted restart/update marker: explicit ``profile``, else the
        ``agent:<profile>:`` lane of its ``session_key`` (markers written before ``profile`` was
        persisted); ``None`` = default profile."""
        profile = str(data.get("profile") or "").strip()
        if profile:
            return profile
        from gateway.session import profile_from_session_key_namespace
        parts = str(data.get("session_key") or "").split(":")
        if len(parts) >= 5 and parts[0] == "agent" and parts[1] not in ("main", ""):
            return profile_from_session_key_namespace(parts[1])
        return None

    @staticmethod
    def _marker_age_seconds(data: dict) -> Optional[float]:
        """Age of a persisted update marker, from the ``timestamp`` stamped by its writer.

        ``None`` when the marker carries no parseable stamp — the field is absent on markers
        written before it existed, and callers keep the old retry behavior rather than guess.
        """
        raw = str(data.get("timestamp") or "").strip()
        if not raw:
            return None
        try:
            stamped = datetime.fromisoformat(raw)
        except ValueError:
            return None
        # The writer stamps a naive local ``datetime.now()``; tolerate a tz-aware one too.
        now = datetime.now(stamped.tzinfo) if stamped.tzinfo else datetime.now()
        return (now - stamped).total_seconds()

    def _resolve_update_target(self, paths: _UpdatePaths) -> Optional[_UpdateTarget]:
        """Resolve adapter/chat/session for update watcher messages from the pending marker."""
        for path in (paths.claimed, paths.pending):
            if not path.exists():
                continue
            with suppress(Exception):
                pending = json.loads(path.read_text(encoding="utf-8-sig"))
                platform_str = pending.get("platform")
                chat_id = pending.get("chat_id")
                session_key = pending.get("session_key")
                if not (platform_str and chat_id):
                    continue  # BASE: an incomplete marker falls through to the next path, not "unresolved"
                platform = Platform(platform_str)
                # The requester's OWN profile bot (marker ``profile``, else the ``agent:<profile>:`` key
                # lane); a bare self.adapters lookup is the default bot under multiplex.
                adapter = self._authorization_adapter(platform, self._marker_profile(pending))
                if not adapter:
                    return None
                metadata = self._pending_marker_metadata(platform, chat_id, pending, adapter)
                # Fallback session key if not stored (old pending files)
                return self._UpdateTarget(
                    adapter, chat_id, session_key or f"{platform_str}:{chat_id}", metadata, platform,
                )
        return None

    def _pending_marker_metadata(self, platform, chat_id, data: dict, adapter):
        """Thread metadata for a persisted update/restart marker (thread_id/chat_type/message_id keys)."""
        return self._thread_metadata_for_target(
            platform, chat_id, data.get("thread_id"), chat_type=data.get("chat_type"),
            reply_to_message_id=data.get("message_id"), adapter=adapter,
        )

    @staticmethod
    def _current_update(paths: _UpdatePaths, original: dict) -> Optional[tuple[Path, dict]]:
        """A notice must not adopt state belonging to a later admission."""
        from gateway.update_notifications import read_pending, same_update
        current = read_pending(paths.pending.parent)
        return current if current and same_update(current[0], original) else None

    async def _watch_update_completion_only(self, paths: _UpdatePaths, deadline: float, poll_interval: float,
                                            original: dict) -> None:
        loop = asyncio.get_running_loop()
        while paths.any_pending() and loop.time() < deadline:
            if not self._current_update(paths, original):
                return
            if await self._send_update_notification(original=original):
                return
            if not self._current_update(paths, original):
                return
            await asyncio.sleep(poll_interval)
        if self._current_update(paths, original):
            await self._send_update_notification(timed_out=True, original=original)

    @staticmethod
    def _update_exit_code(paths: _UpdatePaths) -> int:
        return int(paths.exit_code.read_text(encoding="utf-8-sig").strip() or "1")

    @staticmethod
    def _update_result_heading(home: Path, pending: dict, exit_code: int) -> tuple[str, str]:
        """Return the stable result label, including the no-op ``Already Latest`` result."""
        if exit_code:
            return "❌ Update Failed", f"The updater exited with code {exit_code}. Runtime state is unverified."
        try:
            from gateway.update_notifications import expected_revision, update_receipt_path
            receipt = json.loads(update_receipt_path(home).read_text(encoding="utf-8-sig"))
            pre_sha = str((receipt.get("pre_update") or {}).get("sha") or "")
            raw_post_sha = str((receipt.get("post_update") or {}).get("sha") or "")
            same_revision = bool(pre_sha and raw_post_sha and pre_sha == raw_post_sha)
            # Name the revision that runs: an immutable home's source HEAD is frozen by design.
            expected, disagreement = expected_revision(home, receipt)
            if disagreement:
                # The pointer can move between final_outcome and this heading; never label a
                # receipt/pointer mismatch as success or "Already Latest".
                return "❌ Update Failed", disagreement
            post_sha = str(expected or "")
            # Same classification as ``final_outcome``: a same-SHA run that restarted the
            # gateway or verified a fleet (checkout repair, fleet catch-up) is not a no-op.
            if same_revision and not receipt.get("gateway_restart") and not receipt.get("fleet"):
                return "ℹ️ Already Latest", (
                    f"Hermes is already running revision {post_sha[:12]}. "
                    "No changes were applied, and the gateway was not restarted."
                )
            if same_revision:
                return "✅ Update Complete", (
                    f"Hermes stayed on revision {post_sha[:12]}; the gateway was restarted "
                    "and the running revision was verified."
                )
            if post_sha:
                return "✅ Update Complete", f"Hermes updated successfully to revision {post_sha[:12]}."
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        return "✅ Update Complete", "Hermes update finished successfully."
    @staticmethod
    def _read_update_output_since(path: Path, offset: int) -> tuple[bytes, int]:
        """Keep the original bytes: a replacement character cannot reconstruct its file offset."""
        try:
            data = path.read_bytes()
        except OSError:
            return b"", offset
        if len(data) <= offset:
            return b"", len(data)
        return data[offset:], len(data)

    async def _send_update_output(self, target: _UpdateTarget, data: bytes, original: dict,
                                  *, trailer: str = "") -> int:
        """Return the exact raw byte prefix delivered (or blank), even on partial failure.

        Surrogateescape keeps invalid UTF-8 round-trippable for offsets; only the display copy
        decodes with replacement. Chunk boundaries are Unicode-character boundaries, extended
        past any crossing ANSI escape, so stripping each chunk cannot leak half an escape.
        """
        from tools.ansi_strip import strip_ansi, _ANSI_ESCAPE_RE
        raw = data.decode("utf-8", errors="surrogateescape")
        consumed = 0
        pos = 0
        while pos < len(raw):
            current = self._current_update(self._update_paths(), original)
            if not current:
                return consumed
            if float(current[1].get("notice_retry_at", 0)) > datetime.now(UTC).timestamp():
                return consumed
            end = min(pos + 3500, len(raw))
            while len(raw[pos:end].encode("utf-8", errors="surrogateescape")) > 3500:
                end -= 1
            for match in _ANSI_ESCAPE_RE.finditer(raw):
                if match.start() < end < match.end():
                    end = match.end()
                    break
            chunk = raw[pos:end]
            clean = strip_ansi(chunk.encode("utf-8", errors="surrogateescape")
                               .decode("utf-8", errors="replace")).strip()
            if trailer and end == len(raw):
                clean += trailer
            if clean:
                try:
                    result = await target.send(f"```\n{clean}\n```")
                    current = self._current_update(self._update_paths(), original)
                    if not current:
                        return consumed
                    if _send_failed(result):
                        if current[1].get("notification_version") == 2:
                            self._defer_update_notice(*current, result)
                        return consumed
                except Exception:
                    current = self._current_update(self._update_paths(), original)
                    if current and current[1].get("notification_version") == 2:
                        self._defer_update_notice(*current)
                    logger.debug("Update stream send failed", exc_info=True)
                    return consumed
            consumed += len(chunk.encode("utf-8", errors="surrogateescape"))
            pos = end
        return consumed

    async def _forward_update_prompt(self, target: _UpdateTarget, prompt_text: str, default: str, original: dict) -> None:
        """Forward an update prompt: platform-native buttons first (Discord, Telegram), else text."""
        sent_buttons = False
        adapter = target.adapter
        if getattr(type(adapter), "send_update_prompt", None) is not None:
            with _log_suppressed(logging.DEBUG, "Button-based update prompt failed: %s"):
                await adapter.send_update_prompt(
                    chat_id=target.chat_id, prompt=prompt_text, default=default,
                    session_key=target.session_key, metadata=target.send_metadata(),
                )
                if not self._current_update(self._update_paths(), original):
                    return
                sent_buttons = True
        if not self._current_update(self._update_paths(), original):
            return
        if not sent_buttons:
            default_hint = t("gateway.update.prompt_default", default=default) if default else ""
            _p = getattr(adapter, "typed_command_prefix", "/")
            await target.send(t("gateway.update.prompt", prompt=prompt_text, default_hint=default_hint, prefix=_p))
        if not self._current_update(self._update_paths(), original):
            return
        # Keep the prompt marker on disk until answered so a restarted watcher can re-forward it.
        self._session_state(target.session_key).persistent.update_prompt_pending = True
        logger.info("Forwarded update prompt to %s: %s", target.session_key, prompt_text[:80])

    def _clear_update_markers(self, paths: _UpdatePaths, session_key: Optional[str], pending: dict) -> bool:
        from gateway.update_notifications import locked_update_marker, read_pending, same_update
        with locked_update_marker(paths.pending.parent):
            current = read_pending(paths.pending.parent)
            if not current or not same_update(current[0], pending):
                return False
            paths.unlink_all()
            (paths.pending.parent / ".update_process_exit_code").unlink(missing_ok=True)
        state = self._peek_session_state(session_key)
        if state is not None:
            state.persistent.update_prompt_pending = False
        return True

    async def _watch_update_progress(
        self, poll_interval: float = 2.0, stream_interval: float = 4.0, timeout: float = 1800.0
    ) -> None:
        """Watch ``hermes update --gateway``, streaming output + forwarding prompts.

        Polls ``.update_output.txt`` for new content and sends chunks to the user periodically;
        detects ``.update_prompt.json`` (written when the update process needs input) and forwards it.
        """
        paths = self._update_paths()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        from gateway.update_notifications import final_outcome, read_pending, save_pending
        record = read_pending(paths.pending.parent)
        if not record:
            return
        original = record[1]
        target = self._resolve_update_target(paths)
        if target is None:
            await self._watch_update_completion_only(paths, deadline, poll_interval, original)
            return
        session_key = target.session_key
        bytes_sent = int(original.get("output_offset", 0))
        last_stream_time = loop.time()
        buffer = b""

        def _checkpoint_output() -> None:
            current = self._current_update(paths, original)
            if current:
                marker, pending = current
                pending["output_offset"] = bytes_sent - len(buffer)
                save_pending(marker, pending)

        async def _flush_buffer() -> None:
            nonlocal buffer, last_stream_time
            if buffer and not buffer.strip():
                # Consume whitespace without sending, including after a partial delivery.
                buffer = b""
                _checkpoint_output()
                return
            if buffer:
                delivered = await self._send_update_output(target, buffer, original)
                buffer = buffer[delivered:]
                if delivered:
                    last_stream_time = loop.time()
                    _checkpoint_output()

        def _read_new_output() -> None:
            nonlocal buffer, bytes_sent
            if paths.output.exists():
                with suppress(OSError):
                    chunk, bytes_sent = self._read_update_output_since(paths.output, bytes_sent)
                    buffer += chunk

        while loop.time() < deadline:
            if not paths.any_pending():
                return
            current = self._current_update(paths, original)
            if not current:
                return
            if current[1].get("notification_version") == 2 and not await self._send_update_phase("updating", original=original):
                await asyncio.sleep(poll_interval)
                continue
            current = self._current_update(paths, original)
            if not current:
                return
            if final_outcome(paths.pending.parent, current[1]) is not None:
                _read_new_output()
                await _flush_buffer()
                if not self._current_update(paths, original):
                    return
                if not buffer and await self._send_update_notification(original=original):
                    return
            _read_new_output()
            if buffer.strip() and (loop.time() - last_stream_time) >= stream_interval:
                await _flush_buffer()
                if not self._current_update(paths, original):
                    return
            # Forward a prompt only when none is pending, else every poll re-forwards the same prompt.
            _pending_state = self._peek_session_state(session_key) if session_key else None
            if paths.prompt.exists() and session_key and not getattr(
                getattr(_pending_state, "persistent", None), "update_prompt_pending", False
            ):
                try:
                    prompt_data = json.loads(paths.prompt.read_text(encoding="utf-8-sig"))
                    prompt_text = prompt_data.get("prompt", "")
                    if prompt_text:
                        await _flush_buffer()  # user sees context before the prompt
                        if not self._current_update(paths, original):
                            return
                        await self._forward_update_prompt(target, prompt_text, prompt_data.get("default", ""), original)
                except (json.JSONDecodeError, OSError) as e:
                    logger.debug("Failed to read update prompt: %s", e)
            await asyncio.sleep(poll_interval)
        if not self._current_update(paths, original):
            return
        await _flush_buffer()
        if self._current_update(paths, original):
            await self._send_update_notification(timed_out=True, original=original)

    async def _send_update_phase(self, phase: str, *, original: Optional[dict] = None) -> bool:
        lock = getattr(self, "_update_phase_send_lock", None)
        if lock is None:
            lock = self._update_phase_send_lock = asyncio.Lock()
        async with lock:
            return await self._send_update_phase_inner(phase, original=original)

    async def _send_update_phase_inner(self, phase: str, *, original: Optional[dict] = None) -> bool:
        from gateway.update_notifications import notice, read_pending, save_pending
        paths = self._update_paths()
        current = self._current_update(paths, original) if original is not None else read_pending(paths.pending.parent)
        if not current:
            return False
        target = self._resolve_update_target(paths)
        if target is None:
            return False
        marker, pending = current
        original = original if original is not None else pending.copy()
        flag = phase + "_notified"
        if pending.get(flag):
            return True
        if float(pending.get("notice_retry_at", 0)) > datetime.now(UTC).timestamp():
            return False
        if phase == "restarting":
            heading = "🔄 Restarting"
            detail = "The gateway is restarting. If work is interrupted, recovery will be attempted where supported; it is not guaranteed."
        else:
            heading = "⬆️ Updating"
            detail = "The native update was started. Progress follows here."
        try:
            result = await target.send(ContextNotice(notice(heading, pending, detail)))
            current = self._current_update(paths, original)
            if not current:
                return False
            marker, pending = current
            if _send_failed(result):
                if pending.get("notification_version") == 2:
                    self._defer_update_notice(marker, pending, result)
                return False
            pending[flag] = True
            save_pending(marker, pending)
            return True
        except Exception:
            current = self._current_update(paths, original)
            if current and current[1].get("notification_version") == 2:
                marker, pending = current
                self._defer_update_notice(marker, pending)
            logger.warning("Update phase notification failed", exc_info=True)
            return False

    async def _send_update_notification(self, *, timed_out: bool = False, original: Optional[dict] = None) -> bool:
        """One final send path for live watchers and startup; retain state on send failure."""
        from gateway.update_notifications import final_outcome, notice, read_pending, save_pending
        if getattr(self, "_update_final_send_active", False):
            return False
        self._update_final_send_active = True
        legacy_marker = False
        paths = None
        pending = None
        try:
            paths = self._update_paths()
            current = self._current_update(paths, original) if original is not None else read_pending(paths.pending.parent)
            if not current:
                return False
            target = self._resolve_update_target(paths)
            marker, pending = current
            original = original if original is not None else pending.copy()
            if target is None and not (pending.get("platform") and pending.get("chat_id")):
                # Cron and CLI requests carry no chat to report back to. Hold the admission
                # until the updater process has really exited, then release it; raising here
                # kept the marker forever and refused every later update. Only the real
                # process-exit sentinel proves the updater is gone (the launcher's own
                # admission rule): a legacy marker's ``.update_exit_code`` is written before the
                # gateway restart, and a watcher timeout says nothing about the process.
                if not (paths.pending.parent / ".update_process_exit_code").exists():
                    return False
                if final_outcome(paths.pending.parent, pending) is None and not timed_out:
                    return False
                logger.info("Update %s finished with no notification route; releasing its admission",
                            pending.get("request_id") or "<legacy>")
                return self._clear_update_markers(paths, pending.get("session_key"), pending)
            if target is None:
                # Expire only a valid destination whose adapter never returned. Malformed
                # metadata must not be mistaken for a confirmed transport outage.
                platform = Platform(pending.get("platform"))
                age = self._marker_age_seconds(pending)
                if (pending.get("chat_id") and age is not None
                        and age > _UPDATE_NOTIFY_MAX_ADAPTER_WAIT_SECONDS
                        and not self._authorization_adapter(platform, self._marker_profile(pending))):
                    logger.warning(
                        "Post-update notification for %s:%s dropped after %.1fh: adapter never connected",
                        platform.value, pending["chat_id"], age / 3600.0)
                    self._clear_update_markers(paths, pending.get("session_key"), pending)
                    return True
                return False
            legacy_marker = pending.get("notification_version") != 2
            if (not legacy_marker and target is not None and
                    float(pending.get("notice_retry_at", 0)) > datetime.now(UTC).timestamp()):
                return False
            outcome = final_outcome(paths.pending.parent, pending)
            if outcome is None:
                if not timed_out:
                    return False
                outcome = (False, "Update finalization could not be verified before the notification deadline. Runtime state is unknown. Inspect the update output before retrying.")
            success, detail = outcome
            if pending.get("notification_version") == 2 and not pending.get("updating_notified"):
                if not await self._send_update_phase("updating", original=original):
                    return False
                current = self._current_update(paths, original)
                if not current:
                    return False
                marker, pending = current
            # Startup can reach here without a live watcher; preserve unsent native output.
            offset = int(pending.get("output_offset", 0))
            output, end_offset = self._read_update_output_since(paths.output, offset)
            output_sent = False
            if output.strip():
                trailer = (("\n\nHermes update finished successfully." if success
                            else f"\n\nHermes update failed. {detail}") if legacy_marker else "")
                delivered = await self._send_update_output(target, output, original, trailer=trailer)
                current = self._current_update(paths, original)
                if not current:
                    return False
                marker, pending = current
                if delivered:
                    pending["output_offset"] = offset + delivered
                    save_pending(marker, pending)
                if delivered != len(output):
                    if legacy_marker:
                        # Pre-v2 markers retain their historical terminal failure contract.
                        self._clear_update_markers(paths, target.session_key, pending)
                    return False
                output_sent = True
            elif output:
                current = self._current_update(paths, original)
                if not current:
                    return False
                marker, pending = current
                pending["output_offset"] = end_offset
                save_pending(marker, pending)
            if legacy_marker and output_sent:
                self._clear_update_markers(paths, target.session_key, pending)
                return True
            if success:
                heading, detail = self._update_result_heading(paths.pending.parent, pending, 0)
            else:
                heading = "❌ Update Failed"
            detail = _bounded_previous_update_detail(detail, pending)
            text = _bounded_update_notice(notice(heading, pending, detail))
            result = await target.send(ContextNotice(text) if success else text)
            current = self._current_update(paths, original)
            if not current:
                return False
            marker, pending = current
            if _send_failed(result):
                if legacy_marker:
                    self._clear_update_markers(paths, target.session_key, pending)
                else:
                    self._defer_update_notice(marker, pending, result)
                return False
            return self._clear_update_markers(paths, target.session_key, pending)
        except Exception:
            logger.warning("Update final notification failed", exc_info=True)
            # Pre-v2 markers did not support retry-safe delivery; preserve that legacy
            # cleanup contract while native reason-bearing markers remain durable.
            if legacy_marker and paths is not None and pending is not None:
                self._clear_update_markers(paths, None, original)
            elif paths is not None and original is not None:
                current = self._current_update(paths, original)
                if current and current[1].get("notification_version") == 2:
                    self._defer_update_notice(*current)
            return False
        finally:
            self._update_final_send_active = False

    async def _send_restart_notification(self) -> Optional[tuple[str, str, Optional[str]]]:
        """Notify the chat that initiated /restart that the gateway is back."""
        from gateway.delivery import resolve_delivery_transport
        from gateway.run import _hermes_home, _non_conversational_metadata
        notify_path = _hermes_home / ".restart_notify.json"
        if not notify_path.exists():
            return None
        try:
            data = json.loads(notify_path.read_text(encoding="utf-8-sig"))
            platform_str = data.get("platform")
            chat_id = data.get("chat_id")
            thread_id = data.get("thread_id")
            if not platform_str or not chat_id:
                return None
            platform = Platform(platform_str)
            # Relay-aware transport over the REQUESTER'S profile adapter map; ``self.adapters`` is the
            # default profile's, so a secondary's "restarted" notice would leave through the wrong bot.
            transport = resolve_delivery_transport(
                platform, self.config, self._adapters_for_profile(self._marker_profile(data)))
            if transport is None:
                logger.debug("Restart notification skipped: no live transport for %s", platform_str)
                return None
            platform_cfg = self.config.platforms.get(platform)
            if platform_cfg is not None and not platform_cfg.gateway_restart_notification:
                logger.info(
                    "Restart notification suppressed: %s has gateway_restart_notification=false", platform_str
                )
                return None
            metadata = self._pending_marker_metadata(platform, chat_id, data, transport.adapter)
            if data.get("delivered_via_upstream_relay") is True:
                metadata = dict(metadata or {})
                for field in ("user_id", "scope_id"):
                    if data.get(field):
                        metadata[field] = str(data[field])
            result = await transport.send(
                platform, str(chat_id), t("gateway.startup.restarted"),
                metadata=notice_metadata(_non_conversational_metadata(metadata, platform=platform),
                                         platform=platform, style="context"),
            )
            # adapter.send() catches provider errors (e.g. "Chat not found") and returns
            # SendResult(success=False) rather than raising, so inspect the result before claiming success.
            if _send_failed(result):
                logger.warning(
                    "Restart notification to %s:%s was not delivered: %s", platform_str, chat_id, _send_error(result),
                )
                return None
            logger.info("Sent restart notification to %s:%s", platform_str, chat_id)
            return str(platform_str), str(chat_id), str(thread_id) if thread_id else None
        except Exception as e:
            logger.warning("Restart notification failed: %s", e)
            return None
        finally:
            notify_path.unlink(missing_ok=True)

    def _home_channel_transports(self):
        """Yield ``(platform, platform_cfg, home, transport)`` for every home channel with a live transport."""
        for platform, platform_cfg in self.config.platforms.items():
            home = platform_cfg.home_channel
            if not home or not home.chat_id:
                continue
            transport = _safe_delivery_transport(platform, self.config, self.adapters)
            if transport is None:
                continue
            yield platform, platform_cfg, home, transport

    def _served_home_channel_configs(self):
        """``(profile, platform, platform_cfg)`` for every SERVED profile's configured home channel.

        ``self.config`` is the launch profile's alone, but one host process multiplexes every
        profile, so a host-wide notice built from it silently skips the others' channels. The
        secondary configs are the ones ``_load_secondary_profile_config`` already cached at
        adapter start; ``profile`` is ``None`` for the launch profile.
        """
        for platform, platform_cfg in self.config.platforms.items():
            yield None, platform, platform_cfg
        for profile, profile_cfg in (getattr(self, "_profile_configs", None) or {}).items():
            for platform, platform_cfg in profile_cfg.platforms.items():
                yield profile, platform, platform_cfg

    def _served_home_channel_transports(self):
        """``(profile, platform, platform_cfg, home, transport)`` for every served profile's home
        channel with a live transport — the launch profile's (``profile`` ``None``) first."""
        for platform, platform_cfg, home, transport in self._home_channel_transports():
            yield None, platform, platform_cfg, home, transport
        for profile, profile_cfg in (getattr(self, "_profile_configs", None) or {}).items():
            adapters = (getattr(self, "_profile_adapters", None) or {}).get(profile) or {}
            for platform, platform_cfg in profile_cfg.platforms.items():
                home = platform_cfg.home_channel
                if not home or not home.chat_id:
                    continue
                transport = _safe_delivery_transport(platform, profile_cfg, adapters, profile=profile)
                if transport is None:
                    continue
                yield profile, platform, platform_cfg, home, transport

    async def _send_home_channel_message(self, platform, home, transport, message: str, failure_fmt: str) -> bool:
        """Best-effort send to one home channel; True on success, failures logged with ``failure_fmt``."""
        from gateway.run import _non_conversational_metadata
        try:
            metadata = self._thread_metadata_for_target(platform, home.chat_id, home.thread_id, adapter=transport.adapter)
            if transport.is_relay:
                metadata = dict(metadata or {})
                if home.user_id:
                    metadata["user_id"] = home.user_id
                if home.scope_id:
                    metadata["scope_id"] = home.scope_id
            send_metadata = notice_metadata(_non_conversational_metadata(metadata, platform=platform),
                                            platform=platform, style=getattr(message, "notice_style", None))
            if send_metadata is not None or transport.is_relay:
                result = await transport.send(platform, str(home.chat_id), message, metadata=send_metadata)
            else:
                result = await transport.adapter.send(str(home.chat_id), message)
            if _send_failed(result):
                logger.warning(failure_fmt, platform.value, home.chat_id, _send_error(result))
                return False
            return True
        except Exception as exc:
            logger.warning(failure_fmt, platform.value, home.chat_id, exc)
            return False

    _planned_restart_notice_lock: Optional[asyncio.Lock] = None

    async def _replay_pending_planned_restart_notification(self) -> None:
        """Send the planned-restart online notice to every home channel still owed one; clear
        ``.restart_pending.json`` only once all of them were reached.

        Runs from the boot pass and again from ``_install_reconnected_adapter``, so a home whose
        platform was down at boot gets its notice when the platform comes back (#112109). Delivered
        targets are recorded in the marker so neither a later replay nor the next process (if this
        one restarts first) notifies a home twice. The lock serializes a boot pass that outlived the
        restore gate against a concurrent reconnect replay.
        """
        from gateway.run import _planned_restart_notification_path
        from utils import atomic_json_write

        if self._planned_restart_notice_lock is None:
            self._planned_restart_notice_lock = asyncio.Lock()
        async with self._planned_restart_notice_lock:
            path = _planned_restart_notification_path()
            if not path.exists():
                return
            try:
                data = json.loads(path.read_text(encoding="utf-8-sig"))
                delivered = {tuple(target) for target in data.get("delivered_targets", [])}
                # Owed targets come from config, not live transports: a removed home or an opt-out
                # (gateway_restart_notification=false) must not keep the marker alive forever.
                owed = {
                    _served_notice_target_key(
                        profile, platform.value, cfg.home_channel.chat_id, cfg.home_channel.thread_id)
                    for profile, platform, cfg in self._served_home_channel_configs()
                    if cfg.home_channel and cfg.home_channel.chat_id and cfg.gateway_restart_notification
                }
                delivered |= await self._send_home_channel_startup_notifications(skip_targets=delivered)
                if owed <= delivered:
                    path.unlink(missing_ok=True)
                    return
                data["delivered_targets"] = [list(target) for target in delivered]
                atomic_json_write(path, data, indent=None)
            except Exception:
                logger.warning("Planned-restart notification remains pending", exc_info=True)

    async def _send_home_channel_startup_notifications(
        self, *, skip_targets: Optional[set[tuple[str, str, Optional[str]]]] = None
    ) -> set[tuple[str, str, Optional[str]]]:
        """Notify EVERY served profile's configured home channels that the gateway is back online.

        Best-effort, once per home CHAT — several served profiles can share one chat (a single
        Telegram group for the whole host), and one host process restarting once owes that chat
        one notice. Accounting stays per profile so the marker's owed set still discharges.
        ``skip_targets`` lets startup avoid duplicate messages when a more specific restart
        notification is queued for the same chat.
        """
        delivered: set[tuple[str, str, Optional[str]]] = set()
        skipped = skip_targets or set()
        message = ContextNotice(t("gateway.startup.online"))
        targets = list(self._served_home_channel_transports())
        # A chat already notified for ANOTHER profile is not notified again.
        notified_chats = {
            _delivery_target_key(platform.value, home.chat_id, home.thread_id, profile=profile)
            for profile, platform, _cfg, home, _transport in targets
            if _served_notice_target_key(profile, platform.value, home.chat_id, home.thread_id) in skipped
        }
        for profile, platform, platform_cfg, home, transport in targets:
            if not platform_cfg.gateway_restart_notification:
                logger.info(
                    "Home-channel startup notification suppressed: %s has gateway_restart_notification=false",
                    platform.value,
                )
                continue
            target = _served_notice_target_key(profile, platform.value, home.chat_id, home.thread_id)
            if target in skipped or target in delivered:
                continue
            chat = _delivery_target_key(platform.value, home.chat_id, home.thread_id, profile=profile)
            if chat in notified_chats:
                delivered.add(target)
                continue
            if await self._send_home_channel_message(
                platform, home, transport, message, "Home-channel startup notification failed for %s:%s: %s",
            ):
                notified_chats.add(chat)
                delivered.add(target)
                logger.info("Sent home-channel startup notification to %s:%s", platform.value, home.chat_id)
        return delivered

    async def _send_session_db_warning_notifications(self) -> None:
        """Broadcast a state.db failure warning to all home channels.

        When SessionDB init fails at gateway startup, messages may flow but nothing is persisted
        — /resume, /history, and session_search all silently break. Best-effort: failures are
        logged, not raised.

        See #88235.
        """
        error = getattr(self, "_session_db_init_error", None)
        if not error:
            return
        # Re-check the live store before warning: a startup `database is locked` routinely clears while
        # the adapters are still connecting, and a borrowed store handle comes back once its owner
        # releases it. The cache's opener clears ``_session_db_init_error`` on recovery, so a stale
        # startup failure must not be broadcast as current (#108031).
        if getattr(self, "_session_db_handle_cache", None) is not None:
            await asyncio.to_thread(self._open_session_db_for_active_scope)
            error = self._session_db_init_error
            if not error:
                logger.info("state.db recovered before the home-channel warning went out; not broadcasting")
                return
        from hermes_constants import get_default_hermes_root, profile_cli_selector
        from hermes_state import _default_db_path, classify_persistence_error
        cause = classify_persistence_error(error)
        # Copy-pasteable, so name the real store and pin the profile: a bare `hermes` follows
        # active_profile, which may be a different database (#105887).
        profile_arg = profile_cli_selector()
        if cause == "corrupt":
            db_path = _default_db_path()
            backups_dir = get_default_hermes_root() / "backups"
            message = t("gateway.startup.db_corrupt", profile_arg=profile_arg, db_path=db_path, backups_dir=backups_dir)
        elif cause == "fts_index":
            # Index-scoped corruption: the message tables are not damaged, so the recover /
            # restore advice above would be destructive on a healthy file (#97794).
            message = t("gateway.startup.db_fts_corrupt", profile_arg=profile_arg)
        else:
            from hermes_state_user_copy import describe_storage_failure
            failure = describe_storage_failure(error)
            # The cause table owns the remedy: for a held retired-WAL generation a bare `doctor --fix`
            # is the second-writer trap this notice used to send users into (#110054). Its copy is
            # user-phrased, so a store-level failure still gets the operator tail — this gateway
            # opened its store at startup and stays broken until it is restarted.
            action = failure.action
            if failure.cause not in _SELF_CLEARING_STORAGE_CAUSES:
                action = t("gateway.startup.db_then_restart", action=action, profile_arg=profile_arg)
            message = t("gateway.startup.db_unavailable", cause=failure.gloss, action=action)
        logger.warning("Broadcasting state.db failure warning to home channels: %s", error)
        from gateway.warning_notifications import present_notification
        for platform, _platform_cfg, home, transport in self._home_channel_transports():
            await present_notification(
                lambda: self._send_home_channel_message(
                    platform, home, transport, message, "state.db warning notification failed for %s:%s: %s"),
                platform=platform)

    async def _drain_watch_notifications(self, completion_queue) -> None:
        """Consume watches in their owning profile, backing off refusals even during drain."""
        from gateway.run import _drain_gateway_watch_events, _format_gateway_process_notification
        watch_events = _coalesce_pending_watch_matches(_drain_gateway_watch_events(completion_queue))
        for evt in watch_events:
            next_retry_at = evt.get("_watch_next_retry_at")
            draining = getattr(self, "_draining", False)
            drain_offered = evt.pop("_watch_drain_offered", False)
            if draining:
                evt["_watch_drain_offered"] = True
            # Offer a held event once per drain for spooling, not on every refused tick.
            if (next_retry_at is not None and next_retry_at > time.monotonic()
                    and (not draining or drain_offered)):
                # Backoff between refused injections: hold the event for a later drain tick
                # instead of re-attempting admission every 2 s (#140899).
                completion_queue.put(evt)
                continue
            async with self._completion_event_scope(evt):
                from tools.process_registry_notifications import should_surface_notification
                if not should_surface_notification(evt):
                    continue
                if self._load_background_notifications_mode() == "off":
                    continue
                if evt.get("type") == "heartbeat":
                    from gateway.session_automation import deliver_heartbeat
                    delivered = await deliver_heartbeat(self, evt)
                else:
                    synth_text = _format_gateway_process_notification(evt)
                    if not synth_text:
                        continue
                    try:
                        delivered = await self._inject_watch_notification(synth_text, evt)
                    except Exception:
                        logger.exception("Watch notification injection error")
                        delivered = False
            # The original event: a requeued tick folds again from what is still queued.
            if delivered is False:
                self._requeue_refused_watch_event(completion_queue, evt)

    @staticmethod
    def _requeue_refused_watch_event(completion_queue, evt: dict) -> None:
        """Retry a refused watch event on a bounded backoff, never on every 2 s tick (#140899).

        Only a refusal proven permanent (``WakeNotAccepted.permanent``: the route has no session,
        or the payload is invalid) counts toward the cap that drops it. A transient one (a restart's
        after-turn wait, a parked profile, a full owner FIFO) or one whose owner may still appear (an
        unowned or foreign-pinned notice) waits it out however long it lasts.
        """
        step = int(evt.get("_watch_retry_step") or 0) + 1
        evt["_watch_retry_step"] = step
        if evt.pop("_refusal_permanent", False):
            refusals = int(evt.get("_watch_injection_attempts") or 0) + 1
            evt["_watch_injection_attempts"] = refusals
            if refusals >= _MAX_COMPLETION_INJECTION_ATTEMPTS:
                logger.warning(
                    "Watch notification for %s chat=%s (type=%s session_id=%s) was refused "
                    "%d times; dropping the queued event (output remains available via "
                    "process(action='log'))",
                    evt.get("platform") or "?", evt.get("chat_id") or "?",
                    evt.get("type") or "?", evt.get("session_id", "unknown"), refusals,
                )
                return
        evt["_watch_next_retry_at"] = _refusal_retry_at(step)
        completion_queue.put(evt)

    def _adapter_by_platform_value(self, platform_name: str):
        """Literal ``p.value == platform_name`` scan over connected adapters (native adapters only)."""
        for p, a in self.adapters.items():
            if p.value == platform_name:
                return a
        return None

    async def _self_post_api_server(self, adapter, synth_text: str, raw_sid: str, evt: dict) -> bool:
        """Deliver to a non-push (api_server) session by raw session id.

        Async-delegation completions are persisted as a durable delivery row — after the parent
        turn's event.complete the CLIENT owns the next turn on this stateless surface, so never
        self-post them as a new role=user prompt. Other watch events wake the session via self-post.
        """
        from gateway.wake import deliver_wake, persist_delegation_delivery
        scope = contextlib.nullcontext()
        if evt.get("type") == "async_delegation":
            info = "Async delegation completion — persisting delivery row for api_server session %s (no wake turn)"
            fail = "Async delegation delivery persist failed for session %s: %s"
            # This route is persist-only: the client owns the next model turn, so
            # keep the durable row's content exactly as produced.
            deliver = lambda: persist_delegation_delivery(adapter, text=synth_text, session_id=raw_sid, evt=evt)
        else:
            info = "Watch pattern notification — waking api_server session %s via self-post"
            fail = "Watch notification self-post wake failed for session %s: %s"
            from agent.notification_presentation import diagnostic_process_event
            try:
                served = await asyncio.to_thread(self._served_api_server_wake_profile, evt, raw_sid)
            except LookupError as e:
                logger.warning(fail, raw_sid, e)
                return False
            if served:
                # The wake runs in the OWNING profile's scope, in-process (see ``deliver_wake``):
                # the raw event carries no profile, so a completion scope was never installed.
                from gateway.run import _async_profile_runtime_scope
                source = SessionSource(platform=Platform.API_SERVER, chat_id=raw_sid, profile=served)
                scope = _async_profile_runtime_scope(self._resolve_profile_home_for_source(source))
            deliver = lambda: deliver_wake(adapter, text=_mark_internal_notification(synth_text), session_id=raw_sid, profile=served,
                notification_category="diagnostic" if diagnostic_process_event(evt) else "result")
        try:
            logger.info(info, raw_sid)
            async with scope:
                await deliver()
            return True
        except Exception as e:
            logger.warning(fail, raw_sid, e)
            return False

    def _served_api_server_wake_profile(self, evt: dict, raw_sid: str) -> Optional[str]:
        """The served (non-primary) profile whose own session store holds *raw_sid*, else ``None``
        (the default profile's HTTP self-post). Blocking: reads served ``state.db`` files.

        A served profile's ``api_server`` turn binds the RAW session id as its session key, so its
        completion event names no profile: the only ownership proof is the served profile's own
        store, exactly the rung the Kanban notifier applies. An event whose source DOES name a
        served profile (structured key / persisted origin) must be owned by that profile or it
        raises ``LookupError`` — never a wake in the default profile's store.
        """
        if not getattr(self.config, "multiplex_profiles", False):
            return None
        from gateway.run import _multiplex_profile_homes
        from gateway.wake import session_owned_by_profile
        primary = getattr(self, "_primary_profile_name", None) or "default"
        hinted = str(getattr(self._build_process_event_source(evt), "profile", None) or "").strip()
        if hinted and hinted != primary:
            if session_owned_by_profile(self.config, hinted, raw_sid):
                return hinted
            raise LookupError(f"session is not in served profile {hinted!r}'s own store")
        return next((name for name, _home in _multiplex_profile_homes(self.config)
                     if name != primary and session_owned_by_profile(self.config, name, raw_sid)), None)

    def _resolve_injection_adapter(self, platform_name: str, source=None):
        """Adapter for a synthetic-event platform: alias-aware transport resolver first (one
        Platform.RELAY adapter fronts N logical platforms; native wins), literal ``p.value`` scan as
        fallback for minimal runner stubs / exotic platform strings when the resolver can't run."""
        from gateway.delivery import resolve_delivery_transport
        from gateway.session_detached import detached_route_of
        if detached_route_of(source):  # an old conversation's app viewers (gateway/session_detached.py)
            return self._delivery_adapter_for(source)
        if source is not None:
            owner = self._transport_owner(source)
            if owner is not None:
                return owner[0]
            if getattr(source, "delivered_via_upstream_relay", False) is True:
                return self.adapters.get(Platform.RELAY)
        # A pinned identity names the bot that received the conversation: it answers, as
        # ``_delivery_adapter_for`` rules (a ``bot_profile`` route's runtime may own no adapter for this
        # platform, #139726). Otherwise one resolver with authz/kanban/cron: a secondary's own map, or the
        # primary's for a shared-bot satellite; a disconnected bot fails closed to ``{}``.
        from gateway.session_identity import identity_of
        identity = identity_of(source)
        pinned = (identity is not None and identity.multiplexed and not identity.transport_inferred
                  and self._restored_transport_trusted(source, identity))
        adapters = self._adapters_for_profile(
            identity.transport_profile if pinned else getattr(source, "profile", None))
        try:
            _transport = resolve_delivery_transport(Platform(platform_name), self.config, adapters)
        except Exception:
            _transport = None
        if _transport is not None:
            return _transport.adapter
        if not pinned and source is not None and (borrowed := self._routed_primary_adapter(source)) is not None:
            return borrowed
        if source is not None and (relay := self._committed_relay_adapter(source)) is not None:
            return relay
        return next((a for p, a in adapters.items() if p.value == platform_name), None)

    def _committed_relay_adapter(self, source):
        """The relay a restored *source* was admitted through, proven by its route's last committed
        admission. Persisted origins omit relay trust, so a connector-stamped profile with no local
        route to borrow the primary relay by otherwise finds no adapter after a restart."""
        from gateway.session_authorities import active_authority
        from gateway.session_automation import committed_route_envelope
        from gateway.session_envelope import restore_native
        from hermes_state_runtime import RuntimeStoreError
        authority = active_authority(self)
        if authority is None:
            return None
        entry = self.session_store.lookup_by_session_key(self.session_store._generate_session_key(source))
        envelope = committed_route_envelope(authority, entry) if entry is not None else None
        if envelope is None or 'relay' not in envelope['provenance']:
            return None
        # The seal is rechecked against the live connector, as ``snapshot_automation`` does.
        try:
            restored = restore_native({'text': '', 'native_text_v1': {
                key: value for key, value in envelope.items() if key != 'media'}}, self)
        except RuntimeStoreError:
            return None
        return self._adapter_for_source(restored.source)

    def _bind_registered_local_source(self, adapter, source):
        """The live object local admission will accept, or None.

        ``authorize_source`` requires the registered instance, not an equal
        copy. Rebind only when the reconstructed identity matches that live
        source, so a foreign chat cannot borrow it.
        """
        from gateway.session_local import LocalSessionAdapter
        if not isinstance(adapter, LocalSessionAdapter) or source is None:
            return None
        saved = adapter.sources.get(getattr(source, "chat_id", None))
        if not saved:
            return None
        original, identity = saved
        if adapter._identity(source) != identity:
            return None
        if not any(getattr(live, "source", None) is original
                   for live in adapter.authority.sessions.values()):
            return None
        return original

    def _platform_adapter_for_unproven_relay(self, platform_name: str, source, evt: dict):
        """Platform adapter for a relay-flagged pre-ledger route, else None.

        A legacy delegation with no committed envelope reaches a permanent
        refusal, not a silent retry. An envelope-backed relay route keeps waiting.
        """
        from copy import copy

        probe = copy(source)
        probe.delivered_via_upstream_relay = False
        platform_adapter = self._resolve_injection_adapter(platform_name, probe)
        if platform_adapter is None:
            return None
        from gateway.session_authorities import active_authority
        authority = active_authority(self)
        session_key = str(evt.get("session_key") or "").strip()
        if authority is None or not session_key:
            return None
        try:
            self.session_store._ensure_loaded()
            entry = self.session_store._entries.get(session_key)
        except Exception:
            return None
        if entry is None:
            return None
        from gateway.session_automation import committed_route_envelope
        if committed_route_envelope(authority, entry) is not None:
            return None
        return platform_adapter

    async def _inject_watch_notification(
        self, synth_text: str, evt: dict, *, raise_not_accepted: bool = False,
    ) -> Optional[bool]:
        """Inject a watch/completion notification as a synthetic message event.

        Routing comes from the queued event, never the active foreground message. Returns
        ``True`` on adapter acceptance, ``False`` on retryable adapter failure, ``None`` with no
        gateway route or a refused notice pinned to a permanently-gone session (``_watch_pin_gone``).
        Not transactional: a crash after acceptance can replay (at-least-once).
        """
        from gateway.session_detached import follow_detached_pin
        from gateway.session_local_recovery import restore_completion_route
        from gateway.wake import WakeNotAccepted, adapter_supports_push, admit_internal_event
        await restore_completion_route(self, str(evt.get("session_key") or ""))  # watch notices and heartbeats too
        if self._completion_held_by_stop(evt):
            # Retryable, not refused: the event stays queued until the user's next turn.
            if raise_not_accepted:
                raise WakeNotAccepted("session stopped by the user; holding the wake")
            return False
        queued, evt = evt, await follow_detached_pin(self, evt)  # the drain reads its verdict off *queued*
        source = await asyncio.to_thread(self._build_process_event_source, evt)
        if not source:
            # API-server sessions bind the RAW X-Hermes-Session-Id key, not a structured ``agent:...`` key.
            raw_sid = _raw_process_event_session_id(evt)
            if raw_sid:
                profile = str(evt.get("profile") or "").strip() or None
                adapter = self._adapters_for_profile(profile).get(Platform.API_SERVER)
                if adapter is not None and not adapter_supports_push(adapter):
                    return await self._self_post_api_server(adapter, synth_text, raw_sid, evt)
                logger.debug(
                    "Deferring watch notification for raw session %s: no api_server adapter to self-post through",
                    raw_sid,
                )
                return False
            logger.warning(
                "Dropping watch notification with no routing metadata for process %s",
                evt.get("session_id", "unknown"),
            )
            return None
        platform_name = source.platform.value if hasattr(source.platform, "value") else str(source.platform)
        adapter = self._resolve_injection_adapter(platform_name, source)
        if not adapter and platform_name == Platform.API_SERVER.value and getattr(source, "profile", None):
            # A route-only served profile owns no adapter map; the shared listener wakes exactly the
            # session that profile's store owns (proven in ``_self_post_api_server``), fail-closed.
            adapter = self.adapters.get(Platform.API_SERVER)
        if not adapter and getattr(source, "delivered_via_upstream_relay", False) is True:
            # A persisted relay flag is not a connector. A pre-ledger route must
            # still reach admission so the unproven grant is permanently refused.
            # A committed envelope waits for the relay adapter.
            adapter = self._platform_adapter_for_unproven_relay(platform_name, source, evt)
        if not adapter:
            return False
        if not adapter_supports_push(adapter):
            # Non-push adapter (api_server): its chat_id IS the raw session id, so handle_message would
            # key the wake under a build_session_key() that never matches — self-post instead.
            raw_sid = str(evt.get("origin_session_id") or "").strip() or str(source.chat_id or "")
            return await self._self_post_api_server(adapter, synth_text, raw_sid, evt)
        try:
            synth_text = _mark_internal_notification(synth_text)
            session_key = str(evt.get("session_key") or "").strip()
            metadata = {} if platform_name == Platform.SLACK.value else _watch_notification_metadata(evt, source)
            from agent.notification_presentation import diagnostic_process_event
            if diagnostic_process_event(evt):
                metadata["notification_category"] = "diagnostic"
            if session_key.startswith("agent:"):
                metadata["gateway_session_key"] = session_key
            parent_session_id = str(evt.get("parent_session_id") or "").strip()
            if parent_session_id:
                metadata["gateway_session_id"] = parent_session_id
                # Compressed routes keep the admission FIFO on the logical root, even though the
                # transcript and event provenance point at the child tip.
                authority = getattr(self, "session_authority", None)
                if authority is not None:
                    metadata["gateway_session_id"] = authority.logical_owner(parent_session_id)
            proven_session_id = str(evt.get("proven_session_id") or "").strip()
            if proven_session_id:
                metadata["gateway_proven_session_id"] = proven_session_id
            if completion_ids := evt.get("completion_ids"):
                metadata["gateway_completion_ids"] = list(completion_ids)
            if retry_claim := str(evt.get("retry_notice_claim") or "").strip():
                # Lets every later wake fence drop this notice once its retry is cancelled.
                from hermes_cli.session_controls import RETRY_WAKE_METADATA_KEY
                metadata[RETRY_WAKE_METADATA_KEY] = {
                    "delegation_id": str(evt.get("delegation_id") or ""), "claim": retry_claim}
            # The queued event's ``message_id`` is the message that STARTED the process, and the
            # persisted origin carries the same stale id. A synthetic completion is not a reply to
            # it: by delivery time the user has often continued elsewhere, and an event anchored
            # there makes the finished job's reply quote that old message on every reply-anchoring
            # platform (#52694: a background completion visibly replying to a stale Discord DM
            # message). Routing is unaffected — topic lanes carry thread_id and the anchor-less
            # synthetic-send branches are covered (#87051); the original id rides metadata for
            # debugging only.
            trigger_message_id = str(evt.get("message_id") or "").strip() or None
            if trigger_message_id:
                metadata["original_trigger_message_id"] = trigger_message_id
            # Local admission accepts only the registered live SessionSource.
            # A rebuilt origin, or clearing its stale reply anchor, is a
            # different object: snapshot_local_automation then raises
            # permission_denied and the completion is logged as an internal
            # refusal, so the chat never starts the notification turn.
            bound = self._bind_registered_local_source(adapter, source)
            if bound is not None:
                source = bound
            elif getattr(source, "message_id", None):
                from gateway.session_identity import replace_source
                source = replace_source(source, message_id=None)
            synth_event = MessageEvent(
                text=synth_text, message_type=MessageType.TEXT, source=source, internal=True,
                metadata=metadata,
            )
            logger.info(
                "Watch pattern notification — injecting for %s chat=%s thread=%s",
                platform_name, source.chat_id, source.thread_id,
            )
            # Relay egress priming: post-restart routing caches are cold (they warm only on inbound), so
            # replies would egress without tenant discriminators and be declined by the connector.
            _prime = getattr(adapter, "prime_routing_cache", None)
            if callable(_prime):
                _prime(synth_event)
            from gateway.session_authorities import active_authority
            authority = active_authority(self)
            if getattr(self, "_draining", False) and authority is None:
                # Heartbeats have their durable producer queue as the retry owner. Do not move a
                # refused replacement into the adapter pending slot during teardown.
                if evt.get("type") == "heartbeat":
                    return False
                # A wake started now would race adapter teardown. A delegation result already has
                # a durable ledger row: refuse it so the claim is deferred and the row stays
                # pending for the next boot's /new-aware replay. A process completion has no other
                # copy, so it goes to the drain spool the handler would use anyway.
                if evt.get("type") == "async_delegation":
                    if raise_not_accepted:
                        raise WakeNotAccepted("gateway draining; delegation result stays pending")
                    return False
                return self._preserve_drain_event(self._session_key_for_source(source), synth_event)
            if authority is not None:
                from gateway.session_automation import admit_process_notice
                from hermes_state_runtime import RuntimeStoreError
                try:
                    await admit_process_notice(self, authority, adapter, synth_event, evt)
                except RuntimeStoreError as exc:
                    raise WakeNotAccepted(str(exc), reason=exc.reason) from exc
            else:
                await admit_internal_event(adapter, synth_event)
            return True
        except WakeNotAccepted as exc:
            # Permanent only on proof: a pin the authority's state shows gone (/new, #141562) or a
            # final code. An unowned or foreign-pinned refusal may still find its owner (d6635ffe03b).
            from gateway.wake import PERMANENT_REFUSAL_REASONS
            pin_gone = await self._watch_pin_gone(evt, str(exc), log=not raise_not_accepted)
            exc.permanent = pin_gone or exc.reason in PERMANENT_REFUSAL_REASONS
            if pin_gone and not raise_not_accepted:
                return None
            if not raise_not_accepted:
                queued["_refusal_permanent"] = exc.permanent  # the drain counts only permanent refusals
            from gateway.session_refusals import log_internal_refusal
            cause = exc.__cause__
            log_internal_refusal(self, source, exc.reason or str(exc), getattr(cause, 'field', None))
            # Durable callers refund the claim; ordinary watch callers just requeue.
            if raise_not_accepted:
                raise
            return False
        except Exception as e:
            logger.error("Watch notification injection error: %s", e)
            return False

    # Ledger transitions write state.db: run them off the loop (see ``gateway.run_notifications_ledger``).
    _settle_durable_claim = staticmethod(settle_durable_claim)

    @staticmethod
    def _back_off_refused_completions(group: list[dict]) -> None:
        """Space out an undelivered async group's retries instead of re-offering it every 2 s pass.
        The backoff only paces; the durable ledger decides when a row gives up (a permanent refusal
        spends a delivery attempt there, so the count survives restarts) (#141153)."""
        for evt in group:
            step = int(evt.get("_watch_retry_step") or 0) + 1
            evt["_watch_retry_step"] = step
            evt["_watch_next_retry_at"] = _refusal_retry_at(step)

    async def _launching_turn_active(self, platform_name: str, watcher: dict) -> bool:
        """Whether the session that launched *watcher*'s process is still inside a turn on its
        adapter (``_active_sessions`` is the base adapter's busy guard)."""
        session_key = str(watcher.get("session_key") or "").strip()
        if not session_key:
            return False
        source = await asyncio.to_thread(self._build_process_event_source, watcher)
        adapter = self._resolve_injection_adapter(platform_name, source)
        return session_key in (getattr(adapter, "_active_sessions", None) or {})

    async def _send_watcher_message(self, platform_name: str, chat_id, thread_id, message_text: str, watcher: dict) -> bool:
        """True = accepted or unknown receipt. False = not delivered: a raised send, or an
        adapter/route that is absent right now (reconnecting, failed platform). False stays retryable."""
        from gateway.run import _non_conversational_metadata
        source = await asyncio.to_thread(self._build_process_event_source, watcher)
        adapter = self._resolve_injection_adapter(platform_name, source)
        if adapter and chat_id:
            with _log_suppressed(logging.ERROR, "Watcher delivery error: %s"):
                send_meta = {"thread_id": thread_id} if thread_id else None
                result = await adapter.send(
                    chat_id, message_text, metadata=_non_conversational_metadata(send_meta, platform=platform_name),
                )
                # Some adapters send successfully but return no receipt. Unknown is not
                # a known failure: replaying it could duplicate an already delivered message.
                return result is None or bool(result.success)
        return False

    @staticmethod
    def _build_process_completion_event(watcher: dict, session, session_id: str) -> dict:
        """Build the synthetic ``completion`` event for an agent-notify watcher."""
        from gateway.run import _redact_gateway_user_facing_secrets
        from agent.redact import redact_terminal_output
        from tools.ansi_strip import strip_ansi
        from tools.process_registry import transform_process_output
        _command = getattr(session, "command", "") or ""
        _raw = strip_ansi(session.output_buffer) if session.output_buffer else ""
        _raw = transform_process_output(_raw, command=_command, returncode=session.exit_code,
                                        task_id=getattr(session, "task_id", "") or "") if _raw else _raw
        _raw = redact_terminal_output(_raw, _command)
        # Keep the last ~2000 chars snapped to a line boundary, with a marker when cut.
        _LIMIT = 2000
        # Truncate at line boundaries so notifications never start mid-line (fixes #23284). Keep the last
        # ~2000 chars but snap to the nearest preceding newline, then prepend a truncation marker when
        # output was cut.
        if len(_raw) > _LIMIT:
            _tail = _raw[-_LIMIT:]
            _nl = _tail.find("\n")
            _tail = _tail[_nl + 1:] if _nl != -1 else _tail
            _out = f"[… output truncated — showing last {len(_tail)} chars]\n{_tail}"
        else:
            _out = _raw
        return {
            "type": "completion",
            "session_id": session_id,
            **{k: watcher.get(k, "") for k in _WATCHER_ROUTE_FIELDS},
            "owner_task_id": getattr(session, "owner_task_id", "") or "",
            "task_id": getattr(session, "task_id", "") or "",
            "message_id": str(watcher.get("message_id") or "").strip() or None,
            "started_at": getattr(session, "started_at", None),
            "command": _redact_gateway_user_facing_secrets(_command),
            "exit_code": session.exit_code,
            "completion_reason": getattr(session, "completion_reason", "exited"),
            "termination_source": getattr(session, "termination_source", ""),
            "output": _redact_gateway_user_facing_secrets(_out),
            "handoff_note": _redact_gateway_user_facing_secrets(getattr(session, "handoff_note", "") or ""),
            # Spawning session-db id: lets pre-flight drop this completion if the user /new'd first.
            "parent_session_id": (
                watcher.get("parent_session_id") or getattr(session, "parent_session_id", "") or ""
            ),
        }

    def _format_process_final_message(self, session_id: str, session, notify_mode: str) -> str:
        """Human-facing completion message. Every mode shares the one-line status header; the
        raw-output modes (all/result/error) append the bounded output tail under it instead of the
        old bracketed ``[Background process proc_… finished~ …]`` debug wrapper (#54266)."""
        from gateway.run import _format_concise_process_notification, _redact_gateway_user_facing_secrets
        new_output = self._redacted_output_tail(session, 1000)
        _started = getattr(session, "started_at", None)
        _dur = max(0.0, time.time() - _started) if isinstance(_started, (int, float)) else None
        command = _redact_gateway_user_facing_secrets(getattr(session, "command", "") or "")
        provenance = {"completion_reason": getattr(session, "completion_reason", ""),
                      "termination_source": getattr(session, "termination_source", "")}
        if notify_mode == "concise":
            return _format_concise_process_notification(session_id, command, session.exit_code, new_output,
                                                        duration_seconds=_dur, **provenance)
        header = _format_concise_process_notification(session_id, command, session.exit_code, "",
                                                      duration_seconds=_dur, **provenance)
        return t("gateway.background.final_output", header=header, output=new_output.strip()) if new_output.strip() else header

    def _format_process_running_message(self, session) -> str:
        from gateway.run import _redact_gateway_user_facing_secrets, _shorten_command_for_display
        new_output = self._redacted_output_tail(session, 500)
        short_cmd = _shorten_command_for_display(_redact_gateway_user_facing_secrets(getattr(session, "command", "") or ""))
        header = t("gateway.background.still_running") + (f" — `{short_cmd}`" if short_cmd else "")
        return t("gateway.background.recent_output", header=header, output=new_output.strip()) if new_output.strip() else header

    def arm_process_watcher(self, watcher: dict) -> bool:
        """Start ``_run_process_watcher`` for a watcher registered mid-turn, from the agent's
        tool thread. Waiting for the post-turn drain leaves a process that finishes while its
        launching turn is still running with no watcher at all (#112033). False = the gateway
        is not serving (startup, shutdown): the caller keeps the descriptor in
        ``pending_watchers`` for the startup / post-turn drain.

        The watcher starts in an empty context, as a recovered watcher does: scheduling copies the
        CALLING thread's context, and the turns its completion admits inherit the watcher's. Armed
        from a delegate child, a process handed to the parent completed into a parent turn that ran
        as that child (its own notify watchers downgraded, sudo refused, the board fenced). Delivery
        binds the owning profile's scope itself (``_completion_event_scope``)."""
        loop = getattr(self, "_gateway_loop", None)
        if not getattr(self, "_running", False) or loop is None or not loop.is_running():
            return False
        from agent.async_utils import safe_schedule_threadsafe
        # Arm in a FRESH context, like a startup-recovered watcher. Scheduling captures the
        # caller's contextvars, and the caller can be a delegate_task child's tool thread: the
        # watcher, and the completion turn it injects into the parent, would then inherit the
        # child's delegated-child marker and session id. The parent's turns then read as a
        # subagent's (parent-only tools refused, background processes tagged subagent-owned).
        # The watcher already resolves the owning profile's scope itself (_completion_event_scope).
        future = contextvars.Context().run(
            safe_schedule_threadsafe, self._run_process_watcher(watcher), loop, logger=logger,
            log_message="Live process watcher arming failed",
        )
        return future is not None

    async def _run_process_watcher(self, watcher: dict) -> None:
        """Run :meth:`_watch_process` inside the owning profile's scope, start to finish. A watcher starts
        in an empty context (live-armed from a tool thread, or recovered at startup), and the output it
        prepares runs that profile's own ``transform_terminal_output`` hooks before any delivery."""
        async with self._completion_event_scope(watcher):
            await self._watch_process(watcher)

    async def _process_completion_committed(self, completion_evt: dict, session_id: str) -> bool:
        """Recheck a possibly lost admission ACK before falling back to a plain notice."""
        if (getattr(self, 'session_authority', None) is not None
                and self._completion_delivery_identity(completion_evt) is not None):
            from gateway.session_automation import completion_admission
            try:
                # The owning profile's authority, as on the delivery path.
                async with self._completion_event_scope(completion_evt):
                    committed = await asyncio.to_thread(completion_admission, self, completion_evt) is not None
            except Exception:
                # Failing open sends the notice; a dead watcher would send nothing.
                logger.debug("Process watcher: admission recheck failed for %s", session_id,
                             exc_info=True)
                committed = False
            return committed
        return False

    async def _watch_process(self, watcher: dict) -> None:
        """Poll a background process and push updates until it exits. Mode
        (``display.background_process_notifications``): concise (default one-liner; failures append
        the output tail) / all (running updates + final raw) / result (final raw) / error (final raw
        if exit != 0) / off."""
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
                        acknowledge_survivor_notifications(process_registry, ((completion_evt, synth_text),))
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
                        acknowledge_survivor_notifications(process_registry, ((completion_evt, synth_text),))
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
