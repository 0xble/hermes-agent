"""Pure attended-model routing and inbound activity helpers.

This module deliberately has no gateway state or provider I/O.  The gateway resolves
credentials and applies the returned route; keeping the precedence here makes the
idle/temporary/pinned rules testable without constructing a GatewayRunner.
"""
from __future__ import annotations

import logging
import math
import re
import time
from dataclasses import dataclass
from typing import Any, Mapping, Optional

logger = logging.getLogger("gateway.attended_model")

_RELAY_HEADER_RE = re.compile(r"\[relay from=[^\s\]]+ receipt=[^\s\]]+(?: task=[^\s\]]+)?\]")

# Mirrors ~/.hermes/plugins/ask-tracking/capture.py.  Keep these local so the
# gateway remains usable without the optional plugin installed.
_AUTOMATION_PREFIXES = (
    "[System:", "[CONTEXT COMPACTION", "[Conversation Summary", "[PRIOR CONTEXT —",
    "[STILL IN PROGRESS —", "[GOAL WAKEUP", "[Goal wakeup", "[LOOP TICK", "[Loop tick",
    "[CRON", "[Cron", "[RELAY", "[Relay", "[Continuing toward your standing goal]",
    "[Process completed", "[Process failed", "[Background process", "[DELEGATION RESULT",
    "[ASYNC DELEGATION ", "[IMPORTANT:", "[System note", "[SYSTEM NOTE",
    "[INTERNAL NOTIFICATION", "[HERMES PROCESS NOTIFICATION", "[/loop", "[Loop wakeup",
    "Review the conversation above and consider saving to memory",
    "Review the conversation above and update the skill library",
)
_AUTOMATION_LINE_MARKERS = (
    "[relay from=", "[/HERMES PROCESS NOTIFICATION]", "[HERMES PROCESS NOTIFICATION",
    "[INTERNAL NOTIFICATION", "[System note:",
)
_AUTOMATION_KINDS = {
    "async_delegation", "async_delegation_complete", "automation", "background_review", "compaction",
    "context_summary", "cron", "delegation", "delegation_complete", "delegation_notification",
    "goal", "goal_wakeup", "internal", "internal_notification", "loop", "loop_tick", "notification",
    "process", "process_complete", "process_notification", "relay", "relay_delivery", "relay_fyi",
    "subagent", "system", "synthetic",
}


@dataclass(frozen=True)
class AttendedConfig:
    """Validated feature configuration. ``enabled`` is false when all new keys are unset."""

    enabled: bool = False
    model: str = ""
    provider: str = ""
    reasoning_effort: str = ""
    idle_revert_minutes: float = 15.0
    compact_before_switch_tokens: int = 80000


@dataclass(frozen=True)
class AttendedResolution:
    model: str
    provider: str
    reasoning_effort: str
    reason: str
    active: bool = False

    @property
    def decision_key(self) -> tuple[str, str, str, str, bool]:
        """Stable desired-route identity; never compare it with the live agent model.

        The gateway uses this key to distinguish a real attended decision change from
        fallback activation, which is deliberately allowed to change ``agent.model``
        without changing the desired route for the current turn.
        """
        return (self.model, self.provider, self.reasoning_effort, self.reason, self.active)


def _as_model_config(config: Mapping[str, Any] | None) -> Mapping[str, Any]:
    value = (config or {}).get("model", {}) if isinstance(config, Mapping) else {}
    return value if isinstance(value, Mapping) else {}


def _positive_number(raw: Any, *, default: float, key: str) -> float:
    try:
        if isinstance(raw, bool):
            raise ValueError
        value = float(raw)
        if not math.isfinite(value) or value <= 0:
            raise ValueError
        return value
    except (TypeError, ValueError, OverflowError):
        logger.warning("model.%s=%r must be a positive finite number; using %s", key, raw, default)
        return default


def _positive_int(raw: Any, *, default: int, key: str) -> int:
    try:
        if isinstance(raw, bool):
            raise ValueError
        value = int(raw)
        if value <= 0:
            raise ValueError
        return value
    except (TypeError, ValueError, OverflowError):
        logger.warning("model.%s=%r must be a positive integer; using %s", key, raw, default)
        return default


def parse_attended_config(config: Mapping[str, Any] | None) -> AttendedConfig:
    """Validate the opt-in ``model.attended`` block and its idle/compaction knobs."""
    model_cfg = _as_model_config(config)
    attended_raw = model_cfg.get("attended")
    enabled = any(key in model_cfg for key in ("attended", "idle_revert_minutes", "compact_before_switch_tokens"))
    if attended_raw is None and "attended" not in model_cfg:
        attended_raw = {}
    if not isinstance(attended_raw, Mapping):
        logger.warning("model.attended=%r must be a mapping; attended switching is disabled", attended_raw)
        attended_raw = {}
        enabled = False
    model = str(attended_raw.get("model") or "").strip()
    provider = str(attended_raw.get("provider") or "").strip()
    effort = str(attended_raw.get("reasoning_effort") or "").strip().lower()
    if effort and effort not in {"none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra"}:
        logger.warning("model.attended.reasoning_effort=%r is invalid; ignoring it", effort)
        effort = ""
    return AttendedConfig(
        enabled=enabled,
        model=model,
        provider=provider,
        reasoning_effort=effort,
        idle_revert_minutes=_positive_number(
            model_cfg.get("idle_revert_minutes", 15), default=15.0, key="idle_revert_minutes"
        ),
        compact_before_switch_tokens=_positive_int(
            model_cfg.get("compact_before_switch_tokens", 80000), default=80000,
            key="compact_before_switch_tokens",
        ),
    )


