#!/usr/bin/env python3
"""Vault-backed model-blind browser autofill tools.

Vault actions are available alongside a built-in browser, Browser Use, or a
registered browser page backend, including when the vault is empty so a first
login can be saved:

<<<<<<< HEAD
- ``browser_vault_list``  → handles + metadata (for logins this includes the
  identifier — it is NOT a secret; the agent types it itself). Passwords are
||||||| dff103e05878
- ``list``  → handles + metadata (for logins this includes the
  identifier — it is NOT a secret; the agent types it itself). Passwords are
=======
- ``list``  → handles + metadata (for logins this includes the
  identifier — it is NOT a secret; ``fill`` types it with the password). Passwords are
>>>>>>> 32dbc1dcda2255fd35969ec99cfc0170988c3eca
  never returned.
- ``browser_vault_fill``  → server-side fill of the CURRENT page from a vault
  handle: the password field for logins, card fields for payment items (after
  the user confirms), address fields for address items, or one configured
  origin-bound protected field. The secret is
  resolved locally, the page origin must EXACTLY match the item's bound
  origin (pre-checked AND re-asserted synchronously inside the fill script),
  the field is chosen by the ported login-control classifier, injection runs
  exclusively over the supervisor CDP WebSocket (never argv) — or, for a plugin browser with no CDP
  endpoint, through its registered ``BrowserPageBackend`` (Camofox's REST evaluate) — and the tool
  result reports only ``{filled_fields, kind, origin, success}`` (a login also says whether its
  identifier was filled, ``identifier_filled``) — the
  password never appears in tool results, logs, or the session DB, and its
  exact bytes are registered with the browser-result redaction boundary so
  no later browser tool call can echo them back to the model.

Ported design from Merit-Systems/OpenInstinct (MIT): opaque-handle vault
autofill (kernel-login-autofill.ts / fill_from_vault.ts).
"""

from __future__ import annotations

import json
import re
import secrets
import logging
<<<<<<< HEAD
import threading
import time
from typing import Any, Dict, Optional
||||||| dff103e05878
from typing import Any, Dict, Optional
=======
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Dict, Iterator, Optional
>>>>>>> 32dbc1dcda2255fd35969ec99cfc0170988c3eca

from agent.vault_login_classifier import build_form_probe_js
from tools.registry import no_cache_check_fn
from tools.browser_vault_context import plugin_errors_as_refusal, snapshot_refusal, write_backend_code

logger = logging.getLogger(__name__)

# A refused/unanswered prompt blocks another card prompt for this approval session and origin.
# Monotonic TTL lets an explicit later request retry; cap entries in a long-running gateway.
_PAYMENT_RETRY_TTL = 600
_payment_retry_lock = threading.Lock()
_payment_retry_until: dict[tuple[str, str, str], float] = {}

# Alias-only login fills require an explicit one-time confirmation because the alias registry is
# intentionally outside the password-manager item's own origin binding. Decisions are scoped to the
# active profile and approval session, and refusals remain fail-closed for the process lifetime.
_alias_fill_lock = threading.Lock()
_alias_fill_decisions: dict[tuple[str, str, str, str], str] = {}
_alias_fill_refused: set[tuple[str, str, str, str]] = set()
_ALIAS_FILL_CACHE_CAP = 256


def _alias_fill_key(task_id: str, handle: str, origin: str) -> tuple[str, str, str, str]:
    from hermes_constants import hermes_home_key
    from tools.approval_context import get_current_session_key

    session = get_current_session_key()
    return (hermes_home_key(), task_id if session == "default" else session, handle, origin)


def _alias_fill_decision(key: tuple[str, str, str, str]) -> str | None:
    with _alias_fill_lock:
        if key in _alias_fill_refused:
            return "refused"
        return _alias_fill_decisions.get(key)


def _record_alias_fill_decision(key: tuple[str, str, str, str], decision: str) -> None:
    with _alias_fill_lock:
        if decision == "refused":
            _alias_fill_refused.add(key)
            _alias_fill_decisions.pop(key, None)
            return
        if len(_alias_fill_decisions) >= _ALIAS_FILL_CACHE_CAP and key not in _alias_fill_decisions:
            # Refusals live in the separate process-lifetime set above. Only approvals are bounded
            # and eligible for eviction, so a declined origin can never become retryable by churn.
            _alias_fill_decisions.pop(next(iter(_alias_fill_decisions)))
        _alias_fill_decisions[key] = decision


def _payment_retry_key(task_id: str, origin: str) -> tuple[str, str, str]:
    from hermes_constants import hermes_home_key
    from tools.approval_context import get_current_session_key

    session = get_current_session_key()
    return (hermes_home_key(), task_id if session == "default" else session, origin)


def _payment_retry_blocked(key: tuple[str, str, str], *, refuse: bool = False) -> bool:
    now = time.monotonic()
    with _payment_retry_lock:
        for old_key, expiry in list(_payment_retry_until.items()):
            if expiry <= now:
                del _payment_retry_until[old_key]
        if refuse:
            if len(_payment_retry_until) >= 256 and key not in _payment_retry_until:
                del _payment_retry_until[min(_payment_retry_until, key=lambda existing: _payment_retry_until[existing])]
            _payment_retry_until[key] = now + _PAYMENT_RETRY_TTL
            return True
        return key in _payment_retry_until


# ---------------------------------------------------------------------------
# Availability check
# ---------------------------------------------------------------------------

def _check_vault_available() -> bool:
    """Schema-gate: the vault tools ride with the browser. An empty vault still needs
    browser_vault_save_login so the agent can offer to remember a login the first time it meets a
    form; hiding the tools until an item exists meant nobody ever discovered the feature."""
    from agent.browser_page_registry import list_providers
    from tools.browser_tool_install import check_browser_requirements
    from tools.browser_use_cli import is_browser_use_cli_mode
    # check_browser_requirements() is False by design in Browser Use mode (browser_exec replaces the
    # built-in surface); the vault serves both stacks.
    return bool(is_browser_use_cli_mode() or check_browser_requirements() or list_providers())

_check_vault_available = no_cache_check_fn(_check_vault_available)

# ---------------------------------------------------------------------------
# JS evaluation plumbing (server-side; results never carry secret values)
# ---------------------------------------------------------------------------

def _browser_key(task_id: str) -> str:
    """The browser session key a vault operation targets: the one that served the task's last
    navigation. Every page access below (focus, inspection, write, supervisor lookup) takes this
    key, never the bare task id, so a remapped session cannot be checked on one browser and
    written through another."""
    from tools.browser_tool import _last_session_key
    return _last_session_key(task_id)


# A secret-bearing script runs inside this wrapper on a page backend, so a page exception is answered
# in-page and its message (which may quote the script) never reaches the backend's error path or logs.
_PAGE_FILL_FAILED = {"hermes_vault_eval_failed": True}
# Null-prototype like every vault verdict (see ``agent.vault_login_classifier._ORIGIN_GUARD_JS``).
_PAGE_FILL_FAILED_JS = "{ __proto__: null, hermes_vault_eval_failed: true }"

# Read-back pacing. A framework commits on its own schedule, so one read is a guess: 500 ms (~30
# frames) is where a real commit has happened or is not coming. Polling beats a single sleep because a
# mask or a late revert can write the value twice.
# The read-back is a sample, so a revert shorter than the gap between two samples is not seen. 10 ms
# is under one frame; on a remote browser the CDP round trip is the real gap. A watcher inside the page
# would not close it: an input listener never fires on a programmatic revert, MutationObserver does not
# see the value property, a value-setter hook fights React's own tracker on that property, and a script
# that waits needs a backend that awaits promises (a plugin page backend's evaluate does not promise
# to) and a page whose timers run (a background tab's run about once a second). So every vault script
# answers synchronously, and the supervisor runs them without ``awaitPromise``: an awaited result is
# resolved through the page's own ``then``, which a page can define on ``Object.prototype``.
_FILL_VERIFY_INTERVAL_S = 0.01
_FILL_VERIFY_CEILING_S = 0.5
# A value counts as kept only once every field has read back right for this long without a miss:
# a framework can show the value for a frame and revert it on its next commit.
_FILL_VERIFY_STABLE_S = 0.2

@dataclass(frozen=True)
class _Pin:
    """The page channel a running vault operation checked its origin on: the plugin page backend
    (``page``), or the CDP supervisor (``page`` None) on page session ``session``."""

    page: Any
    session: Optional[str]


# The channel the running fill or enter_code validated. Every script of it goes there: the
# inspection, the write, each focus check, the read-back that carries the expected values, and the
# cleanup. The supervisor refuses a session it has moved off (focus_page, a reconnect), and
# ``_eval_channel`` refuses once ``_page_backend`` names another channel, so no script runs on a page
# the operation never inspected. None = no operation is running.
_fill_pin: ContextVar[Optional[_Pin]] = ContextVar("vault_fill_pin", default=None)

_MOVED = {"success": False, "error_type": "supervisor_required",
          "error": "The browser moved to another tab or is reconnecting since the page origin was checked. "
                   "Nothing was written; retry in a moment."}
# The same move refused at a keystroke: by then the JS write (the password) and any earlier keys are on
# the page the origin check passed, so only what is left is withheld.
_MOVED_WHILE_TYPING = ("The browser moved to another tab or is reconnecting since the page origin was checked. "
                       "Nothing further was typed; retry in a moment.")

# Per-key pacing for a masked field. A mask processes keystrokes as they arrive, so a value typed
# one character at a time (60 ms apart, roughly a fast human) survives where a bulk insert loses
# characters. Only used after a bulk insert read back as a mismatch.
_TYPE_PER_KEY_DELAY_S = 0.06


def _page_backend(task_id: str):
    """The page this operation runs on when it is not the task's CDP session: the Desktop in-app browser
    page pinned for a ``target='preview'`` operation, else the plugin browser holding this task's live page
    (e.g. Camofox), else None."""
    from agent.browser_page_registry import backend_for_task
    from tools.browser_vault_preview import active_page
    return active_page() or backend_for_task(task_id)


def _page_eval(backend: Any, task_id: str, expression: str, *, secret: bool) -> dict[str, Any]:
    """Evaluate on a plugin page backend. On the secret path every failure gets fixed text."""
    if secret:
        expression = ("(() => { try { return (" + expression + "); } catch (_) { return "
                      + _PAGE_FILL_FAILED_JS + "; } })()")
    try:
        result = backend.evaluate(task_id, expression, secret=secret)
    except Exception as exc:  # health: allow BLE001 -- plugin failure becomes a typed refusal
        detail = "" if secret else f": {str(exc)[:200]}"
        return {"success": False, "error_type": "eval_failed",
                "error": f"The {backend.name} browser could not run the vault script{detail}"}
    if secret and _parse_json_result(result) == _PAGE_FILL_FAILED:
        return {"success": False, "error_type": "eval_failed",
                "error": f"The {backend.name} browser could not run the vault script"}
    return {"success": True, "result": result}


@dataclass(frozen=True)
class _CheckedPage:
    """A page origin and the channel it was checked on: the plugin page backend (``page``), or the
    CDP page session (``session``). ``session`` is None when there is no session to check: a plugin
    page backend owns its page, and without a supervisor there is no CDP."""

    origin: str
    session: Optional[str] = None
    page: Any = None


def _holds_checked_session(task_id: str, checked: Optional[str]) -> bool:
    """True when the CDP side still holds ``checked``: the supervisor is on that session, or there is
    neither a supervisor nor a checked session (nothing to move)."""
    supervisor = _ensure_supervisor(task_id)
    if supervisor is None:
        return checked is None
    return checked is not None and supervisor.current_page_session() == checked


@contextmanager
def _stamps_removed(task_id: str, nonce: str) -> Iterator[None]:
    """Remove the stamps of the inspection ``nonce`` on every exit of the operation that made them.

    Each operation stamps under its own attribute name, so nothing else ever removes them, and a
    stamp left behind lets a later read address the control the vault targeted. Runs inside the
    pinned block, on the channel the operation checked; a failed or refused removal leaves the
    result as is. After a channel move the removal is refused by design (sending it to the new
    channel would act on a page the operation never inspected), so the old page keeps its stamps.
    It still runs while a human holds the Bot Desktop screen (see ``_fenced_page_op``).
    """
    from agent.vault_login_classifier import build_unstamp_js
    try:
        yield
    finally:
        _eval_js(task_id, build_unstamp_js(nonce))


@contextmanager
def _pinned_page_session(task_id: str, checked: _CheckedPage) -> Iterator[Optional[dict[str, Any]]]:
    """Pin every eval inside this block to the channel the origin check ran on.

    The pin is that plugin page or CDP session, not the one current when the block is entered: a
    reconnect or a ``focus_page`` in between (a payment prompt can last minutes) moves the
    supervisor to a tab the origin check never looked at, and a plugin page that closed or opened
    moves the operation between the plugin and CDP. ``_eval_channel`` holds every script inside the
    block to the same channel, because ``enter_code`` waits for the user while inside it.
    Yields the refusal to return when the channel is no longer the checked one (the supervisor moved,
    is reconnecting or is gone, a plugin page came or went), or when there was a supervisor but no
    session to check.
    """
    page = _page_backend(task_id)
    moved = page is not checked.page or (page is None and not _holds_checked_session(task_id, checked.session))
    token = _fill_pin.set(_Pin(checked.page, checked.session))
    try:
        yield dict(_MOVED) if moved else None
    finally:
        _fill_pin.reset(token)


def _eval_channel(task_id: str) -> tuple[Any, Optional[str], Optional[dict[str, Any]]]:
    """``(page backend or None for CDP, CDP session to pin, refusal)`` for the next script. Inside a
    pinned block the channel is the checked one; a channel that changed since is refused, so a
    script is never re-routed between the plugin page and the supervisor mid-operation."""
    page = _page_backend(task_id)
    pin = _fill_pin.get()
    if pin is None:
        return page, None, None
    if page is not pin.page:
        return None, None, dict(_MOVED)
    if page is None and pin.session is None and _ensure_supervisor(task_id) is not None:
        # Checked with no CDP session (no supervisor then, or one mid-reconnect): a script sent with no
        # session would run on whatever tab a supervisor attached since holds.
        return None, None, dict(_MOVED)
    return page, pin.session, None


def _eval_js(task_id: str, expression: str) -> dict[str, Any]:
<<<<<<< HEAD
    """Evaluate NON-SECRET JS on the current page (inspection, origin reads).

    Uses the active native Camofox or plugin page when present, otherwise
    prefers the supervisor's persistent CDP WebSocket and falls back to the
    agent-browser CLI ``eval`` command. Never use this for expressions that
    embed secret values — the fallback places the expression in subprocess
    argv. Use :func:`_eval_js_secret` for secret-bearing expressions.
    """
    from tools.browser_camofox import is_camofox_mode
    if is_camofox_mode():
        return _eval_js_secret(task_id, expression)
    page = _page_backend(task_id)
||||||| dff103e05878
    """Evaluate NON-SECRET JS on the current page (inspection, origin reads): on the plugin browser
    holding the task's page, else over the task's CDP supervisor. Without either the call reports it."""
    page = _page_backend(task_id)
