"""Opt-in reviewed cron report delivery.

The scheduler owns the review state machine; this module owns the small, pure
contract checker and the durable JSONL receipt writer.
"""

from __future__ import annotations

import json
import logging
import math
import re
import threading
import time
from pathlib import Path
from typing import Any, Optional

from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

RECEIPT_NAME = "review-receipts.jsonl"
MAX_RECEIPT_BYTES = 2 * 1024 * 1024
_receipt_lock = threading.RLock()
_receipt_lock_holder = threading.local()
_DEFAULT_SILENCE_MARKERS = (
    "[SILENT]",
    "SILENT",
    "NO_REPLY",
    "NO REPLY",
    "[CRON_FAILURE]",
)


def receipt_path() -> Path:
    path = get_hermes_home().resolve() / "cron" / RECEIPT_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def load_contract(contract_ref: str) -> dict[str, Any]:
    """Load a contract from the profile store or the shipped cron contracts."""
    from agent.skill_utils import yaml_load

    ref = str(contract_ref or "").strip()
    if not ref:
        raise ValueError("review.contract is required")
    candidate = Path(ref).expanduser()
    choices = (
        [candidate]
        if candidate.is_absolute()
        else [
            get_hermes_home().resolve() / "cron" / "contracts" / candidate,
            Path(__file__).resolve().parent / "contracts" / candidate,
            get_hermes_home().resolve() / candidate,
        ]
    )
    path = next((p for p in choices if p.is_file()), None)
    if path is None:
        raise FileNotFoundError(f"review contract not found: {ref}")
    parsed = yaml_load(path.read_text(encoding="utf-8-sig"))
    if not isinstance(parsed, dict):
        raise ValueError(f"review contract must be a mapping: {path}")
    parsed["_path"] = str(path)
    return parsed


def _contract_lines(text: str):
    """Quoted Markdown examples cannot satisfy the report's envelope."""
    from gateway.copy_blocks import _next_fence

    fence = None
    for line in text.splitlines():
        previous = fence
        fence = _next_fence(fence, line)
        if (
            previous is None
            and fence is None
            and not re.match(r"^(?: {4}| {0,3}\t)", line)
        ):
            yield line


def _sections(text: str) -> list[str]:
    sections = []
    for line in _contract_lines(text):
        match = re.match(r"^ {0,3}(##)\s+(.+?)\s*#*\s*$", line)
        if match:
            sections.append(match.group(2).strip())
    return sections


def _title(text: str) -> Optional[str]:
    for line in text.splitlines():
        if not line.strip():
            continue
        match = re.match(r"^ {0,3}#\s+(.+?)\s*#*\s*$", line)
        return match.group(1).strip() if match else None
    return None


_DATED_TITLE = re.compile(
    r" — (?:(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday), )?"
    r"(?:January|February|March|April|May|June|July|August|September|"
    r"October|November|December) \d{1,2}(?:, \d{4})?(?: at \d{1,2}:\d{2} [AP]M)?$"
)


def _title_matches(actual: Optional[str], expected: str) -> bool:
    """Accept the exact title, or the report archetype's dated form of it.

    Reports are titled ``<Title> — [Weekday, ]<Month D[, YYYY]>`` (optionally
    ``at h:mm AM``), so only that suffix is tolerated; anything else after the
    title still fails.
    """
    if actual is None:
        return False
    if actual == expected:
        return True
    return actual.startswith(expected) and bool(
        _DATED_TITLE.fullmatch(actual[len(expected) :])
    )


def _word_count(text: str) -> int:
    return len(re.findall(r"\S+", text))


def check_contract(text: str, contract: dict[str, Any]) -> dict[str, Any]:
    """Return ``{passed, violations, words, sections}`` without changing text."""
    text = str(text or "")
    markers = tuple(contract.get("silence_markers") or _DEFAULT_SILENCE_MARKERS)
    nonempty = [line.strip() for line in text.splitlines() if line.strip()]
    if len(nonempty) == 1 and nonempty[0] in markers:
        return {
            "passed": True,
            "violations": [],
            "words": 0,
            "sections": [],
            "silent": True,
        }

    violations: list[str] = []
    expected_title = str(contract.get("title") or "").strip()
    if expected_title and not _title_matches(_title(text), expected_title):
        violations.append(f"title must be {expected_title!r}")
    # The title is the only top-level heading a contract allows; a second H1
    # would let a rewrite smuggle an undeclared section past the H2 checks.
    top_level = [
        line for line in _contract_lines(text) if re.match(r"^ {0,3}#\s+\S", line)
    ]
    if len(top_level) > 1:
        violations.append("contains an extra top-level heading")

    required_lines = contract.get("required_lines") or []
    for required in required_lines:
        if not any(
            line.strip().startswith(str(required)) for line in _contract_lines(text)
        ):
            violations.append(f"missing required line {required!r}")

    sections = _sections(text)
    required_sections = [
        str(s).strip() for s in (contract.get("required_sections") or [])
    ]
    optional_sections = [
        str(s).strip() for s in (contract.get("optional_sections") or [])
    ]
    allowed = required_sections + optional_sections
    if any(section not in allowed for section in sections):
        violations.append("contains an undeclared section")
    if len(sections) != len(set(sections)):
        violations.append("contains duplicate sections")
    ranks = [allowed.index(section) for section in sections if section in allowed]
    if ranks != sorted(ranks):
        violations.append("sections are out of order")
    if any(section not in sections for section in required_sections):
        violations.append("missing required section")

    words = _word_count(text)
    cap = contract.get("word_cap")
    if cap is not None and words > int(cap):
        violations.append(f"word cap exceeded ({words} > {int(cap)})")
    return {
        "passed": not violations,
        "violations": violations,
        "words": words,
        "sections": sections,
        "silent": False,
    }


