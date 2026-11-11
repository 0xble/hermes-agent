"""Token-shape helpers for goal authoring findings (stale-identifier exemptions).

Split from ``hermes_cli.goals`` to keep that module from growing; behavior is unchanged."""

from __future__ import annotations

import re


def _goal_authoring_text_is_path(text: str, start: int, end: int) -> bool:
    """Do not call a plan/file path a stale identifier merely because it has digits."""
    token_start = max(text.rfind(" ", 0, start), text.rfind("\n", 0, start), text.rfind("\t", 0, start)) + 1
    token_end_candidates = [index for index in (text.find(" ", end), text.find("\n", end), text.find("\t", end)) if index >= 0]
    token_end = min(token_end_candidates, default=len(text))
    token = text[token_start:token_end].strip(".,;:!?()[]{}<>\"'")
    # owner/repo#N is a PR/issue reference, not a file path, even though it has a slash.
    if re.fullmatch(r"[\w.-]+/[\w.-]+#\d+", token):
        return False
    return "/" in token or "\\" in token or bool(
        re.search(r"\.(?:md|markdown|txt|yaml|yml|json|toml)\b", token, re.IGNORECASE)
    )


def _goal_authoring_is_ordinal_hash(text: str, start: int) -> bool:
    """Allow ordinal rankings such as ``Ranked #1`` while rejecting bare receipts."""
    return bool(re.search(r"(?i)\b(?:ranked|top)\s*$", text[max(0, start - 24):start]))


def _goal_authoring_is_labeled_reference(text: str, start: int) -> bool:
    """Allow a short task/UUID prefix when its label makes it a live reference, not a receipt SHA."""
    context = text[max(0, start - 32):start]
    return bool(re.search(r"(?i)\b(?:task|uuid|session)\s*(?:id|uuid)?\s*[:#]?\s*$", context))


def _goal_authoring_is_ordinary_identifier(text: str, start: int, end: int) -> bool:
    """Allow human identifiers such as ``cafe5678`` when clearly naming a table."""
    context = text[max(0, start - 32):min(len(text), end + 32)]
    return bool(re.search(r"(?i)\b(?:table|column|record|dataset)\b", context))