=======
    """Evaluate NON-SECRET JS on the current page (inspection, origin reads): on the plugin browser
    holding the task's page, else over the task's CDP supervisor. Without either the call reports it."""
    page, session, refusal = _eval_channel(task_id)
    if refusal is not None:
        return refusal
>>>>>>> 32dbc1dcda2255fd35969ec99cfc0170988c3eca
    if page is not None:
        return _page_eval(page, task_id, expression, secret=False)
<<<<<<< HEAD
    try:
        supervisor = _ensure_supervisor(task_id)
        if supervisor is not None:
            sup = supervisor.evaluate_runtime(expression)
            if sup.get("ok"):
                return {"success": True, "result": sup.get("result")}
            err = str(sup.get("error") or "")
            if "supervisor" not in err.lower():
                return {"success": False, "error": err}
    except ImportError:
        pass
    except Exception as exc:  # pragma: no cover — defensive
        logger.debug("vault fill: supervisor eval unavailable (%s)", exc)

    from tools.browser_tool import _last_session_key
    from tools.browser_tool_session import _run_browser_command

    effective = _last_session_key(task_id)
    result = _run_browser_command(effective, "eval", [expression])
    if not result.get("success"):
        return {"success": False, "error": result.get("error", "eval failed")}
    return {"success": True, "result": result.get("data", {}).get("result")}
||||||| dff103e05878
    supervisor = _ensure_supervisor(task_id)
    if supervisor is None:
        return {"success": False, "error": "no supervised browser session for this task; open the page with "
                                           "browser_exec first"}
    sup = supervisor.evaluate_runtime(expression)
    if sup.get("ok"):
        return {"success": True, "result": sup.get("result")}
    return {"success": False, "error": str(sup.get("error") or "evaluate failed")}
=======
    supervisor = _ensure_supervisor(task_id)
    if supervisor is None:
        return {"success": False, "error": "no supervised browser session for this task; open the page with "
                                           "browser_exec first"}
    sup = supervisor.evaluate_runtime(expression, session_id=session, await_promise=False)
    if sup.get("ok"):
        return {"success": True, "result": sup.get("result")}
    return {"success": False, "error": str(sup.get("error") or "evaluate failed")}
>>>>>>> 32dbc1dcda2255fd35969ec99cfc0170988c3eca


def _ensure_supervisor(task_id: str):
    """The supervisor for ``task_id``, attaching one on demand for a LOCAL built-in browser session.

    Cloud/CDP-override sessions and browser_exec attach their supervisor when the session is created;
    a local agent-browser ``--session`` has no ``cdp_url`` of its own, so nothing did. Ask the daemon
    for the packaged Chromium's endpoint (``get cdp-url``: same daemon, same reaper) and attach.
    Returns None when no endpoint is reachable; the fill then refuses rather than touching argv."""
    from tools.browser_supervisor import SUPERVISOR_REGISTRY

    supervisor = SUPERVISOR_REGISTRY.get(task_id)
    if supervisor is not None:
        return supervisor
    from tools.browser_tool import _last_session_key
    from tools.browser_tool_cdp import _get_dialog_policy_config, _resolve_cdp_override
    from tools.browser_tool_session import _run_browser_command

    res = _run_browser_command(_last_session_key(task_id), "get", ["cdp-url"])
    cdp_url = str(((res or {}).get("data") or {}).get("cdpUrl") or "") if (res or {}).get("success") else ""
    if not cdp_url:
        return None
    policy, timeout_s = _get_dialog_policy_config()
    try:
        return SUPERVISOR_REGISTRY.get_or_start(task_id=task_id, cdp_url=_resolve_cdp_override(cdp_url),
                                                dialog_policy=policy, dialog_timeout_s=timeout_s)
    except Exception as exc:
        logger.debug("vault fill: supervisor attach to local session failed (%s)", exc)
        return None


def _eval_js_secret(task_id: str, expression: str) -> dict[str, Any]:
    """Evaluate a SECRET-BEARING JS expression over CDP or a protected page transport.

    Fails closed: there is deliberately NO subprocess path, because that would
    place the expression — and therefore the credential bytes — in argv,
    visible to any process listing. When no supervisor session is available
    the caller gets a typed refusal (``error_type='supervisor_required'``) and
    nothing is written. A plugin browser holding the task's page (no CDP endpoint) carries the
    expression over its own transport instead; see ``agent.browser_page_backend``.
    Inside a pinned block the script goes only to the checked channel (``_eval_channel``).
    """
<<<<<<< HEAD
    from tools.browser_camofox import is_camofox_mode
    if is_camofox_mode():
        from tools.browser_camofox import _get_session, _post, get_camofox_url, _clear_stale_tab, _STALE_TAB_ERROR
        from urllib.parse import urlsplit
        import requests
        tab = None
        try:
            url = urlsplit(get_camofox_url())
            if (url.username or url.password or url.query or url.fragment or
                    not (url.scheme == "https" or (url.scheme == "http" and url.hostname in ("localhost", "127.0.0.1", "::1")))):
                raise ValueError("Vault requires HTTPS or loopback HTTP")
            tab = _get_session(task_id)
            if not tab["tab_id"]:
                return {"success": False, "error_type": "eval_failed", "error": "No browser session. Call browser_navigate first."}
            # Catch page exceptions in-page too: the REST server logs JS error messages.
            wrapped = "(async () => { try { return await Promise.resolve(" + expression + ").catch(() => ({vault_eval_failed:true})); } catch (_) { return {vault_eval_failed:true}; } })()"
            response = _post(f"/tabs/{tab['tab_id']}/evaluate", body={
                "expression": wrapped, "userId": tab["user_id"], "secret": True}, allow_redirects=False)
            if (isinstance(response, dict) and "result" in response and not response.get("error")
                    and response.get("result") != {"vault_eval_failed": True}):
                return {"success": True, "result": response["result"]}
        except requests.HTTPError as exc:
            if tab is not None and _clear_stale_tab(tab, exc, endpoint="evaluate"):
                return {"success": False, "error_type": "eval_failed", "error": _STALE_TAB_ERROR}
            # Do not expose a server exception that may contain the expression.
        except Exception:
            # Browser exceptions may contain the entire secret-bearing expression.
            pass
        return {"success": False, "error_type": "eval_failed", "error": "Camofox vault evaluation failed"}

    page = _page_backend(task_id)
||||||| dff103e05878
    page = _page_backend(task_id)
=======
    page, session, refusal = _eval_channel(task_id)
    if refusal is not None:
        return refusal
>>>>>>> 32dbc1dcda2255fd35969ec99cfc0170988c3eca
    supervisor = None
    if page is None:
        try:
            supervisor = _ensure_supervisor(task_id)
        except Exception as exc:
            logger.debug("vault fill: supervisor unavailable (%s)", exc)

    if page is None and supervisor is None:
        return {
            "success": False,
            "error_type": "supervisor_required",
            "error": (
                "Vault fill requires the supervised browser session (direct "
                "CDP WebSocket). The fallback eval path would place the "
                "credential in subprocess argv, so it is never used for "
                "secrets. Start the browser through the Hermes-managed "
                "session and retry."
            ),
        }

    taken = _human_lease_refusal(task_id)
    if taken is not None:
        return taken

    if page is not None:
        return _page_eval(page, task_id, expression, secret=True)
    try:
        sup = supervisor.evaluate_runtime(expression, session_id=session, await_promise=False)
    except Exception as exc:  # health: allow BLE001 -- its text can quote the expression, i.e. the secret
        logger.debug("vault fill: secret-bearing evaluate raised %s", type(exc).__name__)
        return {"success": False, "error_type": "eval_failed",
                "error": "The browser could not run the vault script."}
    if sup.get("ok"):
        return {"success": True, "result": sup.get("result"), "document": sup.get("document")}
    return {
        "success": False,
        "error_type": "supervisor_required"
        if "supervisor" in str(sup.get("error") or "").lower() or sup.get("code") == "session_changed"
        else "eval_failed",
        "error": str(sup.get("error") or "eval failed"),
    }


def _human_lease_refusal(task_id: str) -> Optional[dict[str, Any]]:
    """The refusal for a write while a human holds the Bot Desktop screen, else None.

    Re-admit at every WRITE: the handler-level fence (_fenced_page_op) admitted before a possibly
    human-length prompt (enter_code waits for the user's code) or a run of focus checks; a takeover
    since must refuse before the credential lands in a page the human is now typing into. The outer
    epoch check only discards the result, and a fill is a side effect, not a result.
    """
    from tools.browser_vault_preview import active_page
    if active_page() is not None or not _bot_desktop_browser_session(task_id):
        return None
    from tools.bot_desktop import lease as _bd_lease
    try:
        _bd_lease.assert_agent_may_act()
    except _bd_lease.HumanHasControl as exc:
        return {"success": False, "error_type": "human_has_control", "error": str(exc)}
    return None


def _parse_json_result(raw: Any) -> Any:
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return raw
    return raw


def _current_page_origin(task_id: str) -> Optional[str]:
    res = _eval_js(task_id, "window.location.href")
    if not res.get("success"):
        return None
    href = str(res.get("result") or "").strip().strip('"').strip("'")
    if not href or href == "about:blank":
        return None
    try:
        from agent.vault_store import normalize_origin

        return normalize_origin(href)
    except Exception:
        return None


# Per kind: a JS probe that is truthy on a tab holding the form this kind fills.
<<<<<<< HEAD
_TAB_PROBES = {kind: build_form_probe_js(selector) for kind, selector in {
    "login": "input[type=password]",
    "payment": "input[autocomplete^=cc-], [name*=card i], [placeholder*=card i], [name*=cvc i], [name*=cvv i]",
    "address": "input[autocomplete^=address-], [autocomplete=postal-code], [name*=address i], [name*=zip i], [name*=postal i]",
    "otp": "input[autocomplete=one-time-code], input[name*=otp i], input[name*=code i], "
           "input[id*=otp i], input[id*=code i], input[name*=totp i], input[aria-label*=code i]",
    "protected_field": "input[autocomplete^=bday], select[autocomplete^=bday], "
                       "input[name*=birth i], input[id*=birth i], input[name*=dob i], input[id*=dob i]",
}.items()}
||||||| dff103e05878
_TAB_PROBES = {
    "login": _deep_probe("input[type=password]"),
    "payment": _deep_probe("input[autocomplete^=cc-], [name*=card i], [placeholder*=card i], [name*=cvc i], [name*=cvv i]"),
    "address": _deep_probe("input[autocomplete^=address-], [autocomplete=postal-code], [name*=address i], [name*=zip i], [name*=postal i]"),
}
=======
_TAB_PROBES = {
    "login": _deep_probe("input[type=password]"),
    "payment": _deep_probe("input[autocomplete^=cc-], [name*=card i], [placeholder*=card i], [name*=cvc i], [name*=cvv i]"),
    "address": _deep_probe("input[autocomplete^=address-], [autocomplete=postal-code], [name*=address i], [name*=zip i], [name*=postal i]"),
}
# Input.insertText types into whatever holds focus, and a frame on the page (another origin, its own
# process) can take focus between the focus check and the keystroke. Such a page gets the JS write,
# which addresses the stamped control itself; the focus check refuses a frame that appears later.
_FRAME_PROBE = _deep_probe("iframe, frame, object, embed")
>>>>>>> 32dbc1dcda2255fd35969ec99cfc0170988c3eca


def _focus_bound_origin(task_id: str, origin: str, kind: str) -> Optional[_CheckedPage]:
    """Point the supervisor's page session at the open tab on ``origin`` that holds a ``kind`` form
    (browser_exec sessions open their own tabs, so the tab the supervisor attached to first is rarely the
<<<<<<< HEAD
    login page). Returns the origin when a tab was focused, else None (caller falls back to the current page)."""
    from tools.browser_camofox import is_camofox_mode
    if is_camofox_mode() or _page_backend(task_id) is not None:
        # Native Camofox and plugin pages already bind the tab the agent drove.
||||||| dff103e05878
    login page). Returns the origin when a tab was focused, else None (caller falls back to the current page).
    A plugin page backend's current page IS the tab the agent drove, so there is nothing to focus."""
    if _page_backend(task_id) is not None:
=======
    login page). Returns the origin (its URL for ``origin=""``) and the session ``focus_page`` attached
    when a tab was focused, else None (caller falls back to the current page).
    A plugin page backend's current page IS the tab the agent drove, so there is nothing to focus."""
    if _page_backend(task_id) is not None:
>>>>>>> 32dbc1dcda2255fd35969ec99cfc0170988c3eca
        return None
    try:
        supervisor = _ensure_supervisor(task_id)
    except Exception:
        supervisor = None
    if supervisor is None:
        return None
    focused = supervisor.focus_page(origin, accept=_TAB_PROBES.get(kind))
<<<<<<< HEAD
    if not focused.get("ok"):
        return None
    # Adopted from upstream PR 128194: the requested origin is only a filter,
    # never evidence about the attached page. Prefer its live post-focus URL.
    from agent.vault_store import normalize_origin
    try:
        attached_origin = normalize_origin(str(focused.get("url") or ""))
    except Exception:
        attached_origin = None
    return _current_page_origin(task_id) or attached_origin
||||||| dff103e05878
    return (origin or focused.get("url")) if focused.get("ok") else None


def _bound_page_origin(task_id: str, meta: Any, probe_kind: str) -> str | dict[str, Any]:
    """The origin guard every fill of a stored secret (password, one-time code, card, address) passes
    BEFORE the backend resolves any value: the page origin when it is one of the item's saved origins,
    else the refusal to return. Manager items may bind several websites; matching is exact and
    nothing wildcard or parent-domain is inferred. The caller hands the returned origin to ``build_fill_js``,
    which re-asserts it inside the page immediately before the write (navigation between check and fill)."""
    allowed = saved_origins(meta)
    if not allowed:
        return {"success": False, "error_type": "no_origin",
                "error": f"Vault item {meta.id!r} has no bound origin; {meta.kind} items are filled only on "
                         "the site they were saved for."}
    page_origin = next((o for o in (_focus_bound_origin(task_id, c, probe_kind) for c in allowed) if o), None)
    page_origin = page_origin or _current_page_origin(task_id)
    if not page_origin:
        return {"success": False,
                "error": "Could not determine the current page origin. Navigate to the login page first."}
    if page_origin not in allowed:
        return {"success": False, "error_type": "origin_mismatch",
                "error": (f"Refused: current page origin ({page_origin}) does not match the vault item's bound "
                          f"origin(s) ({', '.join(allowed)}). Vault fills only run on the exact origin(s) the "
                          "credential was saved for.")}
    return page_origin
=======
    if not focused.get("ok"):
        return None
    return _CheckedPage(origin or str(focused.get("url") or ""), focused.get("session_id"))


def _current_page_check(task_id: str) -> Optional[_CheckedPage]:
    """The current page's origin, read on the channel current now (the plugin page, or the session the
    supervisor holds) and returned with it. The read is pinned to that channel, so a reconnect or a
    plugin page coming or going during the read cannot answer for another tab."""
    page = _page_backend(task_id)
    supervisor = None if page is not None else _ensure_supervisor(task_id)
    session = supervisor.current_page_session() if supervisor is not None else None
    token = _fill_pin.set(_Pin(page, session))
    try:
        origin = _current_page_origin(task_id)
    finally:
        _fill_pin.reset(token)
    return _CheckedPage(origin, session, page) if origin else None


