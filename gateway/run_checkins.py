"""Gateway integration for automatic, bounded background-work supervision.

The heartbeat driver's profile-safe poll owns scheduling. Native automation admission owns
FIFO, reauthorization, restart recovery and delivery, just as for other generated wakeups.
"""
from __future__ import annotations

import asyncio
import copy
import functools
import inspect
import json
import logging
import time
import uuid

from hermes_cli.checkins import (
    CheckinConfig, CheckinManager, DISPLAY_KIND, checkin_prompt, fallback_status, outstanding_work,
)

logger = logging.getLogger(__name__)


def _manager(runner, entry):
    return CheckinManager(runner.session_store._db_for_key(entry.session_key), entry.session_id,
                          CheckinConfig.load())


def _observe(manager, session_id):
    if not manager.config.enabled:
        return []
    try:
        return outstanding_work(manager.db, session_id)
    except Exception:
        # An error is neither completion nor a new dependency. Retain only a previously
        # armed clock, including after restart, and tell the truth in its normal status.
        if manager.state is None:
            return []
        logger.warning("Supervisor roster unknown for %s", session_id, exc_info=True)
        return [{"kind": "unknown", "id": "roster-unavailable", "name": "unknown outstanding work, local roster unavailable",
                 "status": "unknown, inspect the local roster before claiming completion"}]


def _reconcile(runner, entry):
    manager = _manager(runner, entry)
    work = _observe(manager, entry.session_id)
    visible = getattr(runner, "_checkin_visible", {}).get((entry.session_key, manager.root))
    return manager.reconcile(work, last_visible_at=visible)


async def reconcile_turn(runner, source, session_key):
    """Retain the candidate on an observation error, so the next cheap scan retries it."""
    entry = await runner._run_in_executor_with_context(
        runner.session_store.lookup_by_session_key, session_key)
    if entry is None or entry.suspended:
        return
    watches = getattr(runner, "_checkin_watch", None)
    if watches is None:
        watches = runner._checkin_watch = {}
    watches[session_key] = (source, entry.session_id)
    try:
        state = await runner._run_in_executor_with_context(lambda: _reconcile(runner, entry))
        if state is None:
            watches.pop(session_key, None)
    except Exception:
        logger.warning("Supervisor roster unavailable for %s, will retry", session_key, exc_info=True)


def _queued(adapter, route):
    pending = getattr(adapter, "_pending_messages", {})
    batches = getattr(adapter, "_text_batches", {})
    return route in pending or route in batches


async def poll_checkins(runner):
    from gateway.session_authorities import active_authority
    from gateway.platforms.event import MessageEvent
    from hermes_state_runtime import list_session_admissions

    if getattr(runner, "_startup_restore_in_progress", False):
        return  # Registries and queued human input have not finished recovering yet.
    # New session identities are inspected once, including restored sessions after a restart.
    # Only armed/error candidates are observed on subsequent ticks, not the whole transcript DB.
    seen = getattr(runner, "_checkin_seen", None)
    if seen is None:
        seen = runner._checkin_seen = set()
    for entry in await runner._run_in_executor_with_context(runner.session_store.list_sessions):
        identity = (entry.session_key, entry.session_id)
        if identity in seen or entry.suspended or not entry.origin:
            continue
        source = runner._restored_source(entry)
        if source is None:
            continue
        scope = runner._async_profile_scope_for_source(source)
        if scope is None:
            continue
        async with scope:
            seen.add(identity)
            await reconcile_turn(runner, source, entry.session_key)

    for route, (source, expected) in list(getattr(runner, "_checkin_watch", {}).items()):
        try:
            scope = runner._async_profile_scope_for_source(source)
            if scope is None:
                continue
            async with scope:
                from agent import estop
                if (getattr(runner, "_draining", False) or estop.paused_reply() is not None
                        or getattr(runner, "_user_stop_latched", lambda *_: False)(route)):
                    continue
                entry = await runner._run_in_executor_with_context(
                    runner.session_store.lookup_by_session_key, route)
                if entry is None or entry.suspended:
                    runner._checkin_watch.pop(route, None)
                    continue
                manager = await runner._run_in_executor_with_context(lambda: _manager(runner, entry))
                if expected not in manager.lineage:
                    runner._checkin_watch.pop(route, None)  # reset/fork, not compression
                    continue
                state = await runner._run_in_executor_with_context(lambda: _reconcile(runner, entry))
                if state is None:
                    runner._checkin_watch.pop(route, None)
                    continue
                if max(state.due_at, state.next_attempt_at) > time.time() or runner._is_session_running(route):
                    continue
                adapter = runner._delivery_adapter_for(source)
                if adapter is None or route in getattr(adapter, "_active_sessions", {}) or _queued(adapter, route):
                    continue
                authority = active_authority(runner)
                # Never fall back around a missing/failed canonical owner. The next poll retries.
                if authority is None:
                    continue
                pending = await runner._run_in_executor_with_context(lambda: list_session_admissions(
                    authority.db, session_id=authority.logical_owner(entry.session_id)))
                if pending:
                    continue
                descriptor = [manager.root, state.due_at]
                # The logical due identity is fenced by start(). A refused or pre-execution
                # cancelled admission must not tombstone it forever in the durable FIFO.
                identity = json.dumps(["supervisor-checkin", manager.root, state.due_at, uuid.uuid4().hex],
                                      separators=(",", ":"))
                event = MessageEvent(text="[Supervisor check-in]", source=source, internal=True,
                                     message_id=identity, metadata={"gateway_session_key": route,
                                     "gateway_session_id": entry.session_id, "checkin": descriptor,
                                     "display_kind": DISPLAY_KIND, "display_text": "Supervisor check-in"})
                from gateway.config import Platform
                # LOCAL sources are in-process capabilities. Cloning one invalidates
                # the registered owner's identity, unlike a native transport route.
                if source.platform != Platform.LOCAL:
                    event.source = copy.copy(source)
                    event.source.message_id = None
                await authority.admit_automation(adapter, event, identity)
        except Exception:
            logger.warning("Supervisor check-in deferred for %s", route, exc_info=True)


