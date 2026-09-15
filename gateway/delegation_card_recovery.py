"""Exact durable execution evidence for a presented, restart-recovered card row."""
import logging

from gateway.delegation_card_presentation import _TERMINAL
from hermes_constants import reset_hermes_home_override, set_hermes_home_override

logger = logging.getLogger(__name__)


def terminal_states(home, card, parent_task_id, presentation):
    """Read the card's own profile ledger, never infer outcomes from presentation.

    Aggregate batch status cannot settle a child. Unknown partial units may
    contain positively recorded terminal children alongside unresolved siblings.
    Duplicate evidence is ambiguous even when the statuses happen to agree.
    """
    from tools.async_delegation import get_delegation_result

    ids = presentation.get("delegation_ids")
    if not isinstance(ids, list):
        return {}
    refs = presentation.get("thread_refs") or ()
    evidence = {ref: [] for ref in refs if isinstance(ref, str) and ref in card["rows"]}
    owner = card.get("delegation_owner", card["owner"])
    token = set_hermes_home_override(home)
    try:
        for delegation_id in ids:
            if not isinstance(delegation_id, str) or not delegation_id:
                continue
            try:
                record = get_delegation_result(delegation_id, owner=owner)
            except Exception:
                logger.exception("Could not read durable delegation card completion")
                continue
            if (not record or not isinstance(record.get("state"), str)
                    or record["state"] not in _TERMINAL | {"unknown", "stalled"}):
                continue
            metadata, result = record.get("delegation_metadata"), record.get("result")
            if (not isinstance(metadata, dict) or metadata.get("parent_task_id") != parent_task_id
                    or not isinstance(result, dict) or not isinstance(metadata.get("threads"), list)):
                continue
            entries = result.get("results", [result])
            attempts = metadata.get("attempts")
            if not isinstance(entries, list) or not isinstance(attempts, dict):
                continue
            for ref in evidence:
                row = card["rows"][ref]
                attempt = attempts.get(ref)
                if (row.get("state") != "unknown" or type(attempt) is not int
                        or type(row.get("attempt")) is not int
                        or type((presentation.get("attempts") or {}).get(ref)) is not int
                        or attempt != row.get("attempt", 0)
                        or attempt != (presentation.get("attempts") or {}).get(ref)
                        or not isinstance(row.get("child_session_id"), str)
                        or not row["child_session_id"]):
                    continue
                threads = [thread for thread in metadata["threads"]
                           if isinstance(thread, dict) and thread.get("thread_ref") == ref]
                if not threads:
                    continue
                state = None
                if (len(threads) == 1 and type(threads[0].get("task_index")) is int
                        and threads[0]["task_index"] >= 0
                        and sum(isinstance(thread, dict) and thread.get("task_index") == threads[0]["task_index"]
                                for thread in metadata["threads"]) == 1
                        and threads[0].get("original_call_id") == row.get("original_call_id")):
                    children = [entry for entry in entries if isinstance(entry, dict)
                                and type(entry.get("task_index")) is int
                                and entry["task_index"] == threads[0]["task_index"]]
                    if (len(children) == 1 and children[0].get("child_session_id") == row["child_session_id"]
                            and isinstance(children[0].get("status"), str)
                            and children[0].get("status") in _TERMINAL):
                        state = children[0]["status"]
                evidence[ref].append(state)
    finally:
        reset_hermes_home_override(token)
    return {ref: states[0] for ref, states in evidence.items()
            if len(states) == 1 and states[0] is not None}
