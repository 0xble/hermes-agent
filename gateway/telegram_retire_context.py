"""Bound task briefs without discarding authored work or copying execution state."""
from __future__ import annotations

import hashlib
import json


# task_events.ck_task_events_data is 16 KiB. Leave room for the task_create
# envelope and jsonb headers, and measure the JSON-escaped context, not chars.
CONTEXT_BUDGET = 12 * 1024
CONTEXT_PREFIX = "Own the transferred work. Preserve its original limits and report to this topic.\n"
DEFINITION_FIELDS = frozenset({
    "id", "name", "prompt", "goal", "schedule", "timezone", "schedule_display",
    "repeat", "mode", "interval_seconds", "times", "until", "max_ticks", "max_turns",
    "skills", "skill", "deliver", "failure_deliver", "model", "provider", "base_url",
    "script", "no_agent", "monitor_script", "monitor_url", "context_from", "origin",
    "enabled_toolsets", "workdir", "attach_to_session", "reasoning_effort", "keep_pinned_lane",
    "review", "output_local_only", "interpreter", "hard_wall_timeout_seconds", "route",
    "contract", "subgoals", "gates", "revisions",
})
REVISION_PROGRESS = frozenset({
    "at", "timestamp", "created_at", "current_delay", "ticks_fired", "turns_used",
    "last_run_at", "last_fired_at", "next_due_at", "mutation_id", "tick_id",
    "last_output_tail", "last_exit_code", "last_checked_at",
})


def authored_definition(record):
    """Keep explicit job/control inputs, including limits, never scheduler output."""
    definition = {key: value for key, value in record.items() if key in DEFINITION_FIELDS}
    if isinstance(definition.get("repeat"), dict):
        definition["repeat"] = {key: value for key, value in definition["repeat"].items() if key != "completed"}
    for field in ("gates", "revisions"):
        if field in definition:
            definition[field] = strip_revision_progress(definition[field])
    return definition


def strip_revision_progress(value):
    if isinstance(value, list):
        return [strip_revision_progress(entry) for entry in value]
    if isinstance(value, dict):
        return {key: strip_revision_progress(entry) for key, entry in value.items() if key not in REVISION_PROGRESS}
    return value


def stable_item(item):
    if item["kind"] not in {"cron", "loop", "goal"}:
        return item
    return {**item, "record": authored_definition(item["record"])}


def definition_digest(item):
    encoded = json.dumps(stable_item(item), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def task_context(source, state, item):
    from gateway.telegram_retire import state_key

    stable = stable_item(item)
    context = CONTEXT_PREFIX + json.dumps(stable, ensure_ascii=False)
    if len(json.dumps(context, ensure_ascii=False).encode("utf-8")) <= CONTEXT_BUDGET:
        return context
    # prepare persists this complete snapshot before calling receipt. Reuse it
    # rather than introducing an attachment store or truncating a long prompt.
    # Cron transfer changes routes in place, never deletes the original job.
    manifest = {"id": item["id"], "kind": item["kind"], "definition_ref": {
        "state_key": state_key(source), "item_id": item["id"],
        "handoff_generation": state.get("handoff_generation", state["generation"]),
        "source_session": state["session_id"], "job_id": item.get("record", {}).get("id"),
        "sha256": definition_digest(item),
        "read": "Read state_meta[state_key].handoffs[item_id].item in this profile's session DB. "
                "The full original definition is retained there after retirement. Project with "
                "gateway.telegram_retire_context.stable_item, then verify SHA-256 of sorted, compact "
                "UTF-8 JSON before acting. Cron jobs also remain readable by job_id; routes may have moved.",
    }}
    context = CONTEXT_PREFIX + json.dumps(manifest, ensure_ascii=False)
    if len(json.dumps(context, ensure_ascii=False).encode("utf-8")) > CONTEXT_BUDGET:
        raise RuntimeError("Handoff definition reference exceeds the task context budget")
    return context


def context_identity(item):
    reference = item.get("definition_ref")
    digest = reference["sha256"] if reference else definition_digest(item)
    return item["id"], item["kind"], digest