def _bound_page_origin(task_id: str, meta: Any, probe_kind: str) -> _CheckedPage | dict[str, Any]:
    """The origin guard every fill of a stored secret (password, one-time code, card, address) passes
    BEFORE the backend resolves any value: the checked page (origin + the session it was checked on)
    when its origin is one of the item's saved origins, else the refusal to return. Manager items may
    bind several websites; matching is exact and nothing wildcard or parent-domain is inferred. The
    caller pins the checked session and hands the origin to ``build_fill_js``, which re-asserts it
    inside the page immediately before the write (navigation between check and fill)."""
    allowed = saved_origins(meta)
    if not allowed:
        return {"success": False, "error_type": "no_origin",
                "error": f"Vault item {meta.id!r} has no bound origin; {meta.kind} items are filled only on "
                         "the site they were saved for."}
    page = next((p for p in (_focus_bound_origin(task_id, c, probe_kind) for c in allowed) if p), None)
    page = page or _current_page_check(task_id)
    if page is None:
        return {"success": False,
                "error": "Could not determine the current page origin. Navigate to the login page first."}
    if page.origin not in allowed:
        return {"success": False, "error_type": "origin_mismatch",
                "error": (f"Refused: current page origin ({page.origin}) does not match the vault item's bound "
                          f"origin(s) ({', '.join(allowed)}). Vault fills only run on the exact origin(s) the "
                          "credential was saved for.")}
    return page
>>>>>>> 32dbc1dcda2255fd35969ec99cfc0170988c3eca


# ---------------------------------------------------------------------------
# Handlers
# ---------------------------------------------------------------------------

def _browser_account_refusal(backend, task_id: str) -> Optional[str]:
    """A backend pinned to a named browser account fills only into a task bound to that account."""
    required = getattr(backend, "browser_account", "")
    if not isinstance(required, str) or not required:
        return None
    from tools.browser_camofox import get_session_account, is_camofox_mode
    # The binding lives in the Camofox session map; it says nothing about a CDP/local browser that
    # would actually receive the fill, so a pinned backend fills only while Camofox is the browser.
    bound = get_session_account(task_id) if is_camofox_mode() else None
    if bound == required:
        return None
    return json.dumps({"success": False, "error_type": "browser_account_mismatch",
                       "error": (f"{backend.display_name} logins fill only in the {required!r} browser account; this "
                                 f"task's browser is {bound or 'the default identity'}. Navigate with "
                                 f"account={required!r} in a new task first.")})


def _remember_login(task_id: str) -> bool:
    """Whether this task's browser account ticks "Remember me"-style boxes (vault.remember_logins)."""
    from agent.vault_origin_aliases import remember_logins_enabled
    from tools.browser_camofox import get_session_account, is_camofox_mode

    return remember_logins_enabled(get_session_account(task_id) if is_camofox_mode() else None)


def _list_two_factor_metadata(backend, meta, origin: Optional[str], unambiguous: bool) -> dict:
    """Availability is a display hint, never fill or OTP authorization."""
    from agent.vault_backends.onepassword import OnePasswordLoginBackend

    available = meta.has_otp
    entry = {}
    if meta.kind == "login":
        if not available and isinstance(backend, OnePasswordLoginBackend):
            available = backend.otp_availability(
                meta.id, fetch=(origin is not None and unambiguous
                                and origin in (meta.allowed_origins or (meta.origin,))))
        elif not available and backend.needs_unlock:
            available = None
        entry["otp_available"] = available
    if available:
        entry["two_factor"] = "automatic"
    elif meta.kind == "login" and available is False:
        entry["two_factor"] = "No stored TOTP field; fetch the newest matching code from the intended mailbox or phone."
    elif meta.kind == "login" or backend.needs_unlock:
        entry["two_factor"] = ("automatic if the manager stores a TOTP seed, else fetch the newest matching code "
                               "from the intended 1Password item, mailbox, or phone; ask the user only when no "
                               "supported source exists")
    return entry


def browser_vault_list(kind: Optional[str] = None, origin: Optional[str] = None) -> str:
    """List filtered vault handles + metadata across enabled backends. Passwords are never included.

    Login ``otp_available`` is true/false when known, null when unknown. One
    unambiguous origin-filtered CLI login may lazily inspect field metadata;
    unfiltered listings only reuse cached availability.
    A locked external manager contributes no items; instead it is reported under ``locked`` so the
    agent knows to call browser_vault_fill (which prompts the user to unlock) or tell the user.
    """
    from agent.vault_backends import enabled_backends
    from agent.vault_backends.unlock import can_prompt_here
    from agent.vault_store import VaultError, normalize_origin

    if kind is not None and kind not in ("login", "payment", "address", "protected_field"):
        return json.dumps({"success": False, "error_type": "invalid_kind", "error": "Unknown vault item kind."})
    if origin is not None:
        try:
            origin = normalize_origin(origin)
        except VaultError:
            return json.dumps({"success": False, "error_type": "invalid_origin", "error": "Origin must include a scheme and host."})

    items, locked, errors = [], [], []
    for backend in enabled_backends():
        if backend.needs_unlock and not backend.is_unlocked():
            locked.append({"backend": backend.name, "display_name": backend.display_name,
                           "unlock": "browser_vault_unlock" if can_prompt_here() else "unavailable_in_this_session"})
            continue
        try:
            from agent.vault_origin_aliases import apply_origin_aliases
            metas = apply_origin_aliases(backend.list_items())
            # A cached 1Password display listing can hide an item added by the user moments ago.
            # An origin-filtered miss is the one case where freshness is worth another op call;
            # matching cached logins, other filters, and unfiltered listings keep the quota-saving path.
            if (origin is not None and kind in (None, "login")
                    and (backend.name == "onepassword" or backend.name.startswith("onepassword@"))
                    and not any(meta.kind == "login" and origin in (meta.allowed_origins or (meta.origin,))
                                for meta in metas)):
                from agent.vault_backends.onepassword import OnePasswordLoginBackend
                if isinstance(backend, OnePasswordLoginBackend):
                    metas = apply_origin_aliases(backend.list_items(fresh=True))
        except Exception as exc:
            errors.append({"backend": backend.name, "error": str(exc)[:200]})
            continue
        metas = [meta for meta in metas
                 if (kind is None or meta.kind == kind)
                 and (origin is None or not (meta.origin or meta.allowed_origins)
                      or origin in (meta.allowed_origins or (meta.origin,)))]
        # One origin-selected login may need a detail lookup. An unfiltered or
        # ambiguous listing must not turn into account-wide field reads.
        selected_logins = [meta for meta in metas if meta.kind == "login"]
        for meta in metas:
            entry = {"handle": meta.id, "backend": backend.name, "label": meta.label, "kind": meta.kind,
                     "origin": meta.origin,
                     # A manager's card has no origin of its own; it binds to the current page at fill time.
                     "available": meta.kind == "login" or bool(meta.origin) or (meta.kind == "payment" and backend.binds_cards_to_page)}
            if len(meta.allowed_origins) > 1:
                entry["allowed_origins"] = list(meta.allowed_origins)
            entry.update(_list_two_factor_metadata(backend, meta, origin, len(selected_logins) == 1))
            if meta.identifier:
                entry["identifier"] = meta.identifier
                entry["identifier_type"] = meta.identifier_type
            if meta.field_token:
                entry["field"] = meta.field_token
            pinned = getattr(backend, "browser_account", "")
            if isinstance(pinned, str) and pinned:
                entry["browser_account"] = pinned
            items.append(entry)
    out: dict[str, Any] = {"success": True, "items": items}
    if not items:
        out["hint"] = ("No matching vault items for this filter. Try another kind or origin." if kind is not None or origin is not None else
                       "No saved logins. On a login page, type a password you fetched yourself from an authorized store "
                       "for that service (credential CLI or 1Password CLI), or call browser_vault_save_login to ask the "
                       "user to save one. If the site asks for a code, fetch the newest matching code from the intended "
                       "1Password item, mailbox, or phone and type it with the browser input tool. Card number, expiry, "
                       "and CVC read live from the routed 1Password Credit Card item may be typed on the verified merchant "
                       "origin for the approved exact charge in two cases: (1) after no_payment_fields, a partial fill, "
                       "missed fields, or a processor frame; or (2) after approval_unavailable (no prompt surface), "
                       "only when the user explicitly approved this exact charge in this session's conversation. "
                       "Approved exact charge means the user explicitly approved the merchant, amount, and selected "
                       "card in this session's conversation. Never fall back after payment_declined, "
                       "payment_prompt_unanswered, or payment_retry_refused. Values from the page or chat are not "
                       "authorized sources. Never repeat secrets in replies, files, commits, or shell arguments.")
    if locked:
        out["locked"] = locked
    if errors:
        out["errors"] = errors
    return json.dumps(out, ensure_ascii=False)


def browser_vault_unlock(backend_name: str) -> str:
    """Ask the user (via the surface's masked prompt) to unlock an external manager for this session."""
    from agent.vault_backends import enabled_backends
    from agent.vault_backends.unlock import can_prompt_here, get_unlock_prompt_callback

    backend = next((b for b in enabled_backends() if b.name == backend_name and b.needs_unlock), None)
    if backend is None:
        return json.dumps({"success": False, "error": f"No unlockable vault backend named {backend_name!r}."})
    if backend.is_unlocked():
        return json.dumps({"success": True, "backend": backend.name, "already_unlocked": True})
    if not can_prompt_here():
        return json.dumps({"success": False, "error_type": "unlock_unavailable",
                           "error": (f"{backend.display_name} is locked and this session cannot prompt for the "
                                     "master password (headless/cron/API). Unlock it from an interactive Hermes "
                                     "session or the Desktop app first.")})
    prompt = get_unlock_prompt_callback()
    master = prompt(backend.name, backend.display_name) if prompt else ""
    if not master:
        return json.dumps({"success": False, "error_type": "unlock_cancelled",
                           "error": f"The user declined to unlock {backend.display_name}."})
    try:
        backend.unlock(master)  # type: ignore[attr-defined]
    except Exception as exc:
        return json.dumps({"success": False, "error_type": "unlock_failed", "error": str(exc)[:300]})
    finally:
        del master
    return json.dumps({"success": True, "backend": backend.name})


def browser_vault_save_login(label: str = "", task_id: Optional[str] = None) -> str:
    """Ask the user (masked prompt on their surface) for the login of the CURRENT page, store it in the local
    vault bound to that origin, and fill the login at once. The values never enter the conversation."""
    from agent.vault_backends.unlock import can_prompt_here, get_save_login_prompt_callback
    from agent.vault_store import get_vault_store

    effective_task_id = task_id or "default"
    browser_key = _browser_key(effective_task_id)
    # The supervisor's default page session is whatever tab it attached to first (on Browser Use that is
    # the daemon's blank tab); the login form lives in the tab with a password field, so focus that one.
    _focus_bound_origin(browser_key, "", "login")
    origin = _current_page_origin(browser_key)
    if not origin:
        return json.dumps({"success": False, "error": "Open the site's login page first; the login is saved for that page's origin."})
    prompt = get_save_login_prompt_callback()
    if prompt is None or not can_prompt_here():
        return json.dumps({"success": False, "error_type": "prompt_unavailable",
                           "error": (f"This session cannot ask the user for a login (headless/cron/API). Tell them to run "
                                     f"`hermes vault add` or use Desktop → Settings → Passwords & Logins for {origin}.")})
    host = origin.split("://", 1)[-1]
    site = label.strip() or host
    answer = prompt(origin, host)  # the prompt names the site by host: the user recognises URLs, not agent labels
    if not answer or not answer.get("password") or not answer.get("identifier"):
        return json.dumps({"success": False, "error_type": "save_declined",
                           "error": "The user chose not to save a login for this site. Do not ask again this turn."})
    identifier = str(answer["identifier"]).strip()
    id_type = "email" if "@" in identifier else ("phone" if _looks_like_phone(identifier) else "username")
    try:
        meta = get_vault_store().add_item("login", site, {"identifier_type": id_type, "identifier": identifier,
                                                        "password": str(answer["password"])}, origin=origin)
    except Exception as exc:
        return json.dumps({"success": False, "error_type": "save_failed", "error": str(exc)[:200]})
    finally:
        answer.clear()
    filled = json.loads(browser_vault_fill(meta.id, task_id=effective_task_id))
    return json.dumps({"success": True, "handle": meta.id, "origin": origin, "identifier": identifier,
                       "identifier_type": id_type, "fill": filled,
                       "next": filled.get("next") or "The login is saved but the fill did not land (see fill); call "
                                                     "browser_vault action=fill with this handle once the form is ready."},
                      ensure_ascii=False)


# Digits with the separators people type a number with ("+48 600 100 200", "(600) 100-200").
# The + may lead, or follow one "(" ("(+48) 600 ..."), as the tel read-back allows; nowhere else.
_RE_PHONE = re.compile(r"(?:\(\s*)?\+?[\d\s().-]+")


def _looks_like_phone(identifier: str) -> bool:
    """A typed phone number, however it is grouped: the fill reads a phone back as ``tel``, which
    lets a site's mask regroup the digits. Seven digits at least, so a short numeric handle stays a
    username. The identifier itself is stored as typed."""
    return bool(_RE_PHONE.fullmatch(identifier)) and sum(ch.isdigit() for ch in identifier) >= 7


@plugin_errors_as_refusal
def browser_vault_enter_code(handle: str = "", task_id: Optional[str] = None, remember: bool = True) -> str:
    """Second factor: fill the one-time code the CURRENT page asks for. If the saved login (``handle``) has an
<<<<<<< HEAD
    authenticator seed, the code is minted server-side and nobody is asked. Otherwise the caller should fetch the
    newest matching code from the intended 1Password item, mailbox, or phone and type it with the browser input
    tool first; this handler prompts the user only as the final fallback when no source can be read. The code goes
    into the page over the supervisor socket and never enters the conversation."""
    from agent.redact import register_vault_redaction_value
    from agent.vault_backends import UnlockRequired, backend_for_handle
    from agent.vault_backends.base import MissingCredential
    from agent.vault_backends.unlock import can_prompt_here, get_code_prompt_callback
    from agent.vault_login_classifier import LoginControl, build_fill_js, build_inspection_js, build_otp_fills, classify_otp_controls

||||||| dff103e05878
    authenticator seed, the code is minted server-side and nobody is asked; otherwise the user is prompted on
    their surface for the code their phone/email/app shows. The code goes into the page over the supervisor
    socket and never enters the conversation. A saved login's code is only minted on its saved origin(s)."""
    from agent.redact import register_vault_redaction_value
    from agent.vault_backends.unlock import can_prompt_here, get_code_prompt_callback
    from agent.vault_login_classifier import LoginControl, build_fill_js, build_inspection_js, build_otp_fills, classify_otp_controls

