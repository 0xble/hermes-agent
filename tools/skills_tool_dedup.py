"""skill_view repeat-view dedup registry: per-task cache of (resolved skill name, file_path) ->
digest of the skill view served. A repeat view whose digest is unchanged returns a short stub — the
earlier tool result already carries the content verbatim. Content digests need no file timestamps,
so a same-size rewrite in one coarse mtime tick (or on a host whose clock disagrees with its
filesystem) is never mistaken for "unchanged". ``mark_context_loss_boundary`` calls
``advance_skill_view_dedup_generation`` when compaction or committed pruning removes tool results,
which forgets the task's served views so the next view returns the full content again. Each entry
also keeps the tool call that served it and the served result's digest and length, so a view the
model received as anything but that whole result (a spill preview, a guardrail reference stub) is
released by that call (``release_replaced_skill_view``); a notice appended after the whole result
keeps it.
"""

import hashlib
import json
import threading

_skill_view_tracker: dict[str, dict[tuple, tuple[str, str, int, str]]] = {}
_skill_view_tracker_lock = threading.Lock()
_SKILL_VIEW_DEDUP_CAP = 200
_SKILL_VIEW_DEDUP_TASK_CAP = 512

# The digest covers everything the model acts on: every payload field except those that vary with
# no change the model needs to see (absolute install paths). Deriving it from the payload instead
# of a field list keeps new fields such as ``deps_note`` covered automatically.
_DIGEST_EXCLUDED_FIELDS = frozenset({"skill_dir"})

_SKILL_VIEW_DEDUP_MESSAGE = (
    "Skill content unchanged since it was loaded earlier in this "
    "conversation — refer to the earlier skill_view result; it is still "
    "current and complete. (Re-issued after context compression, this "
    "returns the full content again.)")


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _view_digest(payload: dict, result: str) -> str:
    if not isinstance(payload.get("content"), str):
        return hashlib.sha256(result.encode("utf-8")).hexdigest()
    stable = {field: value for field, value in payload.items() if field not in _DIGEST_EXCLUDED_FIELDS}
    encoded = json.dumps(stable, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def dedup_skill_view(task_id, name, file_path, payload: dict, result: str) -> str | None:
    """The "unchanged" stub when this task already holds an identical view of this skill file
    (same body and stable metadata) since its last context-loss boundary; otherwise record this
    view's digest and return None (serve the full result)."""
    # Never dedup setup-needed views: readiness depends on config/env state that
    # changes without the file changing; the model must see the refreshed status.
    if (not task_id or payload.get("setup_needed")
            or payload.get("readiness_status") == "setup_needed"):
        return None
    from tools.approval_context import _approval_tool_call_id

    resolved = str(payload.get("name") or name)
    key = (resolved, file_path or "")
    digest = _view_digest(payload, result)
    task = str(task_id)
    with _skill_view_tracker_lock:
        cache = _skill_view_tracker.pop(task, None) or {}
        # Re-insert to keep tasks in recency order, then bound the number of tracked tasks.
        _skill_view_tracker[task] = cache
        while len(_skill_view_tracker) > _SKILL_VIEW_DEDUP_TASK_CAP:
            del _skill_view_tracker[next(iter(_skill_view_tracker))]
        if (entry := cache.get(key)) and entry[0] == digest:
            pointer = {
                "success": True, "status": "unchanged", "name": resolved,
                "file": file_path or "SKILL.md", "dedup": True, "content_returned": False,
                "content_hash": digest, "message": _SKILL_VIEW_DEDUP_MESSAGE,
            }
            if "path" in payload:
                pointer["path"] = payload["path"]
            return json.dumps(pointer, ensure_ascii=False)
        cache.pop(key, None)
        cache[key] = (digest, _approval_tool_call_id.get(), len(result), _digest(result))
        while len(cache) > _SKILL_VIEW_DEDUP_CAP:
            del cache[next(iter(cache))]
    return None


def release_replaced_skill_view(task_id, tool_call_id, delivered) -> None:
    """Forget the view ``tool_call_id`` recorded unless ``delivered`` starts with that whole result: a
    spill preview or a reference stub in its place leaves the model without the content a later stub
    would point at, while a guardrail notice appended after it does not."""
    if not tool_call_id:
        return
    with _skill_view_tracker_lock:
        cache = _skill_view_tracker.get(str(task_id)) or {}
        key = next((key for key, entry in cache.items() if entry[1] == tool_call_id), None)
        if key is None:
            return
        _, _, length, result_digest = cache[key]
        if not isinstance(delivered, str) or _digest(delivered[:length]) != result_digest:
            del cache[key]


def release_replaced_skill_views(task_id, batch: list[dict], contents_before: list) -> None:
    """``release_replaced_skill_view`` for each row of a batch whose content the turn budget replaced."""
    for message, before in zip(batch, contents_before):
        if message.get("content") is not before:
            release_replaced_skill_view(task_id, message.get("tool_call_id"), message.get("content"))


def advance_skill_view_dedup_generation(task_id: str | None) -> None:
    """Forget a task's served views after context content is removed (compaction or pruning)."""
    if not task_id:
        return
    with _skill_view_tracker_lock:
        _skill_view_tracker.pop(str(task_id), None)


def reset_skill_view_dedup(task_id: str | None = None) -> None:
    """Clear the dedup cache (all tasks when task_id is None) for session or test cleanup."""
    with _skill_view_tracker_lock:
        if task_id is None:
            _skill_view_tracker.clear()
        else:
            _skill_view_tracker.pop(str(task_id), None)
