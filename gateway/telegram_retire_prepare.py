"""Quiet, generation-fenced preparation through the retiring session's normal turn."""
from __future__ import annotations

import dataclasses
import json

from gateway.platforms.base import MessageEvent, MessageType
from gateway.wake import admit_internal_event
from gateway.telegram_topic_sessions import TopicRequestError


def validate_decision(existing, owners, generation):
    """Reject malformed owner choices and preparation for a superseded request."""
    if generation and (not existing or existing["generation"] != generation
                       or existing["phase"] not in {"pending", "waiting"}):
        raise TopicRequestError("Retirement preparation was superseded. No topic was changed.")
    if owners is not None and (not isinstance(owners, dict)
                               or any(not isinstance(k, str) or not isinstance(v, str) or not v.strip()
                                      for k, v in owners.items())):
        raise TopicRequestError("Retirement owners must map item IDs to live topic titles.")


async def queue_preparation(runner, source, state: dict, items: list[dict], *, busy: str = "") -> bool:
    """Choose owners in context, never turn an ownership gap into a user questionnaire."""
    from gateway.telegram_retire import save

    # Runtime snapshots contain advancing clocks and output. Only a changed
    # decision, not another observation of the same work, may wake the model.
    decision_items = [
        {field: item.get(field, "") for field in ("id", "kind", "title", "blocked_owner", "blocked_reason")}
        for item in sorted(items, key=lambda item: item["id"])
    ]
    fingerprint = json.dumps({"items": decision_items, "busy": busy, "reason": state.get("reason", ""),
                              "target": state.get("target", ""), "context": state.get("context", ""),
                              "owners": state.get("owners", {})},
                             sort_keys=True, default=str)
    key = (state["session_id"], state["generation"])
    admitted = getattr(runner, "_telegram_retire_preparations", None)
    if admitted is None:
        admitted = runner._telegram_retire_preparations = {}
    if admitted.get(key) == fingerprint:
        return False
    state.update(phase="waiting", questions=[], preparation={"items": items, "busy": busy})
    await save(runner, source, state)
    text = (
        "[Native Topic Retirement Preparation]\n"
        "The user explicitly requested retirement of THIS topic. Resolve the routine choices yourself, "
        "without confirmation or owner questions. This wake does not authorize retiring any other topic. "
        "Finish the user's stated prerequisites and deliver their result before retirement. Finishing "
        "a turn alone does not prove a deferred request is satisfied.\n"
        "Decide merge versus delete from the conversation and verified task/topic records, not title "
        "similarity or recency. Merge into the existing live topic that owns the same lasting subject. "
        "Delete when the work is complete and nothing lasting remains. Preserve useful unfinished work: "
        "choose verified owners for each item, or create a subject-specific successor if none exists. "
        "Do not use an Inbox or generic retirement queue. Do not cancel standing commitments or overwrite "
        "destination goals/loops. Stop only proven disposable machinery whose purpose has ended, using "
        "its normal control path. Settle completed execution claims normally, never transfer tokens. "
        "If claims are still executing, wait silently and do not requeue retirement just to poll them. "
        "Retirement cannot authorize spending or an irreversible external action. Ask only if that "
        "decision belongs to the user and there is no safe way to preserve the work without it.\n"
        "Use telegram_topic action=retire with a short reason explaining the choice. For a merge set "
        "target to the exact live subject-owner title. Use owners={item_id: exact live topic title} "
        "for individual destinations. For a merge, put a concise source context summary in message, "
        "including decisions, evidence, limits and remaining work. Reuse verified successors after interruptions. The native path "
        "verifies handoffs, preserves the source transcript, sends the closing note, and only then "
        "deletes the topic. Do not delete it manually. Use generation="
        f"{json.dumps(state['generation'])} to fence this preparation. End silently with NO_REPLY.\n"
        f"Source session: {state['session_id']}\n"
        f"Current retirement target: {state.get('target') or '(choose automatically)'}\n"
        f"Open items: {json.dumps(items, ensure_ascii=False, default=str)}\n"
        f"Work still running: {busy or '(none)'}"
    )
    event = MessageEvent(
        text=text, message_type=MessageType.TEXT,
        source=dataclasses.replace(source, message_id=None), internal=True, reply_expected=False,
        metadata={"gateway_session_id": state["session_id"], "gateway_session_strict": True,
                  "retirement_generation": state["generation"]},
    )
    adapter = runner._delivery_adapter_for(source)
    await admit_internal_event(adapter, event)
    admitted[key] = fingerprint
    return False


def closing_note(state: dict) -> str:
    """Expose the automatic disposition, not internal inventory or receipt machinery."""
    target = str(state.get("target") or "").strip()
    reason = str(state.get("reason") or "The work is finished and nothing lasting remains.").strip()
    choice = f"Merged into {target}" if target else "Retired"
    destinations = sorted({str(row["destination"]["title"]) for row in state.get("handoffs", {}).values()
                           if row.get("moved") and row.get("destination", {}).get("title")})
    destinations = [title for title in destinations if title != target]
    details = f" Open work continues in {', '.join(destinations)}." if destinations else ""
    return f"{choice}: {reason}{details} Its session and transcript are preserved."


async def redirect_owner(runner, source, state: dict) -> None:
    """Preserve existing automation owner keys as aliases of the merge destination."""
    profile = source.profile or "default"
    db = runner._session_db
    kwargs = {"chat_id": str(source.chat_id), "profile_name": profile}
    if state.get("routing_owner"):
        owner = await db.get_telegram_topic_owner(owner_key=state["routing_owner"], **kwargs)
    else:
        owner = await db.get_telegram_topic_owner_for_thread(thread_id=str(source.thread_id), **kwargs)
    if not owner:
        return
    if not state.get("routing_owner"):
        from gateway.telegram_retire import save
        state["routing_owner"] = owner["owner_key"]
        await save(runner, source, state)
    dest = state.get("merge_destination")
    if dest:
        canonical = await db.get_telegram_topic_owner_for_thread(thread_id=dest["thread_id"], **kwargs)
        if not canonical:
            key = f"topic:{dest['thread_id']}"
            canonical = await db.reserve_telegram_topic_owner(owner_key=key, topic_name=dest["title"], **kwargs)
            if canonical["status"] != "creating":
                raise RuntimeError("Merge destination owner reservation is unresolved")
            canonical = await db.set_telegram_topic_owner_active(
                owner_key=key, thread_id=dest["thread_id"], topic_name=dest["title"], **kwargs)
        await db.set_telegram_topic_owner_state(owner_key=owner["owner_key"], status="retired", **kwargs)
        alias = await db.set_telegram_topic_owner_shared(
            owner_key=owner["owner_key"], canonical_owner_key=canonical["owner_key"],
            topic_name=owner["topic_name"], **kwargs)
        if alias.get("canonical_owner_key") != canonical["owner_key"] or alias.get("status") != "active":
            raise RuntimeError("Merged topic owner redirect was not preserved")
    else:
        await db.set_telegram_topic_owner_state(owner_key=owner["owner_key"], status="retired", **kwargs)
