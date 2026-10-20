"""Schema for the agent-owned context compaction request tool."""

from tools.registry import registry

COMPACT_CONTEXT_SCHEMA = {
    "name": "compact_context",
    "description": (
        "Request context compaction at the next safe tool boundary. When a new user message arrives, check "
        "this BEFORE calling any other tool, including skill_view. Call compact_context first if "
        "(1) the previous sub-task just finished and its raw tool output is no longer needed verbatim, or "
        "(2) the message switches to an unrelated topic and prior task details are no longer needed "
        "verbatim. A long transcript alone is enough. Do not wait for a token count. Skip it when the new "
        "message continues the same unfinished edit or debug, because exact recent output matters there. "
        "Also skip it right after a compaction, on a short transcript, or more than once per phase. "
        "Always pass focus naming decisions, open items, file paths, and IDs to preserve."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "focus": {
                "type": "string",
                "description": "Optional decisions, open items, paths, and IDs the summary must preserve.",
            },
            "reason": {
                "type": "string",
                "description": "Short reason this is a natural compaction boundary.",
            },
        },
        "required": ["reason"],
    },
}

# The live agent intercepts this tool before registry dispatch. The fallback handler keeps
# direct model_tools callers safe and explains why a live agent is required.
registry.register(
    name="compact_context",
    toolset="compression",
    schema=COMPACT_CONTEXT_SCHEMA,
    handler=lambda args, **_kwargs: {"accepted": False, "reason": "compact_context requires a live agent loop"},
    emoji="🧹",
)
