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

from agent.i18n import t

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


_KNOWN_MODEL_FAMILIES = {"claude", "gemini", "gpt", "llama", "mistral", "qwen", "deepseek", "glm"}
_CLAUDE_DISPLAY_RE = re.compile(
    r"^claude-(?:(?P<version>\d+(?:-\d+)?)-)?(?P<family>opus|sonnet|haiku)"
    r"(?:-(?P<family_version>\d{1,2}(?:-\d{1,2})?))?(?:-(?P<date>\d{8}))?$",
    re.IGNORECASE,
)


def friendly_model_name(model: str) -> str:
    """Return a short card label for a model id, preserving unknown ids verbatim."""
    raw = str(model or "").strip()
    if not raw:
        return raw
    candidate = raw.rsplit("/", 1)[-1]
    if ":" in candidate:
        prefix, suffix = candidate.split(":", 1)
        if prefix.lower() in _KNOWN_MODEL_FAMILIES or prefix.lower().startswith("custom"):
            candidate = suffix
    lower = candidate.lower()
    if lower.startswith("claude-"):
        match = _CLAUDE_DISPLAY_RE.fullmatch(candidate)
        if match:
            version = match.group("version") or ""
            family = match.group("family").capitalize()
            family_version = match.group("family_version") or ""
            if family_version:
                family_version = family_version.replace("-", ".")
                family = f"{family} {family_version}"
            if version:
                version = version.replace("-", ".")
                return f"{version} {family}"
            return family
    if lower.startswith("gpt-"):
        parts = candidate.split("-")[1:]
        if parts:
            first = f"GPT-{parts.pop(0)}"
            return " ".join([first, *(part.capitalize() for part in parts)])
    if lower.startswith("gemini-"):
        return "Gemini " + " ".join(part.capitalize() for part in candidate.split("-")[1:])
    if lower in {"opus", "sonnet", "haiku"}:
        return candidate.capitalize()
    return raw


def format_attended_switch_notice(
    model: str, effort: Any = "", *, idle_model: str = "", idle_minutes: float = 0,
) -> str:
    """Render the one-shot attended switch-in status line.

    One sentence-style line like Hermes' other automatic-change notices (for example the /fast
    expiry). It names the model the topic returns to, because switching back is silent. ``effort``
    is accepted for callers but not shown.
    """
    del effort
    if idle_model and idle_model != model and idle_minutes > 0:
        minutes = int(idle_minutes) if float(idle_minutes).is_integer() else idle_minutes
        return t(
            "gateway.model.attended_switched_until_idle",
            model=friendly_model_name(model),
            idle_model=friendly_model_name(idle_model),
            minutes=minutes,
        )
    return t("gateway.model.attended_switched", model=friendly_model_name(model))


def format_attended_switch_back_notice(idle_model: str, idle_minutes: float) -> str:
    """Render the one-shot notice emitted when an attended route expires."""
    minutes = int(idle_minutes) if float(idle_minutes).is_integer() else idle_minutes
    return t(
        "gateway.model.attended_switched_back_idle",
        idle_model=friendly_model_name(idle_model),
        minutes=minutes,
    )


@dataclass(frozen=True)
class AttendedConfig:
    """Validated feature configuration. ``enabled`` is false when all new keys are unset."""

    enabled: bool = False
    model: str = ""
    provider: str = ""
    reasoning_effort: str | dict[str, Any] = ""
    idle_revert_minutes: float = 15.0
    compact_before_switch_tokens: int = 80000
    start_new_sessions: bool = True