def descriptor(event):
    metadata = getattr(event, "metadata", None)
    return (metadata.get("checkin") if getattr(event, "internal", False)
            and isinstance(metadata, dict) and metadata.get("display_kind") == DISPLAY_KIND else None)


async def prepare_checkin(runner, event, entry, *, start=False):
    """Refresh immediately before execution. User/completion/machinery input always wins.

    The durable descriptor is invalidated by a visible update, completion or reset. A discarded
    tick simply settles its native admission without invoking the model or writing a user row.
    """
    tick = descriptor(event)
    if tick is None:
        return True
    from gateway.session_authorities import active_authority
    from hermes_state_runtime import list_session_admissions

    route = entry.session_key
    adapter = runner._delivery_adapter_for(event.source)
    authority = active_authority(runner)
    if (authority is None or adapter is None or _queued(adapter, route)
            or getattr(runner, "_user_stop_latched", lambda *_: False)(route)):
        return False
    pending = await runner._run_in_executor_with_context(lambda: list_session_admissions(
        authority.db, session_id=authority.logical_owner(entry.session_id)))
    if any(row["request_id"] != event.message_id for row in pending):
        return False

    def observe():
        manager = _manager(runner, entry)
        work = _observe(manager, entry.session_id)
        state = manager.reconcile(work)
        if (not state or tick != [manager.root, state.due_at] or time.time() < state.due_at):
            return None
        if start and not manager.start(state.due_at, work):
            return None
        return checkin_prompt(work, manager.config), fallback_status(work, manager.config)

    observation = await runner._run_in_executor_with_context(observe)
    if observation is None:
        return False
    event.text, event._checkin_fallback = observation
    return True


async def silence_fallback(runner, event, entry, result: dict) -> dict | None:
    """A failed turn or second refusal cannot strand a required check-in. Use normal delivery."""
    if descriptor(event) is None or not isinstance(result, dict):
        return result
    if result.get("interrupted"):
        return result
    if result.get("queued_terminal_display_kind", DISPLAY_KIND) != DISPLAY_KIND:
        return result
    response = str(result.get("final_response") or "").strip()
    from agent.turn_failure_copy import exit_reason_failure
    failed = (result.get("completed") is False or result.get("failed") or result.get("partial")
              or result.get("failure_reason") or result.get("error")
              or exit_reason_failure(result.get("turn_exit_reason")))
    def observe():
        manager = _manager(runner, entry)
        work = _observe(manager, entry.session_id)
        manager.reconcile(work)
        return fallback_status(work, manager.config) if work else ""

    try:
        status = await runner._run_in_executor_with_context(observe)
    except Exception:
        logger.warning("Supervisor fallback could not refresh its roster", exc_info=True)
        status = getattr(event, "_checkin_fallback", "Background work could not be inspected. Supervision will retry.")
    if status and not failed and response and not runner._is_intentional_silence(result, response):
        return result
    recovered = {**result, "final_response": status, "delivery_text": status,
                 "_model_final_response": status, "intentional_silence": not bool(status),
                 "already_sent": False}
    # The worker has already captured its raw result. Recovery must agree across the live send,
    # durable completion and delivery replay without pretending that the model turn succeeded.
    from gateway.session_results import execution_result
    captured = execution_result.get()
    if captured is not None:
        captured["result"] = {**(captured.get("result") or {}), **recovered}
    return recovered if status else None