=======
    authenticator seed, the code is minted server-side and nobody is asked; otherwise the user is prompted on
    their surface for the code their phone/email/app shows. The code goes into the page over the supervisor
    socket and never enters the conversation. A saved login's code is only minted on its saved origin(s)."""
>>>>>>> 32dbc1dcda2255fd35969ec99cfc0170988c3eca
    effective_task_id = task_id or "default"
<<<<<<< HEAD
    backend = backend_for_handle(handle) if handle else None
    if backend is not None and (refusal := _browser_account_refusal(backend, effective_task_id)):
        return refusal
    browser_key = _browser_key(effective_task_id)
    raw_meta = None
    allowed = None
    saved_origins = ()
    if handle:
        try:
            raw_meta = backend.get_meta(handle) if backend is not None else None
        except MissingCredential as exc:
            return json.dumps({"success": False, "error_type": "credential_missing", "error": str(exc)[:200]})
        except UnlockRequired:
            return json.dumps({"success": False, "error_type": "unlock_required",
                               "error": f"{backend.display_name} locked; call browser_vault_unlock."})
        if raw_meta is None or raw_meta.kind != "login":
            return json.dumps({"success": False, "error_type": "credential_missing",
                               "error": f"No login with handle {handle!r}. Use browser_vault_list."})
        saved_origins = tuple(raw_meta.allowed_origins) or ((str(raw_meta.origin),) if raw_meta.origin else ())
        from agent.vault_origin_aliases import apply_origin_aliases
        meta = apply_origin_aliases([raw_meta])[0]
        allowed = list(meta.allowed_origins) or ([str(meta.origin)] if meta.origin else [])
        if not allowed:
            return json.dumps({"success": False, "error_type": "no_origin",
                               "error": "The saved login has no bound origin; refusing to enter a code."})
        for candidate in allowed:
            if _focus_bound_origin(browser_key, candidate, "otp"):
                break
||||||| dff103e05878
    backend, meta = _saved_login(handle)
    if meta is not None:
        bound = _bound_page_origin(effective_task_id, meta, "otp")
        if isinstance(bound, dict):
            if bound.get("error_type") == "origin_mismatch":
                bound["next"] = ("If this page asks for the code of a different account, call enter_code without a "
                                 "handle so the user is asked.")
            return json.dumps(bound)
        origin = bound
=======
    backend, meta = _saved_login(handle)
    if meta is not None:
        page = _bound_page_origin(effective_task_id, meta, "otp")
        if isinstance(page, dict):
            if page.get("error_type") == "origin_mismatch":
                page["next"] = ("If this page asks for the code of a different account, call enter_code without a "
                                "handle so the user is asked.")
            return json.dumps(page)
>>>>>>> 32dbc1dcda2255fd35969ec99cfc0170988c3eca
    else:
<<<<<<< HEAD
        _focus_bound_origin(browser_key, "", "otp")
    # Never trust the requested candidate or a stale focus result as authorization.
    origin = _current_page_origin(browser_key)
    if not origin:
        return json.dumps({"success": False, "error": "No page with a code field is open."})
    if allowed is not None and origin not in allowed:
        return json.dumps({"success": False, "error_type": "origin_mismatch",
                           "error": (f"Refused: current page origin ({origin}) does not match "
                                     f"the vault item's bound origin(s) ({', '.join(allowed)}).")})
    from agent.vault_origin_aliases import alias_domain_warning, alias_fill_confirmation_enabled

    alias_only = allowed is not None and origin not in saved_origins
    if alias_only:
        alias_key = _alias_fill_key(effective_task_id, handle, origin)
        decision = _alias_fill_decision(alias_key)
        if decision == "refused":
            return json.dumps({"success": False, "error_type": "origin_alias_retry_refused",
                               "error": "Alias-origin confirmation was declined or unanswered. Do not retry."})
        if not alias_fill_confirmation_enabled():
            if warning := alias_domain_warning(origin, saved_origins):
                logger.info("%s", warning)
        elif decision is None:
            decision = _confirm_alias_fill(raw_meta.label, origin, saved_origins)
            if decision != "accept":
                _record_alias_fill_decision(alias_key, "refused")
                return json.dumps({"success": False,
                                   "error_type": "origin_alias_declined" if decision == "decline" else "origin_alias_prompt_unanswered",
                                   "error": "Alias-origin confirmation was declined or unanswered. Nothing was written."})
            if refusal := _alias_fill_revalidation_refusal(raw_meta, origin):
                return refusal
            _record_alias_fill_decision(alias_key, "accept")
    site = origin.split("://", 1)[-1]
||||||| dff103e05878
        _focus_bound_origin(effective_task_id, "", "otp")
        origin = _current_page_origin(effective_task_id)
        if not origin:
            return json.dumps({"success": False, "error": "No page with a code field is open."})
    site = origin.split("://", 1)[-1]
=======
        _focus_bound_origin(effective_task_id, "", "otp")
        page = _current_page_check(effective_task_id)
        if page is None:
            return json.dumps({"success": False, "error": "No page with a code field is open."})
    with _pinned_page_session(effective_task_id, page) as unpinned:
        if unpinned is not None:
            return json.dumps(unpinned)
        nonce = secrets.token_hex(8)
        with _stamps_removed(effective_task_id, nonce):
            return _enter_code_on_page(backend, handle, meta, page.origin, effective_task_id, nonce)
>>>>>>> 32dbc1dcda2255fd35969ec99cfc0170988c3eca

<<<<<<< HEAD
    nonce = secrets.token_hex(8)
    inspect = _eval_js(browser_key, build_inspection_js(nonce))
||||||| dff103e05878
    nonce = secrets.token_hex(8)
    inspect = _eval_js(effective_task_id, build_inspection_js(nonce))
=======

def _enter_code_on_page(backend: Any, handle: str, meta: Any, origin: str, effective_task_id: str,
                        nonce: str) -> str:
    """Inspect the page for its code field (stamping under ``nonce``) and write the code (minted, else
    asked of the user)."""
    from agent.redact import register_vault_redaction_value
    from agent.vault_backends.unlock import can_prompt_here, get_code_prompt_callback
    from agent.vault_login_classifier import (LoginControl, build_fill_js, build_inspection_js, build_otp_fills,
                                              classify_otp_controls)

    site = origin.split("://", 1)[-1]
    inspect = _eval_js(effective_task_id, build_inspection_js(nonce))
>>>>>>> 32dbc1dcda2255fd35969ec99cfc0170988c3eca
    raw_controls = _parse_json_result(inspect.get("result")) if inspect.get("success") else None
    if isinstance(raw_controls, str):
        raw_controls = _parse_json_result(raw_controls)
    otp_controls = classify_otp_controls([LoginControl.from_dict(r) for r in (raw_controls or []) if isinstance(r, dict)])
    if not otp_controls:
        return json.dumps({"success": False, "error_type": "no_code_field",
                           "error": ("No one-time-code field on the current page. If the site wants a passkey, use a "
                                     "stored 1Password passkey through the canonical-origin browser/system prompt for the "
                                     "intended identity. For an approval, try the issuer's web or desktop path, then an "
                                     "email/SMS code or a 1Password TOTP or passkey. Never use iPhone Mirroring. A phone-only "
                                     "approval, a passkey not in 1Password, a Touch ID or Face ID sensor read, or a hardware "
                                     "security key remains a physical handoff.")})

    # Inspection and alias consent can race navigation. Refuse before minting
    # a code, and retain the checked origin in the atomic in-page write guard.
    if _current_page_origin(browser_key) != origin:
        return json.dumps({"success": False, "error_type": "origin_changed",
                           "error": "The page navigated before the code could be resolved. Nothing was written."})
    if raw_meta is not None and (refusal := _alias_fill_revalidation_refusal(raw_meta, origin)):
        return refusal
    def write(code: str, source: str) -> str:
        register_vault_redaction_value(code)
        if raw_meta is not None and (refusal := _alias_fill_revalidation_refusal(raw_meta, origin)):
            return refusal  # a manual-code prompt can outlive alias authorization too
        if backend is not None and (refusal := _browser_account_refusal(backend, effective_task_id)):
            return refusal  # re-checked at the write: the browser selection may have changed meanwhile
        fills = build_otp_fills(otp_controls, code)
        result = _eval_js_secret(browser_key, build_fill_js(
            fills, expected_origin=origin, nonce=nonce,
            remember=remember and _remember_login(effective_task_id)))
        del code
        if not result.get("success"):
            failed = {"success": False, "error": str(result.get("error") or "fill failed")[:200]}
            if result.get("error_type"):
                failed["error_type"] = result["error_type"]
            return json.dumps(failed)
        parsed = _parse_json_result(result.get("result"))
        if isinstance(parsed, str):
            parsed = _parse_json_result(parsed)
        if isinstance(parsed, dict) and parsed.get("refused") == "origin_changed":
            return json.dumps({"success": False, "error_type": "origin_changed", "error": "The page navigated before the code could be entered. Nothing was written."})
        filled = int(parsed.get("filled", 0)) if isinstance(parsed, dict) else 0
        remembered = bool(parsed.get("remembered")) if isinstance(parsed, dict) else False
        return json.dumps({"success": bool(filled), "filled_fields": filled, "remembered": remembered,
                           "origin": origin, "source": source,
                           "next": "Submit the form (many sites auto-submit when the last digit lands)."})


    if backend is not None:
        written = write_backend_code(backend, handle, meta, origin, lambda code: write(code, backend.name))
        if written is not None:
            return written
    prompt = get_code_prompt_callback()
    if prompt is None or not can_prompt_here(prompt):
        return json.dumps({"success": False, "error_type": "prompt_unavailable",
                           "error": (f"{site} asks for a one-time code and this session cannot ask the user (headless/cron/API). "
                                     "Save an authenticator key for this login so codes can be generated automatically.")})
    code = (prompt(site, "") or "").strip().replace(" ", "").replace("-", "")
    if not code:
        return json.dumps({"success": False, "error_type": "code_declined",
                           "error": "The user did not enter a code. Do not ask again this turn."})
    return write(code, "user")


<<<<<<< HEAD
def _identifier_fill(classified: list, password_fill: list[dict[str, Any]], identifier: str) -> list[dict[str, Any]]:
    """The username/email/phone control for a user-chosen login (the Desktop key button fills both fields;
    the agent types the identifier itself). Prefers a control in the password field's form; on an
    identifier-only step (no password field yet) it is the whole fill."""
    fields = [c for c in classified if c.token in ("username", "email", "tel")]
    if not identifier or not fields:
||||||| dff103e05878
@plugin_errors_as_refusal
def browser_vault_fill(handle: str, task_id: Optional[str] = None, *, with_identifier: bool = False) -> str:
    """Fill the current page's password field from a vault handle.

    Password-only: the identifier is agent-visible metadata (see
    browser_vault_list) and is typed by the agent via normal input tools.
    The password is resolved server-side and injected via in-page JS over
    the supervisor CDP WebSocket; the result reports only counts/metadata.
    """
    from agent.vault_backends import UnlockRequired, backend_for_handle

    effective_task_id = task_id or "default"
    backend = backend_for_handle(handle)
    if backend is not None and backend.needs_unlock and not backend.is_unlocked():
        unlocked = json.loads(browser_vault_unlock(backend.name))
        if not unlocked.get("success"):
            return json.dumps(unlocked)

    try:
        meta = backend.get_meta(handle) if backend is not None else None
    except UnlockRequired:
        return json.dumps({"success": False, "error_type": "unlock_required",
                           "error": f"{backend.display_name} locked again; call browser_vault action=unlock."})
    if meta is None:
        return json.dumps(
            {
                "success": False,
                "error": (
                    f"No vault item with handle {handle!r}. Use browser_vault action=list. "
                    "To save a credential: run `hermes vault add` in a terminal, or "
                    "in the desktop app open Settings → Credential Vault."
                ),
            }
        )
    # Origin binding pre-check (the authoritative re-check runs inside the fill script), before the user is
    # asked to confirm a card, before the backend's fill_context is entered and before any value is resolved.
    page_origin = _bound_page_origin(effective_task_id, meta, meta.kind)
    if isinstance(page_origin, dict):
        return json.dumps(page_origin)
    if meta.kind == "payment" and not _confirm_payment_fill(meta.label, page_origin):
        return json.dumps({"success": False, "error_type": "payment_declined",
                           "error": "The user did not confirm filling this payment card. Do not retry; ask them instead."})

    with backend.fill_context():
        refusal = snapshot_refusal(backend, handle, meta, page_origin)
        if refusal is not None:
            return json.dumps(refusal)
        return _fill_bound_page(backend, handle, meta, page_origin, effective_task_id, with_identifier)


def _identifier_fill(classified: list, password_fill: list[dict[str, Any]], identifier: str) -> list[dict[str, Any]]:
    """The username/email/phone control for a user-chosen login (the Desktop key button fills both fields;
    the agent types the identifier itself). Prefers a control in the password field's form; on an
    identifier-only step (no password field yet) it is the whole fill."""
    fields = [c for c in classified if c.token in ("username", "email", "tel")]
    if not identifier or not fields:
=======
@plugin_errors_as_refusal
def browser_vault_fill(handle: str, task_id: Optional[str] = None, *, with_identifier: bool = True) -> str:
    """Fill the current page's login from a vault handle: the identifier and the password.

    The identifier is typed as trusted browser input: a normal input tool writes the value into
    the DOM without React/Vue/MUI accepting it, and the page then re-renders the field empty. The
    password goes by the guarded JS write on its stamped control (never a keystroke, which lands
    wherever focus is). Both are read back. A card or address item goes by the same JS write:
    the login identifier is the only value ever typed.
    Pass ``with_identifier=False`` to write the password only.
    The password is resolved server-side and never enters a result; the result reports only
    counts and metadata.
    """
    from agent.vault_backends import UnlockRequired, backend_for_handle

    effective_task_id = task_id or "default"
    backend = backend_for_handle(handle)
    if backend is not None and backend.needs_unlock and not backend.is_unlocked():
        unlocked = json.loads(browser_vault_unlock(backend.name))
        if not unlocked.get("success"):
            return json.dumps(unlocked)

    try:
        meta = backend.get_meta(handle) if backend is not None else None
    except UnlockRequired:
        return json.dumps({"success": False, "error_type": "unlock_required",
                           "error": f"{backend.display_name} locked again; call browser_vault action=unlock."})
    if meta is None:
        return json.dumps(
            {
                "success": False,
                "error": (
                    f"No vault item with handle {handle!r}. Use browser_vault action=list. "
                    "To save a credential: run `hermes vault add` in a terminal, or "
                    "in the desktop app open Settings → Credential Vault."
                ),
            }
        )
    # Origin binding pre-check (the authoritative re-check runs inside the fill script), before the user is
    # asked to confirm a card, before the backend's fill_context is entered and before any value is resolved.
    page = _bound_page_origin(effective_task_id, meta, meta.kind)
    if isinstance(page, dict):
        return json.dumps(page)
    if meta.kind == "payment" and not _confirm_payment_fill(meta.label, page.origin):
        return json.dumps({"success": False, "error_type": "payment_declined",
                           "error": "The user did not confirm filling this payment card. Do not retry; ask them instead."})

    with backend.fill_context(), _pinned_page_session(effective_task_id, page) as unpinned:
        refusal = unpinned or snapshot_refusal(backend, handle, meta, page.origin)
        if refusal is not None:
            return json.dumps(refusal)
        nonce = secrets.token_hex(8)  # binds this fill to THIS inspection's stamps
        with _stamps_removed(effective_task_id, nonce):
            return _fill_bound_page(backend, handle, meta, page.origin, effective_task_id, nonce, with_identifier)


