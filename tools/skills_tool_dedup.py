"""skill_view repeat-view dedup registry: per-task cache of (resolved skill name, file_path) ->
digest of the skill body served. A repeat view whose body hash is unchanged returns a short stub — the
earlier tool result already carries the content verbatim. Content digests need no file timestamps,
so a same-size rewrite in one coarse mtime tick (or on a host whose clock disagrees with its
filesystem) is never mistaken for "unchanged". The per-task context generation is advanced by
``mark_context_loss_boundary`` when compaction or committed pruning removes tool results; explicit
``reset_skill_view_dedup()`` remains available for session/test cleanup.
"""

import hashlib
import json
import threading

_skill_view_tracker: dict[str, dict[tuple, tuple[int, str]]] = {}
_skill_view_generation: dict[str, int] = {}
_skill_view_tracker_lock = threading.Lock()
_SKILL_VIEW_DEDUP_CAP = 200

_SKILL_VIEW_DEDUP_MESSAGE = (
    "Skill content unchanged since it was loaded earlier in this "
    "conversation — refer to the earlier skill_view result; it is still "
    "current and complete. (Re-issued after context compression, this "
    "returns the full content again.)")


def dedup_skill_view(task_id, name, file_path, payload: dict, result: str) -> str | None:
    """The "unchanged" stub when this task was already served exactly ``result`` for this skill
    file; otherwise record ``result``'s digest and return None (serve the full result)."""
    # Never dedup setup-needed views: readiness depends on config/env state that
    # changes without the file changing; the model must see the refreshed status.
    if (not task_id or payload.get("setup_needed")
            or payload.get("readiness_status") == "setup_needed"):
        return None
    resolved = str(payload.get("name") or name)
    key = (resolved, file_path or "")
    # Hash only the returned body, not volatile metadata such as readiness notes or
    # absolute paths. The key already separates SKILL.md from each linked file.
    body = payload.get("content")
    digest_input = body if isinstance(body, str) else result
    digest = hashlib.sha256(digest_input.encode("utf-8")).hexdigest()
    with _skill_view_tracker_lock:
        generation = _skill_view_generation.setdefault(str(task_id), 0)
        cache = _skill_view_tracker.setdefault(str(task_id), {})
        if cache.get(key) == (generation, digest):
            pointer = {
                "success": True, "status": "unchanged", "name": resolved,
                "file": file_path or "SKILL.md", "dedup": True, "content_returned": False,
                "content_hash": digest, "message": _SKILL_VIEW_DEDUP_MESSAGE,
            }
            if "path" in payload:
                pointer["path"] = payload["path"]
            return json.dumps(pointer, ensure_ascii=False)
        cache.pop(key, None)
        cache[key] = (generation, digest)
        while len(cache) > _SKILL_VIEW_DEDUP_CAP:
            del cache[next(iter(cache))]
    return None


def advance_skill_view_dedup_generation(task_id: str | None) -> None:
    """Advance the per-session generation after context content is removed."""
    if not task_id:
        return
    with _skill_view_tracker_lock:
        key = str(task_id)
        _skill_view_generation[key] = _skill_view_generation.get(key, 0) + 1
        _skill_view_tracker.pop(key, None)


def reset_skill_view_dedup(task_id: str | None = None) -> None:
    """Clear the dedup cache (all tasks when task_id is None); called on context compression."""
    with _skill_view_tracker_lock:
        if task_id is None:
            _skill_view_tracker.clear()
            _skill_view_generation.clear()
        else:
            key = str(task_id)
            _skill_view_tracker.pop(key, None)
            _skill_view_generation.pop(key, None)
