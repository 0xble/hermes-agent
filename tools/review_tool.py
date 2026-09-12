"""Parent-only model-facing bridge to candidate-bound native review."""

from tools.registry import registry, tool_error


REVIEW_CHANGES_SCHEMA = {
    "name": "review_changes",
    "description": (
        "Capture and dispatch an independent inspection-only review of an explicit Git candidate. "
        "The candidate is bound to repository, base revision, accepted paths, tracked patch, and "
        "untracked file bytes. A dispatched review is a hard boundary for the current tool batch."
    ),
    "parameters": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "repository": {
                "type": "string",
                "description": "Absolute or current-process-resolvable path to the Git repository.",
            },
            "base_revision": {
                "type": "string",
                "description": "Explicit Git revision against which the candidate is captured.",
            },
            "accepted_scope": {
                "type": "array",
                "minItems": 1,
                "items": {"type": "string"},
                "description": "Explicit repository-relative paths that comprise the accepted review scope.",
            },
            "focus": {
                "type": "string",
                "description": "Optional narrow review question or gate-specific focus.",
            },
        },
        "required": ["repository", "base_revision", "accepted_scope"],
    },
}


def _parent_context_required(args, **_kwargs):
    return tool_error(
        "review_changes requires the parent agent loop context and cannot run as a generic tool call."
    )


registry.register(
    name="review_changes",
    toolset="review",
    schema=REVIEW_CHANGES_SCHEMA,
    handler=_parent_context_required,
    emoji="⚖",
)
