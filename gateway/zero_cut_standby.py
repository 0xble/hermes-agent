"""Local readiness state for a slot-b zero-cut standby gateway."""

from __future__ import annotations

from typing import Any

from gateway.gateway_slot import SLOT_STANDBY, effective_gateway_slot

_ready = False


def is_standby() -> bool:
    """Return whether this process is the pinned, opt-in standby slot."""
    return effective_gateway_slot() == SLOT_STANDBY


def mark_ready() -> None:
    """Mark the local gateway ready after its runner has started successfully."""
    global _ready
    _ready = True


def readiness() -> dict[str, Any]:
    """Return the local readiness fields exposed by control-socket status."""
    slot = effective_gateway_slot()
    return {"slot": slot, "standby": slot == SLOT_STANDBY, "ready": _ready, "deferred": []}


def reset_for_tests() -> None:
    """Reset local readiness for isolated tests."""
    global _ready
    _ready = False
