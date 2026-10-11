"""Disk bounds for redacted request-debug artifacts, not provider requests."""

import json
import os
import re
from itertools import islice
from pathlib import Path

REQUEST_DUMP_MAX_BYTES = 1024 * 1024
REQUEST_DUMP_MAX_FILES = 100
_TRUNCATED = "[request dump truncated]"
_DUMP_NAME = re.compile(r"request_dump_.+_[0-9]{8}_[0-9]{6}_[0-9]{6}\.json")


def bounded_request_dump(payload: dict) -> dict:
    """Keep small dumps intact; progressively clip oversized JSON with head/tail context.

    Size with the same UTF-8/indent settings as atomic_json_write, including escaping,
    rather than estimating from character counts. The fixed envelope stays recognizable.
    """
    def size(value):
        text = json.dumps(value, ensure_ascii=False, indent=2)
        try:
            encoded = len(text.encode("utf-8"))
        except UnicodeEncodeError:
            # atomic_json_write escapes the entire payload on surrogate fallback.
            text = json.dumps(value, ensure_ascii=True, indent=2)
            encoded = len(text.encode("utf-8"))
        # atomic_json_write writes in text mode, so each newline becomes os.linesep on disk.
        return encoded + text.count("\n") * (len(os.linesep) - 1)

    original_bytes = size(payload)
    if original_bytes <= REQUEST_DUMP_MAX_BYTES:
        return payload

    def clip(value, text_limit, item_limit, depth=0):
        if isinstance(value, str):
            if len(value) <= text_limit:
                return value
            head = text_limit // 2
            tail = text_limit - head
            return value[:head] + _TRUNCATED + value[-tail:]
        if isinstance(value, (dict, list)) and depth >= 12:
            return _TRUNCATED
        if isinstance(value, dict):
            result = {
                clip(key, text_limit, item_limit): clip(child, text_limit, item_limit, depth + 1)
                for key, child in islice(value.items(), item_limit)
            }
            if len(value) > item_limit:
                result[_TRUNCATED] = f"{len(value) - item_limit} entries omitted"
            return result
        if isinstance(value, list):
            if len(value) > item_limit:
                head = item_limit // 2
                tail = item_limit - head
                value = value[:head] + [f"{_TRUNCATED} {len(value) - item_limit} items omitted"] + value[-tail:]
            return [clip(child, text_limit, item_limit, depth + 1) for child in value]
        return value

    text_limit, item_limit = 8192, 128
    while True:
        result = {key: clip(value, text_limit, item_limit) for key, value in payload.items()}
        # Don't discard the URL, headers or body merely to shrink a large conversation.
        result["request"] = {
            key: clip(value, text_limit, item_limit) for key, value in payload["request"].items()
        }
        result["truncation"] = {"marker": _TRUNCATED, "original_bytes": original_bytes}
        if size(result) <= REQUEST_DUMP_MAX_BYTES:
            return result
        text_limit, item_limit = max(1, text_limit // 2), max(1, item_limit // 2)


def prune_request_dumps(newest: Path) -> None:
    """Best-effort profile-wide count retention.

    Every write scans the whole directory so pruning cannot starve behind unrelated
    entries. The scan reads names only and stats just dump-shaped entries. Only the
    writer's timestamped regular files qualify. Keep the just-written dump even across
    clock skew; permission errors and concurrent writers defer pruning.
    """
    candidates = []
    try:
        with os.scandir(newest.parent) as entries:
            for entry in entries:
                if entry.name == newest.name or not _DUMP_NAME.fullmatch(entry.name):
                    continue
                try:
                    if entry.is_file(follow_symlinks=False):
                        candidates.append((entry.stat(follow_symlinks=False).st_mtime_ns, entry.name))
                except OSError:
                    continue
    except OSError:
        return
    candidates.sort(reverse=True)
    for _, name in candidates[REQUEST_DUMP_MAX_FILES - 1:]:
        try:
            (newest.parent / name).unlink()
        except OSError:
            pass
