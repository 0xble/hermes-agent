"""Topical helpers extracted from hermes_cli/observability/shared_metrics_contract.py."""
from __future__ import annotations
from typing import Any

def _metric_identifier(value: Any, *, max_length: int) -> str:
    """Normalize one structurally safe identifier without a product catalog."""
    from hermes_cli.observability.shared_metrics_contract import (_METRIC_IDENTIFIER_CHARACTERS, _METRIC_IDENTIFIER_START_CHARACTERS)
    from hermes_cli.observability.shared_metrics_contract import (
        Any
    )
    if not isinstance(value, str):
        return "unknown"
    identifier = value.strip().lower()
    if (
        not identifier
        or len(identifier) > max_length
        or identifier[0] not in _METRIC_IDENTIFIER_START_CHARACTERS
        or not _METRIC_IDENTIFIER_CHARACTERS.issuperset(identifier)
    ):
        return "unknown"
    return identifier
