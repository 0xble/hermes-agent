"""Schema for the agent-owned context compaction request tool."""

from tools.registry import registry

COMPACT_CONTEXT_SCHEMA = {
    "name": "compact_context",
    "description": (
        "Request context compaction at the next safe tool boundary. Call at a natural phase boundary when "
        "(1) a sub-task is finished and raw tool output is no longer needed verbatim, (2) the conversation "
        "switched to an unrelated topic, or (3) a long multi-step stretch is about to start and context is "
        "already past half the auto-compress threshold. Do not call mid-edit or mid-debug when exact recent "
        "output matters, right after compaction, to tidy a small context, or more than once per phase. "
        "Always pass focus naming decisions, open items, file paths, and IDs to preserve. "
        "Not supported on codex_app_server because native thread compaction cannot accept focus guidance."
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
