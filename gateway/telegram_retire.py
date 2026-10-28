"""Native Telegram topic retirement. State lives beside goals/loops in state_meta.

The turn-completion hook only wakes this executor. It is never a delivery receipt.
The executor sends its own short, thread-strict closing line and persists the
transport acknowledgement before it can dispatch a destructive request.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from dataclasses import asdict

from gateway.config import Platform
from gateway.session import SessionSource
from gateway.telegram_topic_sessions import TopicRequestError

logger = logging.getLogger(__name__)
PREFIX = "telegram_retire:"
NOTICE = "This topic is retiring. Use /retire cancel to keep it."


def state_key(source):
    return f"{PREFIX}{source.profile or 'default'}:{source.chat_id}:{source.thread_id}"


async def load(runner, source):
    if source.platform != Platform.TELEGRAM or not source.thread_id:
        return None
    db = getattr(runner, "_session_db", None)
    if db is None:
        return None
    raw = await db.get_meta(state_key(source))
    return json.loads(raw) if isinstance(raw, str) and raw else None


class RetirementSuperseded(Exception):
    """A control request fenced an older executor generation."""


async def save(runner, source, state, *, force=False, expected_generation=""):
    locks = getattr(runner, "_telegram_retire_state_locks", None)
    if locks is None:
        locks = runner._telegram_retire_state_locks = {}
    async with locks.setdefault(state_key(source), asyncio.Lock()):
        current = await load(runner, source)
        if expected_generation and (not current or current["generation"] != expected_generation
                                    or current["phase"] not in {"pending", "waiting"}):
            raise TopicRequestError("Retirement preparation was superseded. No topic was changed.")
        if not force and current and current["generation"] != state["generation"]:
            raise RetirementSuperseded
        await runner._session_db.set_meta(state_key(source), json.dumps(state))


async def request(runner, source, *, target=None, now=False, cancel=False, owners=None, reason="", generation="", context=""):
    if source.platform != Platform.TELEGRAM or not source.thread_id or str(source.thread_id) in {"0", "1"}:
        raise TopicRequestError("Retirement requires a non-General Telegram topic.")
    if runner._session_db is None:
        raise TopicRequestError("Session storage is unavailable. The topic was not changed.")
    existing = await load(runner, source)
    from gateway.telegram_retire_prepare import validate_decision
    validate_decision(existing, owners, generation)
    target = target.strip() if target is not None else None
    reason, context = reason.strip(), context.strip()
    if existing and existing["phase"] == "delete_unresolved":
        # Only an explicit command retries recovery after a failed dispatch.
        # Serialize the existence check against an executor already in flight.
        locks = getattr(runner, "_telegram_retire_locks", None)
        if locks is None:
            locks = runner._telegram_retire_locks = {}
        lock = locks.setdefault(state_key(source), asyncio.Lock())
        if lock.locked():
            raise TopicRequestError("Topic deletion is still being reconciled. Retry the command after it settles.")
        async with lock:
            current = await load(runner, source)
            if not current or current["generation"] != existing["generation"] or current["phase"] != "delete_unresolved":
                raise TopicRequestError("Retirement changed during recovery. Retry the command.")
            bot = getattr(runner._delivery_adapter_for(source), "_bot", None)
            if bot is None:
                raise TopicRequestError("Telegram bot is disconnected. The previous deletion is unresolved.")
            if await _probe_deleted(bot, source, runner=runner):
                await _unbind(runner, source, existing)
                return {"action": "cancel_retire" if cancel else "retire", "status": "done",
                        "thread_id": str(source.thread_id)}
            # Exact-topic existence makes cancellation safe. A retry instead
            # starts a new generation, re-inventories work and closes again.
            existing["phase"] = "failed"
            await save(runner, source, existing)
    if cancel:
        if existing and existing["phase"] in {"deleting", "delete_unresolved"}:
            raise TopicRequestError("Topic deletion has already been dispatched and cannot be cancelled.")
        if existing and existing["phase"] != "done":
            existing["phase"] = "cancelled"
            existing["generation"] = uuid.uuid4().hex
            await save(runner, source, existing, force=True)
        return {"action": "cancel_retire", "status": "cancelled", "thread_id": str(source.thread_id)}
    if existing and existing["phase"] in {"deleting", "delete_unresolved", "done"}:
        return {"action": "retire", "status": existing["phase"], "thread_id": str(source.thread_id)}
    session_key = runner._session_key_for_source(source)
    session_id = await asyncio.to_thread(runner.session_store.peek_session_id, session_key)
    if not session_id:
        raise TopicRequestError("This topic has no bound session to retire.")
    # A changed decision fences preparation and closing callbacks without losing
    # receipt/transfer proofs for obligations already moved by an earlier turn.
    continuing = existing and existing["phase"] not in {"cancelled", "failed"}
    previous = existing or {}
    selected_owners = {**(previous.get("owners", {}) if continuing else {}), **(owners or {})}
    selected_target = target if target is not None else (previous.get("target", "") if continuing else "")
    selected_reason = reason or (previous.get("reason", "") if continuing else "")
    selected_context = context or (previous.get("context", "") if continuing else "")
    if (continuing and selected_target == previous.get("target", "")
            and selected_owners == previous.get("owners", {}) and selected_reason == previous.get("reason", "")
            and selected_context == previous.get("context", "")):
        state = previous
    else:
        state = {
            "generation": uuid.uuid4().hex, "phase": "pending", "session_id": session_id,
            "session_key": session_key, "target": selected_target, "owners": selected_owners,
            "reason": selected_reason, "context": selected_context, "mode": "now" if now else "after_turn",
            "requested_at": time.time(), "source": {**asdict(source), "platform": "telegram"},
            "handoffs": (existing or {}).get("handoffs", {}) if existing and existing["phase"] != "cancelled" else {},
        }
        # Receipt idempotency and executor cancellation are different identities.
        # A control generation must fence stale saves without duplicating work.
        if state["handoffs"]:
            state["handoff_generation"] = previous.get("handoff_generation", previous["generation"])
        await save(runner, source, state, force=True, expected_generation=generation)
    if now:
        adapter = runner._delivery_adapter_for(source)
        agent = (getattr(runner, "_running_agents", {}) or {}).get(session_key)
        if agent is not None and callable(getattr(agent, "interrupt", None)):
            agent.interrupt("Topic retirement requested now")
        if adapter is not None:
            # Keep queued obligations: now interrupts the turn, it does not discard work.
            await adapter.cancel_session_processing(session_key, discard_pending=False)
    return {"action": "retire", "status": "queued", "thread_id": str(source.thread_id),
            "generation": state["generation"], "effective_when": "after_current_turn"}


async def intercept(runner, event):
    """Called before both adapter queuing and runner dispatch. None means continue."""
    state = await load(runner, event.source)
    preparation = (getattr(event, "metadata", None) or {}).get("retirement_generation")
    if event.internal and preparation and (not state or state["generation"] != preparation
                                           or state["phase"] not in {"pending", "waiting"}):
        return ""
    if not state or state["phase"] in {"cancelled", "done", "failed"}:
        return None
    if event.internal:
        # Already-owned completion work may settle while retirement waits. The
        # admission fence is for new user work, not a way to discard results.
        return None
    if event.allow_gateway_control and not event.internal:
        if event.get_command() == "retire":
            return None
    return NOTICE


def confirmed(result, source=None):
    """No outbox acceptance, held retry, partial send or missing transport ID authorizes delete."""
    raw = getattr(result, "raw_response", None)
    if isinstance(raw, dict):
        if raw.get("thread_fallback") or raw.get("undelivered_chunks"):
            return False
        if len(raw.get("message_ids") or []) > 1 and raw.get("requested_thread_id") is None:
            return False
        if source is not None and raw.get("requested_thread_id") is not None and str(raw["requested_thread_id"]) != str(source.thread_id):
            return False
    if getattr(result, "continuation_message_ids", None):
        ids = {str(value) for value in raw.get("message_ids", [])} if isinstance(raw, dict) else set()
        if not {str(result.message_id), *map(str, result.continuation_message_ids)} <= ids:
            return False
    return bool(result is not None and result.success and result.message_id
                and not result.error and not result.deferred and not result.held
                and not (isinstance(result.raw_response, dict)
                         and result.raw_response.get("partial_overflow")))


async def _current(runner, source, generation):
    state = await load(runner, source)
    return state if state and state["generation"] == generation and state["phase"] not in {"cancelled", "done"} else None


async def _probe_deleted(bot, source, *, runner=None, read_only=False):
    from gateway.telegram_retire_work import cli
    from hermes_platform.resolver import locate_command
    if runner is not None and locate_command("telegram").command:
        topics = await cli(runner, source, "telegram", "--agent", "topic", "list",
                           await _cli_peer(bot, source), "--id", str(source.thread_id))
        if not isinstance(topics, list):
            raise RuntimeError("Exact-topic lookup returned an incomplete response")
        return not topics
    if read_only:
        return False
    # The Bot API has no getForumTopic. A thread-strict send is the documented
    # absence probe. Never retry or fall back to the chat root.
    try:
        await bot.send_message(chat_id=int(source.chat_id), message_thread_id=int(source.thread_id), text=NOTICE)
    except Exception as exc:
        if "message thread not found" in str(exc).lower() or "topic_deleted" in str(exc).lower():
            return True
        raise
    return False


async def _cli_peer(bot, source):
    if source.chat_type != "dm":
        return str(source.chat_id)
    # The Bot API's DM chat_id is the user. MTProto addresses the bot dialog.
    me = await bot.get_me()
    return "@" + me.username if me.username else str(me.id)


async def _unbind(runner, source, state):
    profile = source.profile or "default"
    from gateway.telegram_retire_prepare import redirect_owner
    await redirect_owner(runner, source, state)
    await runner._session_db.delete_telegram_topic_binding(
        chat_id=str(source.chat_id), thread_id=str(source.thread_id), profile_name=profile)
    if not await asyncio.to_thread(runner.session_store.unbind_session, state["session_key"], state["session_id"]):
        raise RuntimeError("Topic routing changed before unbinding, session was preserved")
    if not await runner._session_db.unbind_gateway_session_peer(state["session_key"], state["session_id"]):
        raise RuntimeError("Persisted topic routing changed before unbinding, session was preserved")
    state["phase"] = "done"
    await save(runner, source, state)


async def _finalize_absent(runner, source, state, bot):
    """A failed request can finish quietly after independently confirmed absence."""
    from gateway.telegram_retire_work import inventory, settle_handoffs
    key, generation = state["session_key"], state["generation"]
    if await asyncio.to_thread(runner.session_store.peek_session_id, key) not in {None, state["session_id"]}:
        return
    if bot is None or not await _probe_deleted(bot, source, runner=runner, read_only=True):
        return
    # No new source-topic turn or closing send is possible. A requested merge
    # must already have a saved destination and context handoff to reconcile.
    if state.get("target") and (not state.get("merge_destination") or not any(
            record.get("item", {}).get("kind") == "context" for record in state["handoffs"].values())):
        return
    await settle_handoffs(runner, source, state)
    if await inventory(runner, source, state):
        return
    current = await _current(runner, source, generation)
    if (not current or current["phase"] != "failed" or runner._is_session_running(key)
            or await asyncio.to_thread(runner.session_store.peek_session_id, key) not in {None, state["session_id"]}):
        return
    await _unbind(runner, source, current)


async def advance(runner, source):
    """Resume persisted retirement only after the active turn relinquishes its lane."""
    from gateway.telegram_retire_work import HandoffPending
    state = await load(runner, source)
    if not state or state["phase"] in {"cancelled", "done"}:
        return
    key = state["session_key"]
    adapter = runner._delivery_adapter_for(source)
    if adapter is None or runner._is_session_running(key):
        return
    tasks = getattr(adapter, "_session_tasks", {})
    task = tasks.get(key)
    if task is not None and task is not asyncio.current_task() and not task.done():
        return
    locks = getattr(runner, "_telegram_retire_locks", None)
    if locks is None:
        locks = runner._telegram_retire_locks = {}
    lock = locks.setdefault(state_key(source), asyncio.Lock())
    if lock.locked():
        return
    async with lock:
        state = await _current(runner, source, state["generation"])
        if not state:
            return
        generation = state["generation"]
        bot = getattr(adapter, "_bot", None)
        if state["phase"] == "failed":
            try:
                await _finalize_absent(runner, source, state, bot)
            except Exception:
                logger.debug("Absent-topic retirement recovery remains blocked for %s", key, exc_info=True)
            return
        try:
            if bot is None:
                raise RuntimeError("Telegram bot is disconnected")
            if state["phase"] in {"deleting", "delete_unresolved"}:
                # A restart/timeout cannot authorize a second delete. Reconcile
                # first using the saved successful closing acknowledgement.
                receipt = state.get("closing_receipt") or {}
                if (receipt.get("generation") != generation or not receipt.get("message_id")
                        or receipt.get("chat_id") != str(source.chat_id)
                        or receipt.get("thread_id") != str(source.thread_id)):
                    raise RuntimeError("Missing exact-topic confirmed closing receipt")
                passive = state["phase"] == "delete_unresolved"
                if await _probe_deleted(bot, source, runner=runner, read_only=passive):
                    state = await _current(runner, source, generation)
                    if state:
                        await _unbind(runner, source, state)
                    return
                if passive:
                    return
                raise RuntimeError("The previous delete was not confirmed. Use /retire to retry or /retire cancel to keep this topic.")
            from gateway.telegram_retire_work import prepare
            # Inventory happens here, after the turn, not when /retire was typed.
            if await asyncio.to_thread(runner.session_store.peek_session_id, key) != state["session_id"]:
                raise RuntimeError("The bound session changed during retirement. Cancel and retry from its current session.")
            if not await prepare(runner, source, state):
                return
            state = await _current(runner, source, generation)
            if not state:
                return
            if state.get("closing_attempt"):
                raise RuntimeError("Closing delivery is unresolved. Cancel retirement before retrying.")
            state["closing_attempt"] = time.time()
            await save(runner, source, state)
            from gateway.telegram_retire_prepare import closing_note
            result = await adapter.send(str(source.chat_id), closing_note(state), metadata={"thread_id": str(source.thread_id)})
            state = await _current(runner, source, generation)
            if not state:
                return
            if not confirmed(result, source):
                raise RuntimeError("Closing reply was not fully acknowledged in this topic. The topic was kept.")
            state["closing_receipt"] = {
                "generation": generation, "message_id": str(result.message_id),
                "chat_id": str(source.chat_id), "thread_id": str(source.thread_id), "delivered_at": time.time(),
            }
            state["phase"] = "deleting"
            await save(runner, source, state)
            # Cancellation changes the generation before this check. No await
            # separates the final fence from initiating the destructive request.
            if not await _current(runner, source, generation):
                return
            try:
                if await bot.delete_forum_topic(chat_id=int(source.chat_id), message_thread_id=int(source.thread_id)) is not True:
                    raise RuntimeError("Telegram did not acknowledge topic deletion")
            except Exception:
                # A lost response can mean the topic is already gone. Reconcile
                # the exact thread before invoking the user-CLI fallback once.
                if not await _probe_deleted(bot, source, runner=runner):
                    from gateway.telegram_retire_work import cli
                    peer = await _cli_peer(bot, source)
                    await cli(runner, source, "telegram", "--agent", "--yes", "topic", "delete",
                              str(peer), str(source.thread_id))
            if not await _probe_deleted(bot, source, runner=runner):
                raise RuntimeError("Telegram still accepts messages in the topic after deletion")
            await _unbind(runner, source, state)
        except asyncio.CancelledError:
            raise
        except RetirementSuperseded:
            return
        except HandoffPending:
            # The receipt is durable. Housekeeping or /retire rechecks it without
            # repeating the handoff or treating a busy destination as failure.
            current = await _current(runner, source, generation)
            if current:
                current["phase"] = "pending"
                await save(runner, source, current)
        except Exception as exc:
            if state and state["phase"] == "delete_unresolved":
                # A deleted topic cannot receive another user retry. Quietly
                # reconcile read-only evidence without replaying sends/deletes.
                logger.debug("Topic retirement reconciliation deferred for %s: %s", key, exc)
                return
            logger.warning("Topic retirement failed for %s: %s", key, exc, exc_info=True)
            current = await _current(runner, source, generation)
            if current:
                current["phase"] = "delete_unresolved" if current["phase"] in {"deleting", "delete_unresolved"} else "failed"
                current["error"] = str(exc)
                await save(runner, source, current)
                recovery = " Use /retire to retry or /retire cancel to drop the request."
                await adapter.send(str(source.chat_id), f"Topic retirement failed: {exc}{recovery}",
                                   metadata={"thread_id": str(source.thread_id)})


async def resume_all(runner):
    if getattr(runner, "_session_db", None) is None:
        return
    for _, raw in await runner._session_db.list_meta_prefix(PREFIX):
        try:
            state = json.loads(raw)
            if state["phase"] not in {"cancelled", "done"}:
                source = SessionSource(**{**state["source"], "platform": Platform.TELEGRAM})
                async with runner._async_profile_scope_for_source(source):
                    await advance(runner, source)
        except Exception:
            logger.warning("Unable to resume one topic retirement", exc_info=True)
