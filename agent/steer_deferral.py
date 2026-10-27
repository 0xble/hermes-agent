"""Turn-scoped auto-steer receipts. Hosts retain their original queue envelopes."""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import logging
import threading
from typing import Callable
from uuid import uuid4

logger = logging.getLogger(__name__)


@dataclass
class SteerReceipt:
    turn_id: str
    text: str
    handoff: Callable[[], bool]
    wrapper: str
    deferred: bool = False
    lock: threading.RLock = field(default_factory=threading.RLock)


def register_steer(agent, text: str, handoff: Callable[[], bool]) -> tuple[str, str]:
    turn_id = getattr(agent, "_current_turn_id", "")
    receipts = agent.__dict__.setdefault("_auto_steer_receipts", {})
    # A new turn cannot defer an old turn's already-absorbed messages.
    for key, receipt in list(receipts.items()):
        if receipt.turn_id != turn_id:
            receipts.pop(key, None)
    message_id = str(uuid4())
    wrapper = (
        f"Auto-steer message_id: {message_id}\n"
        "If this message is unrelated to the current task, call defer_message with this exact "
        "message_id instead of acting on it. A successful deferral queues it as a separate next turn.\n\n"
        f"{text}"
    ).strip()  # Native steer/redirect trim the frame. Keep receipt.text verbatim for its original envelope.
    receipts[message_id] = SteerReceipt(turn_id, text, handoff, wrapper)
    return message_id, wrapper


def forget_steer(agent, message_id: str) -> None:
    agent.__dict__.get("_auto_steer_receipts", {}).pop(message_id, None)


def _admission_lock(agent):
    return agent.__dict__.setdefault("_auto_steer_admission_lock", threading.RLock())


def close_auto_steer(agent) -> None:
    """Close auto admission before the final drain, including slow post-turn cleanup."""
    with _admission_lock(agent):
        agent._auto_steer_closed_turn_id = getattr(agent, "_current_turn_id", "")


def offer_auto_steer(agent, text: str, handoff: Callable[[], bool], *, redirect: bool = True) -> bool:
    if agent is None:
        return False
    with _admission_lock(agent):
        turn_id = getattr(agent, "_current_turn_id", "")
        if not turn_id or agent.__dict__.get("_auto_steer_closed_turn_id") == turn_id:
            return False
        message_id, wrapped = register_steer(agent, text, handoff)
        methods = (["redirect"] if redirect and getattr(agent, "_supports_active_turn_redirect", False) is True else []) + ["steer"]
        for method in methods:
            try:
                if hasattr(agent, method) and getattr(agent, method)(wrapped):
                    return True
            except Exception:
                logger.debug("Auto-steer %s rejected", method, exc_info=True)
        forget_steer(agent, message_id)
        return False


def auto_messages_for_delivery(agent, text: str) -> list[dict]:
    """Expose host-minted identities to observers, never associate asks by model-regenerated text."""
    receipts = agent.__dict__.get("_auto_steer_receipts", {})
    current = getattr(agent, "_current_turn_id", "")
    return [{"message_id": key, "text": receipt.text} for key, receipt in list(receipts.items())
            if receipt.turn_id == current and receipt.wrapper in text]


def manual_text_for_delivery(agent, text: str) -> str:
    """Exclude only exact host-minted frames, never infer identity from user-written markers."""
    receipts = agent.__dict__.get("_auto_steer_receipts", {})
    current = getattr(agent, "_current_turn_id", "")
    for receipt in list(receipts.values()):
        if receipt.turn_id != current or receipt.wrapper not in text:
            continue
        # Both native joiners are owned by InterruptControlMixin. Remove one adjacent joiner
        # with its exact frame so intervening manual messages retain their original newlines.
        for separator in ("\n\n[Additional user correction]\n", "\n"):
            if separator + receipt.wrapper in text:
                text = text.replace(separator + receipt.wrapper, "", 1)
                break
            if receipt.wrapper + separator in text:
                text = text.replace(receipt.wrapper + separator, "", 1)
                break
        else:
            text = text.replace(receipt.wrapper, "", 1)
    return text


def defer_message(agent, args: dict) -> dict:
    message_id = args.get("message_id")
    receipts = agent.__dict__.get("_auto_steer_receipts", {})
    receipt = receipts.get(message_id) if isinstance(message_id, str) else None
    if receipt is None or receipt.turn_id != getattr(agent, "_current_turn_id", ""):
        return {"accepted": False, "error": "message_id is not an auto-steered message in this turn"}
    if getattr(agent, "_interrupt_requested", False) is True:
        return {"accepted": False, "error": "The current turn is stopping; no message queued"}
    with receipt.lock:
        if not receipt.deferred:
            try:
                if not receipt.handoff():
                    raise RuntimeError("host queue did not accept the original message")
            except Exception as exc:
                logger.warning("Auto-steer deferral failed: %s", exc, exc_info=True)
                return {"accepted": False, "message_id": message_id,
                        "error": f"Queue handoff failed: {exc}. The original steer is still actionable."}
            receipt.deferred = True
    # This tool result is the append-only deferred marker. Never edit the steered user row.
    return {"accepted": True, "message_id": message_id, "status": "deferred",
            "instruction": "Do not act on or answer this message in the current turn. It will run next."}


def drain_leftover_steer(agent) -> str:
    """Close auto admission, drain the post-turn steer and return auto originals to their hosts."""
    close_auto_steer(agent)
    leftover = (getattr(agent, "_drain_leftover_steer", None) or agent._drain_pending_steer)()
    return requeue_undelivered_steers(agent, leftover) if leftover else leftover


def requeue_undelivered_steers(agent, pending: str) -> str:
    """Restore original host envelopes for steers accepted after the last model call."""
    receipts = agent.__dict__.get("_auto_steer_receipts", {})
    for message_id, receipt in list(receipts.items()):
        if receipt.turn_id != getattr(agent, "_current_turn_id", "") or receipt.wrapper not in pending:
            continue
        if defer_message(agent, {"message_id": message_id})["accepted"]:
            pending = pending.replace(receipt.wrapper, "", 1)
    return pending.strip()


def preserve_steer_receipts(agent, compressed: list) -> None:
    """Native compression may remove the wrapper. Preserve exact IDs and queue status there only."""
    receipts = agent.__dict__.get("_auto_steer_receipts", {})
    current = getattr(agent, "_current_turn_id", "")
    records = [{"message_id": key, "status": "deferred" if receipt.deferred else "actionable",
                "text": receipt.text} for key, receipt in list(receipts.items())
               if receipt.turn_id == current]
    if not records:
        return
    text = ("Auto-steer receipts for the current turn (deferred messages must not be answered here; "
            "unrelated actionable messages may be handed to defer_message by message_id):\n"
            + json.dumps(records, ensure_ascii=False))
    # Compression is the only permitted prefix rewrite. Reuse its user anchor to keep alternation.
    for row in reversed(compressed):
        if row.get("role") == "user":
            content = row.get("content")
            row["content"] = ([*content, {"type": "text", "text": text}] if isinstance(content, list)
                              else f"{content or ''}\n\n{text}")
            return
    compressed.append({"role": "user", "content": text})