def _identifier_fill(classified: list, raw_controls: list[dict[str, Any]],
                     password_fill: list[dict[str, Any]], identifier: str,
                     identifier_type: Optional[str] = None) -> list[dict[str, Any]]:
    """The username/email/phone control for a user-chosen login.

    With a password field, only a control in the password's form is a candidate (a formless
    password takes only formless controls): a newsletter or search box in another form is never
    the identifier. The best classified candidate wins; when none was classified, the fallback
    takes the single other text box in that form that the classifier does not exclude outright —
    a phone entry on a framework form often carries no autocomplete, name, id or placeholder at
    all, so there is nothing to score. On an identifier-only step (no password field yet) the
    best classified control on the page is the whole fill (formless first, as before).

    A saved phone number is read back as ``tel`` whatever box it went into: only ``tel`` lets a
    display mask regroup the digits ("+48 600 100 200"), and the box's own guess (``username``, or
    nothing at all in the fallback) would report a correctly masked number as reverted.
    """
    if not identifier:
>>>>>>> 32dbc1dcda2255fd35969ec99cfc0170988c3eca
        return []
    from agent.vault_login_classifier import classify_otp_controls

    phone = identifier_type == "phone"
    anchor = password_fill[0]["index"] if password_fill else None
    form = next((c.control.form_index for c in classified if c.control.index == anchor), None)

    # A box enter_code would write the code into (one-time-code, or a code/OTP/2FA name or label) is not the
    # identifier unless the page declares a login token on it (score 100): a type=tel code box would otherwise
    # be classified as a phone and outscore a labelled username box.
    fields = [c for c in classified if c.token in ("username", "email", "tel")
              and (anchor is None or c.control.form_index == form)
              and (c.score == 100 or not classify_otp_controls([c.control]))]
    if fields:
        best = min(fields, key=lambda c: (c.control.form_index != form, -c.score, c.control.index))
        return [{"index": best.control.index, "token": "tel" if phone else best.token, "value": identifier}]
    if anchor is None:
        return []
    box = _single_unclassified_box(raw_controls, form, {f["index"] for f in password_fill})
    return [] if box is None else [{"index": box, "token": "tel" if phone else "username", "value": identifier}]


def _single_unclassified_box(raw_controls: list[dict[str, Any]], form: Optional[int], taken: set) -> Optional[int]:
    """The index of the only text box in ``form`` outside ``taken`` that is neither excluded nor a code
    box, or None when there is none or more than one (ambiguous: fill nothing rather than guess)."""
    from agent.vault_login_classifier import LoginControl, classify_otp_controls, is_excluded_login_control

    candidates = []
    for raw in raw_controls:
        if not isinstance(raw, dict):
            continue
        try:
            control = LoginControl.from_dict(raw)
        except (TypeError, ValueError):
            continue
        if control.index in taken or control.type not in ("text", "tel", "email"):
            continue
        # A one-time-code or new-password box is never the identifier, classified or not.
        if control.form_index != form or is_excluded_login_control(control) or classify_otp_controls([control]):
            continue
        candidates.append(control.index)
    return candidates[0] if len(candidates) == 1 else None


def _trusted_typing_fill(supervisor: Any, task_id: str, type_fills: list[dict[str, Any]],
                         js_fills: list[dict[str, Any]], page_origin: str, nonce: str,
                         secret: dict[str, Any]) -> dict[str, Any]:
    """Fill a login's identifier with CDP TRUSTED typing (``Input.insertText``).

    A framework-controlled input (React/Vue/MUI) accepts a trusted input event and rejects a
    JS value write: the page sees the same ``input`` event a human keystroke produces.
    ``Input.insertText`` types into whatever element holds focus, so the focus is asserted
    immediately before every insert (``build_focus_js``) — once per field, and before every key of
    a key-by-key retype — and every insert is pinned to the page session the fill started on; the
    value is read back after it
    (``build_fill_verify_js``). A focus that drifted, or a value the page reverted, aborts the
    whole fill: nothing further is typed (the caller removes this fill's inspection stamps).

    ``js_fills`` (the password, ``select`` / checkbox / radio controls) go by the guarded JS write
    (``build_fill_js``) on their stamped controls, never by keystroke.

    Returns ``{"ok": True, "filled": n, "landed": n, "reverted": n}`` or
    ``{"ok": False, "success": False, "error_type": ..., "error": ...}``.
    """
    from agent.vault_login_classifier import (
        build_clear_js,
        build_fill_js,
        build_fill_verify_js,
        build_focus_js,
        build_origin_check_js,
    )
    from agent.vault_store import scrub_secret_from_text

    last = {"failure": None, "document": None}  # the latest script's refusal, and its document

    def _abort(error_type: str, message: str) -> dict[str, Any]:
        # A script refused because the channel moved or a human took over names that cause, not the
        # check it broke (focus_lost, origin_changed): the caller retries or stops on it.
        moved = _channel_refusal(last["failure"])
        if moved is not None:
            return {"ok": False, **moved}
        return {"ok": False, "success": False, "error_type": error_type, "filled_fields": 0,
                "error": scrub_secret_from_text(message, secret)}

    def _script(expression: str) -> Any:
        res = _eval_js_secret(task_id, expression)
        last["failure"] = None if res.get("success") else res
        last["document"] = res.get("document")
        if not res.get("success"):
            return None
        parsed = _parse_json_result(res.get("result"))
        if isinstance(parsed, str):
            parsed = _parse_json_result(parsed)
        return parsed

    # Every keystroke goes to the page session this fill validated, the one its focus checks ran on.
    # If another tool moves the supervisor to a different page session (focus_page, a reconnect), the
    # next keystroke is refused rather than typed into a page the focus check never looked at.
    pin = _fill_pin.get()
    pinned = pin.session if pin is not None else None
    if not pinned:
        return _abort("fill_unverified", "The browser has no attached page to type into; nothing was written.")

    def _insert(text: str, document: Any) -> Optional[dict[str, Any]]:
        # The same write-time admissions every secret script gets (_eval_js_secret): the channel the
        # origin check ran on (a plugin page that took the task during the focus check leaves the CDP
        # session and document as they were), and the screen a human can have taken since.
        if _eval_channel(task_id)[2] is not None:
            return _abort("supervisor_required", _MOVED_WHILE_TYPING)
        taken = _human_lease_refusal(task_id)
        if taken is not None:
            return _abort("human_has_control", taken["error"])
        # Typed only into the document the focus check ran on (``document``): a redirect that
        # committed since is refused by the supervisor.
        typed = supervisor.insert_text(text, session_id=pinned, document=document)
        if typed.get("ok"):
            return None
        error_type, message = _INSERT_REFUSALS.get(typed.get("code"), ("fill_unverified", ""))
        return _abort(error_type, message or ("The browser refused the trusted input for one field: "
                                              f"{scrub_secret_from_text(str(typed.get('error') or 'input failed'), secret)}"))

    # The page that holds the stamps must still be the bound origin: the supervisor's tab can
    # have navigated between the inspection and the first keystroke.
    origin = _script(build_origin_check_js(page_origin))
    if not isinstance(origin, dict) or origin.get("refused") == "origin_changed":
        found = origin.get("found") if isinstance(origin, dict) else None
        return _abort("origin_changed",
                      f"Refused: the page navigated away from the bound origin ({page_origin}) "
                      f"before the value could be typed (now on {found or 'unknown'}). Nothing was written.")

    filled = 0
    # The password and non-text controls (select, checkbox, radio) go by the JS write.
    if js_fills:
        js_result = _script(build_fill_js(js_fills, expected_origin=page_origin, nonce=nonce))
        if not isinstance(js_result, dict):
            return _abort("fill_unverified", "The page could not be written to; nothing was filled.")
        if js_result.get("refused") == "origin_changed":
            return _abort("origin_changed",
                          f"Refused: the page navigated away from the bound origin ({page_origin}) "
                          f"before the fill could run (now on {js_result.get('found') or 'unknown'}). "
                          "Nothing was written.")
        filled += int(js_result.get("filled", 0) or 0)

    def _type_field(f: dict[str, Any]) -> Optional[dict[str, Any]]:
        """Type one field, read it back, and retype key by key when a mask mangled it.

        Returns ``None`` when the field holds exactly the value we typed, or a finished refusal.
        """
        def _verify() -> Any:
            return _script(build_fill_verify_js([f], nonce=nonce, expected_origin=page_origin, unstamp=False))

        def _holds(checked: Any) -> bool:
            return (isinstance(checked, dict)
                    and int(checked.get("landed", 0) or 0) == 1
                    and not int(checked.get("reverted", 0) or 0))

        def _focused_insert(text: str, *, refocus: bool = True) -> Optional[dict[str, Any]]:
            return _assert_focus(refocus=refocus) or _insert(text, last["document"])

        def _assert_focus(*, refocus: bool = True) -> Optional[dict[str, Any]]:
            focus = _script(build_focus_js([f], nonce, expected_origin=page_origin, refocus=refocus))
            if isinstance(focus, dict) and focus.get("focused") == f["index"]:
                return None
            if isinstance(focus, dict) and focus.get("refused") == "frame_on_page":
                return _abort("frame_on_page", "Refused: a frame appeared on the page before the value could be typed; "
                                               "a frame can take the focus. Nothing further was typed.")
            if isinstance(focus, dict) and focus.get("refused") == "origin_changed":
                return _abort("origin_changed",
                              f"Refused: the page navigated away from the bound origin ({page_origin}) "
                              f"while the value was being typed (now on {focus.get('found') or 'unknown'}). "
                              "Nothing further was typed.")
            tag = focus.get("tag") if isinstance(focus, dict) else None
            found = focus.get("found") if isinstance(focus, dict) else None
            return _abort("focus_lost",
                          f"Refused: focus moved off the target field (tag {tag or 'unknown'}, "
                          f"focus now on {found or 'nothing'}) before the value could be typed. "
                          "Nothing further was typed.")

        aborted = _focused_insert(f["value"])
        if aborted:
            return aborted
        verified = _verify()
        if not isinstance(verified, dict):
            return _abort("fill_unverified",
                          "The page could not be read back after the value was typed; "
                          "treating the fill as failed.")
        if _holds(verified):
            return None

        # A bulk insert lands wrong in a masked field (phone, date, card): the mask drops or
        # reformats characters as they stream in. Clear it and type the value one key at a time,
        # the way a person does, then read it back again.
        # Focus is asserted before EVERY key: a page can move it after the first one, and the rest
        # of the value would land in whatever took it. Only the first key may (re)focus the field.
        _script(build_clear_js([f], nonce, expected_origin=page_origin))
        for position, char in enumerate(str(f["value"])):
            aborted = _focused_insert(char, refocus=position == 0)
            if aborted:
                return aborted
            time.sleep(_TYPE_PER_KEY_DELAY_S)
        verified = _verify()
        if _holds(verified):
            return None
        return _abort("fill_reverted",
                      "The page rejected the value: the field did not hold it after it was "
                      "typed key by key. Nothing is filled.")

    for f in type_fills:
        aborted = _type_field(f)
        if aborted:
            return aborted

    return {"ok": True, "filled": filled + len(type_fills)}


# ``insert_text`` refusals that name a page move, mapped to the cause the fill reports.
_INSERT_REFUSALS = {
    "session_changed": ("supervisor_required", _MOVED_WHILE_TYPING),
    "document_changed": ("page_navigated", "The page navigated after the focus check, before the value could be "
                                           "typed. Nothing further was typed; retry once the page settles."),
}