def install_visible_observer(runner, adapter):
    """Observe ACKs, not attempts/drafts/typing. Preserve the platform's original result/errors.

    Adapter instances (not classes) own receiving-bot identity. This is independent of optional
    telemetry and covers streamed edits, direct sends and attachment-only finals as well.
    """
    if getattr(adapter, "_checkin_observer_installed", False):
        return
    adapter._checkin_observer_installed = True
    methods = ("send", "edit_message", "send_voice", "send_image", "send_image_file", "send_document",
               "send_video", "send_animation", "send_multiple_images", "send_for_platform")
    for name in methods:
        method = getattr(adapter, name, None)
        if not callable(method):
            continue
        signature = inspect.signature(method)

        def wrap(method, signature, name):
            @functools.wraps(method)
            async def observed(*args, **kwargs):
                result = await method(*args, **kwargs)
                try:
                    if (getattr(result, "success", False) is True and not getattr(result, "deferred", False)
                            and not getattr(result, "held", False)):
                        bound = signature.bind_partial(*args, **kwargs).arguments
                        metadata = bound.get("metadata") or {}
                        chat_id = bound.get("chat_id")
                        threads = getattr(adapter, "_checkin_message_threads", None)
                        if threads is None:
                            threads = adapter._checkin_message_threads = {}
                        thread = bound.get("thread_id") or metadata.get("thread_id")
                        previous = threads.get((str(chat_id), str(bound.get("message_id"))))
                        if name == "edit_message" and thread is None and previous:
                            thread = previous[0]
                        content = bound.get("content")
                        changed = name != "edit_message" or previous is None or previous[1] != content
                        for mid in (getattr(result, "message_id", None), *getattr(result, "continuation_message_ids", ())):
                            if mid:
                                threads[(str(chat_id), str(mid))] = (thread, content)
                        while len(threads) > 2000:
                            threads.pop(next(iter(threads)))
                        if changed:
                            await record_visible(runner, adapter, chat_id, thread, bound.get("platform"))
                except Exception:
                    logger.warning("Could not record supervisor visible-output anchor", exc_info=True)
                return result
            return observed

        setattr(adapter, name, wrap(method, signature, name))


async def record_local_settlement(runner, source, settled, response, captured):
    """LOCAL delivery is the committed replayable result, never its no-op adapter send."""
    from gateway.config import Platform
    if source.platform != Platform.LOCAL or settled.get("outcome") != "completed" or not response:
        return
    result = (captured or {}).get("result") or {}
    from gateway.response_filters import is_intentional_silence_agent_result
    if is_intentional_silence_agent_result(result, response):
        return
    try:
        adapter = runner._delivery_adapter_for(source)
        if adapter is not None:
            await record_visible(runner, adapter, source.chat_id, source.thread_id, source.platform)
    except Exception:
        # Terminal storage is already committed. An anchor failure cannot rewrite it.
        logger.warning("Could not record supervisor LOCAL visible-output anchor", exc_info=True)


async def record_visible(runner, adapter, chat_id, thread_id=None, platform=None):
    if chat_id is None:
        return

    from gateway.session_context import get_session_env
    owner = get_session_env("HERMES_SESSION_KEY", "")
    task = asyncio.current_task()
    owner = next((key for key, running in getattr(adapter, "_session_tasks", {}).items() if running is task), owner)
    now = time.time()
    candidates = []
    for entry in await runner._run_in_executor_with_context(runner.session_store.list_sessions):
        origin = entry.origin
        if (not origin or entry.suspended or str(origin.chat_id) != str(chat_id)
                or str(origin.thread_id or "") != str(thread_id or "")):
            continue
        source = runner._restored_source(entry)
        if source is None or (platform is not None and source.platform != platform):
            continue
        scope = runner._async_profile_scope_for_source(source)
        if scope is None:
            continue
        async with scope:
            if runner._delivery_adapter_for(source) is not adapter:
                continue
            candidates.append((entry, source))
    owned = [item for item in candidates if item[0].session_key == owner]
    # Per-user group sessions can share a thread. An unowned send must not postpone
    # all their supervisors merely because its transport destination is the same.
    targets = owned or (candidates if len(candidates) == 1 else [])
    for entry, source in targets:
        async with runner._async_profile_scope_for_source(source):
            visible = getattr(runner, "_checkin_visible", None)
            if visible is None:
                visible = runner._checkin_visible = {}
            def record():
                manager = _manager(runner, entry)
                visible[(entry.session_key, manager.root)] = now
                manager.visible(now)
            await runner._run_in_executor_with_context(record)
