# health: allow FILE_LINES -- the wake admission additions share this facade until the existing inbound mixin extraction is completed.
# health: allow BLE001 -- command-hook observers are third-party boundaries; failures are logged and must not block control commands.
# health: allow NESTING -- this dispatch ladder preserves the ordered auth, drain, quick-command, plugin, and adapter fences.
# health: allow CC -- this admission boundary composes the ordered session, wake, and outbox fences and must remain atomic.
"""Inbound message pipeline (_handle_message, text/media preparation, durable-turn markers, plugin injection) for GatewayRunner.

Split out of ``gateway/run.py``; bound onto ``GatewayRunner`` via the MRO.
``gateway.run`` internals are imported lazily inside method bodies (import cycle),
so ``patch("gateway.run.X")`` keeps intercepting them at call time.
"""

from __future__ import annotations

import logging
import asyncio
import inspect
import concurrent.futures
import dataclasses
import json
import os
import re
import time
from contextlib import suppress
from pathlib import Path

from agent.i18n import t
from gateway.config import Platform
from gateway.cron_review import is_review_event
from gateway.platforms.base import EphemeralReply, as_command_reply
from gateway.platforms.event import MessageEvent, MessageType, turn_origin_for_event
from gateway.platforms.typing_state import resume_typing_after_prompt
from gateway.run_busy import approval_input_words
from gateway.run_common import _UNSET
from gateway.run_inbound_media import rehome_inbound_media
from gateway.run_inbound_hooks import GatewayInboundHooksMixin
from gateway.run_loop_wake import GatewayLoopWakeMixin, _CommandDispatch, _CommandHandled
from gateway.run_inbound_transcription import GatewayInboundTranscriptionMixin
from gateway.run_plugin_injection import GatewayPluginInjectionMixin
from gateway.run_inbound_unauthorized import (
    UnauthorizedOwnerNotifier, pairing_code_reply, pairing_profile_arg, pairing_rate_limited_reply,
    unauthorized_owner_hint,
)
from gateway.attended_model import is_genuine_user_message
from gateway.session import (
    SessionSource, build_session_context, is_shared_multi_user_session,
    neutralize_untrusted_inline_text,
)
from gateway.turn_lease import TurnLeaseTimeoutError
from gateway.zero_cut_handover_drain import handover_accepts_received
from typing import TYPE_CHECKING, Any, Dict, List, Optional, Tuple

if TYPE_CHECKING:  # string annotations only; never imported at runtime (cycle)
    from gateway.run import GatewayRunner
    from gateway.run_turn_runner import TurnRunner

# Log-record parity with the origin module.
logger = logging.getLogger("gateway.run")


def _is_restore_control_event(event: MessageEvent) -> bool:
    """Let recognized gateway controls bypass replay fences and reach the busy fast path."""
    if not getattr(event, "allow_gateway_control", False):
        return False
    command = event.get_command()
    if not command:
        return False
    from hermes_cli.commands import should_bypass_active_session
    return should_bypass_active_session(command)


def discord_triggering_note(message_id: Any) -> str:
    """Model-facing routing note for a Discord turn (rides the API-bound user message only)."""
    return (
        f"[Triggering message id: `{message_id}` — use as `message_id` for reply/react/pin "
        f"via the discord tools.]"
    )


def strip_discord_triggering_note(event: Any, message_text: Any) -> Any:
    """Authored text for the durable user row: peel off exactly the note
    ``_prepend_inbound_reply_context`` added for THIS event, if present. The note is a
    model instruction, not something the user wrote — persisted as ``content`` it renders
    verbatim in every transcript surface and pollutes FTS/memory (#71304, #114719). It
    keeps riding ``message_text`` (and the replay-only ``api_content`` sidecar)."""
    message_id = getattr(event, "message_id", None)
    if not message_id or not isinstance(message_text, str):
        return message_text
    prefix = f"{discord_triggering_note(message_id)}\n\n"
    return message_text.removeprefix(prefix)


async def _admit_outbox_event(runner, event, source):
    """Durably admit Telegram events, including internal notices and delegation completions."""
    if not getattr(getattr(runner, "config", None), "durable_outbox_enabled", False) or source.platform != Platform.TELEGRAM:
        return
    from gateway.outbox import (
        store_for, bind_turn, event_kind, transport_id,
        event_admission_scope, local_admission_turn,
    )
    import uuid

    home = runner._resolve_profile_home_for_source(source)
    profile = getattr(source, "profile", None) or "default"
    kind = event_kind(event)
    event_id = transport_id(event)
    if not event_id:
        event_id = uuid.uuid4().hex
        event._outbox_transport_id = event_id
    store = store_for(home)
    turn_id = local_admission_turn(event, runner, home)
    if turn_id is None:
        turn_id, fresh = await asyncio.to_thread(
            store.admit, str(profile), "telegram", event_id, kind)
        if fresh:
            event._outbox_admission_scope = event_admission_scope(event, runner, home)
    else:
        fresh = True  # Native queue/deferred replay retains its admitted turn.
    event._outbox_turn_id = turn_id
    event._outbox_home = home
    event._outbox_duplicate = not fresh
    if not fresh:
        logger.info("Telegram redelivery of admitted outbox turn %s; no second turn", turn_id)
    else:
        bind_turn(home, turn_id)


