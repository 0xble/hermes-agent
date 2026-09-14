"""Deterministic authoring and admission rules for delegated task labels."""

from __future__ import annotations

from typing import Any, Callable, Mapping, Optional, Sequence

MAX_TASK_LABEL_CODEPOINTS = 24
MIN_TASK_LABEL_CODEPOINTS = 12
TASK_LABEL_DEPTH_REDUCTION = 4


def runtime_parent_spawn_depth(parent_agent: Any) -> int:
    """Return the trusted runtime spawn depth; model arguments never participate."""
    raw = getattr(parent_agent, "_delegate_depth", 0)
    if type(raw) is not int:
        return 0
    return max(0, raw)


def task_label_limit_for_depth(spawn_depth: int) -> int:
    """Hard per-admission limit for a parent at ``spawn_depth``."""
    depth = max(0, spawn_depth) if type(spawn_depth) is int else 0
    return max(MIN_TASK_LABEL_CODEPOINTS, MAX_TASK_LABEL_CODEPOINTS - TASK_LABEL_DEPTH_REDUCTION * depth)


def task_label_guidance(limit: Optional[int] = None) -> str:
    """Model-facing authoring guidance; the runtime remains authoritative."""
    upper = MAX_TASK_LABEL_CODEPOINTS if limit is None else int(limit)
    return (
        "Use a concise, meaningful, verb-first, privacy-safe sentence-case display label "
        "(preserve proper nouns/acronyms: Review context forks; Check API routing; not Review Context Forks); "
        "never use the goal. The runtime derives a hard depth-aware Unicode code-point limit from the actual "
        "parent spawn depth: 24 at depth 0, reduced by 4 per level, floored at 12. "
        f"Maximum 24 Unicode code points in task_label itself at the static schema boundary; this call's "
        f"runtime limit is at most {upper} code points, including spaces; "
        "indentation, references, separators and role suffixes do not count. This is a hard admission limit, not truncation; "
        "never silently truncated. "
        "Omit task_label on resume to preserve the existing identity, including historical longer labels."
    )


def admit_task_labels(
    task_list: Sequence[Mapping[str, Any]],
    task_label: Optional[str],
    parent_agent: Any,
    historical_label: Optional[Callable[[Mapping[str, Any]], Optional[str]]] = None,
) -> tuple[list[str] | None, str | None]:
    """Resolve and validate all labels before reservation, claims, or child setup.

    Resume labels supplied by durable history are identity data, not newly authored labels:
    they are returned unchanged and are exempt from the current depth limit.
    """
    limit = task_label_limit_for_depth(runtime_parent_spawn_depth(parent_agent))
    fallback_supplied = task_label is not None
    labels: list[str] = []
    for index, task in enumerate(task_list):
        is_resume = task.get("resume_session_id") is not None
        historical = historical_label(task) if is_resume and historical_label is not None else None
        if historical is not None:
            # Do not strip, truncate, or otherwise rewrite persisted identity data.
            labels.append(historical)
            continue
        supplied = task.get("task_label") if "task_label" in task else task_label
        path = f"tasks[{index}].task_label" if "task_label" in task or not fallback_supplied else "task_label"
        if not isinstance(supplied, str) or not supplied.strip():
            return None, (
                f"Task {index} requires a nonempty {path}. Provide a short verb-first, privacy-safe "
                f"task_label (for example, 'Check receipt'); use at most {limit} Unicode code points, never silently truncated."
            )
        # Check the raw value before canonical whitespace trimming. No truncation is permitted.
        if len(supplied) > limit:
            return None, (
                f"{path} is {len(supplied)} Unicode code points; maximum is {limit} at runtime parent spawn depth "
                f"{runtime_parent_spawn_depth(parent_agent)}. Write a shorter meaningful verb-first label "
                "(for example, 'Check receipt'). No child was started; labels are never silently truncated."
            )
        labels.append(supplied.strip())
    return labels, None