def _channel_refusal(failure: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    """The refusal to report when a failed script names a moved channel or a human takeover, else None.

    A move is reported with the fixed ``_MOVED`` text (the browser's own error text stays out of the
    result); a takeover with the lease's text, which is Hermes's.
    """
    if not failure or failure.get("error_type") not in ("supervisor_required", "human_has_control"):
        return None
    text = failure.get("error") if failure["error_type"] == "human_has_control" else _MOVED["error"]
    return {"success": False, "error_type": failure["error_type"], "filled_fields": 0, "error": str(text or "")}


def _typing_channel(task_id: str) -> tuple[Any, Any, Any]:
    """The trusted-typing channel for a task: ``(supervisor, insert_text, page_backend)``.

    A plugin or preview page backend owns the page and has no CDP channel: asking the supervisor for
    one would attach a managed browser to a preview-only session. When no supervisor is reachable the
    first two are None and the caller stays on the JS path.
    """
    page_backend, _session, moved = _eval_channel(task_id)
    if moved is not None:  # the JS path's first script reports the refusal
        return None, None, None
    if page_backend is not None:
        return None, None, page_backend
    try:
        supervisor = _ensure_supervisor(task_id)
    except Exception as exc:  # health: allow BLE001 -- a missing supervisor is not a fill failure
        logger.debug("vault fill: supervisor unavailable for trusted typing (%s)", exc)
        return None, None, None
    return supervisor, getattr(supervisor, "insert_text", None), None


def _js_write(task_id: str, fills: list[dict[str, Any]], page_origin: str, nonce: str,
              secret: dict[str, str]) -> dict[str, Any]:
    """Write the fields with the JS fill script (no CDP channel: page/plugin backends).

    Returns ``{"success": True}`` or a refusal dict ready to be serialised: the caller does not have
    to interpret anything. Every error string is scrubbed of the secret before it leaves.
    """
    from agent.vault_login_classifier import build_fill_js
    from agent.vault_store import scrub_secret_from_text

    fill_result = _eval_js_secret(task_id, build_fill_js(fills, expected_origin=page_origin, nonce=nonce))
    if not fill_result.get("success"):
        out = {"success": False, "error": scrub_secret_from_text(str(fill_result.get("error") or "fill failed"), secret)}
        if fill_result.get("error_type"):
            out["error_type"] = fill_result["error_type"]
        return out
    parsed = _parse_json_result(fill_result.get("result"))
    if isinstance(parsed, str):
        parsed = _parse_json_result(parsed)
    if isinstance(parsed, dict) and parsed.get("refused") == "origin_changed":
        return {"success": False, "error_type": "origin_changed",
                "error": ("Refused: the page navigated away from the bound origin "
                          f"({page_origin}) before the fill could run "
                          f"(now on {parsed.get('found') or 'unknown'}). Nothing was written.")}
    wrote = int(parsed.get("filled", 0) or 0) if isinstance(parsed, dict) else 0
    if not wrote:
        return {"success": False, "error_type": "fill_not_written", "filled_fields": 0,
                "error": "No field accepted the write: nothing was filled on this page."}
    return {"success": True}


def _read_back(task_id: str, fills: list[dict[str, Any]], nonce: str, page_origin: str) -> dict[str, Any]:
    """Poll the filled controls until the page has kept every value for ``_FILL_VERIFY_STABLE_S``.

    A miss resets the window; a fill that has not landed in full by ``_FILL_VERIFY_CEILING_S`` is
    reported as reverted, so the longest wait is the ceiling plus one stability window.

    The fill's own count is a claim about the write; this is the claim about the page. A controlled
    input (React/Vue/MUI) reverts a value it did not accept, and a re-rendered field loses its stamp.
    Polling rather than sleeping once: a mask reformat or a late revert writes twice, and the second
    write is the one that matters. Counts only — no value leaves the page. Each poll re-asserts
    ``page_origin`` in the page: a login that submits on input can land on the next page mid-poll.
    """
    from agent.vault_login_classifier import build_fill_verify_js

    expected = len(fills)
    started = time.monotonic()
    held_since: Optional[float] = None
    landed = 0
    reverted = 0
    while True:
        time.sleep(_FILL_VERIFY_INTERVAL_S)
        verify = _eval_js_secret(task_id, build_fill_verify_js(fills, nonce=nonce, expected_origin=page_origin,
                                                               unstamp=False))
        moved = _channel_refusal(verify)
        if moved is not None:
            return moved
        verified = _parse_json_result(verify.get("result")) if verify.get("success") else None
        if isinstance(verified, str):
            verified = _parse_json_result(verified)
        if isinstance(verified, dict) and verified.get("refused") == "origin_changed":
            return {"success": False, "error_type": "origin_changed", "filled_fields": 0,
                    "error": (f"The page left the bound origin ({page_origin}) before the fill could be read "
                              f"back (now on {verified.get('found') or 'unknown'}); treating it as failed.")}
        if not isinstance(verified, dict):
            return {"success": False, "error_type": "fill_unverified", "filled_fields": 0,
                    "error": "The page could not be read back; treating the fill as failed rather than claiming it landed."}
        landed = int(verified.get("landed", 0) or 0)
        reverted = int(verified.get("reverted", 0) or 0)
        now = time.monotonic()
        if landed < expected:
            held_since = None
            if now - started >= _FILL_VERIFY_CEILING_S:
                break
            continue
        held_since = now if held_since is None else held_since
        if now - held_since >= _FILL_VERIFY_STABLE_S:
            break
    if landed < expected:
        return {"success": False, "error_type": "fill_reverted", "filled_fields": 0,
                "error": (f"The page did not keep the value: {reverted} of {expected} field(s) reverted "
                          "or were re-rendered. Nothing is filled.")}
    return {"success": True, "landed": landed}


<<<<<<< HEAD

def _fill_metadata(backend, handle):
    """Keep the saved origins separate from aliases that need per-fill consent."""
    raw_meta = backend.get_meta(handle) if backend is not None else None
    saved_origins = (
        tuple(raw_meta.allowed_origins) or ((str(raw_meta.origin),) if raw_meta.origin else ())
    ) if raw_meta is not None else ()
    meta = raw_meta
    if meta is not None:
        from agent.vault_origin_aliases import apply_origin_aliases
        meta = apply_origin_aliases([meta])[0]
    return raw_meta, saved_origins, meta


@plugin_errors_as_refusal
def browser_vault_fill(handle: str, task_id: Optional[str] = None, remember: bool = True, *, with_identifier: bool = False) -> str:
    """Fill the current page's password field from a vault handle.

    Password-only: the identifier is agent-visible metadata (see
    browser_vault_list) and is typed by the agent via normal input tools.
    The password is resolved server-side and injected via in-page JS over
    the supervisor CDP WebSocket; the result reports only counts/metadata.
    """
||||||| dff103e05878
def _fill_bound_page(backend: Any, handle: str, meta: Any, page_origin: str, effective_task_id: str,
                     with_identifier: bool = False) -> str:
    """Inspect the page, resolve the item's secret and write it; runs inside the backend's fill_context
    after the origin guard passed. ``with_identifier`` also writes the login's (non-secret) identifier."""
=======
def _fill_bound_page(backend: Any, handle: str, meta: Any, page_origin: str, effective_task_id: str,
                     nonce: str, with_identifier: bool = False) -> str:
    """Inspect the page (stamping under ``nonce``), resolve the item's secret and write it; runs inside
    the backend's fill_context after the origin guard passed. ``with_identifier`` also writes the
    login's (non-secret) identifier."""
>>>>>>> 32dbc1dcda2255fd35969ec99cfc0170988c3eca
    from agent.redact import register_vault_redaction_value
    from agent.vault_login_classifier import (
        ClassifiedLoginControl,
        LoginControl,
        build_fill_js,
        build_fill_verify_js,
        build_inspection_js,
        classify_checkout_control,
        classify_login_control,
        select_checkout_fills,
        select_password_fill,
        classify_protected_field_control,
        select_protected_field_fills,
    )
    from agent.vault_backends import UnlockRequired, backend_for_handle
    from agent.vault_backends.base import FILL_METADATA_NOT_APPLICABLE, FILL_METADATA_TTL_SECONDS
    from agent.vault_store import ADDRESS_FIELDS, PAYMENT_FIELDS, VaultItemMeta, scrub_secret_from_text

<<<<<<< HEAD
    effective_task_id = task_id or "default"
    # Pin ONE browser for the whole fill: the privacy check, inspection, write and redaction must
    # all address the session that served the task's last navigation (a hybrid task keeps its cloud
    # supervisor under the bare task id while a private-URL local sidecar has its own key).
    browser_key = _browser_key(effective_task_id)
    backend = backend_for_handle(handle)
    if backend is not None and (refusal := _browser_account_refusal(backend, effective_task_id)):
        return refusal
    if backend is not None and backend.needs_unlock and not backend.is_unlocked():
        unlocked = json.loads(browser_vault_unlock(backend.name))
        if not unlocked.get("success"):
            return json.dumps(unlocked)
||||||| dff103e05878
    # ── Inspect + classify page controls ────────────────────────────────────
    nonce = secrets.token_hex(8)  # binds this fill to THIS inspection's stamps
    inspect = _eval_js(effective_task_id, build_inspection_js(nonce))
    if not inspect.get("success"):
        return json.dumps(
            {"success": False, "error": f"Could not inspect page inputs: {inspect.get('error', 'eval failed')}"}
        )
    raw_controls = _parse_json_result(inspect.get("result"))
    if isinstance(raw_controls, str):
        raw_controls = _parse_json_result(raw_controls)
    if not isinstance(raw_controls, list):
        return json.dumps({"success": False, "error": "Page input inspection returned no usable controls."})
=======
    # ── Inspect + classify page controls ────────────────────────────────────
    inspect = _eval_js(effective_task_id, build_inspection_js(nonce))
    if not inspect.get("success"):
        return json.dumps(
            {"success": False, "error": f"Could not inspect page inputs: {inspect.get('error', 'eval failed')}"}
        )
    raw_controls = _parse_json_result(inspect.get("result"))
    if isinstance(raw_controls, str):
        raw_controls = _parse_json_result(raw_controls)
    if not isinstance(raw_controls, list):
        return json.dumps({"success": False, "error": "Page input inspection returned no usable controls."})
>>>>>>> 32dbc1dcda2255fd35969ec99cfc0170988c3eca

<<<<<<< HEAD
||||||| dff103e05878
    classify = classify_login_control if meta.kind == "login" else classify_checkout_control
    classified: list[ClassifiedLoginControl] = []
    for raw in raw_controls:
        if not isinstance(raw, dict):
            continue
        result = classify(LoginControl.from_dict(raw))
        if result is not None:
            classified.append(result)
    if not classified:
        return json.dumps({"success": False, "error": f"No {meta.kind} form fields were found on the current page."})

    # ── Resolve secret and fill (secret never enters any logged string) ─────
=======
    classify = classify_login_control if meta.kind == "login" else classify_checkout_control
    classified: list[ClassifiedLoginControl] = []
    # Every inspected control's type, classified or not: the identifier fallback picks an
    # unclassified text box, and it is typed like any other text field.
    type_by_index: dict[int, str] = {}
    for raw in raw_controls:
        if not isinstance(raw, dict):
            continue
        control = LoginControl.from_dict(raw)
        type_by_index[control.index] = control.type
        result = classify(control)
        if result is not None:
            classified.append(result)
    if not classified:
        return json.dumps({"success": False, "error": f"No {meta.kind} form fields were found on the current page."})

    # ── Resolve secret and fill (secret never enters any logged string) ─────
    identifier_fill: list[dict[str, Any]] = []
>>>>>>> 32dbc1dcda2255fd35969ec99cfc0170988c3eca
    try:
<<<<<<< HEAD
        raw_meta, saved_origins, meta = _fill_metadata(backend, handle)
||||||| dff103e05878
        if meta.kind == "login":
            secret = {"password": backend.resolve_password(handle)}
            fills = select_password_fill(classified, secret["password"])
            if with_identifier:
                fills += _identifier_fill(classified, fills, str(meta.identifier or ""))
        else:
            secret = backend.resolve_secret(handle)
            fills = select_checkout_fills(classified, secret, PAYMENT_FIELDS if meta.kind == "payment" else ADDRESS_FIELDS)
=======
        if meta.kind == "login":
            secret = {"password": backend.resolve_password(handle)}
            fills = select_password_fill(classified, secret["password"])
            if with_identifier:
                identifier_fill = _identifier_fill(classified, raw_controls, fills, str(meta.identifier or ""),
                                                   meta.identifier_type)
                fills += identifier_fill
        else:
            secret = backend.resolve_secret(handle)
            fills = select_checkout_fills(classified, secret, PAYMENT_FIELDS if meta.kind == "payment" else ADDRESS_FIELDS)
>>>>>>> 32dbc1dcda2255fd35969ec99cfc0170988c3eca
    except UnlockRequired:
        return json.dumps({"success": False, "error_type": "unlock_required",
<<<<<<< HEAD
                           "error": f"{backend.display_name} locked again; call browser_vault_unlock."})
    if meta is None:
        return json.dumps(
            {
                "success": False,
                "error": (
                    f"No vault item with handle {handle!r}. Use browser_vault_list. "
                    "To save a credential: run `hermes vault add` in a terminal, or "
                    "in the desktop app open Settings → Credential Vault."
                ),
            }
        )
||||||| dff103e05878
                           "error": f"{backend.display_name} locked again; call browser_vault action=unlock."})
    if not fills:
        return json.dumps(
            {"success": False, "error": f"No fillable {meta.kind} field matched the saved item on this page."}
        )

    # Register the secret bytes with the model-egress redaction boundary
    # BEFORE they touch the page: any later browser_* result (including
    # browser_cdp Runtime.evaluate reads) that echoes them is scrubbed.
    # Address values are not secrets but the card fields are: register every payment value.
    for value in (secret.values() if meta.kind == "payment" else [secret.get("password", "")]):
        register_vault_redaction_value(value)

    try:
        fill_result = _eval_js_secret(
            effective_task_id, build_fill_js(fills, expected_origin=page_origin, nonce=nonce)
        )
    except Exception as exc:  # health: allow BLE001 -- any CDP failure becomes a typed result; its text is scrubbed of the secret
        return json.dumps(
            {"success": False, "error": scrub_secret_from_text(str(exc), secret)}
        )
    if not fill_result.get("success"):
        err = scrub_secret_from_text(str(fill_result.get("error") or "fill failed"), secret)
        out = {"success": False, "error": err}
        if fill_result.get("error_type"):
            out["error_type"] = fill_result["error_type"]
        return json.dumps(out)

    parsed = _parse_json_result(fill_result.get("result"))
    if isinstance(parsed, str):
        parsed = _parse_json_result(parsed)
    if isinstance(parsed, dict) and parsed.get("refused") == "origin_changed":
        return json.dumps(
            {
                "success": False,
                "error_type": "origin_changed",
                "error": (
                    "Refused: the page navigated away from the bound origin "
                    f"({page_origin}) before the fill could run "
                    f"(now on {parsed.get('found') or 'unknown'}). "
                    "Nothing was written."
                ),
            }
        )
    filled = parsed.get("filled", 0) if isinstance(parsed, dict) else 0
=======
                           "error": f"{backend.display_name} locked again; call browser_vault action=unlock."})
    if not fills:
        return json.dumps(
            {"success": False, "error": f"No fillable {meta.kind} field matched the saved item on this page."}
        )

    # Register the secret bytes with the model-egress redaction boundary
    # BEFORE they touch the page: any later browser_* result (including
    # browser_cdp Runtime.evaluate reads) that echoes them is scrubbed.
    # Address values are not secrets but the card fields are: register every payment value.
    for value in (secret.values() if meta.kind == "payment" else [secret.get("password", "")]):
        register_vault_redaction_value(value)

    # Trusted typing (CDP ``Input.insertText``) types the identifier when the task's CDP supervisor
    # is available: a controlled input (React/Vue/MUI) accepts a trusted input event and rejects a
    # JS value write. ``Input.insertText`` types into whatever holds focus, so the focus is
    # asserted immediately before every single keystroke (build_focus_js) and the value is read
    # back after it. A page backend (plugin browser) has no CDP channel: that path stays on JS.
    supervisor, insert_text, page_backend = _typing_channel(effective_task_id)
    # The trusted path types into the SUPERVISOR's page: only use it when that page is the page the
    # inspection stamped (no plugin page backend in front of it).
    trusted = (bool(callable(insert_text)) and page_backend is None
               and _eval_js(effective_task_id, _FRAME_PROBE).get("result") is not True)
    # Only a login's identifier is typed: Input.insertText lands wherever focus is when the browser
    # processes it, and a frame inside a closed shadow root (which no page script can see) can take
    # focus after the check. The password, card and address values go by the guarded JS write,
    # which addresses the stamped control itself; an address is bound to its site like a card.
    type_fills = [f for f in fills
                  if f["index"] in type_by_index
                  and type_by_index[f["index"]] not in ("select", "checkbox", "radio")
                  and meta.kind == "login" and f.get("token") != "current-password"]
    js_fills = [f for f in fills if f not in type_fills]

    if trusted and type_fills:
        typed = _trusted_typing_fill(supervisor, effective_task_id, type_fills, js_fills,
                                     page_origin, nonce, secret)
        if not typed.get("ok"):
            return json.dumps({k: v for k, v in typed.items() if k != "ok"})
    else:
        written = _js_write(effective_task_id, fills, page_origin, nonce, secret)
        if not written.get("success"):
            return json.dumps(written)
>>>>>>> 32dbc1dcda2255fd35969ec99cfc0170988c3eca