def check_candidate(
    original: str, candidate: str, contract: dict[str, Any]
) -> dict[str, Any]:
    """Check the envelope and any opt-in immutable visible source lines."""
    checked = check_contract(candidate, contract)
    if "preserve_line_patterns" not in contract:
        return checked
    patterns = contract["preserve_line_patterns"]
    violation = None
    if not isinstance(patterns, list) or not patterns or any(
        not isinstance(pattern, str) or not pattern.strip() for pattern in patterns
    ):
        violation = "preserve_line_patterns must be a non-empty list of non-empty regex strings"
    else:
        try:
            compiled = [re.compile(pattern) for pattern in patterns]
        except re.error:
            violation = "preserve_line_patterns contains an invalid regex"
        else:
            def protected_lines(text):
                return [
                    line for line in _contract_lines(text)
                    if any(pattern.search(line) for pattern in compiled)
                ]

            source = protected_lines(original)
            if not source:
                violation = "preserve_line_patterns matches no original report lines"
            elif source != protected_lines(candidate):
                violation = "candidate changes protected report lines or their order"
    if violation:
        checked["violations"].append(violation)
        checked["passed"] = False
    return checked


def parse_decision(reply: str) -> tuple[str, str]:
    """Parse the strict invisible-review protocol."""
    lines = str(reply or "").strip().splitlines()
    if lines == ["PRESENT"]:
        return "PRESENT", ""
    if lines and lines[0] == "FIX" and len(lines) > 1 and "\n".join(lines[1:]).strip():
        return "FIX", "\n".join(lines[1:]).strip()
    if len(lines) == 2 and lines[0] == "RERUN" and lines[1].strip():
        return "RERUN", lines[1].strip()
    raise ValueError(
        "review reply must be exactly PRESENT, FIX + report, or RERUN + one-line reason"
    )


def append_receipt(record: dict[str, Any]) -> None:
    from hermes_cli.auth import _file_lock

    payload = dict(record)
    payload.setdefault("recorded_at", time.time())
    path = receipt_path()
    line = (json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n").encode(
        "utf-8"
    )
    with (
        _receipt_lock,
        _file_lock(
            path.with_suffix(".lock"),
            _receipt_lock_holder,
            10,
            "Timed out waiting for cron review receipts",
        ),
    ):
        if path.exists() and path.stat().st_size + len(line) > MAX_RECEIPT_BYTES:
            path.replace(path.with_name(path.name + ".1"))
        with path.open("ab") as stream:
            stream.write(line)


def normalize_review(value: Any) -> Optional[dict[str, Any]]:
    """Normalize the opt-in reviewed-delivery declaration."""
    if value in (None, ""):
        return None
    if not isinstance(value, dict):
        raise ValueError("review must be an object or null")
    contract = str(value.get("contract") or "").strip()
    subject = str(value.get("subject") or "").strip()
    if not contract or not subject:
        raise ValueError("review.contract and review.subject are required")
    try:
        deadline = float(value.get("deadline_minutes", 10))
        rerun_deadline = float(value.get("rerun_deadline_minutes", 20))
    except (TypeError, ValueError, OverflowError):
        raise ValueError("review deadlines must be positive finite minutes") from None
    if any(not math.isfinite(n) or n <= 0 for n in (deadline, rerun_deadline)):
        raise ValueError("review deadlines must be positive finite minutes")
    max_reruns = value.get("max_reruns", 1)
    if type(max_reruns) is not int or max_reruns not in (0, 1):
        raise ValueError("review.max_reruns must be 0 or 1")
    return {
        "contract": contract,
        "subject": subject,
        "deadline_minutes": deadline,
        "rerun_deadline_minutes": rerun_deadline,
        "max_reruns": max_reruns,
    }


def validate_review_delivery(review, deliver):
    if review and str(deliver).strip().lower() != "local":
        raise ValueError("review requires deliver=local")


def apply_review_update(updated: dict, updates: dict) -> None:
    """A reviewed report stays on the local lane through every edit."""
    if "review" in updates:
        updated["output_local_only"] = bool(updated.get("review"))
    if {"review", "deliver"}.intersection(updates):
        validate_review_delivery(updated.get("review"), updated.get("deliver", "local"))
