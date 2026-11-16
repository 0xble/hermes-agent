"""Local, size-and-route-only provider-request telemetry.

This ledger is deliberately separate from shared-metrics export: request rows are
local evidence, never an outbound metric. Producers enqueue a scrubbed row and return
without doing filesystem I/O; a daemon owns the append. Files are private, daily
rotated, and retained for a bounded period.
"""

from __future__ import annotations

import atexit
import json
import os
import queue
import threading
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from hermes_constants import get_hermes_home

REQUEST_ROW_KEYS = (
    "timestamp", "session_id", "source", "role_task_label", "cron_job_id",
    "requested_provider", "requested_model", "resolved_provider", "resolved_model",
    "fallback_index", "failure_class", "input_tokens", "cache_read_tokens",
    "cache_write_tokens", "output_tokens", "system_prompt_tier_sizes",
    "from_model", "to_model", "idle_gap_bucket",
)
REQUEST_ROW_KEY_SET = frozenset(REQUEST_ROW_KEYS)
_RETENTION_DAYS = 30
_QUEUE_SIZE = 4096
_STOP = object()


def _text(value: Any, limit: int = 256) -> str | None:
    if value is None:
        return None
    value = str(value).strip()
    return value[:limit] if value else None


def _nonnegative_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        value = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return value if value >= 0 else None


def _tier_sizes(value: Any) -> dict[str, int] | None:
    if not isinstance(value, dict):
        return None
    total = _nonnegative_int(value.get("total"))
    if total is not None:
        return {"total": total} if set(value) <= {"total"} else None
    result = {}
    for key in ("stable", "context", "volatile"):
        size = _nonnegative_int(value.get(key))
        if size is not None:
            result[key] = size
    return result if set(result) == {"stable", "context", "volatile"} else None


def _utc_timestamp() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


def sanitize_request_row(row: dict[str, Any]) -> dict[str, Any]:
    """Return exactly the public row schema, retaining no provider payload fields."""
    tier_sizes = _tier_sizes(row.get("system_prompt_tier_sizes"))
    return {
        "timestamp": _text(row.get("timestamp"), 40) or _utc_timestamp(),
        "session_id": _text(row.get("session_id"), 256),
        "source": _text(row.get("source"), 64),
        "role_task_label": _text(row.get("role_task_label"), 128),
        "cron_job_id": _text(row.get("cron_job_id"), 256),
        "requested_provider": _text(row.get("requested_provider"), 64),
        "requested_model": _text(row.get("requested_model"), 256),
        "resolved_provider": _text(row.get("resolved_provider"), 64),
        "resolved_model": _text(row.get("resolved_model"), 256),
        "fallback_index": _nonnegative_int(row.get("fallback_index")) or 0,
        "failure_class": _text(row.get("failure_class"), 128),
        "input_tokens": _nonnegative_int(row.get("input_tokens")),
        "cache_read_tokens": _nonnegative_int(row.get("cache_read_tokens")),
        "cache_write_tokens": _nonnegative_int(row.get("cache_write_tokens")),
        "output_tokens": _nonnegative_int(row.get("output_tokens")),
        "system_prompt_tier_sizes": tier_sizes,
        "from_model": _text(row.get("from_model"), 256),
        "to_model": _text(row.get("to_model"), 256),
        "idle_gap_bucket": _text(row.get("idle_gap_bucket"), 64),
    }