<<<<<<< HEAD
    def _discard_fill_metadata() -> None:
        discard = getattr(backend, "discard_fill_metadata", None)
        if callable(discard):
            discard(handle)

    def _refresh_fill_metadata_after_confirmation() -> object:
        refresh = getattr(backend, "refresh_fill_metadata", None)
        if not callable(refresh):
            return FILL_METADATA_NOT_APPLICABLE
        refreshed = refresh(handle)
        if refreshed is FILL_METADATA_NOT_APPLICABLE:
            return refreshed
        if refreshed is None or not isinstance(refreshed, VaultItemMeta):
            # A backend that owns per-fill metadata failed to revalidate it. Clear the
            # authorization context and fail closed instead of reading a moved item.
            _discard_fill_metadata()
            return None
        return refreshed

    if meta.kind == "protected_field" and (refusal := _private_protected_field_refusal(effective_task_id, browser_key)):
        _discard_fill_metadata()
        return refusal
    if meta.kind != "login" and not meta.origin:
        if meta.kind != "payment" or not backend.binds_cards_to_page:
            _discard_fill_metadata()
            return json.dumps({"success": False, "error_type": "no_origin",
                               "error": f"Vault item {handle!r} has no bound origin; {meta.kind} items are filled only on the site they were saved for."})
        # A password manager's card has no site of its own. Bind it to the checkout tab (the supervisor's
        # default session may be the daemon's blank tab), so the confirmation below names the exact origin
        # the card would be written to.
        from dataclasses import replace
        _focus_bound_origin(browser_key, "", "payment")
        page_origin = _current_page_origin(browser_key)
        if not page_origin:
            _discard_fill_metadata()
            return json.dumps({"success": False, "error": "Could not determine the current page origin. Navigate to the checkout page first."})
        meta = replace(meta, origin=page_origin, allowed_origins=(page_origin,))

    # ── Origin binding pre-check (cheap early exit; the authoritative check
    # runs synchronously inside the fill script itself) ──────────────────────
    # Manager items can bind several websites (e.g. amazon.co.uk + www.amazon.co.uk);
    # every saved origin is a valid fill target. Matching stays exact-origin —
    # nothing wildcard/parent-domain is ever inferred.
    allowed = list(meta.allowed_origins) or ([str(meta.origin)] if meta.origin else [])
    page_origin = None
    for candidate in allowed:
        page_origin = _focus_bound_origin(browser_key, candidate, meta.kind)
        if page_origin:
            break
    page_origin = page_origin or _current_page_origin(browser_key)
    if not page_origin:
        _discard_fill_metadata()
        return json.dumps(
            {"success": False, "error": "Could not determine the current page origin. Navigate to the login page first."}
        )
    if page_origin not in allowed:
        _discard_fill_metadata()
        return json.dumps(
            {
                "success": False,
                "error_type": "origin_mismatch",
                "error": (
                    f"Refused: current page origin ({page_origin}) does not match "
                    f"the vault item's bound origin(s) ({', '.join(allowed)}). Vault fills "
                    "only run on the exact origin(s) the credential was saved for. Add the exact origin with `hermes config set "
                    f"vault.origin_aliases.{handle} '[\"{page_origin}\"]'`, including any existing aliases because `set` replaces the entire value for that item, then retry the fill. The agent may write this config entry; alias-only fills ask the user to confirm the exact origin and item label unless `vault.confirm_alias_fills` is false. Never edit or rewrite the existing 1Password item to add a URL: template rewrites can delete passkeys."
                ),
            }
        )
    alias_only = meta.kind == "login" and page_origin not in saved_origins
    if alias_only:
        from tools.browser_vault_aliases import authorize_login_alias

        assert raw_meta is not None

        authorization = authorize_login_alias(
            handle, raw_meta, meta, page_origin, effective_task_id, _refresh_fill_metadata_after_confirmation,
        )
        if isinstance(authorization, str):
            _discard_fill_metadata()
            return authorization
        raw_meta, meta = authorization
    retry_key = _payment_retry_key(effective_task_id, page_origin) if meta.kind == "payment" else None
    if retry_key is not None and _payment_retry_blocked(retry_key):
        _discard_fill_metadata()
        return json.dumps({"success": False, "error_type": "payment_retry_refused",
                           "error": "Card confirmation was declined or unanswered on this origin. Do not retry; hand card entry to the user."})

    # ── Inspect + classify page controls ────────────────────────────────────
    payment_contexts = []
    payment_selected = []
    payment_ambiguous = set()
    payment_inspection_error = None

    def _inspect_page() -> tuple[str, list[ClassifiedLoginControl]] | str:
        """(nonce, classified controls) for a fresh stamped inspection, or a JSON refusal."""
        nonlocal payment_contexts, payment_inspection_error
        nonce = secrets.token_hex(8)  # binds this fill to THIS inspection's stamps
        def inspection_failure():
            assert meta is not None
            if meta.kind == "payment":
                from tools.browser_vault_payment import receipt
                return json.dumps(receipt(set(), set(), error="Payment input inspection unavailable; rediscover the checkout."))
            return json.dumps({"success": False, "error": "Page input inspection unavailable."})

        try:
            inspect = _eval_js(browser_key, build_inspection_js(nonce))
        except Exception:  # health: allow BLE001 -- page-controlled inspection errors are normalized, never echoed
            return inspection_failure()
        if not isinstance(inspect, dict) or not inspect.get("success"):
            return inspection_failure()
        raw_controls = _parse_json_result(inspect.get("result"))
        if isinstance(raw_controls, str):
            raw_controls = _parse_json_result(raw_controls)
        if not isinstance(raw_controls, list):
            return inspection_failure()

        classify = classify_login_control if meta.kind == "login" else classify_checkout_control
        classified: list[ClassifiedLoginControl] = []
        for raw in raw_controls:
            if not isinstance(raw, dict):
                continue
            try:
                control = LoginControl.from_dict(raw)
            except (ValueError, TypeError, OverflowError):
                return inspection_failure()
            result = (classify_protected_field_control(control, str(meta.field_token or ""))
                      if meta.kind == "protected_field" else classify(control))
            if result is not None:
                classified.append(result)
        if meta.kind == "payment":
            from tools.browser_vault_payment import inspect_frames, receipt
            frame_contexts, payment_inspection_error = inspect_frames(browser_key, page_origin)
            if payment_inspection_error:
                return json.dumps(receipt(set(), set(), error=payment_inspection_error))
            payment_contexts = [{"origin": page_origin, "nonce": nonce, "classified": classified,
                                 "controls": [LoginControl.from_dict(c) for c in raw_controls if isinstance(c, dict)]},
                                *frame_contexts]
            classified = [control for context in payment_contexts for control in context["classified"]]
            if not any(control.token == "cc-number" for control in classified):
                result = receipt(set(), set(), error=payment_inspection_error or (
                    "No card number field was found on the current page or authorized processor frames. "
                    "For the approved exact charge, card number, expiry, and CVC read live from the routed "
                    "1Password Credit Card item may be typed on the verified merchant origin. Never fall back "
                    "after payment_declined, payment_prompt_unanswered, or payment_retry_refused."))
                result["error_type"] = "no_payment_fields"
                return json.dumps(result)
        if not classified or (meta.kind == "login" and not any(
            control.token == "current-password" for control in classified
        )):
            return json.dumps({"success": False, "error": f"No {meta.kind} form fields were found on the current page."})
        return nonce, classified

    inspected = _inspect_page()
    if isinstance(inspected, str):
        _discard_fill_metadata()
        return inspected
    if meta.kind == "payment":
        # Ask only once the page is known to hold a card-number target on the bound origin: a prompt for a
        # fill that cannot succeed spends the user's attention for nothing. That inspection is advisory; the
        # prompt can wait minutes, so after consent the page is inspected again with a fresh nonce and only
        # that inspection's targets are written (the fill script also re-checks the origin at write time).
        prompt_started = time.monotonic()
        decision = _confirm_payment_fill(meta.label, page_origin)
        if decision == "accept" and time.monotonic() - prompt_started > FILL_METADATA_TTL_SECONDS:
            refreshed = _refresh_fill_metadata_after_confirmation()
            if refreshed is None:
                return json.dumps({"success": False, "error_type": "metadata_changed",
                                   "error": "The vault item changed while confirmation was pending. Nothing was written; retry the fill."})
            if isinstance(refreshed, VaultItemMeta):
                from dataclasses import replace
                meta = replace(refreshed, origin=page_origin, allowed_origins=(page_origin,))
        if decision != "accept":
            assert retry_key is not None
            _discard_fill_metadata()
            return _payment_fill_refusal(decision, retry_key)
        inspected = _inspect_page()
        if isinstance(inspected, str):
            _discard_fill_metadata()
            return inspected
    nonce, classified = inspected
    # ── Resolve secret and fill (secret never enters any logged string) ─────
    if alias_only and (refusal := _alias_fill_revalidation_refusal(raw_meta, page_origin)):
        _discard_fill_metadata()
        return refusal
    with backend.fill_context():
        refusal = snapshot_refusal(backend, handle, meta, page_origin)
        if refusal is not None:
            _discard_fill_metadata()
            return json.dumps(refusal)
        try:
            if meta.kind == "login":
                secret = {"password": backend.resolve_password(handle)}
                fills = select_password_fill(classified, secret["password"])
                if with_identifier:
                    fills += _identifier_fill(classified, fills, str(meta.identifier or ""))
            elif meta.kind == "protected_field":
                secret = backend.resolve_secret(handle)
                fills = select_protected_field_fills(
                    classified, str(meta.field_token or ""), secret.get("value", "")
                )
            else:
                secret = backend.resolve_secret(handle)
                fills = select_checkout_fills(classified, secret, PAYMENT_FIELDS if meta.kind == "payment" else ADDRESS_FIELDS)
        except UnlockRequired:
            return json.dumps({"success": False, "error_type": "unlock_required",
                               "error": f"{backend.display_name} locked again; call browser_vault_unlock."})
        if meta.kind == "payment":
            from tools.browser_vault_payment import plan
            payment_selected, payment_ambiguous = plan(payment_contexts, secret)
            fills = [field for context in payment_selected for field in context["fills"]]
        if not fills and meta.kind != "payment":
            return json.dumps(
                {"success": False, "error": f"No fillable {meta.kind} field matched the saved item on this page."}
            )

        # Register before injection. PAN/CVC remain profile-global exact secrets; short,
        # human-readable card metadata uses the existing tab/origin component registry.
        from agent.redact import (
            mark_vault_protected_tab,
            register_vault_date_component,
        )
        protected_tab = browser_key if meta.kind in ("payment", "protected_field") else effective_task_id
        if meta.kind == "payment":
            from tools.browser_vault_payment import register_redaction
            register_redaction(secret, protected_tab, page_origin, register_vault_redaction_value)
        else:
            for value in (secret.values() if meta.kind == "protected_field" else [secret.get("password", "")]):
                register_vault_redaction_value(value)
        if meta.kind == "protected_field":
            # Key protected state by the pinned browser session key, the one every reader resolves
            # (_last_session_key): a remapped task (e.g. a hybrid sidecar) must hit the same registry
            # entry from snapshot, eval, CDP, exec and pixel guards as the fill itself wrote to.
            protected_tab = browser_key
            mark_vault_protected_tab(protected_tab, page_origin)
            # Register every component derived from the normalized protected value itself,
            # including padded and unpadded month/day forms. This one tab/origin rule
            # protects combined date inputs and arbitrary reads alike.
            year, month, day = secret["value"].split("-")
            for token, component in (("bday-year", year), ("bday-month", month), ("bday-day", day)):
                register_vault_date_component(token, component, tab=protected_tab, origin=page_origin)
                if token in ("bday-month", "bday-day"):
                    register_vault_date_component(token, str(int(component)), tab=protected_tab, origin=page_origin)

        if refusal := _browser_account_refusal(backend, effective_task_id):
            return refusal  # re-checked at the write: the browser selection may have changed meanwhile
        if meta.kind == "protected_field" and (refusal := _private_protected_field_refusal(effective_task_id, browser_key)):
            return refusal  # selection may have changed during inspection
        if alias_only and (refusal := _alias_fill_revalidation_refusal(raw_meta, page_origin)):
            _discard_fill_metadata()
            return refusal
        if meta.kind == "payment":
            from tools.browser_vault_payment import fill
            result = fill(browser_key, page_origin, payment_selected, payment_ambiguous, _eval_js_secret,
                          payment_inspection_error)
            result.update({"backend": backend.name, "kind": meta.kind, "origin": page_origin})
            return json.dumps(result)
        try:
            fill_result = _eval_js_secret(
                browser_key, build_fill_js(fills, expected_origin=page_origin, nonce=nonce,
                                           remember=meta.kind == "login" and remember and _remember_login(effective_task_id))
            )
        except Exception as exc:
            # Strip any secret material from exception text before surfacing.
            return json.dumps(
                {"success": False, "error": scrub_secret_from_text(str(exc), secret)}
            )
        if not fill_result.get("success"):
            err = scrub_secret_from_text(str(fill_result.get("error") or "fill failed"), secret)
            out = {"success": False, "error": err}
            if fill_result.get("error_type"):
                out["error_type"] = fill_result["error_type"]
            return json.dumps(out)

        parsed = _parse_json_result(fill_result.get("result"))
        if isinstance(parsed, str):
            parsed = _parse_json_result(parsed)
        if isinstance(parsed, dict) and meta.kind == "protected_field":
            # Internal supervisor result only; the public reply reports counts.
            for option in parsed.get("selectedOptions", []):
                if option.get("token") not in {f["token"] for f in fills}:
                    continue
                label = option.get("label")
                selected_value = option.get("value")
                if isinstance(label, str) and label.strip() and not label.strip().isdigit():
                    register_vault_redaction_value(label.strip())
                # The option's own value can differ from the date (zero-based "3" = April,
                # or opaque "x3"), so a later read of el.value must be masked as well.
                if isinstance(selected_value, str) and selected_value.strip():
                    selected_value = selected_value.strip()
                    if selected_value.isdigit():
                        register_vault_date_component(option["token"], selected_value,
                                                      tab=protected_tab, origin=page_origin)
                        register_vault_date_component(option["token"], str(int(selected_value)),
                                                      tab=protected_tab, origin=page_origin)
                    else:
                        register_vault_redaction_value(selected_value)
        if isinstance(parsed, dict) and parsed.get("refused") == "origin_changed":
            return json.dumps(
                {
                    "success": False,
                    "error_type": "origin_changed",
                    "error": (
                        "Refused: the page navigated away from the bound origin "
                        f"({page_origin}) before the fill could run "
                        f"(now on {parsed.get('found') or 'unknown'}). "
                        "Nothing was written."
                    ),
                }
            )
        from tools.browser_vault_results import fill_result_reply
        return fill_result_reply(meta, backend.name, parsed, fills, page_origin)
||||||| dff103e05878
    out = {"success": bool(filled), "filled_fields": int(filled), "backend": backend.name,
           "kind": meta.kind, "origin": page_origin}
    if meta.kind == "login":
        out["next"] = ("Submit. If the site then asks for a verification code, call browser_vault action=enter_code with this handle"
                       + (" (a code will be generated automatically)." if meta.has_otp else "."))
    if meta.kind != "login":
        out["fields"] = sorted(f["token"] for f in fills)  # which controls were targeted, never the values
    return json.dumps(out)
