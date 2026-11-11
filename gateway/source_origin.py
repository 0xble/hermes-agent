"""Positive transport evidence, separate from inherited routing and claimed author labels."""

from agent.source_origin import NativeOrigin, origin_metadata

SLACK_SYSTEM_USERS = frozenset({"USLACKBOT", "USLACK"})


def stamp_telegram_person(event, message):
    from gateway.platforms.event import turn_origin_for_event
    if turn_origin_for_event(event) in {"relay", "process", "goal_loop", "cron"}:
        return event
    user = getattr(message, "from_user", None)
    # A forwarded message, channel post, or bot-mediated payload is not this person's authored span.
    if (user is not None and getattr(user, "is_bot", None) is False
            and type(getattr(user, "id", None)) is int and user.id > 0
            and not any(getattr(message, name, None) for name in ("forward_origin", "via_bot", "sender_chat"))
            and isinstance(event.text, str) and event.text and event.message_id):
        raw = getattr(message, "text", None) or getattr(message, "caption", None)
        if not isinstance(raw, str) or raw != event.text:
            return event
        spans = _telegram_authored_spans(event.text, getattr(message, "entities", None)
                                        or getattr(message, "caption_entities", None) or ())
        if spans:
            event._native_origin = NativeOrigin.person(event.text, platform="telegram", principal=str(user.id),
                                                       event=str(event.message_id), authored=spans)
    return event


def insert_event_context(event, text, offset, addition):
    """Update the receipt at the formatter's insertion boundary, never by substring search."""
    receipt = getattr(event, "_native_origin", None)
    if isinstance(receipt, NativeOrigin) and receipt.text == text:
        event._native_origin = receipt.insert(offset, addition)
    return text[:offset] + addition + text[offset:]


def stamp_slack_person(event, native, authored_text, *, verified_person=False):
    """Only the adapter's native message event can witness the flat authored body."""
    user = native.get("user")
    if (verified_person is not True or not isinstance(user, str) or not user or user in SLACK_SYSTEM_USERS or native.get("bot_id") or native.get("bot_profile")
            or (native.get("app_id") and not native.get("client_msg_id"))
            or native.get("subtype") not in (None, "file_share", "thread_broadcast")
            or native.get("_hermes_force_process") or event.internal
            or (native.get("user_profile") or {}).get("is_bot")
            or not event.message_id or not authored_text or not event.text.startswith(authored_text)):
        return event
    from agent.source_origin import authored_text_spans
    spans = authored_text_spans(authored_text)
    if spans:
        event._native_origin = NativeOrigin.person(event.text, platform="slack", principal=user,
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
    from gateway.platforms.event import turn_origin_for_event
    producer = turn_origin_for_event(event)
    origin = getattr(event, "_native_origin", None)
    if isinstance(origin, NativeOrigin) and origin.decision(text).status == "verified_human" and event.message_id:
        origin = NativeOrigin.person(text, platform=origin.platform, principal=origin.principal,
                                     event=str(event.message_id), authored=origin.spans, references=origin.references)
    if (isinstance(origin, NativeOrigin) and origin.decision(text).status == "verified_human"
            and origin.spans != ((0, len(text)),)):
        return origin_metadata(origin)
    if event.internal or producer in {"relay", "process", "goal_loop", "cron"}:
        origin = NativeOrigin.generated(text, producer=producer if producer != "unknown" else "gateway_runtime",
                                       event=str(event.message_id or "wake"))
    return origin_metadata(origin) if isinstance(origin, NativeOrigin) and origin.decision(text).status != "unknown" else {}


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
