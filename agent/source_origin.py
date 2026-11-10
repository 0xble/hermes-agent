"""Native occurrence evidence. Text and routing labels never certify a person."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Literal

ORIGIN_KEY = "_hermes_source_origin"
MAX_ORIGIN_BYTES = 8192
MAX_SPANS = 32


@dataclass(frozen=True)
class OriginDecision:
    status: Literal["verified_human", "generated", "unknown"]
    spans: tuple[tuple[int, int], ...] = ()
    principal: str | None = None
    platform: str | None = None
    event: str | None = None
    references: tuple[tuple[int, int, str], ...] = ()


class NativeOrigin(dict):
    """Process-local adapter receipt, deliberately not accepted from serialized client metadata.

    An adapter calls person only after its transport positively identifies a person. Other
    surfaces remain unknown. Capturing the exact text invalidates receipts after merges/edits.
    """
    def __init__(self, text, status, platform, principal, event, spans, references=()):
        # Generic JSON export intentionally carries no authority. Only the native writer
        # expands this process-local receipt after binding the exact durable occurrence.
        super().__init__()
        self.text, self.status, self.platform = text, status, platform
        self.principal, self.event, self.spans, self.references = principal, event, spans, references

    @classmethod
    def person(cls, text, *, platform, principal, event, authored=None, references=None):
        spans = tuple(authored) if authored is not None else ((0, len(text)),)
        return cls(text, "verified_human", platform, principal, event,
                   spans, tuple(references) if references is not None else tuple((a, b, event) for a, b in spans))

    @classmethod
    def generated(cls, text, *, producer, event):
        return cls(text, "generated", producer, "", event, ())

    def decision(self, text):
        if not isinstance(text, str) or text != self.text:
            return OriginDecision("unknown")
        return _decision(self.record("native", "native"))

    def record(self, session_id, message_uid):
        return {"version": 1, "status": self.status, "platform": self.platform,
                "principal": self.principal, "event": self.event, "spans": self.spans,
                "characters": len(self.text), "session_id": session_id, "message_uid": message_uid,
                "references": self.references}


class _NativeRecord(dict):
    """Only the native occurrence writer may serialize a reserved stamp."""


def reserve_metadata(metadata):
    """Imports, client metadata and display edits cannot mint origin evidence."""
    result = dict(metadata)
    if not isinstance(result.get(ORIGIN_KEY), _NativeRecord):
        result.pop(ORIGIN_KEY, None)
    return result


def persistence_metadata(metadata, message, session_id):
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except (ValueError, TypeError):
            return None
    if metadata is None:
        metadata = {}
    if not isinstance(metadata, dict):
        return metadata
    result = reserve_metadata(metadata)
    receipt = metadata.get(ORIGIN_KEY)
    content = message.get("_replay_persisted_content", message.get("content"))
    if not isinstance(receipt, NativeOrigin) and isinstance(content, str):
        from agent.synthetic_prompt import is_runtime_prompt
        if is_runtime_prompt(display_kind=message.get("display_kind")):
            receipt = NativeOrigin.generated(content, producer=message.get("display_kind"),
                                             event=message.get("message_uid") or "native")
    if isinstance(receipt, NativeOrigin) and receipt.decision(content).status != "unknown":
        uid = message.get("message_uid")
        platform_id = message.get("platform_message_id") or message.get("message_id")
        if uid and (receipt.status == "generated" or platform_id == receipt.event):
            result[ORIGIN_KEY] = _NativeRecord(receipt.record(session_id, uid))
    return result


def _decision(record):
    if not isinstance(record, dict) or record.get("version") != 1:
        return OriginDecision("unknown")
    status = record.get("status")
    platform, event = record.get("platform"), record.get("event")
    if not all(isinstance(v, str) and 0 < len(v) <= 512 for v in (platform, event)):
        return OriginDecision("unknown")
    if status == "generated":
        return OriginDecision("generated", platform=platform, event=event)
    principal, spans, length = record.get("principal"), record.get("spans"), record.get("characters")
    if (status != "verified_human" or not isinstance(principal, str) or not 0 < len(principal) <= 512
            or type(length) is not int or length < 1 or not isinstance(spans, (list, tuple))
            or not 0 < len(spans) <= MAX_SPANS):
        return OriginDecision("unknown")
    previous = 0
    for span in spans:
        if (not isinstance(span, (list, tuple)) or len(span) != 2
                or any(type(n) is not int for n in span) or not previous <= span[0] < span[1] <= length):
            return OriginDecision("unknown")
        previous = span[1]
    references = record.get("references")
    if (not isinstance(references, (tuple, list)) or len(references) != len(spans)
            or any(not isinstance(ref, (tuple, list)) or len(ref) != 3 or tuple(ref[:2]) != tuple(span)
                   or not isinstance(ref[2], str) or not 0 < len(ref[2]) <= 512
                   for ref, span in zip(references, spans))):
        return OriginDecision("unknown")
    return OriginDecision("verified_human", tuple(tuple(s) for s in spans), principal, platform, event,
                          tuple(tuple(ref) for ref in references))


def classify_origin(message, *, session_id):
    """Metadata-only preflight on rows acquired from the native store, never import payloads."""
    metadata = message.get("display_metadata") or {}
    if isinstance(metadata, str):
        try:
            metadata = json.loads(metadata)
        except ValueError:
            return OriginDecision("unknown")
    record = metadata.get(ORIGIN_KEY) if isinstance(metadata, dict) else None
    if not isinstance(record, dict):
        from agent.synthetic_prompt import is_runtime_prompt
        return OriginDecision("generated" if is_runtime_prompt(display_kind=message.get("display_kind")) else "unknown")
    if (record.get("session_id") != session_id or not message.get("message_uid")
            or record.get("message_uid") != message.get("message_uid")):
        return OriginDecision("unknown")
    decision = _decision(record)
    from agent.synthetic_prompt import is_runtime_prompt
    if decision.status == "verified_human" and (message.get("role") != "user"
            or decision.event != message.get("platform_message_id")
            or (is_runtime_prompt(display_kind=message.get("display_kind"))
                and decision.spans == ((0, record.get("characters")),))):
        return OriginDecision("unknown")
    return decision


def human_spans(text, decision):
    if decision.status != "verified_human" or not isinstance(text, str):
        return None
    if any(end > len(text) for _, end in decision.spans):
        return None
    return "\n".join(text[start:end] for start, end in decision.spans) or None


def retention_user_text(text, messages):
    """Only a still-native receipt on this exact turn can authorize automatic retention.

    Serialized/imported dictionaries do not become native evidence by being passed to a provider.
    Historical acquisition and recovery instead use classify_origin inside the guarded snapshot.
    """
    from agent.synthetic_prompt import is_runtime_prompt
    for message in reversed(messages or ()):
        if message.get("role") != "user":
            continue
        if message.get("content") != text:
            return None
        metadata = message.get("display_metadata") or {}
        receipt = metadata.get(ORIGIN_KEY) if isinstance(metadata, dict) else None
        if not isinstance(receipt, NativeOrigin):
            return None
        decision = receipt.decision(text)
        if is_runtime_prompt(display_kind=message.get("display_kind")) and decision.spans == ((0, len(text)),):
            return None
        return human_spans(text, decision)
    return None


def stamp_runtime_origin(message, platform):
    """The common turn builder stamps unattended producers without inferring a person."""
    from agent.session_source import session_source_for
    from agent.synthetic_prompt import is_runtime_prompt
    text = message.get("content")
    producer = session_source_for(platform)
    receipt = (message.get("display_metadata") or {}).get(ORIGIN_KEY)
    if (isinstance(receipt, NativeOrigin) and receipt.decision(text).status == "verified_human"
            and receipt.spans != ((0, len(text)),)):
        return
    if isinstance(text, str) and (is_runtime_prompt(display_kind=message.get("display_kind"), platform=platform)
            or producer in {"oneshot", "tool", "subagent", "a2a", "relay", "goal", "loop", "process"}):
        message["display_metadata"] = {**(message.get("display_metadata") or {}), ORIGIN_KEY:
            NativeOrigin.generated(text, producer=producer, event=message.get("message_uid") or "native_turn")}
