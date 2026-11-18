"""Public, in-process task browser attachment for trusted plugins.

Use the runtime-injected task_id, never a model-supplied ID. Attach/detach must
run between (not concurrently with) that task's browser calls. This does not
change profile configuration, environment, or another task's browser routing.
"""
from __future__ import annotations

from urllib.parse import urlsplit


def _task_id(task_id: str) -> str:
    if not isinstance(task_id, str) or not task_id.strip() or task_id == "default" or task_id.endswith("::local"):
        raise ValueError("A nonempty runtime task_id (not default or a sidecar ID) is required")
    return task_id


def task_browser_url(task_id: str | None) -> str | None:
    """Return this profile/task's explicitly attached CDP endpoint, if any."""
    from tools import browser_tool as bt
    from hermes_constants import hermes_home_key
    if not task_id:
        return None
    with bt._cleanup_lock:
        info = bt._active_sessions.get(task_id) or {}
        return info.get("cdp_url") if info.get("external_task_browser") and info.get("home_key") == hermes_home_key() else None


def use_camofox(task_id: str | None) -> bool:
    """Task attachment takes precedence over profile-wide Camofox selection."""
    from tools import browser_tool as bt
    return bt._is_camofox_mode() and not task_browser_url(task_id)


def attach_task_browser(task_id: str, cdp_url: str) -> None:
    """Attach ordinary browser_* calls for ONE task to an external CDP browser.

    The browser remains externally owned. Same-endpoint attach is idempotent;
    replacing an active agent-browser session requires explicit cleanup first.
    Camofox-only tools are unavailable while attached. No network connection is
    made until the first browser call. Connection errors never fall back to the
    task's parked Camofox browser. Process-local; not persisted across restart.
    """
    from tools import browser_tool as bt
    from tools.browser_tool_session import _session_record
    from tools import browser_tool_lifecycle as lifecycle
    from hermes_constants import hermes_home_key
    task_id = _task_id(task_id)
    try:
        endpoint = urlsplit(cdp_url)
        valid = endpoint.scheme in {"ws", "wss", "http", "https"} and endpoint.hostname and endpoint.port != 0
    except (TypeError, ValueError):
        valid = False
    if not valid or not isinstance(cdp_url, str) or cdp_url != cdp_url.strip():
        raise ValueError("cdp_url must be an absolute HTTP(S) or WS(S) CDP endpoint")
    with bt._cleanup_lock:
        current = bt._active_sessions.get(task_id)
        if current:
            if current.get("home_key") == hermes_home_key() and current.get("external_task_browser") and current.get("cdp_url") == cdp_url:
                return
            raise RuntimeError("Task already has an agent-browser session; detach or clean it up before attaching")
        info = _session_record("task_cdp", cdp_url, {"cdp_override": True})
        info.update(external_task_browser=True, home_key=hermes_home_key(),
                    session_key=task_id, owner_task_id=task_id)
        bt._active_sessions[task_id] = info
        bt._suspect_browser_sessions.pop(task_id, None)
        bt._last_active_session_key[task_id] = task_id
    lifecycle._update_session_activity(task_id)
    lifecycle._start_browser_cleanup_thread()


def detach_task_browser(task_id: str) -> bool:
    """Release Hermes's task attachment without closing the external browser.

    Stops only Hermes's supervisor and verified agent-browser daemon, never
    issues `close`/Browser.close/Target.closeTarget. Returns False if not attached
    or protected-date quarantine prevents cleanup. Parked Camofox is unchanged.
    """
    from tools import browser_tool as bt
    from tools import browser_tool_lifecycle as lifecycle
    from agent.redact import has_vault_date_components
    from hermes_constants import hermes_home_key
    task_id = _task_id(task_id)
    with bt._cleanup_lock:
        info = bt._active_sessions.get(task_id)
        if not info or not info.get("external_task_browser") or info.get("home_key") != hermes_home_key() or has_vault_date_components(task_id):
            return False
    # Force reap is resource-only: no backend close and no Camofox cleanup.
    lifecycle._force_reap_browser_session(task_id)
    return True
