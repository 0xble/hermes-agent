"""Durable presentation receipts for gateway delegation-completion turns."""

from __future__ import annotations

from typing import Any, Iterable, Mapping


def _deliveries(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [dict(item) for item in value if isinstance(item, Mapping) and item.get("delegation_id")]


def delivery_metadata_for_event(event: Any, gateway_input_owner: Any) -> dict[str, Any]:
    """Build the DB-only metadata for one gateway input without weakening ownership."""
    metadata = getattr(event, "metadata", None)
    metadata = metadata if isinstance(metadata, Mapping) else {}
    result: dict[str, Any] = {"gateway_input_owner": gateway_input_owner}
    parent_task_id = metadata.get("delegation_parent_task_id")
    if getattr(event, "internal", False) and parent_task_id:
        result["delegation_results"] = [{
            "parent_task_id": parent_task_id,
            "thread_refs": list(metadata.get("delegation_thread_refs") or []),
            "attempts": dict(metadata.get("delegation_attempts") or {}),
        }]
    deliveries = _deliveries(metadata.get("delegation_deliveries"))
    if getattr(event, "internal", False) and deliveries:
        result["delegation_deliveries"] = deliveries
    return result


def mark_persisted_delegation_presentations(
    messages: Iterable[Any], expected_deliveries: Any,
) -> int:
    """Settle only deliveries evidenced by a persisted user row in this result turn."""
    expected = _deliveries(expected_deliveries)
    if not expected:
        return 0
    expected_by_id = {str(item["delegation_id"]): item for item in expected}
    evidenced: set[str] = set()
    for message in messages or ():
        if not isinstance(message, Mapping) or message.get("role") != "user":
            continue
        if not isinstance(message.get("_row_id"), int):
            continue
        display_metadata = message.get("display_metadata")
        if not isinstance(display_metadata, Mapping):
            continue
        for item in _deliveries(display_metadata.get("delegation_deliveries")):
            delegation_id = str(item["delegation_id"])
            if delegation_id in expected_by_id and item == expected_by_id[delegation_id]:
                evidenced.add(delegation_id)
    if not evidenced:
        return 0
    from tools.async_delegation import mark_completion_presented
    settled = 0
    for delegation_id in sorted(evidenced):
        item = expected_by_id[delegation_id]
        owner = item.get("owner")
        settled += bool(mark_completion_presented(
            delegation_id, dict(owner) if isinstance(owner, Mapping) else None))
    return settled
