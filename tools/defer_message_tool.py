"""Schema stays stable. Availability is enforced against the live turn at dispatch."""
import json

from tools.registry import registry

DEFER_MESSAGE_SCHEMA = {
    "name": "defer_message",
    "description": "Hand an unrelated auto-steered user message back to the host queue for its own next turn. "
                   "Use only the exact message_id supplied in its auto-steer wrapper. "
                   "On success, do not act on or answer that message in the current turn. "
                   "Unavailable without an auto-steered message in the current turn.",
    "parameters": {"type": "object", "properties": {
        "message_id": {"type": "string", "description": "Exact immutable ID from the auto-steer wrapper."}},
        "required": ["message_id"], "additionalProperties": False},
}

registry.register(
    name="defer_message", toolset="steering", schema=DEFER_MESSAGE_SCHEMA,
    handler=lambda args, **kwargs: json.dumps({"accepted": False, "error": "Requires a live agent turn"}),
)
