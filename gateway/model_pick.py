"""Opt-in wall-clock deadlines for gateway model picks."""

from __future__ import annotations

import math
from typing import Any


def positive_finite_number(value: Any) -> float:
    """Invalid or disabled durations/deadlines do not enable the feature."""
    if isinstance(value, bool):
        return 0.0
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return number if math.isfinite(number) and number > 0 else 0.0


def model_pick_ttl_hours(config: dict) -> float:
    model = config.get("model")
    hours = positive_finite_number(model.get("pick_ttl_hours")) if isinstance(model, dict) else 0.0
    return hours if math.isfinite(hours * 3600) else 0.0


def model_pick_deadline(override: dict | None) -> float:
    return positive_finite_number((override or {}).get("expires_at"))
