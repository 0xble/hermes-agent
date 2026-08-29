"""Deterministic, opt-in adaptive reasoning policy.

The policy is local and side-effect free until ``begin_adaptive_reasoning_turn``
installs a task-local override. It never invokes a model, mutates the prompt,
or changes the session baseline.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from agent.reasoning_context import (
    get_turn_reasoning_source,
    set_turn_reasoning_config,
)
from hermes_constants import VALID_REASONING_EFFORTS

POLICY_VERSION = "adaptive-reasoning-v1"
NOTICE_KEY = "adaptive-reasoning"

_EFFORT_RANK = {
    effort: rank for rank, effort in enumerate(VALID_REASONING_EFFORTS)
}
_ADAPTIVE_LEVELS = {"low", "medium", "high", "xhigh"}

_SIGNAL_PATTERNS: tuple[tuple[str, int, re.Pattern[str]], ...] = (
    (
        "debugging",
        3,
        re.compile(
            r"\bdebug|\bdiagnos|root[\s-]?cause|race\s+condition|deadlock"
            r"|segfault|memory\s+leak|\bcrash(?:ed|es|ing)?\b|\bregression\b",
            re.IGNORECASE,
        ),
    ),
    (
        "error-evidence",
        2,
        re.compile(
            r"\berror:|traceback \(most recent call last\)|\bexception\b"
            r"|stack\s*trace|connection\s+refused|exit\s+code\s+\d+",
            re.IGNORECASE,
        ),
    ),
    (
        "implementation",
        3,
        re.compile(
            r"\bimplement|\brefactor|\brewrite|\bmigrat(?:e|ion|ing)\b"
            r"|\bintegrat(?:e|ion|ing)\b|build\s+(?:a|an|the)\b",
            re.IGNORECASE,
        ),
    ),
    (
        "architecture",
        3,
        re.compile(r"\barchitect(?:ure|ural|ing)?\b|system\s+design", re.IGNORECASE),
    ),
    (
        "security",
        3,
        re.compile(
            r"security\s+(?:audit|review)|vulnerabilit|threat\s+model"
            r"|authentication\s+bypass|\bexploit\b",
            re.IGNORECASE,
        ),
    ),
    (
        "high-stakes",
        3,
        re.compile(
            r"data\s+(?:loss|corruption)|\boutage\b|production\s+(?:down|outage)"
            r"|disaster\s+recovery|irreversibl",
            re.IGNORECASE,
        ),
    ),
    (
        "research",
        3,
        re.compile(
            r"\bresearch\b|\binvestigat(?:e|ion)\b|literature\s+review"
            r"|comprehensive\s+(?:analysis|review)",
            re.IGNORECASE,
        ),
    ),
    (
        "cross-component",
        2,
        re.compile(r"cross[\s-]?component|multi[\s-]?system", re.IGNORECASE),
    ),
    (
        "production",
        1,
        re.compile(r"\bproduction\b|\bprod\b|live\s+system", re.IGNORECASE),
    ),
)

_SIMPLE_RE = re.compile(
    r"(?:hi|hello|hey|thanks?|thank\s+you|ok(?:ay)?|cool|nice|great|yes|no|ping)"
    r"[\s.!?]*",
    re.IGNORECASE,
)
_MULTI_STEP_RE = re.compile(r"^\s*(?:\d+[.)]|[-*])\s+\S", re.MULTILINE)
_XHIGH_CODES = {"architecture", "security", "high-stakes"}


@dataclass(frozen=True)
class AdaptiveReasoningDecision:
    """Structured evidence for one policy decision."""

    selected_effort: str
    score: int
    reason_codes: tuple[str, ...]
    policy_version: str = POLICY_VERSION
    applied: bool = False
    reason_label: str = ""


def parse_adaptive_reasoning_config(raw: Any) -> dict[str, Any] | None:
    """Validate the opt-in policy while keeping downshift disabled by default."""
    if not isinstance(raw, dict) or raw.get("enabled") is not True:
        return None

    max_effort = str(raw.get("max_effort") or "high").strip().lower()
    if max_effort not in _ADAPTIVE_LEVELS:
        max_effort = "high"

    parsed: dict[str, Any] = {"enabled": True, "max_effort": max_effort}
    min_effort = str(raw.get("min_effort") or "").strip().lower()
    if min_effort in _ADAPTIVE_LEVELS and (
        _EFFORT_RANK[min_effort] <= _EFFORT_RANK[max_effort]
    ):
        parsed["min_effort"] = min_effort
    return parsed


def _reason_label(reason_codes: tuple[str, ...]) -> str:
    codes = set(reason_codes)
    if "debugging" in codes:
        return "complex debugging task"
    if "architecture" in codes and "security" in codes:
        return "architecture and security work"
    if "architecture" in codes:
        return "architecture work"
    if "security" in codes:
        return "security review"
    if "high-stakes" in codes:
        return "high-stakes diagnosis"
    if "implementation" in codes:
        return "multi-step implementation task"
    if "research" in codes:
        return "in-depth research task"
    return "complex multi-step task"


def select_adaptive_reasoning(
    text: str,
    baseline_effort: str,
    config: dict[str, Any] | None,
) -> AdaptiveReasoningDecision:
    """Select an effort using bounded deterministic rules.

    Uncertain input abstains to ``baseline_effort``. Downshift requires both
    positive simplicity evidence and an explicit lower ``min_effort``.
    """
    baseline = str(baseline_effort or "medium").strip().lower()
    if baseline not in _EFFORT_RANK:
        baseline = "medium"
    cfg = parse_adaptive_reasoning_config(config)
    if cfg is None:
        return AdaptiveReasoningDecision(
            baseline, 0, ("disabled",), applied=False
        )

    stripped = (text or "").strip()
    scan = stripped[:8000]
    score = 0
    codes: list[str] = []
    for code, weight, pattern in _SIGNAL_PATTERNS:
        if pattern.search(scan):
            score += weight
            codes.append(code)

    steps = len(_MULTI_STEP_RE.findall(scan))
    if steps >= 3:
        score += 2
        codes.append("multi-step")
    if "```" in scan:
        score += 1
        codes.append("code")
    if len(stripped) > 800:
        score += 1
        codes.append("detailed")

    simple = bool(_SIMPLE_RE.fullmatch(stripped))
    if simple:
        codes.append("simple")

    candidate: str | None = None
    if score >= 6 and _XHIGH_CODES.intersection(codes):
        candidate = "xhigh"
    elif score >= 3:
        candidate = "high"
    elif simple:
        candidate = "low"

    if candidate is None:
        return AdaptiveReasoningDecision(
            baseline, score, ("abstain",), applied=False
        )
    assert candidate is not None

    # A simple-task classification is only permission to downshift. Without an
    # explicit floor, escalation-only mode abstains even when the baseline is
    # below the policy's ordinary "low" simple-task level.
    if simple and "min_effort" not in cfg:
        return AdaptiveReasoningDecision(
            baseline,
            score,
            tuple(codes + ["escalation-only"]),
            applied=False,
        )

    ceiling = cfg["max_effort"]
    if _EFFORT_RANK[candidate] > _EFFORT_RANK[ceiling]:
        candidate = ceiling
        codes.append("max-clamp")

    floor = str(cfg.get("min_effort", baseline))
    if _EFFORT_RANK[floor] > _EFFORT_RANK[baseline]:
        floor = baseline
    if _EFFORT_RANK[candidate] < _EFFORT_RANK[floor]:
        candidate = floor
        codes.append("floor-clamp")

    applied = candidate != baseline
    return AdaptiveReasoningDecision(
        selected_effort=candidate,
        score=score,
        reason_codes=tuple(codes),
        applied=applied,
        reason_label=_reason_label(tuple(codes)) if applied else "",
    )


def _extract_text(user_message: Any) -> str:
    if isinstance(user_message, str):
        return user_message
    if isinstance(user_message, list):
        return "\n".join(
            block.get("text", "")
            for block in user_message
            if isinstance(block, dict) and isinstance(block.get("text"), str)
        )
    if isinstance(user_message, dict) and isinstance(user_message.get("text"), str):
        return user_message["text"]
    return ""


def _emit_notice(agent: Any, decision: AdaptiveReasoningDecision, baseline: str) -> None:
    if getattr(agent, "platform", "") == "subagent":
        return
    callback = getattr(agent, "notice_callback", None)
    if not callable(callback):
        return
    from agent.credits_tracker import AgentNotice

    before = baseline.capitalize()
    after = decision.selected_effort.capitalize()
    callback(
        AgentNotice(
            text=(
                f"🧠 Adaptive reasoning: {before} → {after}"
                f" · {decision.reason_label}"
            ),
            level="info",
            kind="ttl",
            ttl_ms=12000,
            key=NOTICE_KEY,
        )
    )


def begin_adaptive_reasoning_turn(
    agent: Any,
    user_message: Any,
    *,
    moa_config: dict[str, Any] | None = None,
) -> AdaptiveReasoningDecision | None:
    """Apply the policy to the active task-local reasoning scope, if eligible."""
    cfg = getattr(agent, "adaptive_reasoning", None)
    if not isinstance(cfg, dict) or not cfg.get("enabled") or moa_config:
        return None

    reasoning = getattr(agent, "reasoning_config", None)
    baseline = (
        str(reasoning.get("effort") or "medium").strip().lower()
        if isinstance(reasoning, dict) and reasoning.get("enabled") is not False
        else ""
    )
    if baseline not in _EFFORT_RANK:
        return AdaptiveReasoningDecision(
            "medium", 0, ("abstain",), applied=False
        )

    if (
        getattr(agent, "reasoning_user_override", False)
        or get_turn_reasoning_source(agent) == "explicit"
    ):
        return AdaptiveReasoningDecision(
            baseline, 0, ("explicit-override",), applied=False
        )

    decision = select_adaptive_reasoning(_extract_text(user_message), baseline, cfg)
    if not decision.applied:
        return decision

    installed = set_turn_reasoning_config(
        agent,
        {"enabled": True, "effort": decision.selected_effort},
        source="adaptive",
    )
    if not installed:
        return AdaptiveReasoningDecision(
            baseline,
            decision.score,
            ("precedence-abstain",),
            applied=False,
        )
    _emit_notice(agent, decision, baseline)
    return decision
