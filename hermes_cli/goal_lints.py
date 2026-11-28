"""Ownership-goal authoring lints and read-only state inspection helpers.

The lints are deliberately separate from lifecycle authority: an agent may edit its own
outcome, cadence, or wording, but it may not silently weaken an enrolled requirement.
"""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Mapping

_CACHE_PART = Path(os.path.abspath(os.path.expanduser("~/.hermes/cache")))
_APPROVAL_RE = re.compile(
    r"(?i)\b(?:approval|approve|approved|permission|permitted|consent|ask\s+(?:brian|the\s+user)|"
    r"wait\s+for\s+(?:brian|the\s+user)|user\s+(?:input|decision)|human\s+(?:input|approval)|"
    r"requires?\s+(?:brian|the\s+user)|only\s+(?:brian|the\s+user))\b"
)
_SAFETY_RE = re.compile(r"(?i)\b(?:money|messages?\s+to\s+others?|delet(?:e|ion|ing)|credentials?|external\s+commitment)\b")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?)?$")


@dataclass(frozen=True)
class LintFinding:
    code: str
    message: str
    fix: str
    warning: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "fix": self.fix, "warning": self.warning}


class GoalLintError(ValueError):
    """Actionable refusal for a new or edited ownership goal."""

    def __init__(self, findings: Iterable[LintFinding]):
        self.findings = tuple(findings)
        super().__init__("; ".join(f"{f.message} Fix: {f.fix}" for f in self.findings))


def _text(value: Any) -> str:
    return str(value or "").strip()


def _path(value: Any) -> Path | None:
    text = _text(value)
    if not text:
        return None
    return Path(text).expanduser()


def _under_cache(path: Path) -> bool:
    try:
        path.absolute().relative_to(_CACHE_PART)
        return True
    except ValueError:
        return False


def _date(value: Any) -> bool:
    text = _text(value)
    if not text:
        return False
    if text.isdigit():
        return float(text) > 0
    if not _DATE_RE.match(text):
        return False
    try:
        datetime.fromisoformat(text.replace("Z", "+00:00"))
        return True
    except ValueError:
        return False


def _approval_without_safety(text: str) -> bool:
    return bool(_APPROVAL_RE.search(text)) and not _SAFETY_RE.search(text)


def lint_ownership(
    *,
    due: Any = "",
    done_when: Any = "",
    verification: Any = "",
    plan_path: Any = "",
    log_path: Any = "",
    constraints: Any = "",
    stop_when: Any = "",
    user_quote: Any = "",
    existing: Any = None,
    ownership: bool = True,
    reason: Any = "",
) -> list[LintFinding]:
    """Return findings; new ownership rows are refusals, existing rows are warnings.

    ``verification`` is accepted as the legacy spelling of ``done_when``. A quote is only
    considered an authority source when the caller has already authenticated it; this module
    only checks that one was supplied for the approval-language rule.
    """
    if not ownership:
        return []
    existing_row = existing is not None
    findings: list[LintFinding] = []
    missing = []
    if not _date(due):
        missing.append("due")
    if not (_text(done_when) or _text(verification)):
        missing.append("done_when (or verification)")
    if missing:
        findings.append(LintFinding(
            "missing_acceptance_fields",
            "ownership goal is missing " + " and ".join(missing),
            "set a due date (ISO-8601) and a concrete done_when/verification acceptance check",
            warning=existing_row,
        ))
    for label, value in (("plan_path", plan_path), ("log_path", log_path)):
        path = _path(value)
        if path is None:
            continue
        if _under_cache(path):
            findings.append(LintFinding(
                "cache_path",
                f"{label} points under ~/.hermes/cache: {value}",
                f"move {label} to a durable project path outside ~/.hermes/cache",
                warning=existing_row,
            ))
        elif not path.exists():
            findings.append(LintFinding(
                "missing_path",
                f"{label} does not exist: {value}",
                f"create the durable file before enrolling it, or provide an existing {label}",
                warning=existing_row,
            ))
    quote = _text(user_quote)
    for label, value in (("constraints", constraints), ("stop_when", stop_when)):
        if _approval_without_safety(_text(value)) and not quote:
            findings.append(LintFinding(
                "approval_wording_without_authority",
                f"{label} contains approval wording without a user quote or Safety class",
                "quote Brian's instruction verbatim, or name one Safety class: money, messages to others, deletion, credentials, or an external commitment",
                warning=existing_row,
            ))
    return findings


def enforce_ownership(**kwargs: Any) -> list[LintFinding]:
    findings = lint_ownership(**kwargs)
    errors = [f for f in findings if not f.warning]
    if errors:
        raise GoalLintError(errors)
    return findings


def due_change_findings(old_due: Any, new_due: Any, *, user_quote: Any = "", reason: Any = "") -> list[LintFinding]:
    old, new = _text(old_due), _text(new_due)
    if not old or old == new:
        return []
    # Any removal or later date relaxes the enrolled date. Earlier dates are safe to tighten.
    relax = not new
    try:
        relax = relax or datetime.fromisoformat(new.replace("Z", "+00:00")) > datetime.fromisoformat(old.replace("Z", "+00:00"))
    except ValueError:
        relax = True
    if relax and not _text(user_quote):
        return [LintFinding("due_relaxation_quote_required", "due date removal or relaxation would weaken the enrolled date", "retain the original due date or include Brian's verbatim instruction authorizing the change")]
    if not _text(reason):
        return [LintFinding("due_change_reason_required", "a due date change must be recorded in the baseline", "provide a concise reason and append the new due date to baseline")]
    return []


def append_due_baseline(baseline: Any, *, old_due: Any, new_due: Any, reason: Any, at: float | None = None) -> list[dict[str, Any]]:
    rows = [dict(row) for row in baseline] if isinstance(baseline, list) else []
    rows.append({"due": _text(new_due), "previous_due": _text(old_due), "reason": _text(reason), "at": at})
    return rows


__all__ = ["GoalLintError", "LintFinding", "append_due_baseline", "due_change_findings", "enforce_ownership", "lint_ownership"]
