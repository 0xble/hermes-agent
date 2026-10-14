"""One-shot gateway recovery when a human-addressed turn ends on a bare silence marker.

A locally executed turn whose user expects a reply may still finish with ``NO_REPLY`` or
``[SILENT]``, for example when a typed message was folded into a relay turn. Instead of the
"No reply was written" warning, the gateway re-prompts exactly once. The guidance stays in the
API-only ephemeral prompt (see ``gateway.turn_context.silence_retry_prompt``), never in a
persisted user or nudge row, and the outer human contract still owns the terminal verdict.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any, Dict

from gateway.response_filters import reply_expected_metadata, silence_allowed

if TYPE_CHECKING:
    from gateway.turn_context import TurnContext

logger = logging.getLogger("gateway.run_turn")


def silence_recovery_due(runner, response: Any, turn_ctx: "TurnContext", *,
                         silence_retry: bool, scheduled_heartbeat: bool) -> bool:
    """Whether a finished execution should get its single recovery re-prompt.

    Never for the recovery itself (bounded to once), heartbeats, interrupted or incomplete runs
    (a partial marker is not a completed silence choice), or turns whose contract permits silence
    (relay-only and machinery turns keep their allowed silence).
    """
    return bool(
        not (silence_retry or scheduled_heartbeat) and isinstance(response, dict)
        and not response.get("interrupted") and response.get("completed") is not False
        and runner._is_intentional_silence(response, response.get("final_response", ""))
        and not silence_allowed(turn_ctx.persist_user_display_kind, turn_ctx.reply_expected)
        and turn_ctx._run_still_current()
    )


async def run_silence_followup(runner, response: dict, turn_ctx: "TurnContext") -> Dict[str, Any]:
    """One gateway continuation after a rejected marker, with a fresh stream and no human input.

    The first execution and its stream have finished. Put recovery guidance in the existing
    API-only ephemeral prompt, not in a user/nudge pair that tools or compression could retain.
    The empty runtime row has existing internal provenance; it cannot become the latest human
    ask, title, or reply anchor. The outer human contract still owns the terminal verdict.
    """
    from gateway.run import _preserve_queued_followup_history_offset
    session_id = response.get("session_id") or turn_ctx.session_id
    await runner._refresh_agent_cache_message_count(turn_ctx.session_key, session_id)
    if not turn_ctx._run_still_current():
        return response
    adapter = runner._delivery_adapter_for(turn_ctx.source)
    completed = getattr(adapter, "_streaming_tts_completed_turns", None)
    key_fn = getattr(adapter, "_streaming_tts_turn_key", None)
    if completed is not None and callable(key_fn):
        key = key_fn(turn_ctx.session_key, turn_ctx.run_generation)
        if key:
            completed.discard(key)
    logger.info("Rejected human-turn silence; re-prompting once for session %s", turn_ctx.session_key)
    followup = await runner._run_agent(
        message="", context_prompt=turn_ctx.context_prompt,
        history=response.get("messages", turn_ctx.history), source=turn_ctx.source,
        session_id=session_id, session_key=turn_ctx.session_key, run_generation=turn_ctx.run_generation,
        _interrupt_depth=turn_ctx._interrupt_depth, event_message_id=turn_ctx.event_message_id,
        inbound_message_id=turn_ctx.inbound_message_id, channel_prompt=turn_ctx.channel_prompt,
        moa_config=turn_ctx.moa_config, message_type="voice" if turn_ctx.voice_turn else None,
        internal=True, title_user_message="", persist_user_message="",
        persist_user_display_kind="internal_notification", reply_expected=turn_ctx.reply_expected,
        persist_user_display_metadata=reply_expected_metadata(turn_ctx.reply_expected),
        _post_delivery_adapter=turn_ctx._post_delivery_adapter or adapter, _silence_retry=True,
    )
    merged = _preserve_queued_followup_history_offset(response, followup)
    # A real queued successor owns its own verdict. Otherwise the synthetic continuation must
    # not launder a second marker into permitted machinery silence. Its execution may have
    # been redirected onto a newer human message, so preserve that live terminal identity
    # separately from the original turn's silence contract.
    if isinstance(merged, dict) and "queued_terminal_display_kind" not in merged:
        merged = {**merged, "queued_terminal_display_kind": turn_ctx.persist_user_display_kind,
                  "queued_terminal_reply_expected": turn_ctx.reply_expected,
                  "queued_terminal_inbound_id": (
                      followup.get("_terminal_inbound_id") or turn_ctx.inbound_message_id
                      if isinstance(followup, dict) else turn_ctx.inbound_message_id
                  )}
    return merged
