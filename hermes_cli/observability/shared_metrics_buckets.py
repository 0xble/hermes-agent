"""Bounded numeric buckets used by the shared-metrics product contract."""

from __future__ import annotations

from typing import Any


def long_size_bucket(count: int) -> str:
    from hermes_cli.observability.shared_metrics_contract import _LONG_SIZE_THRESHOLDS, _bucket
    return _bucket(max(0, int(count)), _LONG_SIZE_THRESHOLDS, "gte_1001")


def update_duration_bucket(duration_ms: Any) -> str:
    """Bucket an update (or update stage) wall time; non-numbers count as instant."""
    from hermes_cli.observability.shared_metrics_contract import (
        _UPDATE_DURATION_THRESHOLDS, _bucket, _non_negative_number,
    )
    value = _non_negative_number(duration_ms) or 0
    return _bucket(value, _UPDATE_DURATION_THRESHOLDS, "gte_15m")


def version_age_bucket(age_ms: Any) -> str:
    """Bucket the age of the version an update started from; unknown when not measurable."""
    from hermes_cli.observability.shared_metrics_contract import (
        _VERSION_AGE_THRESHOLDS, _bucket, _non_negative_number,
    )
    value = _non_negative_number(age_ms)
    return "unknown" if value is None else _bucket(value, _VERSION_AGE_THRESHOLDS, "gte_90d")


def desktop_active_minutes_bucket(active_ms: Any) -> str:
    """Bucket a day's active time in one Desktop mode; nothing measurable reads 0."""
    from hermes_cli.observability.shared_metrics_contract import (
        _DESKTOP_ACTIVE_THRESHOLDS, _bucket, _non_negative_number,
    )
    value = _non_negative_number(active_ms) or 0
    return "0" if value <= 0 else _bucket(value, _DESKTOP_ACTIVE_THRESHOLDS, "gte_6h")


def _bucket(value: float, thresholds: tuple[tuple[float, str], ...], last: str) -> str:
    return next((label for upper, label in thresholds if value < upper), last)


def duration_bucket(duration_ms: int) -> str:
    """Bucket a non-negative task duration into a fixed low-cardinality range."""
    from hermes_cli.observability.shared_metrics_contract import _DURATION_THRESHOLDS, _bucket
    return _bucket(max(0, int(duration_ms)), _DURATION_THRESHOLDS, "gte_10m")


def count_bucket(count: int) -> str:
    """Bucket a non-negative per-task count into a fixed range."""
    from hermes_cli.observability.shared_metrics_contract import _COUNT_THRESHOLDS, _bucket
    return _bucket(max(0, int(count)), _COUNT_THRESHOLDS, "gte_11")


def tool_latency_bucket(value: Any, *, fallback_duration_ms: int | None = None) -> str:
    """Bucket a tool duration reported in milliseconds."""
    from hermes_cli.observability.shared_metrics_contract import (
        _LATENCY_THRESHOLDS, _bucket, _non_negative_number,
    )
    duration_ms = _non_negative_number(value)
    if duration_ms is None:
        duration_ms = _non_negative_number(fallback_duration_ms)
    if duration_ms is None:
        return "unknown"
    return _bucket(duration_ms, _LATENCY_THRESHOLDS, "gte_30s")


def tool_retry_bucket(value: Any) -> str:
    """Bucket only explicit tool retries; missing relationships stay unknown."""
    from hermes_cli.observability.shared_metrics_contract import count_bucket
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return "unknown"
    return count_bucket(value)


def size_bucket(count: int) -> str:
    """Bucket a non-negative install-scale count."""
    from hermes_cli.observability.shared_metrics_contract import _SIZE_THRESHOLDS, _bucket
    return _bucket(max(0, int(count)), _SIZE_THRESHOLDS, "gte_251")
