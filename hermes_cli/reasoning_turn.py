"""Shared parsing for one-turn reasoning commands."""

from dataclasses import dataclass

from hermes_constants import parse_reasoning_effort


class ReasoningTurnError(ValueError):
    """A recognized one-turn reasoning command is invalid."""


@dataclass(frozen=True)
class ReasoningTurnRequest:
    """A prompt plus an explicit effort that applies to only that prompt's turn."""

    effort: str
    reasoning_config: dict
    prompt: str
    notice: str


_EFFORT_LABELS = {
    "minimal": "Minimal",
    "low": "Low",
    "medium": "Medium",
    "high": "High",
    "xhigh": "XHigh",
    "max": "Max",
    "ultra": "Ultra",
}


def parse_reasoning_turn(raw_args: str) -> ReasoningTurnRequest | None:
    """Parse ``<level> <prompt>`` after any quick-command expansion."""
    normalized = str(raw_args or "").strip()
    if not normalized:
        return None

    parts = normalized.split(None, 1)
    if len(parts) == 1:
        return None

    raw_effort, prompt = parts[0].lower(), parts[1].strip()
    if not prompt:
        return None

    parsed = parse_reasoning_effort(raw_effort)
    if parsed is None:
        return None
    if parsed.get("enabled") is False:
        raise ReasoningTurnError(
            "One-turn reasoning cannot be disabled; use an enabled effort level."
        )

    effort = str(parsed.get("effort") or "").lower()
    if not effort:
        return None
    if "--global" in prompt.split():
        raise ReasoningTurnError(
            "--global cannot be combined with a one-turn reasoning prompt."
        )

    label = _EFFORT_LABELS.get(effort, effort.title())
    return ReasoningTurnRequest(
        effort=effort,
        reasoning_config={"enabled": True, "effort": effort},
        prompt=prompt,
        notice=f"🧠 Reasoning: {label} for this turn.",
    )