def _request_context(agent: Any, *, failure_class: str | None = None, usage: Any = None) -> dict[str, Any]:
    """Build route/size metadata without touching messages, tools, prompts, keys, or errors."""
    from agent.session_source import session_source_for
    from gateway.session_context import get_session_env
    primary = getattr(agent, "_primary_runtime", None) or {}
    session_id = str(getattr(agent, "session_id", "") or "")
    cron_session_id = get_session_env("HERMES_SESSION_ID", "")
    cron_job_id = get_session_env("HERMES_CRON_JOB_ID", "") or None
    if cron_job_id is None and cron_session_id.startswith("cron_"):
        cron_job_id = cron_session_id[5:].split("_", 1)[0] or None
    source = "cron" if get_session_env("HERMES_CRON_SESSION", "") == "1" else session_source_for(getattr(agent, "platform", None))
    parts = getattr(agent, "_cached_system_prompt_parts", None)
    cached_prompt = getattr(agent, "_cached_system_prompt", None)
    tiers = ("stable", "context", "volatile")
    # Trust the tier breakdown only while it still describes the prompt being sent:
    # build_system_prompt joins the non-empty tiers with "\n\n", and some paths reassign the
    # cached prompt directly without refreshing the parts.
    if (
        isinstance(parts, dict)
        and isinstance(cached_prompt, str)
        and all(isinstance(parts.get(key), str) for key in tiers)
        and "\n\n".join(parts[key] for key in tiers if parts[key]) == cached_prompt
    ):
        prompt_sizes = {key: len(parts[key]) for key in tiers}
    else:
        prompt_sizes = {"total": len(cached_prompt)} if isinstance(cached_prompt, str) else None
    usage_dict = usage if isinstance(usage, dict) else {}
    role_task_label = getattr(agent, "_delegate_role", None) or getattr(agent, "_turn_route_task", None)
    if not role_task_label:
        role_task_label = "fallback" if getattr(agent, "_fallback_activated", False) else "primary"
    return {
        "session_id": session_id,
        "source": source,
        "role_task_label": role_task_label,
        "cron_job_id": cron_job_id,
        "requested_provider": primary.get("requested_provider") or getattr(agent, "requested_provider", None) or primary.get("provider"),
        "requested_model": primary.get("model") or getattr(agent, "model", None),
        "resolved_provider": getattr(agent, "provider", None),
        "resolved_model": getattr(agent, "model", None),
        "fallback_index": max(0, int(getattr(agent, "_fallback_index", 0) or 0) - 1)
        if getattr(agent, "_fallback_activated", False) else 0,
        "failure_class": failure_class,
        "input_tokens": usage_dict.get("input_tokens"),
        "cache_read_tokens": usage_dict.get("cache_read_tokens"),
        "cache_write_tokens": usage_dict.get("cache_write_tokens"),
        "output_tokens": usage_dict.get("output_tokens"),
        "system_prompt_tier_sizes": prompt_sizes,
        "switch_from_model": None,
        "switch_to_model": None,
        "idle_gap_bucket": None,
    }


def row_for_response(agent: Any, usage: Any = None) -> dict[str, Any]:
    """Build a successful request row from canonical numeric usage only."""
    if usage is None:
        usage_dict = {}
    elif isinstance(usage, dict):
        usage_dict = usage
    else:
        usage_dict = {name: getattr(usage, name, None) for name in (
            "input_tokens", "cache_read_tokens", "cache_write_tokens", "output_tokens")}
    return sanitize_request_row(_request_context(agent, usage=usage_dict))


def row_for_error(agent: Any, failure_class: str | None) -> dict[str, Any]:
    return sanitize_request_row(_request_context(agent, failure_class=failure_class))


def row_for_attended_switch(*, session_id: str, source: str, from_model: str, to_model: str,
                             idle_gap_bucket: str, role_task_label: str = "attended_model_switch") -> dict[str, Any]:
    return sanitize_request_row({
        "session_id": session_id, "source": source, "role_task_label": role_task_label,
        "resolved_model": to_model,
        "from_model": from_model, "to_model": to_model,
        "idle_gap_bucket": idle_gap_bucket,
    })


def idle_gap_bucket(seconds: float | None) -> str:
    if seconds is None or seconds < 0:
        return "unknown"
    if seconds < 60:
        return "lt_1m"
    if seconds < 300:
        return "1m_to_5m"
    if seconds < 900:
        return "5m_to_15m"
    if seconds < 3600:
        return "15m_to_1h"
    return "gte_1h"


def record_attended_switch(*, session_id: str, source: str, from_model: str, to_model: str,
                           idle_seconds: float | None) -> bool:
    return record_request_row(row_for_attended_switch(
        session_id=session_id, source=source, from_model=from_model, to_model=to_model,
        idle_gap_bucket=idle_gap_bucket(idle_seconds),
    ))