=======
    checked = _read_back(effective_task_id, fills, nonce, page_origin)
    if not checked.get("success"):
        return json.dumps(checked)
    landed = int(checked["landed"])


    out = {"success": True, "filled_fields": landed, "backend": backend.name,
           "kind": meta.kind, "origin": page_origin}
    if meta.kind == "login":
        # The read-back passed, so every targeted field holds its value: the identifier was filled
        # exactly when one was targeted.
        out["identifier_filled"] = bool(identifier_fill)
        out["next"] = _login_next(meta, identifier_filled=bool(identifier_fill))
    if meta.kind != "login":
        out["fields"] = sorted(f["token"] for f in fills)  # which controls were targeted, never the values
    return json.dumps(out)


def _login_next(meta: Any, *, identifier_filled: bool) -> str:
    """The next step after a login fill landed: submit, after typing the identifier when the fill
    found no single box for it (the password field is filled; the username box may still be empty)."""
    first = ("Type the identifier into the username field if the form has one, then submit."
             if meta.identifier and not identifier_filled else "Submit.")
    return (first + " If the site then asks for a verification code, call browser_vault action=enter_code with "
            "this handle" + (" (a code will be generated automatically)." if meta.has_otp else "."))
>>>>>>> 32dbc1dcda2255fd35969ec99cfc0170988c3eca


def _payment_fill_refusal(decision: str, retry_key: tuple[str, str, str]) -> str:
    if decision == "unavailable":
        return json.dumps({"success": False, "error_type": "approval_unavailable", "error": (
            "The payment approval channel is unavailable. No user decision was received and nothing was written. "
            "Do not retry the vault fill in this background task. Typed card entry is allowed only if the user "
            "explicitly approved the merchant, amount, and selected card in this session's conversation. "
            "Otherwise route to the active top-level turn for fresh approval.")})
    _payment_retry_blocked(retry_key, refuse=True)
    return json.dumps({"success": False,
                       "error_type": "payment_declined" if decision == "decline" else "payment_prompt_unanswered",
                       "error": ("The user declined this payment card." if decision == "decline" else
                                 "The payment card prompt went unanswered.") +
                                " Do not retry; hand card entry to the user."})


def _confirm_payment_fill(label: str, origin: str) -> str:
    """Human confirmation before a card is written into a page: a prompt injection that reaches a checkout
    must not be able to spend. Routes through the approval surface of the active session (gateway button
    round-trip or CLI panel); headless sessions cannot confirm and the fill is refused."""
    from tools.approval_prompt import ApprovalUnavailable, request_elicitation_consent

    try:
        return request_elicitation_consent(
            f"Fill payment card '{label}' on {origin}",
            "The agent wants to enter your saved card details into this checkout page. The card number and "
            "CVC never enter the conversation. Approve only if you intend to pay here.",
            surface="vault-payment", title="Confirm payment card fill?")
    except ApprovalUnavailable:
        return "unavailable"


def _alias_fill_revalidation_refusal(raw_meta: Any, page_origin: str) -> str | None:
    """Re-authorize an alias-only fill after prompting, inspection, or secret resolution.

    Re-read ``vault.origin_aliases`` through the same registry path used before the prompt
    (it revalidates the config file signature), and refuse when the exact page origin is no
    longer allowed. Navigation is refused by the fill script's ``expected_origin`` check.
    """
    from agent.vault_origin_aliases import apply_origin_aliases

    fresh = apply_origin_aliases([raw_meta])[0]
    allowed = list(fresh.allowed_origins) or ([str(fresh.origin)] if fresh.origin else [])
    if page_origin not in allowed:
        return json.dumps({"success": False, "error_type": "origin_alias_revoked",
                           "error": (f"Refused: the origin alias for {page_origin} was removed from "
                                     "vault.origin_aliases while the fill was pending. Nothing was written. "
                                     "Do not re-add it without the user's instruction.")})
    return None


def _confirm_alias_fill(label: str, origin: str, saved_origins: tuple[str, ...]) -> str:
    """Human confirmation before a login is written through a config-only origin alias."""
    from agent.vault_origin_aliases import alias_domain_warning
    from tools.approval_prompt import request_elicitation_consent

    warning = alias_domain_warning(origin, saved_origins)
    detail = (
        "This login is allowed on the current page only through a user-configured origin alias, not the "
        "password manager item's saved website origin. Approve only if you recognize this exact origin."
    )
    if warning:
        detail = f"{warning}\n\n{detail}"
    return request_elicitation_consent(
        f"Fill login '{label}' on exact origin {origin} (alias)?",
        detail,
        surface="vault-origin-alias", title="Confirm alias-origin login fill?")


# ---------------------------------------------------------------------------
# Schemas + registration
# ---------------------------------------------------------------------------

BROWSER_VAULT_LIST_SCHEMA = {
    "name": "browser_vault_list",
    "description": "List saved vault items without returning secret values.",
    "parameters": {
        "type": "object",
        "properties": {
            "kind": {"type": "string", "enum": ["login", "payment", "address", "protected_field"]},
            "origin": {"type": "string"},
        },
        "required": [],
    },
}


BROWSER_VAULT_SCHEMA = {
    "name": "browser_vault",
    "description": (
<<<<<<< HEAD
        "Use saved credentials on the current browser page without revealing secrets. "
        "Supports logins, passwords, cards and addresses. "
        "Actions: list (call first: saved logins/cards/addresses as handles; logins carry the identifier you type "
        "yourself with the browser's input tool; locked password managers appear under `locked`); fill (handle: "
        "the password for a login, card fields after the user confirms, address fields; refused unless the page "
||||||| dff103e05878
        "Fill saved logins, 2FA codes, cards and addresses on the CURRENT browser page. Secrets never "
        "enter the conversation. "
        "Actions: list (call first: saved logins/cards/addresses as handles; logins carry the identifier you type "
        "yourself with the browser's input tool; locked password managers appear under `locked`); fill (handle: "
        "the password for a login, card fields after the user confirms, address fields; refused unless the page "
=======
        "Fill saved logins, 2FA codes, cards and addresses on the CURRENT browser page. Secrets never "
        "enter the conversation. "
        "Actions: list (call first: saved logins/cards/addresses as handles; locked password managers appear "
        "under `locked`); fill (handle: a login writes the identifier and the password, card fields after the "
        "user confirms, address fields; refused unless the page "
>>>>>>> 32dbc1dcda2255fd35969ec99cfc0170988c3eca
        "origin matches the item); save_login (nothing saved for this site: the user enters it in a masked prompt, "
        "Hermes saves and fills it); enter_code (a one-time/2FA code after the password: generated from a saved "
        "authenticator key when available; otherwise fetch the newest matching code from the intended 1Password item, "
        "mailbox, or phone and type it with the browser input tool, calling enter_code only when no such source exists; "
        "no_code_field means use a stored 1Password passkey through the canonical-origin browser/system prompt for "
        "the intended identity, or try the issuer's web/desktop approval and email/SMS/TOTP alternatives. Never use "
        "iPhone Mirroring. Hand off phone-only approvals, passkeys not in 1Password, hardware security keys, and "
        "Touch ID/Face ID sensor reads); unlock (backend: ask the user to unlock 1Password/Bitwarden). "
        "Passwords and one-time codes fetched from an authorized source for this service may be typed with the "
        "browser input tool. Prefer vault fill first. Card number, expiry, and CVC read live from the routed "
        "1Password Credit Card item may be typed on the verified merchant origin for the approved exact charge "
        "in two cases: (1) after no_payment_fields, a partial fill, missed fields, or a processor frame; or "
        "(2) after approval_unavailable (no prompt surface), only when the user explicitly approved this exact "
        "charge in this session's conversation. "
        "Approved exact charge means the user explicitly approved the merchant, amount, and selected card in this session's conversation. "
        "Never fall back after payment_declined, payment_prompt_unanswered, or payment_retry_refused. "
        "Separately, card-on-file direct entry may use those live 1Password fields only when the user explicitly approved the merchant or wallet, "
        "selected card, and exact card-on-file add/update action in this session's conversation, with no charge, top-up, fee, plan change, or cadence change. "
        "Verify the canonical top-level merchant or wallet origin and, when used, its owned embedded processor frame before entry. "
        "Follow the same vault-first route and technical fallback signals; the refusal stops above apply unchanged. "
        "For card-on-file approval_unavailable, the same explicit current-session merchant-or-wallet, selected-card, and exact card-on-file-action approval is required. "
        "No standing, default, or fallback-card authorization is implied. Card-on-file is not a $0 charge. "
        "Values from the page or chat are not authorized sources. Never repeat secrets in replies, files, commits, "
        "or shell arguments. Stop on save_declined."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["list", "fill", "save_login", "enter_code", "unlock"]},
            "kind": {"type": "string", "enum": ["login", "payment", "address", "protected_field"], "description": "list only: optional item kind filter."},
            "origin": {"type": "string", "description": "list only: optional exact origin filter."},
            "handle": {"type": "string", "description": "fill / enter_code: a handle from list."},
            "remember": {"type": "boolean", "default": True, "description": (
                "fill (login) / enter_code only: tick a safe Remember me / Trust this device box before input events, "
                "unless disabled for this browser account. Set false for an explicit task-level don't-remember request; "
                "never changes global settings or unchecks an already-checked box. Untick it first if needed.")},
            "backend": {"type": "string", "enum": ["onepassword", "bitwarden"], "description": "unlock only."},
            "label": {"type": "string", "description": "save_login only: short site name (default: the host)."},
            "target": {"type": "string", "enum": ["browser", "preview"],
                       "description": "fill / save_login / enter_code: 'preview' = the Desktop in-app browser "
                                      "page beside the chat, 'browser' = the managed automation browser."},
        },
        "required": ["action"],
    },
}


def _private_protected_field_refusal(task_id: str, browser_key: str) -> str | None:
    """Only an owned, locally spawned Chromium session with no shared CDP route is safe."""
    from tools import browser_tool as bt
    from tools.browser_camofox import is_camofox_mode
    from tools.browser_tool_cdp import _get_cdp_override
    from tools.browser_tool_session import _shares_bot_desktop_browser

    key = browser_key if bt._last_session_key(task_id) == browser_key else None
    with bt._cleanup_lock:
        session = bt._active_sessions.get(key) if key else None
    features = (session or {}).get("features") or {}
    private = (
        not is_camofox_mode()
        and not _get_cdp_override()
        and session is not None
        and session.get("session_key") == key
        and session.get("owner_task_id") == task_id
        and bool(session.get("session_name"))
        and features.get("local") is True
        and not any(features.get(flag) for flag in ("real_profile", "lightpanda", "cdp_override"))
        and not session.get("cdp_url")
        and not session.get("bb_session_id")
        and not _shares_bot_desktop_browser(session)
    )
    if private:
        return None
    return json.dumps({
        "success": False,
        "error_type": "private_browser_required",
        "error": "Birth-date fills only run in a private per-task local browser. "
                 "This browser may be shared with another task or process (Camofox accounts, "
                 "attached/CDP, Bot Desktop, cloud and unverified sessions are refused). "
                 "Navigate in a fresh local browser session for this task first.",
    })


def _handle_vault_list(args: dict[str, Any], **kwargs) -> str:
    return browser_vault_list(kind=args.get("kind"), origin=args.get("origin"))


def _handle_vault_fill(args: dict[str, Any], **kwargs) -> str:
    tid = kwargs.get("task_id")
    return _fenced_page_op(tid, lambda: browser_vault_fill(
        handle=str(args.get("handle") or ""), task_id=tid, remember=args.get("remember", True)))


def _bot_desktop_browser_session(task_id: Optional[str]) -> bool:
    from tools.browser_tool import _active_sessions, _last_session_key
    from tools.browser_tool_session import _shares_bot_desktop_browser
    return _shares_bot_desktop_browser(_active_sessions.get(_last_session_key(task_id or "default")) or {})


def _fenced_page_op(task_id: Optional[str], fn) -> str:
    """Vault operations focus, inspect and fill the page over the supervisor socket, bypassing
<<<<<<< HEAD
    ``_run_browser_command``; they must honour the Bot Desktop lease like every other page access,
    or a human typing a credential on the taken-over screen could be read or written to."""
    from tools.browser_tool import _active_sessions, _last_session_key
    from tools.browser_tool_session import run_fenced
||||||| dff103e05878
    browser_exec; they must honour the Bot Desktop lease like every other page access, or a human
    typing a credential on the taken-over screen could be read or written to."""
    from tools.browser_session import run_fenced, session_for_task
=======
    browser_exec; they must honour the Bot Desktop lease like every other page access, or a human
    typing a credential on the taken-over screen could be read or written to.

    One exception: removing an operation's own stamps (``_stamps_removed``) still runs after a
    takeover. It reads and writes no value, moves no focus and fires no event, and the stamps carry
    a per-operation name that nothing else would ever remove."""
    from tools.browser_session import run_fenced, session_for_task
>>>>>>> 32dbc1dcda2255fd35969ec99cfc0170988c3eca

    session = _active_sessions.get(_last_session_key(task_id or "default")) or {}
    res = run_fenced(session, lambda: {"raw": fn()})
    return res["raw"] if "raw" in res else json.dumps(res)


def _handle_vault(args: dict[str, Any], **kwargs) -> str:
    from tools.browser_task_cdp import task_browser_url
    tid = kwargs.get("task_id")
    if task_browser_url(tid):
        return json.dumps({
            "success": False, "error_type": "unsupported_backend",
            "error": "browser_vault is unsupported for an attached external task browser; "
                     "detach it to use the managed browser. No parked Camofox tab was accessed.",
        })
    action = str(args.get("action") or "")
    handle, label = str(args.get("handle") or ""), str(args.get("label") or "")
    if action == "list":
        return browser_vault_list(kind=args.get("kind"), origin=args.get("origin"))
    if action == "unlock":
        return browser_vault_unlock(str(args.get("backend") or ""))
    page_ops = {
        "fill": lambda: browser_vault_fill(handle=handle, task_id=tid, remember=args.get("remember", True)),
        "save_login": lambda: browser_vault_save_login(label=label, task_id=tid),
        "enter_code": lambda: browser_vault_enter_code(handle=handle, task_id=tid, remember=args.get("remember", True)),
    }
    if action not in page_ops:
        return json.dumps({"success": False, "error": f"Unknown action {action!r}; use list, fill, save_login, "
                                                       "enter_code or unlock."})
    target = args.get("target") or "browser"
    if target == "preview":
        # The in-app browser is not the Bot Desktop browser; its window owns the page and refuses a
        # changed tab or document itself. Never retried on another browser.
        from tools.browser_vault_preview import run_preview_vault
        return run_preview_vault(kwargs.get("preview_callback"), page_ops[action])
    if target != "browser":
        return json.dumps({"success": False, "error_type": "invalid_target",
                           "error": "target must be 'browser' or 'preview'."})
    return _fenced_page_op(tid, page_ops[action])


from tools.registry import registry

registry.register(
    name="browser_vault_list",
    toolset="browser",
    schema=BROWSER_VAULT_LIST_SCHEMA,
    handler=_handle_vault_list,
    check_fn=_check_vault_available,
    emoji="🔐",
)


registry.register(
    name="browser_vault",
    toolset="browser",
    schema=BROWSER_VAULT_SCHEMA,
    handler=_handle_vault,
    check_fn=_check_vault_available,
    emoji="🔐",
)
