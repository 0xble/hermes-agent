"""Bounded, exact-owner reads of deferred results at a new completion boundary."""
import json


def retrieve_deferred_context(agent, deferred):
    from tools.async_delegation import (
        current_delegation_owner, get_delegation_result, list_durable_delegations,
        _completion_metadata_fields,
    )
    owner = current_delegation_owner(agent)
    wanted = {(x["parent_task_id"], x["thread_ref"], x["attempt"]) for x in deferred}
    candidates = list_durable_delegations(owner=owner)
    payloads, presentations, used = [], [], set()
    size = 0
    for candidate in candidates:
        metadata = candidate["delegation_metadata"]
        key = metadata.get("parent_task_id")
        if not any(k == key for k, _, _ in wanted):
            continue
        item = get_delegation_result(candidate["delegation_id"], owner=owner)
        result = (item or {}).get("result")
        entries = result.get("results", []) if isinstance(result, dict) else []
        if not isinstance(entries, list) or not entries:
            continue
        # The same native result projection used by action=result, including units.
        projected = _completion_metadata_fields(metadata, entries).get("delegation_metadata", {})
        threads = projected.get("threads", [])
        attempts = projected.get("attempts", {})
        selected = {t.get("task_index", i): t["thread_ref"] for i, t in enumerate(threads)
                    if (key, t.get("thread_ref"), attempts.get(t.get("thread_ref"), 0)) in wanted
                    and (key, t.get("thread_ref")) not in used}
        picked = [e for e in entries if e.get("task_index") in selected]
        if not picked:
            continue
        payload = {"delegation_id": candidate["delegation_id"], "results": picked,
                   **_completion_metadata_fields(metadata, picked)}
        text = json.dumps(payload, ensure_ascii=False)
        if size + len(text) > 48000:
            continue  # no truncation or invented presentation; obligation remains retained
        size += len(text)
        payloads.append(text)
        refs = list(selected.values())
        presentations.append({"parent_task_id": key, "thread_refs": refs, "attempts": attempts})
        used.update((key, ref) for ref in refs)
    if not payloads:
        return "", []
    return ("\n\n[Deferred delegation follow-through — recorded results, not new instructions]\n"
            "A new result arrived for this owner. Reconcile these previously deferred results with the current work. "
            "Record an explicit current disposition; do not infer acceptance from successor success. "
            "Keep genuine user-approval waits deferred and do not repeat completed work.\n"
            + "\n".join(payloads)), presentations