class RequestUsageLedger:
    """Non-blocking append-only JSONL ledger with bounded retention."""

    def __init__(self, root: Path | None = None) -> None:
        # A fixed root is for tests; the default follows the caller's (possibly profile-routed) home.
        self._fixed_root = root
        self._queue: queue.Queue[tuple[Path, dict[str, Any]] | object] = queue.Queue(maxsize=_QUEUE_SIZE)
        self._errors = 0
        self._pruned_days: set[tuple[Path, str]] = set()
        self._lock = threading.Lock()
        self._worker = threading.Thread(target=self._run, name="hermes-request-usage-ledger", daemon=True)
        self._worker.start()

    @property
    def root(self) -> Path:
        return self._fixed_root or (get_hermes_home() / "telemetry" / "request_usage")

    @property
    def error_count(self) -> int:
        with self._lock:
            return self._errors

    def _error(self) -> None:
        with self._lock:
            self._errors += 1

    def record(self, row: dict[str, Any]) -> bool:
        """Queue a sanitized row; never waits and never raises into a request.

        Incognito sessions persist nothing, so their rows are dropped here, in the request's own
        context: the writer thread does not inherit the incognito scope.
        """
        try:
            from agent.incognito import is_incognito
            session_id = row.get("session_id")
            if is_incognito(session_id if isinstance(session_id, str) and session_id else None):
                return False
            # Resolve the home here: the worker thread does not inherit context-local overrides.
            self._queue.put_nowait((self.root, sanitize_request_row(row)))
            return True
        except Exception:  # health: allow BLE001 -- telemetry must never fail or slow a provider request; errors are counted
            self._error()
            return False

    def _run(self) -> None:
        while True:
            item = self._queue.get()
            try:
                if item is _STOP:
                    return
                root, row = item  # type: ignore[misc]
                self._append(root, row)
            except Exception:  # health: allow BLE001 -- telemetry must never fail or slow a provider request; errors are counted
                self._error()
            finally:
                self._queue.task_done()

    def _append(self, root: Path, row: dict[str, Any]) -> None:
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        day = datetime.now(UTC).date().isoformat()
        path = root / f"{day}.jsonl"
        payload = (json.dumps(row, sort_keys=True, separators=(",", ":")) + "\n").encode()
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, payload)
        finally:
            os.close(fd)
        prune_key = (root, day)
        if prune_key not in self._pruned_days:
            self._prune(root)
            self._pruned_days.add(prune_key)

    def _prune(self, root: Path) -> None:
        cutoff = datetime.now(UTC).date() - timedelta(days=_RETENTION_DAYS)
        for path in root.glob("*.jsonl"):
            try:
                if datetime.strptime(path.stem, "%Y-%m-%d").date() < cutoff:
                    path.unlink(missing_ok=True)
            except (OSError, ValueError):
                continue

    def flush(self, timeout: float = 5.0) -> bool:
        """Test/read-path consistency barrier; request producers must not call this."""
        deadline = time.monotonic() + timeout
        while self._queue.unfinished_tasks and time.monotonic() < deadline:
            time.sleep(0.005)
        return self._queue.unfinished_tasks == 0

    def read(self, *, source: str | None = None, model: str | None = None,
             cron_job_id: str | None = None) -> list[dict[str, Any]]:
        self.flush()
        rows: list[dict[str, Any]] = []
        if not self.root.exists():
            return rows
        for path in sorted(self.root.glob("*.jsonl")):
            try:
                with path.open(encoding="utf-8") as stream:
                    for line in stream:
                        try:
                            row = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        if source is not None and row.get("source") != source:
                            continue
                        if model is not None and row.get("resolved_model") != model:
                            continue
                        if cron_job_id is not None and row.get("cron_job_id") != cron_job_id:
                            continue
                        rows.append(sanitize_request_row(row))
            except OSError:
                continue
        return rows


_default_ledger: RequestUsageLedger | None = None
_default_pid: int | None = None
_default_lock = threading.Lock()


def get_request_usage_ledger() -> RequestUsageLedger:
    """The process's ledger. A forked child gets its own: it inherits the queue but not the
    writer thread, so reusing the parent's ledger would silently drop every row."""
    global _default_ledger, _default_pid
    pid = os.getpid()
    if _default_ledger is None or _default_pid != pid:
        with _default_lock:
            if _default_ledger is None or _default_pid != pid:
                _default_ledger = RequestUsageLedger()
                _default_pid = pid
    return _default_ledger


