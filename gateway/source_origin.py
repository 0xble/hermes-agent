"""Positive transport evidence, separate from inherited routing and claimed author labels."""

from agent.source_origin import NativeOrigin, ORIGIN_KEY


def stamp_telegram_person(event, message):
    user = getattr(message, "from_user", None)
    # A forwarded message, channel post, or bot-mediated payload is not this person's authored span.
    if (user is not None and getattr(user, "is_bot", None) is False
            and not any(getattr(message, name, None) for name in ("forward_origin", "via_bot", "sender_chat"))
            and isinstance(event.text, str) and event.text and event.message_id):
        raw = getattr(message, "text", None) or getattr(message, "caption", None)
        if raw is not None and raw != event.text:
            return event
        spans = _telegram_authored_spans(event.text, getattr(message, "entities", None)
                                        or getattr(message, "caption_entities", None) or ())
        if spans:
            event._native_origin = NativeOrigin.person(event.text, platform="telegram", principal=str(user.id),
                                                       event=str(event.message_id), authored=spans)
    return event


def _telegram_authored_spans(text, entities):
    """Telegram quotes use UTF-16 offsets. Their body is context, not a new assertion."""
    quoted = []
    encoded = text.encode("utf-16-le")
    for entity in entities:
        if str(getattr(entity, "type", "")) not in {"blockquote", "expandable_blockquote"}:
            continue
        start, length = getattr(entity, "offset", None), getattr(entity, "length", None)
        if type(start) is not int or type(length) is not int or start < 0 or length < 1 or (start + length) * 2 > len(encoded):
            return ()
        try:
            quoted.append((len(encoded[:start * 2].decode("utf-16-le")),
                           len(encoded[:(start + length) * 2].decode("utf-16-le"))))
        except UnicodeDecodeError:
            return ()
    spans, end = [], 0
    for left, right in sorted(quoted):
        if end < left:
            spans.append((end, left))
        end = max(end, right)
    if end < len(text):
        spans.append((end, len(text)))
    return tuple(spans)


def source_origin_metadata(event, text):
    if not isinstance(text, str):
        return {}
    metadata = event.metadata or {}
    origin = getattr(event, "_native_origin", None)
    if (isinstance(origin, NativeOrigin) and origin.decision(text).status == "verified_human"
            and origin.spans != ((0, len(text)),)):
        return {ORIGIN_KEY: origin}
    if event.internal or any(metadata.get(key) for key in ("goal_continuation", "loop_wake", "process_notice",
                                                          "delegation_id", "relay_origin")):
        origin = NativeOrigin.generated(text, producer="gateway_runtime", event=str(event.message_id or "wake"))
    else:
        origin = getattr(event, "_native_origin", None)
    return {ORIGIN_KEY: origin} if isinstance(origin, NativeOrigin) and origin.decision(text).status != "unknown" else {}


def merge_event_origins(event, other):
    """Preserve independently witnessed person spans when native batching joins events.

    Unwitnessed text remains context. Multiple principals and ambiguous repeated occurrences
    stay held, because a single-principal row cannot certify either attribution.
    """
    receipts = [getattr(value, "_native_origin", None) for value in (event, other)]
    people = [receipt for receipt in receipts if isinstance(receipt, NativeOrigin)
              and receipt.decision(receipt.text).status == "verified_human"]
    if not people or len({receipt.principal for receipt in people}) != 1:
        event._native_origin = None
        return
    spans, references = set(), set()
    for receipt in people:
        start = event.text.find(receipt.text)
        if start < 0:
            continue
        if event.text.find(receipt.text, start + 1) >= 0:
            event._native_origin = None
            return
        spans.update((start + left, start + right) for left, right in receipt.spans)
        references.update((start + left, start + right, eid) for left, right, eid in receipt.references)
    first = people[0]
    event._native_origin = (NativeOrigin.person(event.text, platform=first.platform, principal=first.principal,
        event=str(event.message_id), authored=tuple(sorted(spans)), references=tuple(sorted(references)))
        if spans and event.message_id else None)