@dataclass(frozen=True)
class AttendedResolution:
    model: str
    provider: str
    reasoning_effort: str | dict[str, Any]
    reason: str
    active: bool = False
    idle_model: str = ""  # route used once the attended window lapses; display only
    new_session: bool = False  # fresh sessions may start attended without a switch-in card
    switched_back: bool = False  # this resolution crossed from attended to the idle route
    role: Optional[str] = None

    @property
    def decision_key(self) -> tuple[str, str, str | dict[str, Any], str, bool]:
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
    """Validate the opt-in ``model.attended`` block and its idle knob.

    ``compact_before_switch_tokens`` remains parsed for backward compatibility, but
    route changes never trigger a dedicated compaction pass.
    """
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
    from hermes_cli.model_presets import normalize_route_reasoning_effort
    effort = normalize_route_reasoning_effort(attended_raw.get("reasoning_effort"))
    # The attended route is live too. Resolve @attended/model_preset at request time
    # from the raw config rather than freezing a concrete model in SessionStore.
    from hermes_cli.model_presets import preset_ref_name
    if preset_ref_name(model) or attended_raw.get("model_preset"):
        try:
            from hermes_cli.model_preset_runtime import resolve_model_ref
            route = resolve_model_ref(model or {"model_preset": attended_raw.get("model_preset")}, config or {})
            model = route.model
            provider = provider or route.provider
            if effort == "":
                effort = route.reasoning_effort
        except Exception as exc:
            logger.warning("Invalid attended model reference: %s", exc)
            model = ""
    if attended_raw.get("reasoning_effort") not in (None, "") and not effort:
        logger.warning("model.attended.reasoning_effort=%r is invalid; ignoring it", attended_raw["reasoning_effort"])
    start_new_sessions = attended_raw.get("start_new_sessions", True)
    if not isinstance(start_new_sessions, bool):
        logger.warning("model.attended.start_new_sessions=%r must be a boolean; using true", start_new_sessions)
        start_new_sessions = True
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
        start_new_sessions=start_new_sessions,
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


def pin_is_active(pinned: bool, pin_until: Optional[float], *, now: float) -> bool:
    return pinned and (pin_until is None or now < pin_until)


def is_active(last_user_activity_at: Any, *, now: Optional[float], idle_revert_minutes: float,
              pinned: bool = False, pin_until: Optional[float] = None) -> bool:
    clock = float(time.time() if now is None else now)
    if pin_is_active(pinned, pin_until, now=clock):
        return True
    deadline = expiry_time(last_user_activity_at, idle_revert_minutes, pinned=pinned, pin_until=pin_until)
    return deadline is not None and 0 < deadline - clock <= idle_revert_minutes * 60.0


def _effort_label(value: Any) -> str | dict[str, Any]:
    """Keep ordinary effort labels compatible without dropping typed provider-specific policy."""
    from hermes_cli.model_presets import normalize_route_reasoning_effort
    from hermes_constants import parse_reasoning_effort

    effort = normalize_route_reasoning_effort(value)
    if isinstance(effort, dict):
        # Standard levels can round-trip through a label. Bespoke enabled levels cannot,
        # so keep their dictionary for the same parser at the request boundary.
        label = effort["effort"]
        return label if parse_reasoning_effort(label) == effort else effort
    return effort


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
    default_reasoning = _effort_label(get("default_reasoning"))
    now_value = time.time() if now is None else float(now)
    new_session = bool(get("new_session", False)) and cfg.start_new_sessions
    model_role = str(get("model_role", "") or "").strip()
    role_route = None
    if model_role:
        from hermes_cli.model_preset_runtime import resolve_model_ref
        try:
            role_route = resolve_model_ref("@" + model_role, config=config or {})
            if not role_route.model:
                raise ValueError("role has no configured model")
        except (ValueError, TypeError) as exc:
            logger.warning("model_role @%s fallback=default: %s", model_role, exc)
            role_route = None
    # An invalid hold has an explicit default fallback, not automatic attended routing.
    active = not (model_role and role_route is None) and cfg.enabled and (new_session or is_active(
        get("last_user_activity_at"), now=now_value, idle_revert_minutes=cfg.idle_revert_minutes, pinned=get("attended_pinned") is True, pin_until=get("attended_pin_until")
    ))

    explicit_model = (
        get("once_model") or get("session_model") or
        get("temporary_model") or get("model_override")
    )
    explicit_provider = (
        get("once_provider") or get("session_provider") or
        get("temporary_provider") or get("model_override_provider") or ""
    )
    explicit_reasoning = _effort_label(
        get("once_reasoning") or get("session_reasoning") or
        get("temporary_reasoning") or get("reasoning_override")
    )
    # A plain /model X is an explicit model pick, but it must use X's normal
    # reasoning setting rather than inheriting attended's effort.
    explicit_model_reasoning = _effort_label(
        get("explicit_model_reasoning") or get("model_reasoning") or default_reasoning
    )
    baseline_provider = channel_provider or default_provider
    if explicit_model:
        return AttendedResolution(
            str(explicit_model), str(explicit_provider or baseline_provider),
            explicit_reasoning or explicit_model_reasoning, "explicit", active,
        )

    if role_route is not None:
        return AttendedResolution(
            role_route.model, role_route.provider or baseline_provider,
            _effort_label(explicit_reasoning or role_route.reasoning_effort or default_reasoning),
            "role", True, idle_model=channel_model or default_model,
            new_session=new_session, role=model_role,
        )

    if active:
        model = cfg.model or channel_model or default_model
        provider = cfg.provider or channel_provider or default_provider
        effort = (
            explicit_reasoning or cfg.reasoning_effort
            or _effort_label(get("attended_reasoning")) or default_reasoning
        )
        return AttendedResolution(
            model, provider, effort, "attended", True, idle_model="" if pin_is_active(get("attended_pinned") is True, get("attended_pin_until"), now=now_value) else channel_model or default_model,
            new_session=new_session,
        )

    return AttendedResolution(
        channel_model or default_model, baseline_provider,
        _effort_label(explicit_reasoning or default_reasoning), "default", False
    )


