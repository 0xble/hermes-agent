"""Best-effort, privacy-preserving attribution for web_search calls."""

from __future__ import annotations

import contextlib
from contextvars import ContextVar
from datetime import UTC, datetime
import hashlib
import json
import logging
import os
from pathlib import Path
import threading
from typing import Iterator

logger = logging.getLogger(__name__)

_CALLER_SURFACE: ContextVar[str] = ContextVar("web_search_caller_surface", default="assistant_tool_call")
_BATCH_SIZE: ContextVar[int | None] = ContextVar("web_search_batch_size", default=None)
_WRITE_LOCK = threading.Lock()
_MAX_BYTES = 10 * 1024 * 1024
_BACKUPS = 2


@contextlib.contextmanager
def search_call_context(surface: str, batch_size: int | None = None) -> Iterator[None]:
    """Bind attribution metadata for one web_search dispatch."""
    surface_token = _CALLER_SURFACE.set(surface or "other")
    batch_token = _BATCH_SIZE.set(batch_size)
    try:
        yield
    finally:
        _BATCH_SIZE.reset(batch_token)
        _CALLER_SURFACE.reset(surface_token)


def caller_surface() -> str:
    return _CALLER_SURFACE.get()


def batch_size() -> int | None:
    return _BATCH_SIZE.get()


def _session_identity(session_id: str | None) -> tuple[str, str, str]:
    try:
        from gateway.session_context import bound_session_env
        current = bound_session_env("HERMES_SESSION_ID", "")
    except Exception:
        current = ""
    sid = str(session_id or current or "")
    parent = root = ""
    try:
        from agent.delegation_context import delegated_child_parent_session_id, delegated_child_root_session_id
        parent = delegated_child_parent_session_id()
        root = delegated_child_root_session_id()
    except Exception:
        pass
    return sid, parent, root


def _log_path() -> Path:
    from hermes_constants import get_hermes_home
    return get_hermes_home() / "logs" / "web_search_calls.jsonl"


def _rotate_if_needed(path: Path) -> None:
    try:
        if path.stat().st_size <= _MAX_BYTES:
            return
        with contextlib.suppress(OSError):
            path.with_name(f"{path.name}.{_BACKUPS}").unlink()
        for index in range(_BACKUPS - 1, 0, -1):
            with contextlib.suppress(OSError):
                path.with_name(f"{path.name}.{index}").replace(path.with_name(f"{path.name}.{index + 1}"))
        path.replace(path.with_name(f"{path.name}.1"))
    except OSError:
        pass


def record_search_call(
    *,
    session_id: str | None,
    provider: str,
    search_type: str,
    search_type_reason: str,
    query: str,
    cache_outcome: str,
    success: bool,
) -> None:
    """Append one bounded, secret-free record; all logging errors are swallowed."""
    try:
        sid, parent, root = _session_identity(session_id)
        record = {
            "timestamp": datetime.now(UTC).isoformat(),
            "session_id": sid,
            "parent_session_id": parent,
            "root_session_id": root,
            "caller_surface": caller_surface(),
            "batch_size": batch_size(),
            "provider": provider,
            "backend": provider,
            "search_type": search_type or "default",
            "search_type_reason": search_type_reason or "",
            "cache_outcome": cache_outcome,
            "success": bool(success),
            "query_hash": hashlib.sha256((query or "").encode("utf-8")).hexdigest()[:16],
            "query_length": len(query or ""),
        }
        path = _log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        line = json.dumps(record, ensure_ascii=True, separators=(",", ":")) + "\n"
        with _WRITE_LOCK:
            _rotate_if_needed(path)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(line)
    except Exception as exc:
        logger.debug("web_search attribution logging failed: %s", exc)


def summarize_records(path: str | os.PathLike[str]) -> list[dict]:
    """Aggregate JSONL records by UTC day, surface, root session, and cache outcome."""
    from collections import Counter
    counts: Counter[tuple[str, str, str, str]] = Counter()
    with Path(path).open(encoding="utf-8") as handle:
        for line in handle:
            try:
                row = json.loads(line)
                day = str(row.get("timestamp", ""))[:10]
                key = (day, str(row.get("caller_surface", "")), str(row.get("root_session_id", "")), str(row.get("cache_outcome", "")))
                counts[key] += 1
            except (json.JSONDecodeError, TypeError):
                continue
    return [
        {"day": day, "caller_surface": surface, "root_session_id": root, "cache_outcome": outcome, "calls": count}
        for (day, surface, root, outcome), count in sorted(counts.items())
    ]