def _stamp_seconds(value: Any) -> Optional[float]:
    if value is None:
        return None
    if hasattr(value, "timestamp"):
        try:
            return float(value.timestamp())
        except Exception:
            return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def is_active(last_user_activity_at: Any, *, now: Optional[float], idle_revert_minutes: float) -> bool:
    stamp = _stamp_seconds(last_user_activity_at)
    if stamp is None:
        return False
    clock = float(time.time() if now is None else now)
    return 0 <= clock - stamp < idle_revert_minutes * 60.0


def resolve_attended_model(
    session_state: Mapping[str, Any] | Any,
    now: Optional[float],
    config: Mapping[str, Any] | None,
) -> AttendedResolution:
    """Resolve route precedence without provider I/O.

    Accepted state keys/attributes are ``once_model``, ``session_model``,
    ``temporary_model``, ``session_reasoning``, ``temporary_reasoning``,
    ``last_user_activity_at``, ``default_model`` and ``default_provider``.
    ``channel_model`` is the chat's default model.  Explicit picks win over
    attended; attended wins over channel/default while active.
    """
    cfg = parse_attended_config(config)
    get = session_state.get if isinstance(session_state, Mapping) else lambda key, default=None: getattr(session_state, key, default)
    default_model = str(get("default_model", "") or "")
    default_provider = str(get("default_provider", "") or "")
    channel_model = str(get("channel_model", "") or "")
    channel_provider = str(get("channel_provider", "") or "")
    default_reasoning = str(get("default_reasoning", "") or "")
    now_value = time.time() if now is None else float(now)
    active = cfg.enabled and is_active(
        get("last_user_activity_at"), now=now_value, idle_revert_minutes=cfg.idle_revert_minutes
    )

    explicit_model = (
        get("once_model") or get("session_model") or
        get("temporary_model") or get("model_override")
    )
    explicit_provider = (
        get("once_provider") or get("session_provider") or
        get("temporary_provider") or get("model_override_provider") or ""
    )
    explicit_reasoning = (
        get("once_reasoning") or get("session_reasoning") or
        get("temporary_reasoning") or get("reasoning_override")
    )
    # A plain /model X is an explicit model pick, but it must use X's normal
    # reasoning setting rather than inheriting attended's effort.
    explicit_model_reasoning = str(
        get("explicit_model_reasoning") or get("model_reasoning") or default_reasoning or ""
    )
    baseline_provider = channel_provider or default_provider
    if explicit_model:
        return AttendedResolution(
            str(explicit_model), str(explicit_provider or baseline_provider),
            str(explicit_reasoning or explicit_model_reasoning), "explicit", active,
        )

    if active:
        model = cfg.model or channel_model or default_model
        provider = cfg.provider or channel_provider or default_provider
        effort = str(explicit_reasoning or cfg.reasoning_effort or get("attended_reasoning") or default_reasoning)
        return AttendedResolution(model, provider, effort, "attended", True)

    return AttendedResolution(
        channel_model or default_model, baseline_provider,
        str(explicit_reasoning or default_reasoning), "default", False,
    )


def is_genuine_user_message(event: Any) -> bool:
    """Return true only for a human message that should reset attended idle time."""
    if event is None or bool(getattr(event, "internal", False)):
        return False
    metadata = getattr(event, "metadata", None) or {}
    if not isinstance(metadata, Mapping):
        metadata = {}
    if any(bool(metadata.get(key)) for key in ("reaction", "is_reaction", "edited", "is_edit", "plain_edit")):
        return False
    kind_values = {
        str(metadata.get(key) or "").strip().lower().replace("-", "_")
        for key in ("display_kind", "event_type", "kind", "notification_type", "origin", "origin_type", "type")
    }
    if kind_values & _AUTOMATION_KINDS:
        return False
    text = str(getattr(event, "text", "") or "")
    if _RELAY_HEADER_RE.search(text):
        return False
    stripped = text.lstrip()
    if stripped.startswith(_AUTOMATION_PREFIXES):
        return False
    if any(line.lstrip().startswith(_AUTOMATION_LINE_MARKERS) for line in text.splitlines()):
        return False
    platform = getattr(getattr(event, "source", None), "platform", "")
    if str(getattr(platform, "value", platform) or "").lower() in {"automation", "cron", "goal_wakeup", "kanban", "subagent", "system"}:
        return False
    return True


def expiry_time(last_user_activity_at: Any, idle_revert_minutes: float) -> Optional[float]:
    stamp = _stamp_seconds(last_user_activity_at)
    return None if stamp is None else stamp + idle_revert_minutes * 60.0