def is_genuine_user_message(event: Any) -> bool:
    """Return true only for a human message that should reset attended idle time."""
    if event is None or bool(getattr(event, "internal", False)):
        return False
    if bool(getattr(getattr(event, "source", None), "is_bot", False)):
        return False
    # Recovery is not arrival. A legacy spool's ordering/flush time is not a receipt.
    if (getattr(event, "_hermes_recovered_followup", False)
            and getattr(event, "_attended_activity_received_at", None) is None):
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
    # /attend owns idle timing. Inspections and rejected durations must not extend it.
    get_command = getattr(event, "get_command", None)
    if callable(get_command) and get_command() == "attend":
        return False
    if stripped.startswith(_AUTOMATION_PREFIXES):
        return False
    if any(line.lstrip().startswith(_AUTOMATION_LINE_MARKERS) for line in text.splitlines()):
        return False
    platform = getattr(getattr(event, "source", None), "platform", "")
    if str(getattr(platform, "value", platform) or "").lower() in {"automation", "cron", "goal_wakeup", "kanban", "subagent", "system"}:
        return False
    return True


def attended_request_context_too_large(agent, loop_state, *, model, runtime) -> bool:
    """Return whether the current request is already at the default route's compaction trigger."""
    compressor = getattr(agent, "context_compressor", None)
    if compressor is None or loop_state is None:
        return False
    try:
        request_tokens = getattr(loop_state, "request_pressure_tokens", None)
        if not isinstance(request_tokens, int) or request_tokens <= 0:
            from agent.model_metadata import estimate_request_tokens_rough
            request_tokens = int(estimate_request_tokens_rough(
                getattr(loop_state, "messages", []) or [],
                system_prompt=getattr(loop_state, "active_system_prompt", "") or "",
                tools=getattr(agent, "tools", None) or None,
            ))
        context_length = int(runtime.get("context_length", 0) or 0)
        if context_length <= 0:
            from agent.model_metadata import get_model_context_length
            context_length = int(get_model_context_length(
                model, base_url=runtime.get("base_url", "") or "",
                api_key=runtime.get("api_key", "") or "",
                provider=runtime.get("provider", "") or "",
                api_mode=runtime.get("api_mode", "") or "",
            ))
        preview = getattr(compressor, "preview_threshold_tokens", None)
        if callable(preview):
            threshold = int(preview(model, context_length, runtime.get("provider", "") or ""))
        else:
            threshold = int(getattr(compressor, "threshold_tokens", 0) or 0)
        return threshold > 0 and request_tokens >= threshold
    except Exception:
        logger.debug("attended destination context threshold lookup failed", exc_info=True)
        return False


def expiry_time(last_user_activity_at: Any, idle_revert_minutes: float, *, pinned: bool = False,
                pin_until: Optional[float] = None) -> Optional[float]:
    if pinned and pin_until is None:
        return None
    stamp = _stamp_seconds(last_user_activity_at)
    if pinned and pin_until is not None:
        # Expiry acts like /attend off at the deadline, not at the next read.
        stamp = max(stamp, pin_until) if stamp is not None else pin_until
    return None if stamp is None else stamp + idle_revert_minutes * 60.0
