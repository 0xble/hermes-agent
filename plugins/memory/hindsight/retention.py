"""Hindsight retain-operation bookkeeping. Turn filtering lives in ``agent.memory_retention``."""

from __future__ import annotations

from dataclasses import dataclass

from agent.memory_retention import filter_retain_messages

__all__ = ["PendingRetainOperation", "filter_retain_messages"]


@dataclass
class PendingRetainOperation:
    bank_id: str
    expires_at: float
    wait_for_visibility: bool = True
