"""Origin codec for the private, authenticated worker channel, never client metadata.

Callers must establish worker/admission ownership before decoding. The transcript's
reserved metadata key is deliberately not a wire input to this codec.
"""
from agent.source_origin import NativeOrigin, _decision, native_origin, origin_metadata

_MESSAGE_OPERATIONS = {"transcript.append", "transcript.append_one", "compression.append",
                       "compression.archive", "compression.publish"}
_MESSAGE_LISTS = ("messages", "carried_messages", "unresolved_held", "held_messages", "held_for_cas")


def pack_origin(receipt):
    if isinstance(receipt, NativeOrigin) and receipt.decision(receipt.text).status != "unknown":
        return {"text": receipt.text, "record": receipt.record("worker", "worker")}
    return None


def unpack_origin(value):
    if not isinstance(value, dict) or not isinstance(value.get("text"), str):
        return None
    decision = _decision(value.get("record"))
    text = value["text"]
    if decision.status == "unknown" or value["record"].get("characters") != len(text):
        return None
    return NativeOrigin(text, decision.status, decision.platform, decision.principal or "",
                        decision.event, decision.spans, decision.references)


def pack_worker_payload(operation, payload):
    if operation not in _MESSAGE_OPERATIONS:
        return payload
    payload = {key: value for key, value in payload.items() if key != "native_origins"}
    lists = {key: payload[key] for key in _MESSAGE_LISTS if isinstance(payload.get(key), list)}
    if operation == "transcript.append_one":
        lists = {"single": [payload]}
    origins = {f"{key}:{i}": packed for key, messages in lists.items() for i, message in enumerate(messages)
               if isinstance(message, dict)
               and (packed := pack_origin(native_origin(message.get("display_metadata")))) is not None}
    return {**payload, "native_origins": origins} if origins else payload


def unpack_worker_payload(payload, messages, key="messages"):
    """Only worker receipt handlers call this, after assignment and sequence validation."""
    origins = payload.get("native_origins", {})
    for index, message in enumerate(messages):
        receipt = unpack_origin(origins.get(f"{key}:{index}")) if isinstance(origins, dict) else None
        if (receipt is not None and receipt.text == message.get("_replay_persisted_content", message.get("content"))
                and receipt.event == message.get("platform_message_id", message.get("message_id"))):
            message["display_metadata"] = origin_metadata(receipt, message.get("display_metadata"))


def decode_worker_payload(operation, payload):
    """Private receipt annotations are consumed only after the worker assignment is fenced."""
    if operation in _MESSAGE_OPERATIONS:
        if operation == "transcript.append_one":
            unpack_worker_payload(payload, [payload], "single")
        for key in _MESSAGE_LISTS:
            if isinstance(payload.get(key), list):
                unpack_worker_payload(payload, payload[key], key)
        payload.pop("native_origins", None)
    return payload


def pack_stored_history(messages):
    from agent.source_origin import _StoredMetadata, _StoredRecord
    origins = {}
    for index, message in enumerate(messages):
        metadata = message.get("display_metadata")
        record = getattr(metadata, "origin", None)
        text = message.get("_replay_persisted_content", message.get("content"))
        if (not isinstance(metadata, _StoredMetadata) or not isinstance(record, _StoredRecord)
                or not record.unchanged() or record.binding is None or not isinstance(text, str)
                or record.binding[4] != text or record.get("message_uid") != message.get("message_uid")
                or record.binding[2] != message.get("role")
                or record.get("event") != message.get("platform_message_id", message.get("message_id"))):
            continue
        receipt = unpack_origin({"text": text, "record": record})
        if receipt is not None:
            origins[f"messages:{index}"] = pack_origin(receipt)
    return {"messages": messages, **({"native_origins": origins} if origins else {})}