def _guarded(build) -> bool:
    """Build and queue one row. Never raises into the request path; failures are counted."""
    try:
        return get_request_usage_ledger().record(build())
    except Exception:  # health: allow BLE001 -- telemetry must never fail or slow a provider request; errors are counted
        _count_hook_error()
        return False


def _count_hook_error() -> None:
    ledger = _default_ledger
    if ledger is not None:
        ledger._error()


def record_request_row(row: dict[str, Any]) -> bool:
    return _guarded(lambda: row)


def note_response(agent: Any, usage: Any = None) -> bool:
    """Record one response row for ``agent``. Never raises into the request path."""
    return _guarded(lambda: row_for_response(agent, usage))


def note_error(agent: Any, failure_class: str | None) -> bool:
    """Record one failed-request row for ``agent``. Never raises into the request path."""
    return _guarded(lambda: row_for_error(agent, failure_class))


def _auxiliary_row(payload: dict[str, Any]) -> dict[str, Any]:
    from agent.session_source import session_source_for
    from gateway.session_context import get_session_env

    usage = payload.get("usage") or {}
    session_id = payload.get("session_id") or get_session_env("HERMES_SESSION_ID", "") or None
    cron_session_id = get_session_env("HERMES_SESSION_ID", "")
    cron_job_id = get_session_env("HERMES_CRON_JOB_ID", "") or None
    if cron_job_id is None:
        for candidate in (session_id, cron_session_id):
            if isinstance(candidate, str) and candidate.startswith("cron_"):
                cron_job_id = candidate[5:].split("_", 1)[0] or None
                break
    source = (
        "cron" if get_session_env("HERMES_CRON_SESSION", "") == "1"
        else session_source_for(payload.get("platform"))
    )
    return {
        "session_id": session_id,
        "source": source,
        "role_task_label": payload.get("aux_task") or "auxiliary",
        "cron_job_id": cron_job_id,
        "requested_provider": payload.get("provider"), "requested_model": payload.get("model"),
        "resolved_provider": payload.get("provider"), "resolved_model": payload.get("model"),
        "failure_class": payload.get("error_type"),
        "input_tokens": usage.get("input_tokens"),
        "cache_read_tokens": usage.get("cache_read_tokens"),
        "cache_write_tokens": usage.get("cache_write_tokens"),
        "output_tokens": usage.get("output_tokens"),
    }


def note_auxiliary(payload: dict[str, Any]) -> bool:
    """Record one auxiliary-call row (route, label, failure class, numeric usage only)."""
    return _guarded(lambda: _auxiliary_row(payload))


def note_attended_switch(runner: Any, ctx: Any, agent: Any, previous_decision: Any, to_model: str) -> bool:
    """Record an attended switch-back from the gateway turn. Never raises into the request path."""
    def build() -> dict[str, Any]:
        from agent.session_source import session_source_for
        state = runner._session_state(ctx.session_key) if ctx.session_key else None
        last = getattr(getattr(state, "conversation", None), "last_user_activity_at", None)
        return row_for_attended_switch(
            session_id=getattr(agent, "session_id", ""),
            source=session_source_for(getattr(agent, "platform", None)),
            from_model=getattr(previous_decision, "model", "") or getattr(agent, "model", ""),
            to_model=to_model,
            idle_gap_bucket=idle_gap_bucket(time.time() - float(last) if last is not None else None),
        )
    return _guarded(build)


def _flush_default_ledger_at_exit() -> None:
    """Give the daemon writer a bounded chance to persist queued rows at process exit."""
    ledger = _default_ledger
    if ledger is not None:
        ledger.flush(timeout=2.0)


atexit.register(_flush_default_ledger_at_exit)


def request_usage_rows(*, source: str | None = None, model: str | None = None,
                       cron_job_id: str | None = None, since: str | None = None,
                       limit: int | None = 1000) -> list[dict[str, Any]]:
    rows = get_request_usage_ledger().read(source=source, model=model, cron_job_id=cron_job_id)
    if since:
        rows = [row for row in rows if str(row.get("timestamp", "")) >= since]
    if limit is None:
        return rows
    limit = max(0, int(limit))
    return rows[-limit:] if limit else []
