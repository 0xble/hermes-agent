"""Bounded runtime evidence helpers used by the persistent goal judge."""

from __future__ import annotations

import hashlib
import json
import re
import time
from typing import Any, List, Optional


JUDGE_EVIDENCE_BLOCK_TEMPLATE = (
    "Tool results recorded by the runtime while this goal was active (oldest "
    "first; authoritative, not written by the agent). Treat them as concrete "
    "evidence: the response does not need to repeat them. Weigh their age, "
    "since a later change can make an earlier result stale:\n{evidence_lines}\n\n"
)


_READ_ONLY_STATUS_CALL_RE = re.compile(
    r"^(?![^\r\n]*[\r\n])(?!.*(?:>>|>|\||&&|\|\||;|`|\$\(|\n|\r|&|<\(|>\())"
    r"(?!.*(?:\s-(?:exec|execdir|ok|fprint|fprint0|fprintf|fls|delete)(?:\s|$)|\s--delete(?:\s|$)))"
    r"(?!.*(?:--ext-diff|--textconv)(?:=|\s|$))"
    r"(?!.*(?:--output)(?:=|\s|$))(?!.*(?:--pre)(?:=|\s|$))"
    r"(?:gh\s+(?:pr|run)\s+(?:view|checks|status|list)(?:\s+.*)?|"
    r"git\s+(?:status|diff|log|show|rev-parse)(?:\s+.*)?|"
    r"git\s+branch(?:\s+(?:--show-current|--list|-a|--all))?\s*\Z|"
    r"(?:ls|pwd|rg|grep|cat|head|tail)(?:\s+.*)?|"
    r"find(?:\s+.*)?)\Z",
    re.I,
)
_READ_ONLY_STATUS_TOOLS = frozenset({
    "browser_get_images", "browser_snapshot", "read_file", "search_files",
    "web_extract", "web_search", "skill_view", "skills_list", "tool_search", "tool_describe",
})


def _goal_progress_fingerprint(evidence: Optional[List[dict[str, Any]]]) -> Optional[str]:
    """Return a stable identity for recorded tool progress, or ``None`` when there is no signal.

    Tool-result timestamps and content are runtime-owned, unlike the agent's prose. Hashing the
    bounded evidence ledger keeps the durable state small while distinguishing a new result from a
    repeated no-op evaluation.
    """
    rows = []
    for item in evidence or []:
        if not isinstance(item, dict) or str(item.get("tool") or "").strip().lower() == "quality gate":
            continue
        rows.append({key: item.get(key) for key in ("tool", "call", "output")})
    if not rows:
        return None
    payload = json.dumps(rows, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _evidence_scan_truncated_for_turn(evidence: Optional[List[dict[str, Any]]], previous_turn_at: float) -> bool:
    """Whether the bounded evidence scan may have omitted rows from this turn.

    A long transcript alone is not enough: older rows can fill the scan while the current
    turn remains fully visible. If every returned row is newer than the previous-turn boundary,
    the current turn may itself exceed the scan bound, so classify it as actionable.
    """
    if not bool(getattr(evidence, "truncated", False)):
        return False
    rows = [item for item in (evidence or []) if isinstance(item, dict)]
    if previous_turn_at <= 0.0:
        return True
    timestamps = []
    for item in rows:
        try:
            timestamps.append(float(item.get("timestamp") or 0.0))
        except (TypeError, ValueError):
            continue
    return bool(timestamps) and all(timestamp > previous_turn_at for timestamp in timestamps)


def _browser_console_read_only_call(call: Any) -> bool:
    """Accept only browser-console inspection calls, never arbitrary JS or malformed args."""
    text = str(call or "").strip()
    if not text:
        return False
    try:
        payload = json.loads(text)
    except (TypeError, ValueError):
        return False
    if not isinstance(payload, dict) or "expression" in payload:
        return False
    return set(payload).issubset({"clear"}) and payload.get("clear", False) is False


def _read_only_status_call(call: Any) -> str:
    """Extract a shell command from the collector's bounded JSON argument representation.

    The collector stores shell arguments as bounded JSON. A bare, truncated, or redacted call is
    deliberately not accepted: the classifier must fail closed rather than turn an allowed prefix
    into an actionable-command bypass.
    """
    text = str(call or "").strip()
    if not text.startswith("{"):
        return ""
    try:
        payload = json.loads(text)
    except (TypeError, ValueError):
        return ""
    if not isinstance(payload, dict):
        return ""
    command = payload.get("command")
    if not isinstance(command, str):
        command = payload.get("cmd")
    return command.strip() if isinstance(command, str) else ""


def _evidence_only_read_only_status(evidence: Optional[List[dict[str, Any]]]) -> bool:
    """Whether current-turn evidence consists only of bounded status/read operations."""
    from hermes_cli.goals import _READ_ONLY_STATUS_CALL_RE, _READ_ONLY_STATUS_TOOLS, _read_only_status_call
    rows = [item for item in (evidence or []) if isinstance(item, dict)]
    if not rows:
        return True
    for item in rows:
        tool = str(item.get("tool") or "").strip().lower()
        if tool == "browser_console":
            if _browser_console_read_only_call(item.get("call")):
                continue
            return False
        if tool in _READ_ONLY_STATUS_TOOLS:
            continue
        call = _read_only_status_call(item.get("call"))
        if tool in {"terminal", "shell"} and _READ_ONLY_STATUS_CALL_RE.fullmatch(call):
            continue
        return False
    return True


def _render_evidence_block(evidence: Optional[List[dict[str, Any]]], now: Optional[float] = None) -> str:
    """Judge prompt block for the evidence ledger; empty when there is none."""
    from hermes_cli.goals import JUDGE_EVIDENCE_BLOCK_TEMPLATE
    lines: list[str] = []
    now = time.time() if now is None else now
    for item in evidence or []:
        if not isinstance(item, dict):
            continue
        age = max(0, int(now - float(item.get("timestamp") or now)))
        head = f"- {item.get('tool') or 'tool'} ({age}s ago)"
        if item.get("call"):
            head += f": {item['call']}"
        output = str(item.get("output") or "").strip() or "(no output)"
        lines.append(f"{head}\n  output: {output}")
    if not lines:
        return ""
    return JUDGE_EVIDENCE_BLOCK_TEMPLATE.format(evidence_lines="\n".join(lines))