class GatewayInboundMixin(GatewayInboundHooksMixin, GatewayInboundTranscriptionMixin, GatewayPluginInjectionMixin, GatewayLoopWakeMixin):
    """Inbound message pipeline (_handle_message, text/media preparation, durable-turn markers, plugin injection) for GatewayRunner."""

    async def _hm_stop_owner_turn(self, _quick_key: str) -> None:
        """Messaging /stop and /new (busy, idle or confirmed) act on the authority's turn, as main
        cancelled the adapter task that ran it and dropped its pending slot. Cancels the still-queued
        ``messaging:*`` rows of this route's logical owner first (automation wakes, LOCAL/API input
        and ``unknown`` rows are untouched; settled silently, no pause notice), then interrupts the
        claimed generation: a turn still constructing its agent latches the Stop for adopt_agent."""
        from gateway.session_authorities import active_authority
        authority = active_authority(self)
        sid = self._hm_mounted_owner(authority, _quick_key) if authority else None
        if not sid:
            if authority is not None:
                # The chat's first input, and a command's turn, not committed yet never commit.
                authority.fence_route(_quick_key)
            return
        from gateway.session_contract import Principal, SessionRef
        from hermes_state_runtime import RuntimeStoreError, list_session_admissions
        ref = SessionRef(authority.profile_id, sid)
        actor = Principal('messaging-control', authority.profile_id,
                          frozenset({'session:submit', 'session:control'}), 'busy-command')
        rows = await asyncio.to_thread(list_session_admissions, authority.db, session_id=sid)
        # A FIFO paused on a lost (``unknown``) turn is the operator's to resume: its pause notice
        # promised the queued followers stay saved, so a /new from the chat leaves them queued.
        paused = any(row['status'] == 'unknown' for row in rows)
        if not paused:
            # Input (and a command's turn) that arrived before this and is not committed yet never
            # commits after it; one that committed before the fence is in the second listing and
            # cancelled below. A paused FIFO keeps both, like its queued followers.
            authority.fence_route(_quick_key)
            rows = await asyncio.to_thread(list_session_admissions, authority.db, session_id=sid)
        for row in [] if paused else rows:
            if row['status'] == 'queued' and row['principal_id'].startswith('messaging:'):
                # Claimed meanwhile (stale_generation): it runs as main's post-claim race does.
                with suppress(RuntimeStoreError):
                    await authority.cancel_queued(actor, ref, row['admission_id'])
        handle = authority._handle(ref)
        if handle.execution_state == 'running':
            # Settled meanwhile (stale_generation): there is no claimed turn left to stop.
            with suppress(RuntimeStoreError):
                await authority.interrupt(actor, ref, handle.execution_generation)

    def _hm_mounted_owner(self, authority, _quick_key: str) -> Optional[str]:
        """The route's durable FIFO owner for /stop and /new, mounted. Right after a restart the
        startup recovery may not have mounted it yet, but its committed rows are still this chat's:
        skipping them left them queued forever once /new moved the lane to a fresh session. None
        when the chat has no durable session here."""
        sid = authority.logical_owner(self.session_store.peek_session_id(_quick_key))
        if sid and sid not in authority.sessions:
            from gateway.session_route_recovery import restore_routed_session
            try:
                restore_routed_session(authority, sid)
            except Exception:
                logger.debug("Could not mount %s for %s before stopping it", sid, _quick_key, exc_info=True)
        return sid if sid and sid in authority.sessions else None

    async def _hm_pre_gateway_dispatch_hook(
        self, event: MessageEvent, source: SessionSource
    ) -> Optional[MessageEvent]:
        """Gate ingress before auth: skip drops; rewrite replaces text; allow/None dispatches."""
        from gateway.run_inbound_hooks import GatewayInboundHooksMixin
        return await GatewayInboundHooksMixin._hm_pre_gateway_dispatch_hook(self, event, source)

    async def _hm_offer_pairing_code(self, source: SessionSource) -> None:
        """DM an unauthorized sender a pairing code (rate-limited; groups never reach here)."""
        platform_name = source.platform.value if source.platform else "unknown"
        pairing_store = self._pairing_store_for(source)
        if pairing_store is None:
            logger.error("Cannot offer pairing code on %s: no pairing store", platform_name)
            return
        # Rate-limit ALL pairing responses (code or rejection) so a burst of DMs doesn't spam.
        if pairing_store._is_rate_limited(platform_name, source.user_id):
            return
        try:
            code = await asyncio.to_thread(
                pairing_store.generate_code, platform_name, source.user_id, source.user_name or "",
            )
        except (OSError, TimeoutError):
            logger.warning("Could not record a pairing code on %s", platform_name, exc_info=True)
            return
        adapter = self._delivery_adapter_for(source)
        if code:
            reply = pairing_code_reply(platform_name, code, pairing_profile_arg(pairing_store))
        else:
            reply = pairing_rate_limited_reply()
        if adapter:
            await adapter.send(source.chat_id, reply)
        if not code:
            # Record rate limit so subsequent messages are silently ignored
            pairing_store._record_rate_limit(platform_name, source.user_id)

    async def _hm_send_unauthorized_decline(self, source: SessionSource) -> None:
        """``decline`` behavior: one short refusal per sender per DECLINE_DEDUPE_SECONDS, then silence
        (#88028). The stamp is written BEFORE the send so a delivery
        hiccup cannot become a decline storm; without a store there is no dedupe state → stay silent."""
        from gateway.config import DEFAULT_UNAUTHORIZED_DM_DECLINE_MESSAGE
        platform_name = source.platform.value if source.platform else "unknown"
        pairing_store = self._pairing_store_for(source)
        if pairing_store is None or pairing_store.has_recent_decline(platform_name, source.user_id):
            return
        # Off the event loop (the stamp waits on a cross-process lock); no stamp → no dedupe → silent.
        try:
            await asyncio.to_thread(pairing_store.record_decline, platform_name, source.user_id)
        except (OSError, TimeoutError):
            logger.warning("Could not record an unauthorized-DM decline on %s", platform_name, exc_info=True)
            return
        adapter = self._delivery_adapter_for(source)
        if not adapter:
            return
        config = getattr(self, "config", None)
        text = str(getattr(config, "unauthorized_dm_decline_message", "") or "").strip()
        try:
            await adapter.send(source.chat_id, text or DEFAULT_UNAUTHORIZED_DM_DECLINE_MESSAGE)
        except Exception:
            logger.warning("Failed to deliver unauthorized-DM decline on %s", platform_name, exc_info=True)

    async def _hm_report_ignored_dm(self, source: SessionSource) -> None:
        """Unauthorized DM under behaviour ``ignore``: nothing goes to the sender. The owner gets the
        sender's ID and the allowlist fix in the WARNING log and, once per sender, in the home channel."""
        from hermes_constants import display_hermes_home
        platform_name = source.platform.value if source.platform else "unknown"
        hint = unauthorized_owner_hint(
            platform_name, source.user_id, source.user_name or "", hermes_home=display_hermes_home(),
        )
        logger.warning("Unauthorized user (ignored): %s", hint)
        notifier = getattr(self, "_unauthorized_owner_notifier", None)
        if notifier is None:
            notifier = self._unauthorized_owner_notifier = UnauthorizedOwnerNotifier()
        if notifier.first_time(platform_name, source.user_id) and getattr(self, "config", None) is not None:
            await notifier.notify(self, source, hint)

    async def _hm_admit_event(
        self, event: MessageEvent
    ) -> Optional[tuple[MessageEvent, SessionSource, bool]]:
        """Ingress gates for ``_handle_message``; None when dropped, else ``(event, source, is_internal)``
        (the ``pre_gateway_dispatch`` hook may have rewritten ``event``)."""
        from gateway.run import _is_slack_ignored_channel
        source = event.source
        # getattr(self, ...) throughout: bare test runners build GatewayRunner via object.__new__.
        _config = getattr(self, "config", None)

        # 🔴 Cross-session leak guard: this per-message task was create_task()'d with a copy of the
        # spawning context, which may carry ANOTHER message's HERMES_SESSION_* ContextVars; until
        # _set_session_env binds ours a subprocess would read the foreign identity. Reset to _UNSET.
        try:
            from gateway.session_context import reset_session_vars
            reset_session_vars()
        except Exception:
            logger.debug("reset_session_vars failed at handler entry", exc_info=True)

        # Identity FIRST. Most adapters canonicalize at their own ingress; internal/voice paths
        # construct SessionSource directly, so this is the shared fail-closed gate. Strict boolean
        # marker: require the literal True so duck-typed test/internal sources with dynamic
        # attributes are not mistaken for a rejection.
        if getattr(_config, "multiplex_profiles", False):
            self._canonicalize(source)
        if getattr(source, "profile_route_rejected", False) is True:
            from gateway.parked_route_refusal import drop_rejected_route
            _intake = None
            with suppress(Exception):
                _intake = self._intake_adapter_for(source)
            drop_rejected_route(_intake, source)  # a parked target is also refused to the sender
            return None

        is_internal = bool(getattr(event, "internal", False))  # e.g. background-process notifications

        # Ignored-channel guard runs FIRST — before startup-restore queueing, plugin hooks, auth,
        # and session setup — so an ignored channel can never reach pairing/auth/session state.
        _chat_id = getattr(source, "chat_id", None)
        if not is_internal and getattr(source, "platform", None) == Platform.SLACK:
            # The routed adapter's extra carries a secondary profile's own list; ``_config`` is the default's.
            _slack_adapter = None
            with suppress(Exception):
                _slack_adapter = self._intake_adapter_for(source)
        if (
            # See #51899.
            not is_internal
            and getattr(source, "platform", None) == Platform.SLACK
            and _is_slack_ignored_channel(_config, _chat_id, _slack_adapter)
        ):
            logger.info("Dropping Slack message from configured ignored channel %s", _chat_id)
            return None

        # Capture before any admission buffer can park input for shutdown spooling,
        # including restore gates, compression deferral, and orphan FIFO rescue.
        from gateway.reply_latency import record_received, stamp_received
        stamp_received(event)

        reconnect_keys = getattr(self, "_reconnect_restore_keys", None)
        startup_restore_keys = getattr(self, "_startup_restore_keys", None)
        # Session-key normalization precedes the turn's key derivation. The
        # pre_gateway_dispatch hook runs only after this gate and must not rewrite
        # the routing identity of an already admitted event.
        reconnect_key = None
        if (reconnect_keys or startup_restore_keys or getattr(self, "_startup_restore_in_progress", False)) and not is_internal:
            normalized = await asyncio.to_thread(self._normalize_source_for_session_key, source)
            reconnect_key = self._session_key_for_source(normalized)
        from gateway.session_ingress import executing_admission
        if (
            not is_internal
            and (
                getattr(self, "_startup_restore_in_progress", False)
                or (reconnect_keys and reconnect_keys.get(reconnect_key, 0))
                or (startup_restore_keys and startup_restore_keys.get(reconnect_key, 0))
            )
            and not getattr(event, "_hermes_startup_restore_replay", False)
            and not executing_admission.get()
            and not _is_restore_control_event(event)
        ):
            # Persist only after the normal user and bot admission checks on drain.
            self._queue_startup_restore_event(event, session_key=reconnect_key)
            return None

        # Adapter admission can precede this task by an arbitrary delay on an idle lane.
        from gateway.wake import automatic_event_is_current

        if not await automatic_event_is_current(self, event):
            return None
        if is_internal:
            await _admit_outbox_event(self, event, source)
            return event, source, True
        if getattr(event, "_fifo_only", False):  # a slash command's queued turn: its command passed ingress
            record_received(event)
            return event, source, False

        # scale-to-zero: only real user-originated inbound stamps the last-inbound clock;
        # counting internal/system events would keep a genuinely idle gateway awake.
        if not getattr(event, "_gateway_compression_deferred", False):
            self._scale_to_zero_note_real_inbound()
            original_event = event
            event = await self._hm_pre_gateway_dispatch_hook(event, source)
            if event is None:
                return None
            if event is not original_event:
                # The native adapter retains this exact object for compression retries.
                # Keep the rewrite and callback proof on that owner, not a throwaway copy.
                from gateway.session_ingress_context import carry_callback
                original_event.text = event.text
                carry_callback(event, original_event)
                event = original_event
        from gateway.response_filters import apply_agent_origin_reply_expectation
        event = apply_agent_origin_reply_expectation(event)
        source = event.source

        if not self._is_user_authorized_for_source(source):
            if source.user_id is None:
                # No user identity (Telegram service messages, channel forwards, anonymous admin
                # posts, sender_chat): can't be paired but may be authorized via a chat allowlist.
                logger.debug("Ignoring message with no user_id from %s", source.platform.value)
                return None
            # DMs get a pairing code or a one-time decline, groups are ignored. A bot cannot pair, and
            # answering one mid-cooldown is outbound traffic.
            pairable_dm = source.chat_type == "dm" and not getattr(source, "is_bot", False)
            behavior = self._get_unauthorized_dm_behavior(source.platform, profile=source.profile) if pairable_dm else None
            if behavior == "pair":
                logger.warning("Unauthorized user: %s (%s) on %s", source.user_id, source.user_name, source.platform.value)
                await self._hm_offer_pairing_code(source)
            elif behavior == "decline":
                logger.warning("Unauthorized user: %s (%s) on %s", source.user_id, source.user_name, source.platform.value)
                await self._hm_send_unauthorized_decline(source)
            elif pairable_dm:
                await self._hm_report_ignored_dm(source)
            else:
                logger.warning("Unauthorized user: %s (%s) on %s", source.user_id, source.user_name, source.platform.value)
            return None
        # Charge each admitted event once, including retries and drained busy follow-ups.
        # Authorization above still checks the live conversation cooldown on every attempt.
        if not getattr(event, "_bot_loop_admitted", False):
            if not self._admit_bot_message_for_source(source):
                return None
            event._bot_loop_admitted = True
        record_received(event)
        await _admit_outbox_event(self, event, source)
        return event, source, False

    def _relay_fyi_passive_enabled(self, source: Optional[SessionSource] = None) -> bool:
        """Read the rollback flag from the routed profile's effective config; absent means enabled."""
        try:
            from gateway.run import _load_gateway_config
            config = None
            if source is not None and getattr(getattr(self, "config", None), "multiplex_profiles", False):
                profile_name = str(getattr(source, "profile", "") or "").strip()
                if not profile_name:
                    with suppress(Exception):
                        profile_name = str(self._profile_name_for_source(source) or "").strip()
                primary = getattr(self, "_primary_profile_name", None) or "default"
                if profile_name and profile_name != primary:
                    config = (getattr(self, "_relay_fyi_passive_by_profile", None) or {}).get(profile_name)
                    if config is None:
                        home = self._resolve_profile_home_for_source(source)
                        config = _load_gateway_config(home / "config.yaml")
            if config is None:
                config = _load_gateway_config()
            raw = ((config.get("relay") or {}).get("fyi_passive", True))
            if isinstance(raw, bool):
                return raw
            if isinstance(raw, str):
                # Only an explicit on value keeps passive delivery; a malformed rollback value
                # turns it off so the normal turn path stays the safe default.
                return raw.strip().lower() in {"1", "true", "yes", "on"}
            return False
        except Exception:
            return False

    async def _hm_try_passive_fyi_relay(
        self, event: MessageEvent, source: SessionSource, session_key: str,
    ) -> bool:
        """Persist one unaddressed Telegram FYI relay without creating an agent turn."""
        from gateway.response_filters import is_passive_fyi_relay_event
        if not is_passive_fyi_relay_event(
            event, source, enabled=self._relay_fyi_passive_enabled(source),
        ):
            return False
        session_store = self.async_session_store
        try:
            session_entry = await session_store.lookup_by_session_key(session_key)
        except Exception:
            logger.debug("FYI relay session lookup failed; falling back to a turn", exc_info=True)
            return False
        if session_entry is None or not getattr(session_entry, "session_id", None):
            return False
        # Claim the same running-session sentinel/lease used by real turns before any further await.
        # The dedupe check and append must be one exclusive interval: otherwise a turn can start between
        # the last idle check and the append and interleave its transcript rows with this FYI.
        from gateway.run import _AGENT_PENDING_SENTINEL
        passive_lease, _limit_message = self._claim_active_session_slot(session_key, source)
        if _limit_message is not None:
            return False
        if self._is_session_running(session_key):
            return False
        state = self._session_state(session_key)
        state.turn.agent = _AGENT_PENDING_SENTINEL
        state.turn.event = event
        state.turn.lease = passive_lease
        state.turn.started_ts = time.time()
        self._persist_active_agents()

        def _active_wakes():
            from hermes_cli.goals import _get_session_db, load_goal
            from hermes_cli.loops import load_loop
            if _get_session_db() is None:
                raise RuntimeError("session control database unavailable")
            return load_goal(session_entry.session_id), load_loop(session_entry.session_id)

        try:
            try:
                goal_state, loop_state = await self._run_in_executor_with_context(_active_wakes)
            except Exception:
                logger.debug("FYI relay goal/loop lookup failed; falling back to a turn", exc_info=True)
                return False
            if (
                getattr(goal_state, "status", None) == "active"
                or getattr(loop_state, "status", None) == "active"
            ):
                return False

            if getattr(event, "message_id", None):
                try:
                    if await session_store.has_platform_message_id(
                        session_entry.session_id, str(event.message_id),
                    ):
                        logger.info(
                            "Skipping duplicate passive FYI relay (message_id=%s) in session %s",
                            event.message_id, session_entry.session_id,
                        )
                        event._agent_turn_succeeded = True
                        return True
                except Exception:
                    logger.debug("FYI relay duplicate lookup failed; falling back to a turn", exc_info=True)
                    return False

            timestamp = getattr(event, "timestamp", None)
            try:
                timestamp = timestamp.timestamp()
            except (AttributeError, TypeError, ValueError):
                timestamp = time.time()
            row = {"role": "user", "content": event.text, "timestamp": timestamp}
            if getattr(event, "message_id", None):
                row["message_id"] = str(event.message_id)
            try:
                await session_store.append_to_transcript(session_entry.session_id, row)
            except Exception:
                logger.warning("FYI relay transcript append failed; falling back to a turn", exc_info=True)
                return False
            try:
                await self._refresh_agent_cache_message_count(session_key, session_entry.session_id)
            except Exception:
                logger.debug("FYI relay agent-cache rebaseline failed", exc_info=True)
            event._agent_turn_succeeded = True
            return True
        finally:
            if state.turn.agent is _AGENT_PENDING_SENTINEL and state.turn.event is event:
                self._release_running_agent_state(session_key)

    def _hm_estop_turn_allowed(self, event: MessageEvent, source: SessionSource) -> bool:
        """Whether a turn may bypass the global emergency stop: pause blocks NEW agent turns, never
        running work or control traffic — recognized slash commands (incl. /pause off, the in-band
        resume) and replies owned by in-flight work (pending update prompt, running session,
        pending slash-confirm, dangerous-command approval) all pass through."""
        with suppress(Exception):
            _estop_cmd = event.get_command()
            if _estop_cmd:
                from hermes_cli.commands import resolve_command as _resolve_estop_cmd
                if _resolve_estop_cmd(_estop_cmd) is not None:
                    return True
        with suppress(Exception):
            _estop_key = self._session_key_for_source(source)
            _estop_state = self._peek_session_state(_estop_key)
            if _estop_state is not None and _estop_state.persistent.update_prompt_pending:
                return True
            # A running session covers steering plus pending clarify / tool approvals it holds.
            if self._is_session_running(_estop_key):
                return True
            from tools import slash_confirm as _estop_confirm_mod
            if _estop_confirm_mod.get_pending(_estop_key):
                return True
            from tools.approval import has_blocking_approval as _estop_has_approval
            if _estop_has_approval(_estop_key):
                return True
        return False

    def _hm_estop_gate(
        self, event: MessageEvent, source: SessionSource, is_internal: bool
    ) -> Optional[str]:
        """Global emergency-stop (`hermes pause`) notice when this turn must be blocked, else None.
        Placed after auth so unauthorized senders can't probe pause state. A synthetic heartbeat that
        slipped past the poller's pause check gets "" (blocked, nothing sent): nobody typed it, and
        settle_heartbeat_attempt leaves its tick due for after resume."""
        if is_internal:
            return None
        try:
            from agent.estop import paused_reply as _estop_paused_reply
        except ImportError:
            return None
        _paused_notice = _estop_paused_reply()
        if _paused_notice is None or self._hm_estop_turn_allowed(event, source):
            return None
        if getattr(event, "_heartbeat_session_id", None):
            logger.debug("Heartbeat turn dropped by global emergency stop")
            return ""
        logger.info(
            "Gateway turn paused by global emergency stop (platform=%s chat=%s)",
            getattr(getattr(source, "platform", None), "value", "unknown"),
            getattr(source, "chat_id", None) or "unknown",
        )
        return _paused_notice

    @staticmethod
    def _hm_write_update_response(response_text: str) -> Optional[str]:
        """Atomically hand *response_text* to the detached update process; returns the OSError str."""
        from gateway.run import _hermes_home
        response_path = _hermes_home / ".update_response"
        try:
            tmp = response_path.with_suffix(".tmp")
            tmp.write_text(response_text, encoding="utf-8")
            tmp.replace(response_path)
            (_hermes_home / ".update_prompt.json").unlink(missing_ok=True)
        except OSError as e:
            return str(e)
        return None

    def _hm_update_prompt_reply(self, event: MessageEvent, _quick_key: str) -> Optional[str]:
        """Consume a reply to a pending ``/update`` prompt (routed to the detached update process via
        ``.update_response``); None when nothing was consumed. Recognized slash commands must bypass
        this or /new, /help etc. get silently consumed as update answers."""
        _up_state = self._peek_session_state(_quick_key)
        if _up_state is None or not _up_state.persistent.update_prompt_pending:
            return None
        # Accept /approve and /deny as shorthand for yes/no
        cmd = event.get_command()
        _recognized_cmd = None
        if cmd in {"approve", "yes"}:
            response_text = "y"
        elif cmd in {"deny", "no"}:
            response_text = "n"
        else:
            if cmd:
                with suppress(Exception):
                    from hermes_cli.commands import resolve_command as _resolve_update_cmd
                    _cmd_def = _resolve_update_cmd(cmd)
                    _recognized_cmd = _cmd_def.name if _cmd_def else None
            response_text = "" if _recognized_cmd else (event.text or "").strip()
        if response_text:
            err = self._hm_write_update_response(response_text)
            if err is not None:
                logger.warning("Failed to write update response: %s", err)
                return t("gateway.update.reply_failed", error=err)
            _up_state.persistent.update_prompt_pending = False
            label = response_text if len(response_text) <= 20 else response_text[:20] + "…"
            return t("gateway.update.reply_sent", label=label)
        # Recognized slash command during a pending update prompt: write a blank response so the
        # detached update's ``_gateway_prompt`` returns the prompt's default (typically a safe
        # "n" / skip) and exits instead of blocking on stdin until the watcher timeout.
        if _recognized_cmd:
            err = self._hm_write_update_response("")
            if err is None:
                logger.info(
                    "Recognized /%s during pending update prompt for %s; "
                    "cancelled prompt with default and dispatching command",
                    _recognized_cmd, _quick_key,
                )
            else:
                logger.warning("Failed to write cancel response for pending update prompt: %s", err)
            _up_state.persistent.update_prompt_pending = False
        return None

    async def _hm_clarify_reply(
        self, event: MessageEvent, source: SessionSource, _quick_key: str
    ) -> Optional[str]:
        """Intercept a reply to a pending clarify prompt; None when the message falls through.
        Free text answers open-ended/"Other" prompts; "2" answers a multi-choice one. Resolved/retained
        replies return "" so adapters don't double-post — the agent produces the next user-facing message."""
        try:
            from tools import clarify_gateway as _clarify_mod
            _pending_clarify = _clarify_mod.get_pending_for_session(_quick_key, include_choice_prompts=True)
        except Exception:
            return None
        if _pending_clarify is None:
            return None
        _clarify_has_audio = bool(self._pending_event_audio_paths(event))
        _raw_clarify_reply = await self._prepare_clarify_reply_text(event)

        def _retain(why: str) -> str:
            logger.info(
                "Gateway retained pending clarify after %s (session=%s, id=%s)",
                why, _quick_key, _pending_clarify.clarify_id,
            )
            return ""

        if _clarify_has_audio and not _raw_clarify_reply:
            return _retain("voice transcription produced no usable text")
        # Slash commands: the user wanted a command, not to answer the clarify. Leave it pending so
        # they can retry; on timeout the agent unblocks with an empty response.
        if not _raw_clarify_reply or _raw_clarify_reply.startswith("/"):
            return None
        _text_outcome = _clarify_mod.attempt_text_response_for_session(_quick_key, _raw_clarify_reply)
        if _text_outcome == _clarify_mod.TEXT_RESOLVED:
            logger.info(
                "Gateway intercepted clarify text response (session=%s, id=%s)",
                _quick_key, _pending_clarify.clarify_id,
            )
            # The clarify callback pauses the platform typing/status indicator while waiting so
            # Slack users can type; the active agent resumes now, so re-enable its indicator.
            _clarify_adapter = self._delivery_adapter_for(source)
            if _clarify_adapter:
                try:
                    resume_typing_after_prompt(_clarify_adapter, _quick_key, source.chat_id,
                                               self._event_thread_metadata(event, source))
                except Exception:
                    logger.debug("Failed to resume typing after clarify response", exc_info=True)
                # A typed answer to a native card (numeric pick, or text after "Other") never
                # reaches the click handler, so the card would keep its buttons forever.
                if callable(getattr(type(_clarify_adapter), "retire_clarify_card", None)):
                    try:
                        await _clarify_adapter.retire_clarify_card(
                            _pending_clarify.clarify_id,
                            t("gateway.clarify.answered",
                              response=_raw_clarify_reply if _pending_clarify.response == _clarify_mod.SKIPPED
                              else _pending_clarify.response or _raw_clarify_reply))
                    except Exception:
                        logger.debug("Failed to retire clarify card after typed answer", exc_info=True)
            return ""
        if _text_outcome == _clarify_mod.TEXT_REJECTED_SELECTION:
            # Selection-shaped but invalid (out-of-range number, bad comma-list): keep the clarify
            # armed for retry — don't cancel, don't treat as an unrelated follow-up.
            return _retain("invalid selection attempt")
        if _text_outcome == _clarify_mod.TEXT_REJECTED_PROSE:
            # Native-choice prompts reject unmatched prose so it continues through normal busy
            # routing. Release this clarify first: redirect() degrades to steer() while tools
            # execute, and that steer cannot drain until the clarify tool returns.
            if _clarify_mod.resolve_gateway_clarify(_pending_clarify.clarify_id, _clarify_mod.CANCELLED):
                # Adapters with a persistent native card (Slack Block Kit) retire it now, before the
                # prose is routed, so its buttons stop advertising a dead answer path. The pop inside
                # retire_clarify_card runs before its first await, so the agent thread's own expiry
                # notice (scheduled once the wait unblocks) finds nothing and stays a no-op.
                _clarify_adapter = self._delivery_adapter_for(source)
                # Class lookup: a MagicMock adapter must not fabricate the method.
                if callable(getattr(type(_clarify_adapter), "retire_clarify_card", None)):
                    try:
                        await _clarify_adapter.retire_clarify_card(
                            _pending_clarify.clarify_id,
                            t("gateway.clarify.cancelled"))
                    except Exception:
                        logger.debug("Failed to retire clarify card after prose cancellation", exc_info=True)
        return None

    # Reply → choice for a pending slash-confirm prompt; the command spelling wins over the
    # bang/slash-stripped free-text spelling.
    _SLASH_CONFIRM_CMD_CHOICES = {
        "approve": "once", "yes": "once", "ok": "once", "confirm": "once",
        "always": "always", "remember": "always",
        "cancel": "cancel", "no": "cancel", "deny": "cancel", "nevermind": "cancel",
    }
    # Free-text spellings: ``approval.inputs.confirm_*`` comma-lists (English ∪ active language).
    _SLASH_CONFIRM_TEXT_INPUT_KEYS = (("confirm_once", "once"), ("confirm_always", "always"), ("confirm_cancel", "cancel"))

    def _slash_confirm_text_choices(self) -> dict[str, str]:
        choices: dict[str, str] = {}
        for input_key, choice in self._SLASH_CONFIRM_TEXT_INPUT_KEYS:
            for word in approval_input_words(input_key):
                choices.setdefault(word, choice)
        return choices

    async def _hm_slash_confirm_reply(self, event: MessageEvent, _quick_key: str) -> Optional[str]:
        """Resolve a reply (/approve, /always, /cancel + aliases) to a pending slash-confirm prompt;
        None when it falls through — a stale pending confirm does NOT block other commands. A pending
        dangerous-command approval takes precedence: /approve there unblocks the waiting tool thread."""
        from tools import slash_confirm as _slash_confirm_mod
        _pending_confirm = _slash_confirm_mod.get_pending(_quick_key)
        if not _pending_confirm:
            return None
        with suppress(Exception):
            from tools.approval import has_blocking_approval
            if has_blocking_approval(_quick_key):
                return None
        # Accept bang-prefixed replies (`!always`, `!cancel`) verbatim: Slack/Matrix show the `!`
        # prefix (typed `/` is blocked in Slack threads) and adapters only rewrite
        # `!<known-command>` — confirm keywords aren't commands, so the `!` survives to here.
        _norm_reply = (event.text or "").strip().lstrip("!/").lower()
        _confirm_choice = (
            self._SLASH_CONFIRM_CMD_CHOICES.get(event.get_command())
            or self._slash_confirm_text_choices().get(_norm_reply)
        )
        if _confirm_choice is not None:
            _resolved = await _slash_confirm_mod.resolve(
                _quick_key, _pending_confirm.get("confirm_id"), _confirm_choice,
            )
            return _resolved or ""
        # Stale pending + unrelated command: the user moved on, so drop the pending state rather
        # than let the confirm block normal usage indefinitely.
        _slash_confirm_mod.clear_if_stale(_quick_key)
        return None

    def _hm_evict_idle_stale_agent(self, _quick_key: str) -> None:
        """Evict a leaked lock from a hung/crashed handler: only when the agent has been *idle* past
        the threshold (active tasks can run for hours), or has no activity tracker and an extreme
        wall-clock age. The pending sentinel is never evicted (no get_activity_summary() → idle
        reads inf and would race the async setup path)."""
        from gateway.run import _AGENT_PENDING_SENTINEL, _float_env
        _raw_stale_timeout = _float_env("HERMES_AGENT_TIMEOUT", 1800)
        _quick_state = self._peek_session_state(_quick_key)
        _stale_ts = _quick_state.turn.started_ts if _quick_state else 0
        if _quick_state is None or _quick_state.turn.agent is None or not _stale_ts:
            return
        _stale_age = time.time() - _stale_ts
        _stale_agent = _quick_state.turn.agent
        _stale_idle = float("inf")  # assume idle if we can't check
        _stale_detail = ""
        _activity_summary_valid = False
        if _stale_agent and hasattr(_stale_agent, "get_activity_summary"):
            with suppress(Exception):
                _sa = _stale_agent.get_activity_summary()
                from gateway.session_stall import resolve_session_idle_seconds_from_activity

                _sa_d = _sa if isinstance(_sa, dict) else {}
                _resolved_idle = resolve_session_idle_seconds_from_activity(
                    _sa if isinstance(_sa, dict) else None, now=time.time(),
                )
                if _resolved_idle is not None:
                    _stale_idle = _resolved_idle
                    _activity_summary_valid = True
                    # Host sleep is not idleness: share the turn poll loop's meter (same event loop).
                    _awake_idle = getattr(_quick_state.turn.ctx, "awake_idle", None)
                    if _awake_idle is not None:
                        _stale_idle = _awake_idle.measure(_stale_idle)
                _stale_detail = (
                    f" | last_activity={_sa_d.get('last_activity_desc', 'unknown')} "
                    f"({_stale_idle:.0f}s ago) "
                    f"| iteration={_sa_d.get('api_call_count', 0)}/{_sa_d.get('max_iterations', 0)}"
                )
        # A valid activity clock is authoritative: total age alone never makes an actively
        # progressing turn stale. The emergency wall TTL is only a fallback when the agent cannot
        # report usable activity.
        _wall_ttl = max(_raw_stale_timeout * 10, 7200) if _raw_stale_timeout > 0 else float("inf")
        _should_evict = _stale_agent is not _AGENT_PENDING_SENTINEL and (
            (_activity_summary_valid and _raw_stale_timeout > 0 and _stale_idle >= _raw_stale_timeout)
            or (not _activity_summary_valid and _stale_age > _wall_ttl)
        )
        if _should_evict:
            logger.warning(
                "Evicting stale _running_agents entry for %s "
                "(age: %.0fs, idle: %.0fs, timeout: %.0fs)%s",
                _quick_key, _stale_age, _stale_idle, _raw_stale_timeout, _stale_detail,
            )
            self._hm_evict_running_agent(_quick_key, "stale_running_agent_eviction")

    def _hm_reaped_session_id(self, _quick_key: str) -> Optional[str]:
        """The session id behind *_quick_key* when its durable row is ended, else None. Takes the
        session-store lock and reads state.db, so callers run it off the event loop."""
        _reap_store = getattr(self, "session_store", None)
        # Public, lock-held accessors: peek_session_id returns a non-str on stubbed stores in
        # bare test runners — the isinstance() / ``is True`` gates keep this inert unless a
        # real SessionStore answers.
        _reap_peek = getattr(_reap_store, "peek_session_id", None)
        _is_ended = getattr(_reap_store, "_is_session_ended_in_db", None)
        _reap_sid = _reap_peek(_quick_key) if callable(_reap_peek) else None
        if isinstance(_reap_sid, str) and _reap_sid and callable(_is_ended) and _is_ended(_reap_sid) is True:
            return _reap_sid
        return None

    async def _hm_evict_reaped_agent(self, _quick_key: str) -> None:
        """Evict the in-memory turn slot of a session whose durable row was ended while the gateway
        lived (``ws_orphan_reap`` / ``agent_close``): otherwise the fast-path queues every next
        message into the dead runtime. The cold path re-attaches via ``get_or_create_session``."""
        try:
            # #99106: durable-reaped guard. This is the live-gateway variant of #54878 and the #632
            # detached/ 405 suppressions in production. Evict the stale slot so the next message falls
            # through to the cold path and re-attaches or creates a fresh session; /status then correctly
            # shows 代理运行中: 否 before the heal and a live turn after.
            # The probe waits on the session-store lock, which a turn worker holds across its
            # state.db write: on the loop thread that wait tripped the liveness watchdog.
            _state = self._peek_session_state(_quick_key)
            _probed_agent = _state.turn.agent if _state else None
            _reap_sid = await asyncio.to_thread(self._hm_reaped_session_id, _quick_key)
            # Only the run the probe judged: a fresh turn that replaced it during the await is live.
            _state = self._peek_session_state(_quick_key)
            if _reap_sid and _state is not None and _state.turn.agent is _probed_agent is not None:
                logger.warning(
                    "Evicting stale _running_agents entry for %s — "
                    "durable session %s is ended (reaped) in state.db; "
                    "healing routing on next message (#99106)", _quick_key, _reap_sid,
                )
                self._hm_evict_running_agent(_quick_key, "reaped_session_eviction")
        except Exception:
            logger.debug("reaped-session staleness check failed", exc_info=True)

    def _hm_evict_running_agent(self, _quick_key: str, reason: str) -> None:
        from gateway.run import _INTERRUPT_REASON_EVICTED, _INTERRUPT_TOOL_REASON_EVICTED
        _generation_at_interrupt = self._interrupt_running_turn(
            _quick_key, interrupt_reason=_INTERRUPT_REASON_EVICTED, invalidation_reason=reason,
            tool_reason=_INTERRUPT_TOOL_REASON_EVICTED)
        self._drop_turn_slot(_quick_key, run_generation=_generation_at_interrupt)

    def _hm_merge_pending_for_source(
        self, source: SessionSource, _quick_key: str, event: MessageEvent, *, merge_text: bool = False
    ) -> None:
        """Merge *event* into the source adapter's pending slot (no-op without an adapter)."""
        from gateway.platforms.base import merge_pending_message_event
        adapter = self._delivery_adapter_for(source)
        if adapter:
            merge_pending_message_event(adapter._pending_messages, _quick_key, event, merge_text=merge_text)

    async def _hm_busy_slash_or_photo(
        self, event: MessageEvent, source: SessionSource, _quick_key: str
    ) -> tuple[bool, Optional[str]]:
        """Slash-command / photo-burst handling on the busy fast-path → ``(handled, result)``. Each
        command's mid-run behavior is declared on its CommandDef (busy_policy / busy_handler)."""
        from hermes_cli.commands import resolve_command as _resolve_cmd_inner
        _evt_cmd = event.get_command()
        _cmd_def_inner = _resolve_cmd_inner(_evt_cmd) if _evt_cmd else None

        if _cmd_def_inner:
            # /status and /context are intentionally pre-gate so users always see session state.
            if _cmd_def_inner.name == "status":
                return True, await self._handle_status_command(event)
            if _cmd_def_inner.name == "context":
                return True, await self._handle_context_command(event)
            # Slash access control mirrors the cold-path gate so non-admins can't bypass gating
            # just because an agent is busy. /help and /whoami are the always-allowed floor.
            _denied = self._check_slash_access(source, _cmd_def_inner.name)
            if _denied is not None:
                return True, _denied
            if _cmd_def_inner.name in ("stop", "new"):
                await self._hm_stop_owner_turn(_quick_key)
            # Any recognized slash command dispatches per its declared busy_policy (dispatch /
            # interrupt_then_dispatch / reject). Unrecognized commands and plain text fall through.
            return True, as_command_reply(
                await self._dispatch_busy_slash_command(event, _cmd_def_inner, _quick_key, source))

        # Telegram photo bursts arrive as near-simultaneous updates — never interrupt for a
        # photo-only follow-up; adapter-level batching absorbs them.
        if event.message_type == MessageType.PHOTO:
            logger.debug("PRIORITY photo follow-up for session %s — queueing without interrupt", _quick_key)
            self._hm_merge_pending_for_source(source, _quick_key, event)
            return True, None
        return False, None

    async def _hm_cancel_queued_followups(self, _quick_key: str) -> None:
        """Busy /stop and /new drop the human follow-up queued behind the running turn, as main
        dropped the adapter slot; under session authority it sits in the durable FIFO instead.
        Only still-queued ``messaging:*`` rows of this route's logical owner: automation wakes,
        LOCAL/API input and ``unknown`` rows are untouched. Settled silently (no pause notice)."""
        from gateway.session_authorities import active_authority
        authority = active_authority(self)
        sid = self._hm_mounted_owner(authority, _quick_key) if authority else None
        if not sid:
            if authority is not None:
                # The chat's first input, and a command's turn, not committed yet never commit.
                authority.fence_route(_quick_key)
            return
        from gateway.session_contract import Principal, SessionRef
        from hermes_state_runtime import RuntimeStoreError, list_session_admissions
        ref = SessionRef(authority.profile_id, sid)
        actor = Principal('messaging-control', authority.profile_id,
                          frozenset({'session:submit', 'session:control'}), 'busy-command')
        rows = await asyncio.to_thread(list_session_admissions, authority.db, session_id=sid)
        # A FIFO paused on a lost (``unknown``) turn is the operator's to resume: its pause notice
        # promised the queued followers stay saved, so a /new from the chat leaves them queued.
        paused = any(row['status'] == 'unknown' for row in rows)
        if not paused:
            # Input (and a command's turn) that arrived before this and is not committed yet never
            # commits after it; one that committed before the fence is in the second listing and
            # cancelled below. A paused FIFO keeps both, like its queued followers.
            authority.fence_route(_quick_key)
            rows = await asyncio.to_thread(list_session_admissions, authority.db, session_id=sid)
        for row in [] if paused else rows:
            if row['status'] == 'queued' and row['principal_id'].startswith('messaging:'):
                # Claimed meanwhile (stale_generation): it runs as main's post-claim race does.
                with suppress(RuntimeStoreError):
                    await authority.cancel_queued(actor, ref, row['admission_id'])

    def _hm_busy_telegram_grace_queue(
        self, event: MessageEvent, source: SessionSource, _quick_key: str, effective_busy_input_mode: str
    ) -> bool:
        """Queue a Telegram text follow-up that lands within the post-start grace window."""
        _grace = float(os.getenv("HERMES_TELEGRAM_FOLLOWUP_GRACE_SECONDS", "3.0"))
        _grace_state = self._peek_session_state(_quick_key)
        _started_at = _grace_state.turn.started_ts if _grace_state else 0
        if not (
            source.platform == Platform.TELEGRAM and event.message_type == MessageType.TEXT
            and _grace > 0 and _started_at and (time.time() - _started_at) <= _grace
        ):
            return False
        logger.debug(
            "Telegram follow-up arrived %.2fs after run start for %s — queueing without interrupt",
            time.time() - _started_at, _quick_key,
        )
        if effective_busy_input_mode != "queue":
            self._hm_merge_pending_for_source(source, _quick_key, event, merge_text=True)
        else:
            adapter = self._delivery_adapter_for(source)
            if adapter:
                self._enqueue_fifo(_quick_key, event, adapter)
        return True

    @staticmethod
    def _hm_text_only(event: MessageEvent) -> bool:
        return event.message_type == MessageType.TEXT and not event.media_urls and not event.media_types

    def _hm_busy_steer(self, event: MessageEvent, running_agent: Any, _quick_key: str) -> None:
        """Steer mode: inject text mid-run via ``agent.steer()``, else fall back to queue semantics."""
        steer_text = (event.text or "").strip()
        steered = False
        if self._hm_text_only(event) and steer_text and hasattr(running_agent, "steer"):
            try:
                steered = self._steer_running_agent(running_agent, self._steer_text_with_origin(steer_text, event))
            except Exception as exc:
                logger.warning("PRIORITY steer failed for session %s: %s", _quick_key, exc)
        if steered:
            self._fold_into_running_turn(running_agent, _quick_key, event)
            logger.debug("PRIORITY steer for session %s", _quick_key)
            return
        logger.debug("PRIORITY steer-fallback-to-queue for session %s", _quick_key)
        self._queue_or_replace_pending_event(_quick_key, event)

    async def _hm_busy_interrupt(
        self, event: MessageEvent, source: SessionSource, running_agent: Any, _quick_key: str
    ) -> None:
        """Interrupt path: redirect text-only corrections when supported, else ``agent.interrupt()``."""
        from gateway.run import _build_media_placeholder
        # Text-only corrections redirect the live turn (preserving displayed context) when the
        # runtime supports it; media/voice and older runtimes use the interrupt path below.
        _can_redirect = getattr(running_agent, "_supports_active_turn_redirect", False) is True
        if self._hm_text_only(event) and _can_redirect and hasattr(running_agent, "redirect"):
            if self._redirect_active_turn(running_agent, (event.text or "").strip(), _quick_key, event):
                logger.debug("PRIORITY redirect for session %s", _quick_key)
                return
        logger.debug("PRIORITY interrupt for session %s", _quick_key)
        _interrupt_text = event.text
        if self._pending_event_audio_paths(event):
            _interrupt_text, _ = await self._transcribe_and_echo_pending_voice(
                event, self._delivery_adapter_for(source), source, event.text or "",
                log_context="Voice-priority-interrupt",
            )
        elif not _interrupt_text and getattr(event, "media_urls", None):
            _interrupt_text = _build_media_placeholder(event)
        # Deliver through adapter._pending_messages, which _run_agent consumes. A second runner-side
        # copy is never consumed and grows without bound.
        running_agent.interrupt(_interrupt_text)

    async def _hm_handle_running_session_message(
        self, event: MessageEvent, source: SessionSource, _quick_key: str
    ) -> Optional[str]:
        """Fast-path while this session's agent is running: interrupt by default (minimal latency);
        busy_input_mode queue/steer, subagent and compression protection demote to queue."""
        # Steered input can stay in this turn and never reach turn-start admission.
        schedule_activity: Any = getattr(self, "_schedule_attended_user_activity", None)
        if callable(schedule_activity):
            schedule_activity(event, source, _quick_key)
        if turn_origin_for_event(event) == "user":  # a queued restart resume's preclaim may hold this slot
            self._promote_background_admission(_quick_key)
        from gateway.cron_review import fail_busy_review
        if fail_busy_review(event):
            # The runner slot can still be live when the adapter guard is not (reconnect,
            # handoff turn); a hidden review must neither interrupt nor queue behind it.
            return None
        from gateway.run import _AGENT_PENDING_SENTINEL
        if (self._draining and not self._hm_is_registered_command(event)
                and not handover_accepts_received(self, source)):
            if await self._route_plaintext_approval_while_busy(event, _quick_key):
                return None
            self._preserve_drain_event(_quick_key, event)
            return None
        if event.internal and not event.get_command():
            # Shared busy dispatch skips the adapter's internal FIFO guard. Completions must
            # never interrupt or steer a running turn, regardless of human busy policy.
            self._queue_or_replace_pending_event(_quick_key, event)
            return None
        _handled, _result = await self._hm_busy_slash_or_photo(event, source, _quick_key)
        if not _handled:
            _handled, _result = await self._busy_skill_turn(event, source, _quick_key)
        if _handled:
            return _result

        effective_busy_input_mode = self._effective_busy_input_mode(source)
        from gateway.response_filters import is_passive_fyi_relay_event
        if effective_busy_input_mode == "interrupt" and is_passive_fyi_relay_event(
            event, source, enabled=bool(self._relay_fyi_passive_enabled(source)),
        ):
            # Same rule as the adapter busy path: an FYI never redirects or interrupts a running turn.
            effective_busy_input_mode = "queue"
        if self._hm_busy_telegram_grace_queue(event, source, _quick_key, effective_busy_input_mode):
            return None

        _ra_state = self._peek_session_state(_quick_key)
        running_agent = _ra_state.turn.agent if _ra_state else None
        if running_agent is _AGENT_PENDING_SENTINEL:  # agent still being set up
            if event.get_command() == "stop":  # force-clean the sentinel so the session is unlocked
                self._latch_user_stop(_quick_key)
                self._release_running_agent_state(_quick_key)
                await self._pause_goal_for_stop(_quick_key, source)
                logger.info("HARD STOP (pending) for session %s — sentinel cleared", _quick_key)
                return EphemeralReply(t("gateway.stop.force_stopped_pending"))
            self._hm_merge_pending_for_source(source, _quick_key, event, merge_text=True)  # picked up after start
            return None
        if effective_busy_input_mode == "queue":
            logger.debug("PRIORITY queue follow-up for session %s", _quick_key)
            self._queue_or_replace_pending_event(_quick_key, event)
            return None
        if effective_busy_input_mode == "steer":
            self._hm_busy_steer(event, running_agent, _quick_key)
            return None
        # Subagent protection: an interrupt cascades through ``_active_children`` and aborts
        # in-flight delegate_task work (/stop reached its handler above — still an escape hatch).
        # Compression protection: an interrupt would start a new turn on the pre-rotation parent
        # while compression rotates the id away, forking orphaned siblings.
        if self._agent_has_active_subagents(running_agent):
            _demote = "because the running agent has active subagents (#30170)"
        elif await self._session_has_compression_in_flight(_quick_key):
            _demote = "because context compression is in flight (#56391)"
        else:
            await self._hm_busy_interrupt(event, source, running_agent, _quick_key)
            return None
        logger.info("PRIORITY interrupt demoted to queue for session %s %s", _quick_key, _demote)
        self._queue_or_replace_pending_event(_quick_key, event)
        return None

    def _hm_quick_commands(self) -> dict:
        """User-defined ``quick_commands`` mapping from config (empty dict when unset/malformed)."""
        cfg = self.config
        qc = (cfg.get("quick_commands") if isinstance(cfg, dict) else getattr(cfg, "quick_commands", None)) or {}
        return qc if isinstance(qc, dict) else {}

    @staticmethod
    def _normalize_alias_target(qcmd: object) -> Optional[str]:
        """``/name`` for an alias quick command with a usable target, else ``None``.

        Rejects a bare or slash-only target: a name-less expansion would crash the busy-path
        guard's ``split()[0]`` and can never resolve to a command anyway."""
        if not isinstance(qcmd, dict) or qcmd.get("type") != "alias":
            return None
        target = str(qcmd.get("target") or "").strip()
        if not target.lstrip("/").strip():
            return None
        return target if target.startswith("/") else f"/{target}"

    def _quick_command_alias_text(
        self, event: MessageEvent, profile_name: Optional[str] = None,
    ) -> Optional[str]:
        """Return the expanded text for a configured alias quick command, without mutating
        ``event``; ``None`` when the typed command is a built-in or no alias matches.

        Built-in dispatch resolves ``resolve_command`` names only, so an alias like ``s`` → ``/steer``
        is invisible to every busy-path guard and gets queued as user text. Both the adapter's
        Level-1 guard and ``_handle_message`` call this BEFORE the running-session split so aliases
        behave identically idle and mid-run. A routed profile resolves against its own snapshot
        (``_snapshot_profile_busy_modes``), so it never inherits the primary's aliases; the primary
        config is consulted only when no snapshot exists for that profile name."""
        get_command = getattr(event, "get_command", None)
        if not callable(get_command):
            return None
        command = get_command()
        if not isinstance(command, str) or not command:
            return None
        from hermes_cli.commands import resolve_command
        if resolve_command(command) is not None:
            return None  # built-ins keep precedence over aliases
        if not profile_name:
            source = getattr(event, "source", None)
            profile_name = str(getattr(source, "profile", "") or "").strip() or None
        quick_commands = None
        if profile_name:
            snapshots = getattr(self, "_quick_commands_by_profile", None)
            if isinstance(snapshots, dict) and profile_name in snapshots:
                quick_commands = snapshots[profile_name]
        if quick_commands is None:
            quick_commands = self._hm_quick_commands()
        if not isinstance(quick_commands, dict):
            return None
        target = self._normalize_alias_target(quick_commands.get(command))
        if target is None:
            return None
        get_command_args = getattr(event, "get_command_args", None)
        raw_args = get_command_args() if callable(get_command_args) else ""
        user_args = raw_args.strip() if isinstance(raw_args, str) else ""
        return f"{target} {user_args}".strip()

    @classmethod
    def _hm_expand_alias_quick_command(cls, event: MessageEvent, qcmd: dict) -> Optional[str]:
        """Rewrite ``event.text`` to an alias quick command's target; returns the new command name."""
        target = cls._normalize_alias_target(qcmd)
        if target is None:
            return None
        event.text = f"{target} {event.get_command_args().strip()}".strip()
        return target.lstrip("/").split()[0]

    async def _hm_command_hooks(  # health: allow CC -- ordered hook dispatch is the control-command fence and preserves observer/interceptor semantics.
        self, event: MessageEvent, source: SessionSource, _quick_key: str, command: str, canonical: str,
    ) -> tuple[_CommandHandled, Optional[str], Optional[str]]:
        """Fire ``pre_command`` (observer) and ``command:<canonical>`` (interceptor) hooks →
        ``(handled, result, new_command)`` (``new_command`` set when a handler rewrote the command).
        The running-agent path deliberately does NOT fire these — a slow or hostile plugin must not
        interfere with the operator's escape hatches for a live agent."""
        raw_args = event.get_command_args().strip()
        platform = source.platform.value if source.platform else ""
        async def _dispatch_hooks():
            try:
                from hermes_cli.plugins import fire_pre_command_hook
                fire_pre_command_hook(
                    surface="gateway", command=str(canonical), alias_used=str(command),
                    args_raw=raw_args, session_key=_quick_key, platform=platform,
                )
            except Exception as _pre_cmd_err:  # health: allow BLE001 -- plugin observer failures are isolated from command control dispatch.
                logger.debug("pre_command hook dispatch failed (non-fatal): %s", _pre_cmd_err)

            # Handlers may return ``{"decision": "deny" | "handled" | "rewrite", ...}`` to intercept
            # dispatch; handlers returning nothing behave as plain observers.
            hook_ctx = {
                "platform": platform, "user_id": source.user_id, "command": canonical,
                "raw_command": command, "args": raw_args, "raw_args": raw_args,
            }
            try:
                return await self.hooks.emit_collect(f"command:{canonical}", hook_ctx)
            except Exception as _hook_err:  # health: allow BLE001 -- plugin interceptor failures are isolated and return no decisions.
                logger.debug("command:%s hook dispatch failed (non-fatal): %s", canonical, _hook_err)
                return []

        _started, hook_results = await self._invoke_loop_wake_effect(event, _dispatch_hooks)
        if not _started:
            return _CommandDispatch.SUPPRESSED, None, command

        for hook_result in hook_results:
            if not isinstance(hook_result, dict):
                continue
            decision = str(hook_result.get("decision", "")).strip().lower()
            message = hook_result.get("message")
            message = message if isinstance(message, str) and message else None
            if decision == "deny":
                return True, message or t("gateway.hooks.command_blocked", command=command), None
            if decision == "handled":
                return True, message, None
            if decision == "rewrite":
                new_command = str(hook_result.get("command_name", "")).strip().lstrip("/")
                if new_command:
                    event.text = f"/{new_command} {str(hook_result.get('raw_args', '')).strip()}".strip()
                    return False, None, event.get_command()
        return False, None, None

    async def _hm_resolve_command(
        self, event: MessageEvent, source: SessionSource, _quick_key: str,
    ) -> tuple[_CommandHandled, Optional[str], Optional[str], Optional[str]]:
        """Resolve the slash command (aliases, access gate, hooks) → ``(handled, result, command,
        canonical)``; when ``handled`` the caller returns ``result`` as-is (may be None)."""
        from hermes_cli.commands import is_gateway_known_command, resolve_command as _resolve_cmd

        def _canon(cmd):
            # Aliases resolve to the canonical name so dispatch and hook names don't depend on them.
            _def = _resolve_cmd(cmd) if cmd else None
            return _def, (_def.name if _def else cmd)

        command = event.get_command()
        _cmd_def, canonical = _canon(command)

        # Expand alias quick commands before built-in dispatch so targets like /model openai/gpt-5.5
        # --provider openrouter reach the /model handler. Built-ins keep precedence: aliases only
        # need early handling when the typed command is not already known.
        if command and _cmd_def is None:
            qcmd = self._hm_quick_commands().get(command)
            if qcmd is not None and qcmd.get("type") == "alias":
                new_command = self._hm_expand_alias_quick_command(event, qcmd)
                if new_command is not None:
                    command = new_command
                    _cmd_def, canonical = _canon(command)

        if not (command and canonical and is_gateway_known_command(canonical)):
            return False, None, command, canonical

        # Per-platform slash access control: only active when the operator set ``allow_admin_from``
        # for the source's scope; then non-admins get ``user_allowed_commands`` plus the
        # /help, /whoami floor. Plain chat is never gated.
        _denied = self._check_slash_access(source, canonical)
        if _denied is not None:
            return True, _denied, command, canonical

        _handled, _result, new_command = await self._hm_command_hooks(
            event, source, _quick_key, command, canonical,
        )
        if _handled:
            return _handled, _result, command, canonical
        if new_command is not None:
            command = new_command
            _cmd_def, canonical = _canon(command)
        return False, None, command, canonical

    async def _hm_confirm_destructive(self, event, command: str, detail: str, handler) -> tuple[bool, Optional[str]]:
        async def _execute():
            return await handler(event)
        return True, await self._maybe_confirm_destructive_slash(
            event=event, command=command, title=f"/{command}", detail=detail, execute=_execute,
        )

    async def _hm_cmd_new(self, event, source, _quick_key):
        if await asyncio.to_thread(self._is_telegram_topic_root_lobby, source):
            return True, self._telegram_topic_root_new_message()
        return await self._hm_confirm_destructive(
            event, "new", t("gateway.confirm.detail_new"),
            self._handle_reset_command,
        )

    async def _hm_cmd_start(self, event, source, _quick_key):
        logger.info("Ignoring /start platform ping for session %s", _quick_key)
        return True, ""

    async def _hm_cmd_egress(self, event, source, _quick_key):
        from hermes_cli.proxy_cli import format_status_text
        return True, format_status_text()

    async def _hm_rewrite_turn_to_prompt(self, event, source, name: str, ack: str, build) -> tuple[bool, Optional[str]]:
        """Ack, then rewrite the turn to ``build()`` and fall through to the agent (keeps role
        alternation; works on any backend). A failing builder replies with a retry hint."""
        if self._loop_wake_command_identity(event) is None:
            await self._send_command_ack(source, ack, name)
        try:
            event.text = await asyncio.to_thread(build)
        except Exception:
            return True, t("gateway.prompt_command.start_failed", command=name)
        return False, None

    # /learn and /plan: ack, rewrite the turn to a builder prompt, fall through to the agent.
    async def _hm_cmd_learn(self, event, source, _quick_key):
        from agent.learn_prompt import build_learn_prompt

        req = event.get_command_args().strip()
        _ack = t("gateway.learn.ack_described") if req else t("gateway.learn.ack_conversation")
        return await self._hm_rewrite_turn_to_prompt(event, source, "learn", _ack, lambda: build_learn_prompt(req))

    async def _hm_cmd_plan(self, event, source, _quick_key):
        from agent.plan_prompt import build_plan_prompt

        task = event.get_command_args().strip()
        _ack = (t("gateway.plan.ack_task", task=task[:80] + ("…" if len(task) > 80 else ""))
                if task else t("gateway.plan.ack_conversation"))
        return await self._hm_rewrite_turn_to_prompt(event, source, "plan", _ack, lambda: build_plan_prompt(task))

    async def _hm_cmd_init(self, event, source, _quick_key):
        # /init builds the prompt first: the ack wording depends on whether AGENTS.md exists.
        from hermes_cli.init_command import build_init_prompt_for_cwd

        try:
            # The SESSION's active directory, not this process's launch dir: the desktop app
            # launches the backend from the home directory, so a bare os.getcwd() scanned and
            # updated the HOME's AGENTS.md instead of the workspace attached to the session.
            _init_prompt = build_init_prompt_for_cwd(
                extra=event.get_command_args().strip(),
                session_key=_quick_key or self._session_key_for_source(source),
            )
        except Exception:
            return True, t("gateway.init.start_failed")
        _ack = (
            t("gateway.init.ack_update")
            if "UPDATE the existing AGENTS.md" in _init_prompt
            else t("gateway.init.ack_generate")
        )
        await self._send_command_ack(source, _ack, "init")
        event.text = _init_prompt
        return False, None

    async def _hm_cmd_blueprint(self, event, source, _quick_key):
        _blueprint_result = await self._handle_blueprint_command(event)
        _text = getattr(_blueprint_result, "text", "") or ""
        _blueprint_seed = getattr(_blueprint_result, "agent_seed", None)
        if not _blueprint_seed:
            return True, _text or None
        # Blueprint matched — rewrite the turn to the seed and fall through so the agent collects
        # each slot value conversationally, then calls the cronjob tool (the /steer pattern).
        if _text:
            await self._send_command_ack(source, _text, "blueprint")
        try:
            event.text = _blueprint_seed
        except Exception:
            return True, _text or None
        return False, None

    async def _hm_cmd_undo(self, event, source, _quick_key):
        _undo_n = 1
        _undo_raw = event.get_command_args().strip()
        if _undo_raw:
            with suppress(ValueError, IndexError):
                _undo_n = max(1, int(_undo_raw.split()[0]))
        _undo_detail = (
            t("gateway.confirm.detail_undo_one")
            if _undo_n == 1
            else t("gateway.confirm.detail_undo_many", count=_undo_n)
        )
        return await self._hm_confirm_destructive(event, "undo", _undo_detail, self._handle_undo_command)

    # /queue and /steer on the idle path: no agent is running, so strip the prefix and send the
    # payload as a regular user turn; an empty payload surfaces the usage hint.
    async def _hm_cmd_queue(self, event, source, _quick_key):
        return self._hm_send_payload_as_turn(event, t("gateway.queue.usage"))

    async def _hm_cmd_steer(self, event, source, _quick_key):
        return self._hm_send_payload_as_turn(event, t("gateway.steer.usage_idle"))

    @staticmethod
    def _hm_send_payload_as_turn(event, usage: str) -> tuple[bool, Optional[str]]:
        payload = event.get_command_args().strip()
        if not payload:
            return True, usage
        with suppress(Exception):
            event.text = payload
        # Admission saw "/queue <header>" and could not match the header; the stripped
        # payload is what the turn persists, so judge agent origin on it now.
        from gateway.response_filters import apply_agent_origin_reply_expectation
        apply_agent_origin_reply_expectation(event)
        return False, None

    async def _hm_cmd_moa(self, event, source, _quick_key):
        # /moa is one-shot sugar only: run a single prompt through the default MoA preset, then
        # restore the prior model. To *switch* to a MoA preset for the session, pick it from the
        # model picker (MoA presets surface as a virtual "Mixture of Agents" provider).
        from hermes_cli.moa_config import moa_usage, normalize_moa_config
        from hermes_cli.config import load_config

        moa_payload = event.get_command_args().strip()
        if not moa_payload:
            return True, moa_usage()
        try:
            cfg = load_config()
            moa_cfg = normalize_moa_config(cfg.get("moa") if isinstance(cfg, dict) else {})
        except Exception:
            moa_cfg = normalize_moa_config({})
        try:
            event.text = moa_payload
            # Same one-shot snapshot `/model --once` uses, so eviction/stop/finalizer settle both alike.
            self._claim_one_turn_restore(_quick_key)
            self._session_model_overrides[_quick_key] = {
                "provider": "moa", "model": moa_cfg["default_preset"], "base_url": "moa://local",
                "api_key": "moa-virtual-provider", "api_mode": "chat_completions",
            }
            self._evict_cached_agent(_quick_key)
        except Exception:
            return True, t("gateway.moa.prepare_failed")
        return False, None

    # Idle-path built-ins with bespoke flow (confirmations, prompt rewrites, one-shot MoA), each
    # handled by ``_hm_cmd_<name>`` → ``(handled, result)``; ``(False, None)`` falls through to the agent.
    _HM_CANONICAL_COMMANDS = frozenset({
        "new", "start", "egress", "learn", "plan", "init", "blueprint", "undo", "queue", "steer", "moa",
    })

    async def _hm_dispatch_canonical_command(
        self, event: MessageEvent, source: SessionSource, _quick_key: str,
        canonical: Optional[str],
    ) -> tuple[_CommandHandled, Optional[str]]:
        """Dispatch built-in idle-path commands → ``(handled, result)``; prompt-rewriting commands
        mutate ``event.text`` and return ``(False, None)`` to fall through to the agent."""
        plain_handler = (
            self._gateway_plain_command_handlers().get(canonical)
            or self._gateway_idle_command_handlers().get(canonical)
        )
        if plain_handler is not None:
            async with self._async_profile_scope_for_source(source):
                _started, result = await self._invoke_loop_wake_effect(event, plain_handler, event)
                return (True, result) if _started else (_CommandDispatch.SUPPRESSED, None)
        if canonical in self._HM_CANONICAL_COMMANDS:
            handler = getattr(self, f"_hm_cmd_{canonical}")
            started, result = await self._invoke_loop_wake_effect(
                event, handler, event, source, _quick_key,
            )
            return result if started else (_CommandDispatch.SUPPRESSED, None)
        return False, None

    @staticmethod
    def _hm_is_registered_command(event: MessageEvent) -> bool:
        command = event.get_command()
        if not command:
            return False
        from hermes_cli.commands import resolve_command
        return resolve_command(command) is not None

    async def _hm_dispatch_quick_and_plugin_commands(  # health: allow NESTING -- ordered command routing retains the drain, alias, plugin, and built-in fences.
        self, event: MessageEvent, source: SessionSource, command: Optional[str]
    ) -> tuple[_CommandHandled, Optional[str], Optional[str]]:
        """Drain gate, user-defined quick commands (exec/alias) and plugin slash commands →
        ``(handled, result, command)``; an alias quick command rewrites ``command``."""
        if (self._draining and not self._hm_is_registered_command(event)
                and not handover_accepts_received(self, source)):
            self._preserve_drain_event(self._session_key_for_source(source), event)
            return True, None, command

        # User-defined quick commands (bypass agent loop, no LLM call)
        qcmd = self._hm_quick_commands().get(command) if command else None
        if qcmd is not None:
            # Quick commands are slash capabilities too — and type:exec ones run a shell command in
            # the gateway process. They are never in the registry, so the early gate never fires for
            # them; apply the same admin/user policy to the raw typed name here.
            # The early gate above only fires for registry-known commands, so quick commands (never in the
            # registry) would otherwise reach this dispatch sink unchecked. (#44727)
            _denied = self._check_slash_access(source, command)
            if _denied is not None:
                return True, _denied, command
            qtype = qcmd.get("type")
            if qtype == "exec":
                exec_cmd = qcmd.get("command", "")
                if not exec_cmd:
                    return True, t("gateway.quick_command.no_command", command=command), command
                _started, result = await self._invoke_loop_wake_effect(
                    event, self._hm_run_exec_quick_command, command, exec_cmd,
                )
                return (True, result, command) if _started else (_CommandDispatch.SUPPRESSED, None, command)
            if qtype != "alias":
                return True, t("gateway.quick_command.unsupported_type", command=command), command
            new_command = self._hm_expand_alias_quick_command(event, qcmd)
            if new_command is None:
                return True, t("gateway.quick_command.no_target", command=command), command
            command = new_command  # Fall through to normal command dispatch below

        # Plugin-registered slash commands. Underscores normalize to hyphens so Telegram's
        # underscored autocomplete form matches plugin commands registered with hyphens.
        if command:
            try:
                from hermes_cli.plugins import get_plugin_command_handler
                plugin_handler = get_plugin_command_handler(command.replace("_", "-"))
                if plugin_handler:
                    async def _fenced(call, is_coroutine):
                        # Enter the effect through the loop-wake fence with no await between the
                        # check and the handler; a sync handler is re-fenced inside its worker.
                        if is_coroutine:
                            return await self._invoke_loop_wake_effect(event, call)
                        started, result = await self._run_in_executor_with_context(
                            self._invoke_loop_wake_sync_effect, event, call)
                        if started and inspect.isawaitable(result):
                            pending = result

                            async def _await_pending():
                                return await pending

                            started, result = await self._invoke_loop_wake_effect(event, _await_pending)
                            if not started and asyncio.iscoroutine(pending):
                                pending.close()
                        return started, result

                    started, result = await self._hm_run_plugin_handler(
                        plugin_handler, event, source, invoke=_fenced)
                    if not started:
                        return _CommandDispatch.SUPPRESSED, None, command
                    return True, result, command
            except Exception as e:
                logger.warning("Plugin command dispatch failed: %s", e)
        return False, None, command

    def _hm_bundle_slash_rewrite(
        self, event: MessageEvent, source: SessionSource, _quick_key: str, command: str
    ) -> bool:
        """Rewrite ``/<bundle>`` to the bundle invocation message; True when handled.
        Skill bundles take precedence over individual skill commands (mirrors CLI dispatch)."""
        try:
            from agent.skill_bundles import (
                build_bundle_invocation_message, resolve_bundle_command_key
            )
            bundle_key = resolve_bundle_command_key(command)
            if bundle_key is None:
                return False
            # Pass the platform explicitly: bundle skill loading bypasses get_skill_commands()'
            # scan-time disabled filter, and one gateway process serves several platforms, so
            # env-var platform resolution can't be trusted here.
            # Mirrors the stacked-skill gate (#58888).
            bundle_result = build_bundle_invocation_message(
                bundle_key, event.get_command_args().strip(), task_id=_quick_key,
                platform=source.platform.value if source.platform else None,
            )
            if not bundle_result:
                return False
            event.text, _loaded, missing = bundle_result
            if missing:
                logger.info("Bundle %s skipped missing skills: %s", bundle_key, ", ".join(missing))
            return True  # Fall through to normal message processing with bundle content
        except Exception as exc:
            logger.warning("Bundle dispatch failed: %s", exc)
            return False

    @staticmethod
    def _hm_unknown_slash_reply(command: str, source: SessionSource) -> Optional[str]:
        """Reply for a /command that is not built-in/plugin/skill; None when it is known."""
        from gateway.run import _check_unavailable_skill
        from hermes_cli.commands import GATEWAY_KNOWN_COMMANDS
        # Known commands never need an unavailable-skill hint (which can require a cold scan).
        if command.replace("_", "-") in GATEWAY_KNOWN_COMMANDS:
            return None
        # Known-but-disabled or uninstalled skill → actionable guidance.
        _unavail_msg = _check_unavailable_skill(command)
        if _unavail_msg:
            return _unavail_msg
        # Genuinely unrecognized: warn instead of forwarding to the LLM as free text (it invents
        # tool calls). Normalize to hyphenated form first: the quick-command block may have set an
        # alias target, so the resolved def can be stale.
        logger.warning(
            "Unrecognized slash command /%s from %s — replying with unknown-command notice",
            command, source.platform.value if source.platform else "?",
        )
        return t("gateway.unknown_command", command=command)

    def _hm_skill_slash_rewrite(
        self, event: MessageEvent, source: SessionSource, _quick_key: str, command: Optional[str]
    ) -> Optional[str]:
        """Rewrite ``/<bundle>`` / ``/<skill>`` invocations into the skill prompt on ``event.text``;
        returns a reply string when the command is disabled/unknown/failed, else None.
        resolve_skill_command_key() handles the Telegram underscore/hyphen round-trip (/claude_code)."""
        if not command or self._hm_bundle_slash_rewrite(event, source, _quick_key, command):
            return None
        try:
            from agent.skill_commands import (
                get_skill_commands, build_skill_invocation_message, resolve_skill_command_key
            )
            skill_cmds = get_skill_commands()
            cmd_key = resolve_skill_command_key(command)
            if cmd_key is None:
                return self._hm_unknown_slash_reply(command, source)
            _plat = source.platform.value if source.platform else None
            user_instruction = event.get_command_args().strip()
            # Stacked slash-skill invocations: `/skill-a /skill-b do XYZ` loads every leading skill
            # (up to 5), not just the first. Mirrors CLI. Native split: plugin skills are interactive-only.
            try:
                from agent.skill_commands import (
                    build_stacked_skill_invocation_message as _build_stacked,
                    split_stacked_skill_commands,
                )
                extra_keys, stacked_instruction = split_stacked_skill_commands(user_instruction)
            except Exception:
                _build_stacked = None
                extra_keys, stacked_instruction = [], user_instruction
            _skill_name = skill_cmds[cmd_key].get("name", "")
            if _plat and (_skill_name or extra_keys):
                # Per-platform disabled check: get_skill_commands() only applies the *global*
                # disabled list at scan time (process-global cache across platforms), and
                # split_stacked_skill_commands() only checks each extra token is a KNOWN skill.
                from agent.skill_utils import get_disabled_skill_names as _get_plat_disabled
                _plat_disabled = _get_plat_disabled(platform=_plat)
                if _skill_name and _skill_name in _plat_disabled:
                    return t("gateway.skills.disabled_for_platform", name=_skill_name, platform=_plat)
                _disabled_extra = [
                    skill_cmds.get(k, {}).get("name", "")
                    for k in extra_keys
                    if skill_cmds.get(k, {}).get("name", "") in _plat_disabled
                ]
                if _disabled_extra:
                    return t("gateway.skills.stacked_disabled", names=", ".join(_disabled_extra), platform=_plat)
            if extra_keys and _build_stacked is not None:
                stacked_result = _build_stacked(
                    [cmd_key, *extra_keys], stacked_instruction, task_id=_quick_key,
                )
                if not stacked_result:
                    return t("gateway.skills.stacked_load_failed", command=command)
                event.text, _loaded, _missing = stacked_result
            else:
                msg = build_skill_invocation_message(cmd_key, user_instruction, task_id=_quick_key)
                if msg:
                    event.text = msg
            # Fall through to normal message processing with skill content
        except Exception as e:
            logger.debug("Skill command check failed (non-fatal): %s", e)
        return None

    async def _hm_pending_reply_intercepts(
        self, event: MessageEvent, source: SessionSource, _quick_key: str
    ) -> Optional[str]:
        """Replies owned by in-flight work: pending /update prompt, clarify, slash-confirm.
        Only gateway-control human events may answer prompts, never internal wakes or an
        already admitted turn whose prompt was offered before commit."""
        from gateway.session_ingress import executing_admission
        if (not event.allow_gateway_control or getattr(event, "internal", False)
                or getattr(event, "_fifo_only", False) or executing_admission.get()):
            return None
        _reply = self._hm_update_prompt_reply(event, _quick_key)
        if _reply is None:
            _reply = await self._hm_clarify_reply(event, source, _quick_key)
        if _reply is None:
            _reply = await self._hm_slash_confirm_reply(event, _quick_key)
        return _reply

    async def _hm_dispatch_idle_commands(
        self, event: MessageEvent, source: SessionSource, _quick_key: str
    ) -> tuple[bool, Optional[str]]:
        """Idle path: resolve + dispatch slash commands; rewriting commands fall through to the agent."""
        stop_epoch = self._route_stop_epoch(_quick_key)
        async def _loop_command_current() -> bool:
            if await self._prepare_loop_wake_execution(event, source):
                return True
            self._log_loop_wake_suppressed(event, "superseded before command dispatch")
            return False

        if not await _loop_command_current():
            return True, None
        _handled, _result, command, canonical = await self._hm_resolve_command(
            event, source, _quick_key,
        )
        async def _loop_command_fallthrough():
            if not await _loop_command_current():
                return True, None
            return False, None

        if not _handled:
            # after resolution and immediately before entering an effectful handler.
            if not await _loop_command_current():
                return True, None
            _handled, _result = await self._hm_dispatch_canonical_command(
                event, source, _quick_key, canonical,
            )
        if not _handled and canonical in {"learn", "plan"}:
            # Resolution may rewrite a command; only its canonical handler builds the prompt.
            if not await _loop_command_current():
                return True, None
            return await self._admit_command_turn(event, source, _quick_key, stop_epoch)
        if not _handled:
            if not await _loop_command_current():
                return True, None
            _handled, _result, command = await self._hm_dispatch_quick_and_plugin_commands(
                event, source, command,
            )
        if not _handled:
            # Skill-slash resolution is disk-bound (cold skill scan, skill file loads, the
            # unavailable-skill rglob over every skills dir) and uncached on a first hit; on a
            # large install it held the loop past the liveness watchdog (#111091). The executor
            # hop carries the profile contextvars the scan is scoped to.
            _result = await self._run_in_executor_with_context(
                self._hm_skill_slash_rewrite, event, source, _quick_key, command)
            _handled = _result is not None
        if not _handled and command:
            _handled, _result = await self._admit_command_turn(event, source, _quick_key, stop_epoch)
        if not _handled:
            return await _loop_command_fallthrough()
        if _handled is _CommandDispatch.SUPPRESSED:
            return True, None
        # Only an effectful handled command settles here; rewrites settle after model work.
        await self._post_command_loop_completion(event, source)
        if _handled and canonical != "retry":  # /retry returns the re-run agent turn's own reply
            _result = as_command_reply(_result)
        return _handled, _result

    def _hm_rescue_orphaned_fifo(
        self, event: MessageEvent, source: SessionSource, is_internal: bool, _quick_key: str
    ) -> tuple[MessageEvent, SessionSource, bool]:
        """FIFO orphan rescue: a session that went idle with a populated overflow (post-turn drain
        never promoted, e.g. a compression-demoted follow-up) silently orphaned those events. The
        oldest orphan runs as THIS turn and the incoming event is parked behind the chain. Skipped
        for control commands and internal events."""
        try:
            # ── FIFO orphan rescue (#99882) ──────────────────────────────── If this session went idle with
            # a populated overflow (queued during a busy window whose post-turn drain never promoted — e.g.
            # a compression-demoted follow-up after the compression window ended through an exit that
            # skipped the promotion site), those events were silently orphaned. We are starting the next
            # turn for this session NOW: re-stage the orphans in FIFO order and enqueue the incoming event
            # behind them, so arrival order (#28503) holds: oldest orphan runs as this turn, the rest drain
            # in order, the new message last.
            _orphan_adapter = self._delivery_adapter_for(source)
            if (_orphan_adapter is None or getattr(event, "internal", False) or event.get_command()
                    or getattr(event, "_gateway_compression_deferred", False)):
                return event, source, is_internal
            _rescued = self._rescue_orphaned_overflow(_quick_key, _orphan_adapter)
            if _rescued is None:
                return event, source, is_internal
            # Into the slot when the chain was a single orphan (post-turn drain picks it up),
            # otherwise into overflow behind the already-staged next orphan.
            self._enqueue_fifo(_quick_key, event, _orphan_adapter)
            # Same session key by construction; carry the orphan's own source so reply anchors /
            # thread metadata point at the message actually being answered.
            _rescued_source = getattr(_rescued, "source", None)
            source = _rescued_source if _rescued_source is not None else source
            return _rescued, source, bool(getattr(_rescued, "internal", False))
        except Exception:
            logger.debug("FIFO orphan rescue pre-claim failed for %s", _quick_key, exc_info=True)
            return event, source, is_internal

    def _schedule_attended_user_activity(self, event, source, session_key: str, *, profile_scope=None):
        """Keep receipt-time bookkeeping alive without delaying busy input or commands.

        ``profile_scope`` is a zero-argument factory for an async profile scope, entered inside
        the background task. Callers on a receive loop that hold no scope use it, so secret
        hydration never runs before the input returns."""
        if not is_genuine_user_message(event) or getattr(event, "_attended_activity_marked", False):
            return
        if getattr(event, "_attended_activity_task", None) is not None:
            return
        from gateway.reply_latency import record_received
        record_received(event)
        mark = self._mark_attended_user_activity
        if profile_scope is None:
            work = mark(event, source, session_key, retry_on_failure=True)
        else:
            async def scoped():
                async with profile_scope():
                    return await mark(event, source, session_key, retry_on_failure=True)
            work = scoped()
        task = asyncio.create_task(work)
        event._attended_activity_task = task
        self._retain_background_task(task)

    async def _mark_attended_user_activity(
        self, event, source, session_key: str | None = None, *, retry_on_failure: bool = False,
    ) -> bool:
        """Persist genuine Brian activity only for an existing session when attended routing is enabled."""
        if not is_genuine_user_message(event):
            return False
        if getattr(event, "_attended_activity_marked", False):
            return True
        pending = getattr(event, "_attended_activity_task", None)
        if pending is not None and pending is not asyncio.current_task():
            try:
                await asyncio.shield(pending)
            except asyncio.CancelledError:
                if not pending.cancelled():
                    raise
            except Exception:
                logger.debug("attended-model receipt task failed", exc_info=True)
            if getattr(event, "_attended_activity_marked", False):
                return True
        from gateway.reply_latency import record_received
        record_received(event)
        stamp = event._attended_activity_received_at
        try:
            from datetime import datetime
            from gateway.run import _load_gateway_config
            from gateway.attended_model import parse_attended_config
            attended_cfg = await asyncio.to_thread(
                lambda: parse_attended_config(_load_gateway_config()),
            )
            if not attended_cfg.enabled:
                return False
            if not session_key:
                session_key = self._session_key_for_source(source)
            if not session_key:
                return False
            entry = await self.async_session_store.lookup_by_session_key(session_key)
            if entry is None:
                return False
            state = self._session_state(session_key)
            from gateway.session_state import session_opening_pending
            if attended_cfg.start_new_sessions and session_opening_pending(
                entry, attended_new_session=state.conversation.attended_new_session,
            ):
                # Preserve the creation boundary before mark_user_activity updates updated_at.
                state.conversation.attended_new_session = True
            await self.async_session_store.mark_user_activity(
                entry.session_key, when=datetime.fromtimestamp(stamp),
            )
            previous = state.conversation.last_user_activity_at
            if previous is None or stamp > previous:
                state.conversation.last_user_activity_at = stamp
            event._attended_activity_marked = True
            return True
        except Exception:
            logger.debug("attended-model activity stamp failed", exc_info=True)
            if retry_on_failure:
                # Steering consumes the event, so only ordinary failures retry here.
                # False eligibility results and task cancellation are terminal.
                return await self._mark_attended_user_activity(event, source, session_key)
            return False

    async def _handle_message(self, event: MessageEvent) -> Optional[str]:
        from agent.delegation_context import top_level_delegation_context
        from gateway.outbox import scoped_turn_entry, restore_turn, store_for
        token = scoped_turn_entry()
        result = None
        try:
            with top_level_delegation_context():
                result = await self._handle_admitted_message(event)
            return result
        finally:
            turn_id = getattr(event, "_outbox_turn_id", None)
            try:
                if (turn_id and not getattr(event, "_outbox_duplicate", False)
                        and not getattr(event, "_gateway_compression_deferred", False)):
                    await asyncio.to_thread(
                        store_for(event._outbox_home).finish_admission,
                        turn_id, "completed" if result is not None else "empty")
            finally:
                restore_turn(token)

    async def _hm_busy_preempt(self, event: MessageEvent, source: SessionSource, _quick_key: str) -> bool:
        """busy_input_mode for a follow-up the durable FIFO is about to admit, through main's own busy
        resolution (voice transcribed into steer/interrupt, steer folded into the turn, busy ack).
        Returns True when the text already landed inside the running turn (steer / redirect), so it
        must NOT also be admitted as its own turn; False when the caller should admit it (queue mode,
        a demotion, or an interrupt — the ended turn lets the FIFO run the follow-up next)."""
        from gateway.run import _AGENT_PENDING_SENTINEL
        mode = self._effective_busy_input_mode(source)
        state = self._peek_session_state(_quick_key)
        running_agent = state.turn.agent if state else None
        adapter = self._delivery_adapter_for(source)
        if (self._draining or adapter is None or running_agent is None or running_agent is _AGENT_PENDING_SENTINEL
                or (event.message_type == MessageType.TEXT and mode != "steer"
                    and self._effective_busy_text_mode(source) == "queue")):
            return False
        outcome = await self._resolve_busy_steer_or_redirect(event, _quick_key, mode, running_agent)
        await self._busy_interrupt_and_ack(event, _quick_key, adapter, state, running_agent, outcome)
        return bool(outcome.steered or outcome.redirected)

    async def _hm_authority_admission(
        self, event: MessageEvent, source: SessionSource, _quick_key: str, is_internal: bool,
    ) -> tuple[bool, Optional[str]]:
        """``(True, reply)`` when the routed profile's session authority owned this message
        (preempted, plugin-consumed or admitted to its durable FIFO); ``(False, None)`` to continue
        on the in-process path. Raises ``RuntimeStoreError('profile_mismatch')`` for a scope this
        process does not serve."""
        # The message handler entered the routed profile's scope; its authority owns this turn.
        from gateway.session_authorities import active_authority
        from gateway.session_ingress import admit_message, executing_admission
        authority = active_authority(self)
        if authority is not None and not event.get_command() and not is_internal:
            if not executing_admission.get():
                # The durable FIFO only orders turns; busy_input_mode still decides what a follow-up
                # does to the RUNNING one (steer into it, redirect it, or end it so the FIFO advances).
                # Without this, ``interrupt`` degrades to ``queue`` behind a turn that may hang for
                # its whole request timeout.
                # A bare "yes"/"no" to a blocking dangerous-command approval answers it (#46866):
                # never an interrupt (read as deny) nor a queued turn (auto-deny deadlock).
                # A turn a slash command produced (``/queue yes``) is never live typing.
                if self._is_session_running(_quick_key) and not getattr(event, "_fifo_only", False) and (
                        await self._route_plaintext_approval_while_busy(event, _quick_key)
                        or await self._hm_busy_preempt(event, source, _quick_key)):
                    return True, None
                _consumed, _consumer_reply = await self._hm_post_admission_consume(event, source, _quick_key)
                if _consumed:
                    return True, _consumer_reply
                if getattr(event, "_fifo_only", False) and event._stop_epoch != self._route_stop_epoch(_quick_key):
                    return True, None  # a /stop or /new came after its command, as it clears the slot
                # A failed admit stays the authority's answer (refusal notice or turn error), never a
                # pending legacy turn: that would run unadmitted, outside the FIFO's order.
                return True, await admit_message(authority, event)
        if (authority is None and not is_internal and not event.get_command()
                and getattr(self, 'session_authority', None) is not None):
            # Scoped to a home this process does not serve: never fall back to the launch ledger.
            from hermes_state_runtime import RuntimeStoreError
            raise RuntimeStoreError('profile_mismatch')
        return False, None

    async def _handle_admitted_message(self, event: MessageEvent) -> Optional[str]:  # health: allow CC -- this is the single ordered admission boundary for session controls and loop wakes.
        """Handle an incoming message from any platform: auth → command check → running-agent
        interrupt → get/create session → build context → run agent → return response."""
        from gateway.run import _AGENT_PENDING_SENTINEL
        _admitted = await self._hm_admit_event(event)
        if _admitted is None:
            return None
        event, source, is_internal = _admitted
        if getattr(event, "_outbox_duplicate", False):
            # The original response was already acknowledged. Returning it
            # here would send it a second time.
            return None
        if (self._draining and not self._hm_is_registered_command(event)
                and not handover_accepts_received(self, source)):
            from gateway.session_retry_admission import refuse_deferred_retry
            refuse_deferred_retry(event)
            session_key = self._session_key_for_source(source)
            self._schedule_attended_user_activity(event, source, session_key)
            self._preserve_drain_event(session_key, event)
            return None
        # Expand alias quick commands before the running-session split (fork patch: the idle
        # path re-expands harmlessly since the target is then a resolvable built-in).
        alias_text = self._quick_command_alias_text(event)
        if alias_text is not None:
            event.text = alias_text
        _attended_activity_marked = False
        if not is_internal:
            from hermes_cli.observability.shared_metrics_events import record_gateway_slash_command
            record_gateway_slash_command(event)
        _paused_notice = self._hm_estop_gate(event, source, is_internal)
        if _paused_notice is not None:
            return _paused_notice

        from gateway.telegram_retire import intercept
        _retire_reply = await intercept(self, event)
        if _retire_reply is not None:
            return _retire_reply
        _quick_key = self._session_key_for_source(source)
        _reply = await self._hm_pending_reply_intercepts(event, source, _quick_key)
        if _reply is not None:
            return _reply

        # Routed profile authorities own non-command external turns. Busy preemption still
        # applies before durable FIFO admission; the fork's zero-cut busy path remains below
        # for in-process ownership.
        _owned, _authority_reply = await self._hm_authority_admission(
            event, source, _quick_key, is_internal,
        )
        if _owned:
            return _authority_reply

        from gateway.zero_cut_busy import handle_local_busy, queue_foreign_owner, _NOT_HANDLED
        if await queue_foreign_owner(self, event, _quick_key):
            return None
        _busy_result = await handle_local_busy(self, event, source, _quick_key)
        if _busy_result is not _NOT_HANDLED:
            return _busy_result

        # Dispatch idle commands before the attended activity bookkeeping.  The disabled path must
        # retain the pre-feature command latency: checking the opt-in config here would otherwise add
        # a worker hop before every /command and can miss the adapter's bounded response window.
        _handled, _result = await self._hm_dispatch_idle_commands(event, source, _quick_key)
        if _handled:
            # Activity bookkeeping is telemetry for an already-dispatched command.  Do not hold the
            # adapter's bounded command response on the opt-in config/store worker hop.
            self._schedule_attended_user_activity(event, source, _quick_key)
            return _result

        # Non-command messages are stamped after the session slot is claimed, immediately before
        # model resolution. Awaiting the store lookup before the claim reopens the duplicate-agent
        # race that the pending sentinel is designed to close.
        if self._is_session_running(_quick_key):
            return await self._hm_handle_running_session_message(event, source, _quick_key)

        # Pending exec approvals go through /approve and /deny only — no bare-text matching, or a
        # conversational "yes" would execute a dangerous command.
        if not is_internal:
            if await asyncio.to_thread(self._is_telegram_topic_root_lobby, source):
                # Debounced so a user who forgets about topic mode doesn't get ten reminders.
                if self._should_send_telegram_lobby_reminder(source):
                    return self._telegram_topic_root_lobby_message()
                return None
            if await self._hm_try_passive_fyi_relay(event, source, _quick_key):
                return None
            if self._is_session_running(_quick_key):
                # The passive check awaits; a turn that started meanwhile owns the session now.
                return await self._hm_handle_running_session_message(event, source, _quick_key)
            # External-drain new-turn gate: when NAS engaged an external drain (.drain_request.json,
            # seen by _drain_control_watcher), refuse to START new turns so the in-flight set can
            # only fall to zero. Reversible.
            if self._external_drain_active and not handover_accepts_received(self, source):
                logger.info("Refusing new turn for session %s — external drain active.", _quick_key)
                return t("gateway.busy.draining_maintenance")

        # Both human inputs and gateway wakes remain retryable while compression
        # owns the transcript. Slash controls above remain available.
        if await self._defer_compression_locked_event(_quick_key, event):
            return None
        if self._is_session_running(_quick_key):
            # The durable lock lookup yielded; another turn may now own the slot.
            return await self._hm_handle_running_session_message(event, source, _quick_key)


        # Claim this session before any await: many awaits sit between here and _run_agent
        # registering the real AIAgent; without this sentinel a second message during any of them
        # passes the "already running" guard and spins up a duplicate agent for the same session.
        _loop_fence = getattr(event, "_gateway_loop_execution_fence", None)
        if not self._loop_wake_execution_fence_sync(event, _loop_fence):
            self._log_loop_wake_suppressed(event, "superseded before the session slot claim")
            return None
        _active_session_lease, _limit_message = self._claim_active_session_slot(_quick_key, source)
        if _limit_message is not None:
            logger.info("Rejecting new active session %s: max_concurrent_sessions reached", _quick_key)
            await self._release_unsettled_loop_tick(event, ran=False)
            return _limit_message

        event, source, is_internal = self._hm_rescue_orphaned_fifo(event, source, is_internal, _quick_key)
        event._gateway_compression_deferred = False

        # A rewrite continues the wake's admission generation. No await may separate this
        # check from sentinel installation, and no rejected wake inherits a successor token.
        _loop_fence = getattr(event, "_gateway_loop_execution_fence", None)
        if not self._loop_wake_execution_fence_sync(event, _loop_fence):
            if _active_session_lease is not None:
                _active_session_lease.release()
            self._log_loop_wake_suppressed(event, "superseded before the running-agent sentinel")
            return None
        _run_generation = (
            _loop_fence[2] if _loop_fence is not None
            else self._begin_session_run_generation(_quick_key)
        )
        _claim_state = self._session_state(_quick_key)
        if _active_session_lease is not None:
            _claim_state.turn.lease = _active_session_lease
        _claim_state.turn.agent = _AGENT_PENDING_SENTINEL
        _claim_state.turn.event = event
        _claim_state.turn.started_ts = _claim_state.conversation.last_turn_started_at = time.time()
        from hermes_cli.observability.shared_metrics_gateway import start_reply_clock
        start_reply_clock(source, internal=is_internal)
        from gateway.reply_latency import bind_agent_start
        bind_agent_start(event)
        self._persist_active_agents()
        # The admission context is set by the durable executor, not by direct legacy dispatch.
        from gateway.session_ingress import executing_admission
        _preserve_lease_wait_on_shutdown = False
        try:
            # Mark genuine user activity inside the claimed turn, before resolving the model. This
            # preserves the attended route's same-turn semantics without delaying the sentinel claim.
            _attended_activity_marked = await self._mark_attended_user_activity(event, source, _quick_key)
            if _attended_activity_marked:
                event._attended_activity_marked = True
            if not is_internal and not executing_admission.get():  # fail-open plugin consume, inside the claimed slot (#129958)
                from gateway.run_inbound_consumer import run_post_admission_hook
                _consumed, _consumer_reply = await run_post_admission_hook(self, event, source, _quick_key)
                if _consumed:
                    return _consumer_reply
            try:
                _agent_result = await self._handle_message_with_agent(event, source, _quick_key, _run_generation)
            except TurnLeaseTimeoutError as exc:
                # The bounded wait expired or its durable spool failed. No turn ran,
                # so refund the wake tick and bypass the /goal judge for the resend notice.
                logger.error(
                    "Cannot run queued turn for routing key %s on session %s: "
                    "turn-lease wait expired or input could not be preserved; "
                    "transcript load was not started and the user must resend",
                    _quick_key, exc.session_id,
                )
                await self._release_unsettled_loop_tick(event, ran=False)
                return t("gateway.busy.another_turn_running")
            try:
                if not getattr(event, "_gateway_compression_deferred", False):
                    await self._run_post_turn_hooks(
                        agent_result=_agent_result, source=source, is_internal=is_internal, event=event,
                    )
            except Exception as _goal_exc:
                logger.debug("post-turn hook failed: %s", _goal_exc)
            return _agent_result
        except asyncio.CancelledError:
            # Shutdown may replay an unfinished turn, but a stop/reset invalidates
            # its generation even when the gateway is already draining.
            _preserve_lease_wait_on_shutdown = (
                self._draining and self._is_session_run_current(_quick_key, _run_generation)
            )
            raise
        finally:
            with suppress(Exception):
                self._session_state(_quick_key).conversation.attended_new_session = False
                self._session_state(getattr(event, "_hermes_attended_session_key", _quick_key)).conversation.attended_new_session = False
            # One-shot restore runs on every exit; the generation guard ignores displaced turns.
            self._restore_pending_one_turn_model_override(_quick_key, _run_generation)
            # SIGKILL/OOM skips finally, leaving the durable marker for the next unclean startup's
            # recovery pass. A turn the adapter delivers hands its marker to that lifecycle, which
            # clears it only once the reply is in the delivery ledger (else a kill in between
            # left neither marker nor ledger row and the persisted reply was never sent).
            if getattr(event, "_gateway_lease_wait_spool", None) is not None:
                from gateway.run_turn_lease_wait import retire_lease_wait
                with suppress(Exception):
                    await asyncio.to_thread(
                        retire_lease_wait, self, event, discard=not _preserve_lease_wait_on_shutdown,
                    )
            if not getattr(event, "_turn_marker_handoff", False):
                await self._clear_durable_active_turn(event)
            # Release only this turn's generation. Eviction may immediately admit a replacement
            # through the cold path; an unconditional release here would then clear the replacement
            # sentinel/agent and lease. Reset/stop release their stale slot before installing a
            # successor, preserving reset-zombie cleanup without granting gen-N successor authority.
            self._release_running_agent_state(_quick_key, run_generation=_run_generation)
            # Turn lease is keyed by (routing key, run generation) so this unwind can only free
            # the lease its own turn acquired, never a newer turn's.
            self._release_turn_lease(_quick_key, _run_generation)
            # Deferred commands wait for the runner's turn, not the adapter task that received them:
            # the two can diverge (an internal wake running after /stop), so resume them here.
            _deferred_adapter = self._delivery_adapter_for(source)
            _resume_deferred = getattr(_deferred_adapter, "resume_deferred_commands", None)
            if callable(_resume_deferred):
                _resume_deferred(_quick_key)
            if getattr(event, "_gateway_compression_deferred", False):
                self._start_compression_deferred_drain(_quick_key, _deferred_adapter)

    def _restore_pending_one_turn_model_override(self, session_key: str, run_generation: int | None = None) -> None:
        """Restore the per-session model override captured by ``/model --once`` or ``/moa``.

        With ``run_generation`` (the turn finalizer) the restore happens only while that generation
        is still current; a stop/reset/eviction has already settled the snapshot itself (see
        ``_invalidate_session_run_generation``), so the displaced finalizer finds nothing to do.
        Without it (the settlement paths) the restore is unconditional."""
        if not session_key:
            return
        try:
            _otr_state = self._peek_session_state(session_key)
            if _otr_state is None or not _otr_state.conversation.one_turn_restore:
                return
            if run_generation is not None and not self._is_session_run_current(session_key, run_generation):
                return
            snapshot = _otr_state.conversation.one_turn_restore
            _otr_state.conversation.one_turn_restore = None
            self._restore_session_model_override(session_key, snapshot)
        except Exception:
            logger.debug("Failed to restore one-turn model override", exc_info=True)

    def _prefix_inbound_sender_context(self, event: MessageEvent, source: SessionSource, message_text: str) -> str:
        """Attribute the sender in shared multi-user sessions and prepend history-backfill channel context."""
        _is_shared_multi_user = is_shared_multi_user_session(
            source, group_sessions_per_user=getattr(self.config, "group_sessions_per_user", True),
            thread_sessions_per_user=getattr(self.config, "thread_sessions_per_user", False),
        )
        if _is_shared_multi_user and source.user_name:
            # Display names are attacker-influenceable: neutralize newlines/control chars or a
            # hostile name masquerades as a fake markdown section (mirrors build_session_context_prompt).
            _safe_user_name = neutralize_untrusted_inline_text(source.user_name)
            # Slack: expose the CURRENT speaker's verifiable `<@U...>` id so "mention me again" has a
            # trusted target (display names are ambiguous). user_id comes from the envelope, not user-editable.
            # See #17916.
            if source.platform == Platform.SLACK and source.user_id:
                _safe_user_name = f"{_safe_user_name} | Slack user <@{source.user_id}>"
            message_text = f"[{_safe_user_name}] {message_text}"
        # After the sender-prefix so the prefix applies only to the trigger message, not the backfill.
        if getattr(event, "channel_context", None):
            message_text = f"{event.channel_context}\n\n[New message]\n{message_text}"
        return message_text

    @staticmethod
    def _classify_inbound_media(
        event: MessageEvent, pending_stt_prepared: bool
    ) -> tuple[list, list, list, list]:
        """Split ``event.media_urls`` into (image, STT-voice, audio-file, video) paths. Per-attachment
        MIME wins over the message-level type (a document sent alongside an image must not be routed
        as an image). MessageType.AUDIO / mixed DOCUMENT audio is a file attachment, never STT."""
        from gateway.run import _event_media_is_audio, _event_media_is_image, _event_media_is_stt_input
        image_paths, audio_paths, audio_file_paths, video_paths = [], [], [], []
        for i, path in enumerate(event.media_urls or []):
            mtype = event.media_types[i] if i < len(event.media_types) else ""
            if _event_media_is_image(event, i):
                image_paths.append(path)
            if _event_media_is_audio(event, i):
                if event.message_type in {MessageType.AUDIO, MessageType.DOCUMENT}:
                    audio_file_paths.append(path)
                elif not pending_stt_prepared and _event_media_is_stt_input(event, i):
                    audio_paths.append(path)
            if mtype.startswith("video/") or (not mtype and event.message_type == MessageType.VIDEO):
                video_paths.append(path)
        return image_paths, audio_paths, audio_file_paths, video_paths

    async def _enrich_inbound_images(
        self, source: SessionSource, session_key: str, message_text: str, image_paths: list[str]
    ) -> str:
        """Route images natively (attach pixels at run_conversation) or pre-analyze them into text."""
        # See agent/image_routing.py. Offloaded to a thread: the decision does blocking network I/O
        # (models.dev fetch on cache miss, Ollama /api/show probe) that would stall the event loop.
        _img_mode = await asyncio.to_thread(
            self._decide_image_input_mode, source=source, session_key=session_key,
        )
        if _img_mode == "native":
            self._session_state(session_key).persistent.native_image_paths = list(image_paths)
            logger.info(
                "Image routing: native (model supports vision). %d image(s) will be attached inline.",
                len(image_paths),
            )
            return message_text
        logger.info(
            "Image routing: text (mode=%s). Pre-analyzing %d image(s) via vision_analyze.",
            _img_mode, len(image_paths),
        )
        # Vision enrichment runs before AIAgent.run_conversation(), so bind this session's resolved
        # runtime explicitly rather than consulting process-global compatibility mirrors.
        vision_runtime = None
        try:
            turn_model, runtime_kwargs = await self._resolve_session_agent_runtime_async(
                source=source, session_key=session_key,
            )
            vision_runtime = {**(runtime_kwargs or {}), "model": turn_model}
        except Exception:
            logger.debug("vision enrichment: session runtime resolution failed", exc_info=True)

        from agent.auxiliary_client import scoped_runtime_main

        with scoped_runtime_main(vision_runtime):
            return await self._enrich_message_with_vision(message_text, image_paths)

    async def _enrich_inbound_voice(
        self, event: MessageEvent, source: SessionSource, message_text: str, audio_paths: list[str]
    ) -> str:
        message_text, _successful_transcripts = await self._enrich_message_with_transcription(
            message_text, audio_paths,
        )
        # Echo each successful transcript back immediately when configured so users can verify STT
        # quality in real time. On transcription failure do NOT send a hardcoded notice: that
        # bypassed the LLM and produced two replies; enrichment leaves one neutral marker instead.
        if _successful_transcripts and self._should_echo_stt_transcripts():
            _echo_adapter = self._delivery_adapter_for(source)
            if _echo_adapter:
                _echo_meta = self._thread_metadata_for_source(source, self._reply_anchor_for_event(event))
                await self._echo_stt_transcripts(
                    _echo_adapter, source, _successful_transcripts, event=event, metadata=_echo_meta,
                )
        return message_text

    @staticmethod
    def _inbound_attachment_display_name(path: str) -> tuple[str, str]:
        """``(display_name, agent_visible_path)``: cache filename is ``<id>_<id>_<original>``; the
        path is translated to the in-container mount under a Docker backend."""
        from tools.credential_files import to_agent_visible_cache_path
        basename = os.path.basename(path)
        parts = basename.split("_", 2)
        return re.sub(r'[^\w.\- ]', '_', parts[2] if len(parts) >= 3 else basename), to_agent_visible_cache_path(path)

    @classmethod
    def _prepend_inbound_media_file_notes(cls, message_text: str, audio_file_paths: list[str], video_paths: list[str]) -> str:
        """Prepend a path-pointing note per audio-file / video attachment (content is not inlined)."""
        for kind, noun, verb, tool, paths in (
            ("an audio file attachment", "audio", "transcribe or process", "a transcription or media tool", audio_file_paths),
            ("a video attachment", "video", "inspect or process", "a video analysis or media tool", video_paths),
        ):
            for _path in paths:
                _display, _agent_path = cls._inbound_attachment_display_name(_path)
                message_text = (
                    f"[The user sent {kind}: '{_display}'. "
                    f"It is saved at: {_agent_path}. "
                    f"Its content is not inlined here. If the user's request involves "
                    f"what the {noun} contains, {verb} it yourself — for "
                    f"example by passing the path to {tool} — "
                    f"instead of asking the user to describe it. Only ask what to do "
                    f"with it if their intent is genuinely unclear.]"
                    f"\n\n{message_text}"
                )
        return message_text

    @classmethod
    def _prepend_inbound_document_notes(cls, event: MessageEvent, message_text: str) -> str:
        """Prepend a context note per non-media attachment (anything not routed as image/audio/video)."""
        from gateway.run import (
            _build_document_context_note, _event_media_is_audio, _event_media_is_image,
            _event_media_is_video,
        )
        if not event.media_urls:
            return message_text
        import mimetypes as _mimetypes

        _TEXT_EXTENSIONS = {".txt", ".md", ".csv", ".log", ".json", ".xml", ".yaml", ".yml", ".toml", ".ini", ".cfg"}
        inline_flags = getattr(event, "media_text_inlined", None) or []
        for i, path in enumerate(event.media_urls):
            # A document mixed into a PHOTO/VOICE message (message-level type != DOCUMENT) still
            # reaches the agent; only genuine non-media files get a note.
            if any(f(event, i) for f in (_event_media_is_image, _event_media_is_audio, _event_media_is_video)):
                continue
            mtype = event.media_types[i] if i < len(event.media_types) else ""
            if mtype in {"", "application/octet-stream"}:
                _is_text = os.path.splitext(path)[1].lower() in _TEXT_EXTENSIONS
                mtype = "text/plain" if _is_text else (_mimetypes.guess_type(path)[0] or "application/octet-stream")
            # Every accepted file gets a note — a non-text/non-application MIME (font/*, model/*)
            # must still tell the agent the file exists.
            display_name, agent_path = cls._inbound_attachment_display_name(path)
            inline_flag = inline_flags[i] if i < len(inline_flags) else None
            context_note = _build_document_context_note(
                display_name, agent_path, mtype, content_inlined=inline_flag is not False,
            )
            message_text = f"{context_note}\n\n{message_text}"
        return message_text

    @staticmethod
    def _prepend_inbound_reply_context(event: MessageEvent, source: SessionSource, message_text: str) -> str:
        """Prepend the reply-to pointer, then the Discord triggering-message note (outermost)."""
        if getattr(event, "reply_to_text", None) and event.reply_to_message_id:
            # Always inject the reply-to pointer even when the quoted text is already in history:
            # it's disambiguation (*which* prior message), not deduplication.
            # Adapters resolve the original message (or the user's native partial quote).
            # A preview here silently loses later list items and code; keep that context intact.
            # JSON quoting preserves the complete resolved text without allowing its quotes or
            # paragraph breaks to collide with the pointer's closing delimiter.
            reply_text = json.dumps(event.reply_to_text, ensure_ascii=False)
            _who = " your previous message" if getattr(event, "reply_to_is_own_message", False) else ""
            message_text = f'[Replying to{_who}: {reply_text}]\n\n{message_text}'

        # Discord: the triggering message id goes on the per-turn user message, never the cached
        # system prompt — it changes every turn and would bust the agent-cache signature. It is
        # the OUTERMOST prefix so strip_discord_triggering_note can peel exactly it off the
        # persisted transcript row without touching the reply pointer.
        if (
            source is not None
            and getattr(source, "platform", None) == Platform.DISCORD
            and getattr(event, "message_id", None)
        ):
            from gateway.session import _discord_tools_loaded as _disc_tools_loaded
            if _disc_tools_loaded():
                message_text = f"{discord_triggering_note(event.message_id)}\n\n{message_text}"
        return message_text

    async def _inbound_model_context_length(self, source: SessionSource, session_key: str) -> int:
        """Context length of the model this turn runs on. A global ``model.context_length`` pin
        belongs to the configured model, not a /model or channel override; custom-provider limits win."""
        from gateway.run import _load_gateway_config
        from agent.model_metadata import get_model_context_length_async

        _msg_config_ctx = None
        _msg_cfg = None
        _msg_model_cfg = {}
        _msg_custom_providers = []
        with suppress(Exception):
            _msg_cfg = _load_gateway_config()
        _msg_cfg = _msg_cfg or {}
        _msg_model_cfg = _msg_cfg.get("model", {})
        from hermes_cli.model_preset_runtime import resolve_default_route
        _msg_default_route = resolve_default_route(_msg_cfg)
        if isinstance(_msg_model_cfg, dict):
            _msg_raw_ctx = _msg_model_cfg.get("context_length")
            if _msg_raw_ctx is not None:
                _msg_config_ctx = int(_msg_raw_ctx)
        try:
            from hermes_cli.config import get_compatible_custom_providers

            _msg_custom_providers = get_compatible_custom_providers(_msg_cfg)
        except Exception:
            _msg_custom_providers = _msg_cfg.get("custom_providers") or []
        # GatewayRunner has no self._model/self._base_url; resolve the session's actual runtime.
        _msg_model, _msg_runtime = await self._resolve_session_agent_runtime_async(
            source=source, session_key=session_key, user_config=_msg_cfg,
        )
        _msg_base_url = _msg_runtime.get("base_url") or ""
        _msg_configured_model = _msg_default_route.model
        if _msg_model != _msg_configured_model:
            _msg_config_ctx = None
        if _msg_config_ctx is not None:
            try:
                from hermes_cli.route_identity import should_clear_context_pin_async

                if await should_clear_context_pin_async(
                    None, None,  # model match already checked above
                    _msg_default_route.base_url, _msg_base_url,
                    _msg_default_route.provider, _msg_runtime.get("provider"),
                ):
                    _msg_config_ctx = None
            except Exception:
                _msg_config_ctx = None
        if _msg_custom_providers and _msg_base_url:
            with suppress(Exception):
                from hermes_cli.config import get_custom_provider_context_length

                _msg_config_ctx = get_custom_provider_context_length(
                    model=_msg_model, base_url=_msg_base_url, custom_providers=_msg_custom_providers,
                ) or _msg_config_ctx
        return await get_model_context_length_async(
            _msg_model, base_url=_msg_base_url, api_key=_msg_runtime.get("api_key") or "",
            config_context_length=_msg_config_ctx, provider=_msg_runtime.get("provider") or "",
            custom_providers=_msg_custom_providers, api_mode=_msg_runtime.get("api_mode") or "",
        )

    async def _expand_inbound_context_references(
        self, source: SessionSource, session_key: str, message_text: str
    ) -> Optional[str]:
        """Expand ``@`` context references; returns None when the injection was refused (user notified)."""
        from gateway.session_context_refs import inbound_references
        expanded, refusal = await inbound_references(self, source, session_key, message_text)
        if refusal is None:
            return expanded
        _adapter = self._delivery_adapter_for(source)
        if _adapter:
            await _adapter.send(source.chat_id, refusal)
        return None

    async def _prepare_inbound_message_text(
        self, *, event: MessageEvent, source: SessionSource, history: list[dict[str, Any]],
        session_key: Optional[str] = None,
    ) -> Optional[str]:
        """Prepare inbound event text for the agent. Shared by the normal inbound and queued
        follow-up paths so attribution, image enrichment, STT, document notes, reply context and
        @ references behave the same. Side effect: buffers per-session native image paths when the
        model supports native vision; the caller consumes that buffer at ``run_conversation``."""
        rehome_inbound_media(event)  # before any consumer (vision, STT, document notes) reads media_urls
        _pending_stt_prepared = hasattr(event, "_gateway_pending_stt_text")
        message_text = (event._gateway_pending_stt_text if _pending_stt_prepared else event.text) or ""
        # Prefer the caller's resolved session key so this write key matches the consume key at the
        # run_conversation site; derive it here only for tests and legacy standalone callers.
        session_key = session_key or self._session_key_for_source(source)
        # Reset only this session's per-call buffer; other sessions may be concurrently preparing.
        self._consume_pending_native_image_paths(session_key)

        message_text = self._prefix_inbound_sender_context(event, source, message_text)
        image_paths, audio_paths, audio_file_paths, video_paths = self._classify_inbound_media(event, _pending_stt_prepared)
        if image_paths:
            message_text = await self._enrich_inbound_images(source, session_key, message_text, image_paths)
        if audio_paths:
            message_text = await self._enrich_inbound_voice(event, source, message_text, audio_paths)
        message_text = self._prepend_inbound_media_file_notes(message_text, audio_file_paths, video_paths)
        message_text = self._prepend_inbound_document_notes(event, message_text)
        from gateway.session_context_refs import turn_references
        message_text = await turn_references(self, event, source, session_key, message_text)
        if message_text is None:
            return None
        # After expansion: the quoted reply is someone else's text and stays literal — an
        # ``@file:`` inside it must never read a local file on the replier's behalf.
        return self._prepend_inbound_reply_context(event, source, message_text)

    async def _prepare_profile_scoped_inbound_message_text(
        self, *, event: MessageEvent, source: SessionSource, history: list[dict[str, Any]],
        session_key: Optional[str] = None,
    ) -> Optional[str]:
        """Run inbound preprocessing under the routed profile when multiplexed."""
        from gateway.run import _async_profile_runtime_scope
        kwargs = dict(event=event, source=source, history=history, session_key=session_key)
        if getattr(getattr(self, "config", None), "multiplex_profiles", False):
            async with _async_profile_runtime_scope(self._resolve_profile_home_for_source(source)):
                return await self._prepare_inbound_message_text(**kwargs)
        return await self._prepare_inbound_message_text(**kwargs)

    async def _prepare_clarify_reply_text(self, event) -> str:
        """Return raw text or successful voice transcripts for a clarify reply."""
        if not self._pending_event_audio_paths(event):
            return (event.text or "").strip()
        _, successful_transcripts = await self._transcribe_pending_audio_event_once(event, "")
        return "\n\n".join(t.strip() for t in successful_transcripts if t.strip())

    def _consume_pending_native_image_paths(self, session_key: str) -> list[str]:
        state = self._peek_session_state(session_key)
        paths = list(state.persistent.native_image_paths or []) if state is not None else []
        if paths:
            state.persistent.native_image_paths = []
        return paths

    async def _mark_durable_active_turn(self, event: MessageEvent, session_key: str) -> bool:
        """Persist the exact resolved routing key for this running turn; never a cron review's, which boot would resume."""
        try:
            token = None if is_review_event(event) else await self.async_session_store.mark_turn_active(
                session_key, human=bool(getattr(self, "_is_user_turn_event", lambda _event: not event.internal)(event))
            )
        except Exception as exc:
            logger.warning("Could not persist active-turn marker for %s: %s", session_key, exc)
            return False
        if not token:
            return False
        # Private event attributes are process-local ownership state: keep the token out of public
        # metadata, transcripts, and platform payloads.
        event._gateway_active_turn_session_key = session_key
        event._gateway_active_turn_token = token
        return True

    async def _clear_durable_active_turn(self, event: MessageEvent) -> bool:
        """Best-effort CAS clear of the marker owned by *event* (3 attempts; never blocks agent/lease
        release — a stale marker is bounded by the agent timeout and clean-start discard)."""
        session_key = getattr(event, "_gateway_active_turn_session_key", None)
        token = getattr(event, "_gateway_active_turn_token", None)
        try:
            if not session_key or not token:
                return False
            last_error: Optional[Exception] = None
            for attempt in range(1, 4):
                try:
                    return bool(await self.async_session_store.clear_turn_active(session_key, token))
                except Exception as exc:
                    last_error = exc
                    if attempt < 3:
                        logger.debug(
                            "Retrying active-turn marker cleanup for %s (%d/3): %s",
                            session_key, attempt, exc,
                        )
            logger.warning(
                "Could not clear active-turn marker for %s after 3 attempts: %s", session_key, last_error,
            )
            return False
        finally:
            for attr in ("_gateway_active_turn_session_key", "_gateway_active_turn_token"):
                with suppress(AttributeError):
                    delattr(event, attr)

    def _decide_image_input_mode(
        self, *, source: Optional[SessionSource] = None, session_key: Optional[str] = None,
        user_config: Optional[dict] = None, provider: Optional[str] = None,
        model: Optional[str] = None,
    ) -> str:
        """Resolve image-input routing (``"native"`` / ``"text"``) for the effective model this turn
        (see agent/image_routing.py). Sessions can carry /model overrides and this runs before AIAgent
        sets the auxiliary_client runtime globals, so resolve the per-session runtime bundle the
        upcoming turn will use, not just the persisted default."""
        try:
            from agent.image_routing import decide_image_input_mode
            from agent.auxiliary_client import _read_main_model, _read_main_provider
            from hermes_cli.config import load_config

            cfg = user_config if isinstance(user_config, dict) else load_config()
            resolved_provider = (provider or "").strip()
            resolved_model = (model or "").strip()
            resolved_requested_provider = ""

            if (not resolved_provider or not resolved_model) and (source is not None or session_key):
                try:
                    turn_model, runtime_kwargs = self._resolve_session_agent_runtime(
                        source=source, session_key=session_key, user_config=cfg,
                    )
                    rk = runtime_kwargs if isinstance(runtime_kwargs, dict) else {}
                    if not resolved_model and isinstance(turn_model, str):
                        resolved_model = turn_model.strip()
                    if not resolved_provider and isinstance(rk.get("provider"), str):
                        resolved_provider = rk["provider"].strip()
                    if isinstance(rk.get("requested_provider"), str):
                        resolved_requested_provider = rk["requested_provider"].strip()
                except Exception as exc:
                    logger.debug(
                        "image_routing: session runtime resolution failed, falling back to config — %s",
                        exc,
                    )

            return decide_image_input_mode(
                resolved_provider or _read_main_provider(), resolved_model or _read_main_model(),
                cfg, requested_provider=resolved_requested_provider,
            )
        except Exception as exc:
            logger.debug("image_routing: decision failed, falling back to text — %s", exc)
            return "text"

    async def _enrich_message_with_vision(self, user_text: str, image_paths: list[str]) -> str:
        """Auto-analyze user-attached images with the vision tool and prepend the descriptions.
        Description *and* local cache path are injected so the model understands the image without
        a tool call and can re-examine it with vision_analyze."""
        from tools.vision_tools import vision_analyze_tool
        from agent.memory_context import sanitize_context

        analysis_prompt = (
            "Concisely describe this image in English using 2-4 sentences "
            "(~150 words). Preserve quoted text and identifiers in their original language. "
            "Cover the main subject, key visible text/data/code, and overall context. "
            "If it is a chart, diagram, or scientific figure, include the important "
            "labels, legend, and key values. Skip decorative details."
        )
        enriched_parts = []
        for path in image_paths:
            try:
                logger.debug("Auto-analyzing user image: %s", path)
                result = json.loads(await vision_analyze_tool(image_url=path, user_prompt=analysis_prompt))
                if result.get("success"):
                    description = sanitize_context(result.get("analysis", ""))
                    note = (
                        f"[The user sent an image~ Here's what I can see:\n{description}]\n"
                        f"[If you need a closer look, use vision_analyze with "
                        f"image_url: {path} ~]"
                    )
                else:
                    note = (
                        "[The user sent an image but I couldn't quite see it "
                        "this time (>_<) You can try looking at it yourself "
                        f"with vision_analyze using image_url: {path}]"
                    )
            except Exception as e:
                logger.error("Vision auto-analysis error: %s", e)
                note = (
                    f"[The user sent an image but something went wrong when I "
                    f"tried to look at it~ You can try examining it yourself "
                    f"with vision_analyze using image_url: {path}]"
                )
            enriched_parts.append(note)
        if not enriched_parts:
            return user_text
        prefix = "\n\n".join(enriched_parts)
        return f"{prefix}\n\n{user_text}" if user_text else prefix

    _EMPTY_TEXT_PLACEHOLDER = "(The user sent a message with no text content)"

    @classmethod
    def _prepend_media_prefix(cls, prefix: str, user_text: str) -> str:
        """``prefix`` + the user's text; the Discord empty-content placeholder is dropped as redundant."""
        if user_text and user_text.strip() != cls._EMPTY_TEXT_PLACEHOLDER:
            return f"{prefix}\n\n{user_text}"
        return prefix

    @staticmethod
    def _untranscribed_audio_note(path: str) -> str:
        """One minimal neutral marker for every STT failure. Never mention "no STT provider" or setup
        steps — persisted in history they make the model keep volunteering STT-setup advice."""
        from tools.credential_files import to_agent_visible_cache_path
        agent_path = to_agent_visible_cache_path(os.path.abspath(path))
        return f"[voice message could not be transcribed automatically; the audio is available at: {agent_path}]"

    async def _transcribe_one_clip(self, path: str, transcribe_audio, transcribe_audio_local_fallback) -> tuple[Optional[str], str]:
        """``(transcript_or_None, note)`` for one clip via configured STT with local fallback."""
        result = await asyncio.to_thread(transcribe_audio, path, None, "gateway")
        if not result.get("success"):
            fallback = await asyncio.to_thread(transcribe_audio_local_fallback, path)
            if fallback.get("success"):
                logger.info("Configured STT failed for %s; recovered with local STT", path)
                result = fallback
        if not result["success"]:
            logger.info("Voice transcription failed for %s: %s", path, result.get("error", "unknown error"))
            return None, self._untranscribed_audio_note(path)
        transcript = result["transcript"]
        # STT may return success=True with an empty/whitespace transcript (silence, cut-off);
        # empty quotes make the agent reply to nothing and can loop, so emit a sentinel note.
        # See #41603.
        if not (transcript or "").strip():
            return None, (
                "[The user sent a voice message but it came through "
                "empty or inaudible — speech-to-text returned no "
                "words. Do not guess at the content; ask the user "
                "to resend or type it out.]"
            )
        # Plain quoted line: a "The user sent a voice message..." wrapper read as a meta-instruction
        # and made the LLM comment on voice mode instead.
        return transcript, f'"{transcript}"'

    async def _enrich_message_with_transcription(
        self, user_text: str, audio_paths: list[str], clip_memo: dict | None = None
    ) -> tuple[str, list[str]]:
        """Transcribe voice clips with the configured STT provider and prepend the transcripts →
        ``(enriched_text, successful_transcripts)``; the transcripts (input order; empty if every clip
        failed or STT is disabled) let callers echo them back before the agent loop. ``clip_memo``
        (path → ``_transcribe_one_clip`` result) skips clips an earlier pass already transcribed."""
        from gateway.run import _probe_audio_duration
        audio_paths = list(dict.fromkeys(audio_paths))
        if not getattr(self.config, "stt_enabled", True):
            notes = []
            for path in audio_paths:
                abs_path = os.path.abspath(path)
                duration_str = await _probe_audio_duration(abs_path)
                suffix = f" (duration: {duration_str})" if duration_str else ""
                notes.append(f"[The user sent a voice message: {abs_path}{suffix}]")
            return (self._prepend_media_prefix("\n\n".join(notes), user_text) if notes else user_text), []

        try:
            from tools.transcription_tools import (
                transcribe_audio, transcribe_audio_local_fallback
            )
        except ModuleNotFoundError as e:
            logger.error("Transcription module unavailable: %s", e)
            return self._prepend_media_prefix("[voice message could not be transcribed]", user_text), []

        enriched_parts = []
        successful_transcripts: list[str] = []
        for path in audio_paths:
            if clip_memo is not None and path in clip_memo:
                transcript, note = clip_memo[path]
                if transcript is not None:
                    successful_transcripts.append(transcript)
                enriched_parts.append(note)
                continue
            try:
                logger.debug("Transcribing user voice: %s", path)
                transcript, note = await self._transcribe_one_clip(
                    path, transcribe_audio, transcribe_audio_local_fallback,
                )
                if clip_memo is not None:
                    clip_memo[path] = (transcript, note)
                if transcript is not None:
                    successful_transcripts.append(transcript)
                enriched_parts.append(note)
            except Exception as e:
                logger.error("Transcription error: %s", e)
                note = self._untranscribed_audio_note(path)
                if clip_memo is not None:
                    clip_memo[path] = (None, note)
                enriched_parts.append(note)

        if enriched_parts:
            user_text = self._prepend_media_prefix("\n\n".join(enriched_parts), user_text)
        return user_text, successful_transcripts

    def _pending_event_audio_paths(self, event) -> list[str]:
        """Return STT-eligible paths from a pending voice message."""
        from gateway.run import _event_media_is_stt_input
        return [
            path for i, path in enumerate(getattr(event, "media_urls", None) or [])
            if _event_media_is_stt_input(event, i)
        ]

    async def _transcribe_pending_audio_event_once(
        self, event, user_text: Optional[str] = None
    ) -> tuple[str | None, list[str]]:
        """Transcribe a pending audio event once and cache the result on the event: the interrupt
        monitor and the pending-drain path both need it — one STT call and one echo per message."""
        if hasattr(event, "_gateway_pending_stt_text"):
            return event._gateway_pending_stt_text, list(getattr(event, "_gateway_pending_stt_transcripts", []) or [])
        # Concurrent callers (queued-voice prefetch, interrupt monitor, drain) share one STT call.
        task = getattr(event, "_gateway_pending_stt_task", None)
        if task is None:
            audio_paths = self._pending_event_audio_paths(event)
            if not audio_paths:
                return user_text if user_text is not None else (getattr(event, "text", None) or None), []
            text = user_text if user_text is not None else (getattr(event, "text", "") or "")
            semaphore = getattr(self, "_pending_stt_semaphore", None)
            if semaphore is None:
                semaphore = self._pending_stt_semaphore = asyncio.Semaphore(2)
            clip_memo = getattr(event, "_gateway_pending_stt_clips", None)
            if clip_memo is None:
                clip_memo = {}
                event._gateway_pending_stt_clips = clip_memo

            async def _transcribe():
                # Limit provider calls (including local fallback) across pending messages, not
                # just clips within one message. Queued events can fill the FIFO at once.
                async with semaphore:
                    return await self._enrich_message_with_transcription(text, audio_paths, clip_memo)

            task = self._retain_background_task(asyncio.create_task(_transcribe()))
            event._gateway_pending_stt_task = task

            def _settle(completed):
                if getattr(event, "_gateway_pending_stt_task", None) is not completed:
                    return  # the event was merged and its STT cache invalidated
                del event._gateway_pending_stt_task
                if completed.cancelled():
                    return
                try:
                    enriched, transcripts = completed.result()
                except Exception:
                    return  # a later drain may retry a failed task
                event._gateway_pending_stt_text = enriched
                event._gateway_pending_stt_transcripts = list(transcripts)

            task.add_done_callback(_settle)
        try:
            enriched_text, successful_transcripts = await asyncio.shield(task)
        except asyncio.CancelledError:
            raise
        except Exception:
            if getattr(event, "_gateway_pending_stt_task", None) is task:
                del event._gateway_pending_stt_task
            raise
        if getattr(event, "_gateway_pending_stt_task", None) is not task:
            # A merge changed the event while STT ran; retry from the merged event's own text, not
            # the pre-merge snapshot, or a caption merged mid-STT is cached away.
            return await self._transcribe_pending_audio_event_once(event)
        del event._gateway_pending_stt_task
        event._gateway_pending_stt_text = enriched_text
        event._gateway_pending_stt_transcripts = list(successful_transcripts)
        return enriched_text, successful_transcripts

    def _prefetch_queued_voice_transcript(self, event, adapter) -> None:
        """Start STT for a voice message queued behind an active run, so its transcript echoes now
        and the queued turn reuses it instead of transcribing after the current task finishes."""
        if (
            adapter is None or getattr(event, "internal", False)
            or hasattr(event, "_gateway_pending_stt_text")
            or getattr(event, "_gateway_pending_stt_prefetch", None) is not None
            or not self._pending_event_audio_paths(event)
        ):
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return  # no loop (sync caller): the drain path transcribes as before
        from gateway.run import _interim_metadata
        # The echo lands mid-turn: mark it interim so stream-is-the-message adapters keep the live
        # stream open rather than sealing it with the transcript.
        metadata = _interim_metadata(
            self._thread_metadata_for_source(event.source, self._reply_anchor_for_event(event))
        )
        event._gateway_pending_stt_prefetch = self._retain_background_task(asyncio.create_task(
            self._transcribe_and_echo_pending_voice(
                event, adapter, event.source, event.text or "", log_context="Voice-queue",
                metadata=metadata,
            )
        ))

    async def _echo_pending_stt_transcripts_once(
        self, event, adapter, source, transcripts: list[str], *, metadata=None,
        log_context: str = "Transcript",
    ) -> None:
        """Echo pending-event STT transcripts to the chat at most once. Tracked as a COUNT (not a
        set — identical transcripts are distinct deliveries): ``merge_pending_message_event`` can
        append a second voice note and invalidate the cache; the re-run returns earlier transcripts
        as a prefix, so only the unsent tail is echoed."""
        if not transcripts or not self._should_echo_stt_transcripts() or adapter is None:
            return
        already_echoed = int(getattr(event, "_gateway_pending_stt_echoed", 0) or 0)
        event._gateway_pending_stt_echoed = max(already_echoed, len(transcripts))
        await self._echo_stt_transcripts(
            adapter, source, transcripts[already_echoed:], event=event, metadata=metadata, log_context=log_context,
        )

    async def _transcribe_and_echo_pending_voice(
        self, event, adapter, source, text: str, *, log_context: str, metadata=_UNSET
    ) -> tuple[str, list[str]]:
        """Transcribe a pending voice event and echo transcripts once → ``(enriched_text,
        transcripts)`` for ``agent.interrupt()`` or the pending-drain flow; ``(text, [])`` when there
        is no STT-eligible media (caller owns the ``_build_media_placeholder`` fallback)."""
        if not self._pending_event_audio_paths(event):
            return text, []
        try:
            enriched_text, transcripts = await self._transcribe_pending_audio_event_once(event, text)
            if metadata is _UNSET:
                metadata = self._thread_metadata_for_source(source, self._reply_anchor_for_event(event))
            await self._echo_pending_stt_transcripts_once(
                event, adapter, source, transcripts, metadata=metadata, log_context=log_context
            )
            return enriched_text or text, transcripts
        except Exception as trans_exc:
            logger.warning("%s transcription failed: %s", log_context, trans_exc)
            return text, []
