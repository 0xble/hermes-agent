"""Native occurrence evidence. Text and routing labels never certify a person."""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Literal
import weakref

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


@dataclass(frozen=True, repr=False)
class NativeOrigin:
    """Process-local adapter receipt, deliberately not accepted from serialized client metadata.

    An adapter calls person only after its transport positively identifies a person. Other
    surfaces remain unknown. Capturing the exact text invalidates receipts after merges/edits.
    """
    text: str
    status: str
    platform: str
    principal: str
    event: str
    spans: tuple
    references: tuple = ()

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

    def insert(self, offset, addition):
        """A formatter inserts context without certifying the inserted characters."""
        if not addition:
            return self
        spans, references = [], []
        for left, right, event in self.references:
            if left < offset < right:
                spans.extend(((left, offset), (offset + len(addition), right + len(addition))))
                references.extend(((left, offset, event), (offset + len(addition), right + len(addition), event)))
            else:
                shift = len(addition) if left >= offset else 0
                spans.append((left + shift, right + shift))
                references.append((left + shift, right + shift, event))
        return NativeOrigin(self.text[:offset] + addition + self.text[offset:], self.status,
                            self.platform, self.principal, self.event, tuple(spans), tuple(references))


class _NativeRecord(dict):
    """Only the native occurrence writer may serialize a reserved stamp."""


class _OriginReference(dict):
    """An empty JSON value, whose exact live identity anchors an out-of-band receipt."""
    __slots__ = ("__weakref__",)


_origin_receipts: dict[int, tuple[weakref.ReferenceType, NativeOrigin]] = {}


def origin_metadata(receipt, metadata=None):
    """Keep native authority off the message graph, including the receipt's original text.

    Shallow history/metadata copies retain the anchor. JSON and deep copies cannot mint
    evidence, and releasing the last live anchor releases its process-local receipt.
    """
    anchor = _OriginReference()
    key = id(anchor)
    _origin_receipts[key] = (weakref.ref(anchor, lambda ref: _origin_receipts.pop(key, None)), receipt)
    return {**(metadata or {}), ORIGIN_KEY: anchor}


def native_origin(metadata):
    """Resolve only a native adapter input or the exact still-live origin anchor."""
    value = metadata.get(ORIGIN_KEY) if isinstance(metadata, dict) else None
    if isinstance(value, NativeOrigin):
        return value
    if isinstance(value, _OriginReference):
        entry = _origin_receipts.get(id(value))
        if entry is not None and entry[0]() is value:
            return entry[1]
    return None


def public_origin_projection(value):
    """Client projections never expose reserved evidence, even its empty anchor."""
    if isinstance(value, dict):
        return {key: public_origin_projection(item) for key, item in value.items() if key != ORIGIN_KEY}
    if isinstance(value, (list, tuple)):
        return [public_origin_projection(item) for item in value]
    return value


def bind_turn_origin(message, platform, persisted_content=None):
    """Bind the durable variant and native typed event before the shared producer guard."""
    if persisted_content is not None and persisted_content != message.get("content"):
        message["_replay_persisted_content"] = persisted_content
    receipt = native_origin(message.get("display_metadata"))
    if (receipt is not None and receipt.platform in {"cli", "tui"}
            and receipt.text == message.get("_replay_persisted_content", message.get("content"))):
        message["platform_message_id"] = receipt.event
    stamp_runtime_origin(message, platform)


def typed_input_message(text, platform):
    """Called only where an interactive surface accepts typed text, never by turn builders."""
    import getpass
    from uuid import uuid4
    event = "typed:" + uuid4().hex
    return {"role": "user", "content": text, "platform_message_id": event,
            "display_metadata": origin_metadata(NativeOrigin.person(
                text, platform=platform, principal=getpass.getuser(), event=event, authored=authored_text_spans(text)))}


def authored_text_spans(text):
    """Plain-text quote lines remain references on native interactive surfaces."""
    spans, offset = [], 0
    for line in text.splitlines(keepends=True):
        if line.lstrip().startswith((">>>", "&gt;&gt;&gt;")):
            break  # Slack's multiline quotation covers the rest of the body.
        if not line.lstrip().startswith((">", "&gt;")):
            if spans and spans[-1][1] == offset:
                spans[-1] = (spans[-1][0], offset + len(line))
            else:
                spans.append((offset, offset + len(line)))
        offset += len(line)
    return tuple(spans)


