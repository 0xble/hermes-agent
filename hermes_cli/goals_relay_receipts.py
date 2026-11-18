"""Topical helpers extracted from hermes_cli/goals.py."""
from __future__ import annotations
from typing import Optional

def _relay_sender(content: str) -> Optional[tuple[str, str]]:
    """Return ``(profile, session_id)`` from a Hermes relay header on the first or last non-empty line."""
    from hermes_cli.goals import _unwrap_steer, _RELAY_HEADER_RE
    if not isinstance(content, str):
        return None
    lines = [line.strip() for line in _unwrap_steer(content).splitlines() if line.strip()]
    for line in (lines[-1:] + lines[:1]) if lines else []:
        match = _RELAY_HEADER_RE.fullmatch(line)
        if match:
            return match.group("profile"), match.group("session")
    return None


def _relay_receipt_id(content: str) -> str:
    from hermes_cli.goals import re, _unwrap_steer, _RELAY_HEADER_RE
    lines = [line.strip() for line in _unwrap_steer(str(content or "")).splitlines() if line.strip()]
    for line in (lines[-1:] + lines[:1]) if lines else []:
        match = re.search(r"\breceipt=([^\s\]]+)", line) if _RELAY_HEADER_RE.fullmatch(line) else None
        if match:
            return match.group(1)
    return ""
