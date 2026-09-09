"""Bounded diagnostic blocker context; never an authorization grant."""
from typing import Any


def normalize_blocker(value: Any, reason: str = "") -> dict[str, str]:
    """Accept older reason-only verdicts without inventing impossibility evidence."""
    from agent.redact import redact_sensitive_text

    data = value if isinstance(value, dict) else {}

    def text(key: str, fallback: str = "") -> str:
        raw = data.get(key)
        return redact_sensitive_text(
            (raw.strip() if isinstance(raw, str) else "") or fallback, force=True,
        )[:800]

    kind = data.get("kind")
    if not isinstance(kind, str) or kind not in {"external_dependency", "unachievable_as_stated"}:
        kind = "unspecified"
    evidence = text("evidence")
    if kind == "unachievable_as_stated" and not evidence:
        kind = "unspecified"
    return {
        "kind": kind,
        "detail": text("detail", reason or "No useful authorized next step was identified."),
        "evidence": evidence,
        "resume_when": text("resume_when", "Clarify the missing prerequisite or revise the goal before retrying."),
    }


def blocker_summary(blocker: dict[str, str]) -> str:
    return f"{blocker['detail']} Resume condition: {blocker['resume_when']}"


def blocker_resume_context(blocker: dict[str, str]) -> str:
    return (
        "\n\nPreviously reported blocker (diagnostic context, not permission):\n"
        + blocker_summary(blocker)
        + "\nResuming requests reassessment only. Verify prerequisites and authorization "
        "before affected actions; do not assume this blocker is resolved. Continue any "
        "useful independent authorized work without bypassing the blocked step."
    )