class _StoredRecord(dict):
    """A native read retains its original bytes and occurrence binding across metadata edits."""
    def __init__(self, record, binding):
        super().__init__(record)
        self.sealed = json.dumps(record, sort_keys=True)
        self.binding = binding

    def unchanged(self):
        return json.dumps(self, sort_keys=True) == self.sealed


class _StoredMetadata(dict):
    """Public metadata with the native row's origin kept off every JSON projection."""
    def __deepcopy__(self, memo):
        from copy import deepcopy
        return deepcopy(dict(self), memo)

    def __bool__(self):
        return bool(len(self)) or hasattr(self, "origin")


def stored_origin_metadata(metadata, row=None, *, session_id=None):
    """Called only on metadata acquired from a stored row, never client/import input."""
    if metadata is None:
        return None
    result = _StoredMetadata(metadata)
    record = getattr(metadata, "origin", result.pop(ORIGIN_KEY, None))
    if isinstance(record, dict):
        binding = None
        if row is not None:
            binding = (row["session_id"] if "session_id" in row.keys() else session_id,
                       row["message_uid"], row["role"], row["platform_message_id"], row["content"])
        result.origin = _StoredRecord(record, binding)
    return result


def reserve_metadata(metadata, stored_metadata=None):
    """Imports and client metadata cannot mint origin evidence. Stored records may survive."""
    result = dict(metadata or {})
    receipt = getattr(metadata, "origin", result.get(ORIGIN_KEY))
    if isinstance(metadata, _StoredMetadata) and isinstance(receipt, _StoredRecord):
        result[ORIGIN_KEY] = receipt
    if not (isinstance(receipt, _NativeRecord)
            or isinstance(receipt, _StoredRecord) and receipt.unchanged()):
        result.pop(ORIGIN_KEY, None)
    if stored_metadata:
        if isinstance(stored_metadata, str):
            try:
                stored_metadata = json.loads(stored_metadata)
            except ValueError:
                return result
        record = stored_metadata.get(ORIGIN_KEY) if isinstance(stored_metadata, dict) else None
        if isinstance(record, dict):
            result[ORIGIN_KEY] = _NativeRecord(record)
    return result


def persistence_metadata(metadata, message, session_id, encoded_content=None):
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
    record = getattr(metadata, "origin", metadata.get(ORIGIN_KEY))
    if isinstance(record, _StoredRecord):
        binding = (session_id, message.get("message_uid"), message.get("role"),
                   message.get("platform_message_id") or message.get("message_id"), encoded_content)
        if record.binding != binding or not record.unchanged():
            result.pop(ORIGIN_KEY, None)
        return result
    content = message.get("_replay_persisted_content", message.get("content"))
    receipt = native_origin(metadata)
    if isinstance(receipt, NativeOrigin) and encoded_content is not None and encoded_content != receipt.text:
        receipt = None
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
    record = getattr(metadata, "origin", metadata.get(ORIGIN_KEY)) if isinstance(metadata, dict) else None
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
    Compare the scaffold-stripped native instruction before egress redaction changes span offsets.
    """
    from agent.synthetic_prompt import is_runtime_prompt
    for message in reversed(messages or ()):
        if message.get("role") != "user":
            continue
        if message.get("_replay_persisted_content", message.get("content")) != text:
            return None
        metadata = message.get("display_metadata") or {}
        receipt = native_origin(metadata)
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
    metadata = message.get("display_metadata") or {}
    receipt = native_origin(metadata)
    if isinstance(metadata.get(ORIGIN_KEY), NativeOrigin):
        message["display_metadata"] = origin_metadata(receipt, metadata)
    if (isinstance(receipt, NativeOrigin) and receipt.decision(text).status == "verified_human"
            and receipt.spans != ((0, len(text)),)):
        return
    if isinstance(text, str) and (is_runtime_prompt(display_kind=message.get("display_kind"), platform=platform)
            or producer in {"oneshot", "tool", "subagent", "a2a", "relay", "goal", "loop", "process"}):
        message["display_metadata"] = origin_metadata(
            NativeOrigin.generated(text, producer=producer, event=message.get("message_uid") or "native_turn"),
            metadata)
