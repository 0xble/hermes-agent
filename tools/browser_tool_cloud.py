"""Cloud browser provider resolution (explicit browser.cloud_provider, auto-detect, per-profile cache), backend/engine selection and headed-mode flags.

Split out of ``tools/browser_tool.py``. Facade-owned state is read through ``_bt`` (``tools.browser_tool``, resolved per call) — no import cycle."""

from __future__ import annotations

import os
from typing import Callable, Optional

from agent.browser_provider import BrowserProvider as CloudBrowserProvider
from agent.browser_registry import get_provider as _registry_get_browser_provider
from hermes_constants import get_hermes_home_override, hermes_home_key
from tools.tool_backend_helpers import normalize_browser_cloud_provider
from utils import is_truthy_value
from tools.browser_tool_origin import origin_module as _origin
from tools import browser_tool_cdp as _cdp


class BrowserTargetUnavailable(RuntimeError):
    """A pinned browser target could not be used without selecting another backend."""

    def __init__(self, target: str, reason: str):
        self.target = target
        self.reason = reason
        super().__init__(f"Pinned browser target {target!r} unavailable: {reason}")


def browser_target() -> str:
    """The active profile's pinned browser target, or an empty string.

    Targets are intentionally URL-only. A per-user loopback HTTP(S) proxy must front
    Unix-socket CDP transports so every backend uses the same safe selection path.
    """
    _bt = _origin()
    return str(_bt._browser_cfg("target", "", lambda v: str(v or "").strip(), "browser.target from config") or "")


def browser_fallback_policy() -> str:
    """The profile's fallback policy; unset/invalid values preserve legacy behavior."""
    _bt = _origin()
    value = str(_bt._browser_cfg("fallback", "allow", lambda v: str(v or "").strip().lower(),
                                "browser.fallback from config") or "allow")
    return "refuse" if value == "refuse" else "allow"


def browser_target_refuses() -> bool:
    return browser_fallback_policy() == "refuse"


def pinned_browser_target_refuses() -> bool:
    """Whether a profile with no fallback is intentionally fail-closed."""
    return browser_target_refuses()


def browser_target_is_camofox() -> bool:
    """Whether ``browser.target`` is the REST endpoint for the selected Camofox backend."""
    if not browser_target():
        return False
    _bt = _origin()
    return str(_bt._browser_cfg("cloud_provider", "", lambda v: str(v or "").strip().lower(),
                                "browser.cloud_provider from config") or "") == "camofox"


def _validate_pinned_target(target: str) -> str:
    """Reject Unix paths rather than guessing a different browser transport."""
    from urllib.parse import urlsplit
    try:
        parsed = urlsplit(target)
    except ValueError as exc:
        raise BrowserTargetUnavailable(target, f"invalid target: {exc}") from exc
    if parsed.scheme not in {"http", "https", "ws", "wss"} or not parsed.netloc:
        raise BrowserTargetUnavailable(
            target,
            "invalid target; Unix-socket paths are unsupported, use a loopback HTTP(S) proxy URL",
        )
    return target


def pinned_browser_target_or_raise() -> str:
    target = browser_target()
    if not target:
        return ""
    return _validate_pinned_target(target)


def browser_target_error(exc: BaseException, target: str = "") -> dict:
    """Stable tool payload for a refused pinned target; never suggests another backend."""
    if isinstance(exc, BrowserTargetUnavailable):
        target = exc.target
        reason = exc.reason
    else:
        reason = str(exc) or "connection failed"
        lower = reason.lower()
        if "logged out" in lower or "login" in lower or "unauthor" in lower:
            reason = "logged out or unauthorized"
        elif "reconnect" in lower:
            reason = "reconnect failed"
        elif "refused" in lower or "connect" in lower or "unreachable" in lower:
            reason = "connection refused or unreachable"
        elif "socket" in lower or "no such file" in lower:
            reason = "missing socket"
    return {"success": False, "error_type": "browser_target_unavailable",
            "code": "browser_target_unavailable", "target": target or browser_target(),
            "reason": reason,
            "error": f"Pinned browser target {(target or browser_target())!r} unavailable: {reason}"}


def pinned_browser_target_error(exc: BaseException) -> Optional[dict]:
    """Return a fail-closed error only when the active profile refuses target fallback."""
    if not pinned_browser_target_refuses():
        return None
    return browser_target_error(exc, browser_target())


def _memo(_bt, resolved_attr: str, cache_attr: str, compute: Callable[[], object]):
    """Process-lifetime cache on ``_bt``: the resolved flag is set BEFORE computing, then the final value is stored.

    Under a routed profile (HERMES_HOME override, multiplexed gateway) the slot is NOT consulted: every
    ``_memo`` here caches a ``browser.*`` config read, and one process-wide slot would hand the launch
    profile's engine/headed/private-URL policy to every other profile (same rule as ``_allow_private_urls``).
    """
    if get_hermes_home_override() is not None:
        return compute()
    if not getattr(_bt, resolved_attr):
        setattr(_bt, resolved_attr, True)
        setattr(_bt, cache_attr, compute())
    return getattr(_bt, cache_attr)


def _ensure_browser_plugins_loaded() -> None:
    """Idempotently trigger plugin discovery (standalone scripts/tests may never import ``model_tools``)."""
    try:
        from hermes_cli.plugins import _ensure_plugins_discovered
        _ensure_plugins_discovered()
    except Exception as exc:
        _origin().logger.debug("Browser plugin discovery failed (non-fatal): %s", exc)


def _get_cloud_provider() -> Optional[CloudBrowserProvider]:
    """Return the provider cached for the active Hermes profile."""
    _bt = _origin()
    scope = hermes_home_key()
    with _bt._cloud_provider_cache_lock:
        # A cleared boolean (tests / legacy reset) is a full reset even if a scoped resolution is still mirrored here.
        if not _bt._cloud_provider_resolved:
            _bt._cached_cloud_provider_scope = None
            _bt._cached_cloud_providers.clear()
        while True:
            before_generation = _bt._browser_registry_generation(scope=scope)
            cache_key = (scope, before_generation)
            if cache_key in _bt._cached_cloud_providers:
                _bt._cached_cloud_provider = _bt._cached_cloud_providers[cache_key]
                _bt._cloud_provider_resolved = True
                _bt._cached_cloud_provider_scope = scope
                return _bt._cached_cloud_provider
            _bt._cached_cloud_provider = None
            _bt._cloud_provider_resolved = False
            resolved = _resolve_cloud_provider_uncached()
            after_generation = _bt._browser_registry_generation(scope=scope)
            if before_generation != after_generation:  # force reload mid-resolution: discard, resolve again
                continue
            if _bt._cloud_provider_resolved:
                _bt._cached_cloud_provider_scope = scope
                for stale_key in [key for key in _bt._cached_cloud_providers if key[0] == scope]:
                    _bt._cached_cloud_providers.pop(stale_key, None)
                _bt._cached_cloud_providers[cache_key] = resolved
            return resolved


def _instantiate_explicit_cloud_provider(provider_key: str) -> Optional[CloudBrowserProvider]:
    """Build the provider named by ``browser.cloud_provider``.

    Strict: an unregistered name raises ``ValueError`` (never a silent reroute to auto-detect); any
    other instantiation error is logged and yields None so the next call retries.
    """
    _bt = _origin()
    try:
        _ensure_browser_plugins_loaded()
        resolved = _registry_get_browser_provider(provider_key)
        if resolved is None:
            from tools.tool_backend_helpers import selection_error
            raise ValueError(selection_error(
                "browser", f"'{provider_key}'",
                "no registered browser plugin has that name (install the corresponding plugin or fix the config key spelling)",
            ))
        return resolved
    except ValueError:
        raise
    except Exception:
        _bt.logger.warning("Failed to instantiate explicit cloud_provider %r; will retry on next call", provider_key, exc_info=True)
        return None


def _autodetect_cloud_provider() -> Optional[CloudBrowserProvider]:
    """Auto-detect: Browser Use, then Browserbase; never raises.

    Third-party plugins are only reachable via explicit ``browser.cloud_provider: <name>``.
    """
    _bt = _origin()
    # Late import: this module is on run_agent's import path, and a core tool must not
    # execute bundled plugin code before a browser session is actually being resolved.
    from plugins.browser.browser_use.provider import BrowserUseBrowserProvider
    from plugins.browser.browserbase.provider import BrowserbaseBrowserProvider
    try:
        for cls in (BrowserUseBrowserProvider, BrowserbaseBrowserProvider):
            fallback_provider = cls()
            if fallback_provider.is_available():
                return fallback_provider
    except Exception:  # pragma: no cover - defensive: never poison cache
        _bt.logger.debug("Cloud provider auto-detect failed", exc_info=True)
    return None


def _resolve_cloud_provider_uncached() -> Optional[CloudBrowserProvider]:
    """Return the configured cloud browser provider, or None for local mode.

    Pins the cache only when definitive (explicit ``local``/``camofox`` or a resolved provider); a transient None
    (unreadable config, missing credentials) is NOT cached so it can self-heal. Auto-detect runs only when no
    selection was ever written.
    """
    _bt = _origin()
    resolved: Optional[CloudBrowserProvider] = None
    provider_key = None
    try:
        from hermes_cli.config import read_raw_config
        browser_cfg = read_raw_config().get("browser", {})
        if isinstance(browser_cfg, dict) and "cloud_provider" in browser_cfg:
            provider_key = normalize_browser_cloud_provider(browser_cfg.get("cloud_provider"))
            if provider_key in ("local", "camofox"):
                # Camofox runs through the built-in browser tools, not a cloud provider.
                _bt._cached_cloud_provider = None
                _bt._cloud_provider_resolved = True
                return None
            if provider_key == "nous":
                # Managed "Nous Subscription" is serviced by the Browser Use provider.
                provider_key = "browser-use"
        if provider_key:
            resolved = _instantiate_explicit_cloud_provider(provider_key)
            if resolved is None:
                return None
    except ValueError:
        raise
    except Exception as e:
        # Config may be temporarily unreadable; still try auto-detect (env/managed creds). Don't pin cache.
        _bt.logger.debug("Could not read cloud_provider from config: %s", e)

    if resolved is None and provider_key is None:
        resolved = _autodetect_cloud_provider()
    if resolved is None:
        return None
    _bt._cached_cloud_provider = resolved
    _bt._cloud_provider_resolved = True
    return _bt._cached_cloud_provider


def _is_local_mode() -> bool:
    """Return True when the browser tool will use a local browser backend."""
    _bt = _origin()
    return not _cdp._get_cdp_override_raw() and _get_cloud_provider() is None


def _is_local_backend() -> bool:
    """True when the browser runs locally AND the terminal is also local.

    SSRF protection only matters when the browser can reach networks the terminal cannot (cloud backends,
    containerized terminals). A CDP override is never trusted as local (that Chrome may live off-host) and MUST
    be checked before the Camofox short-circuit; ``_is_local_mode`` treats overrides the same way — keep the two
    in agreement.
    """
    _bt = _origin()
    if _cdp._get_cdp_override_raw():
        return False
    if _bt._is_camofox_mode():
        return True
    if _get_cloud_provider() is not None:
        return False
    # Scope-aware: under gateway multiplexing the routed profile's terminal backend lives in the per-turn scope.
    # When terminal runs in a container, browser on host can access internal networks the terminal can't →
    # treat as non-local. See #68559.
    from tools.terminal_scope import terminal_env
    return terminal_env("TERMINAL_ENV", "local").strip().lower() in ("local", "")


def browser_backend_name(local_engine: Optional[str] = None) -> str:
    """Shared-metrics label for the backend legacy browser calls use in the active profile, in session-creation
    precedence (CDP override > Camofox > cloud provider > local engine). Config reads only, no network I/O."""
    _bt = _origin()
    if _cdp._get_cdp_override_raw():
        return "cdp"
    if local_engine:
        return local_engine
    if _bt._is_camofox_mode():
        return "camofox"
    provider = _get_cloud_provider()
    if provider is not None:
        return str(getattr(provider, "name", "") or "other")
    return "lightpanda" if _get_browser_engine() == "lightpanda" else "local"


def _get_browser_engine() -> str:
    """Return the browser engine: ``auto`` (no ``--engine`` flag), ``lightpanda`` or ``chrome``.

    ``browser.engine`` first, then ``AGENT_BROWSER_ENGINE``, then ``auto``; cached. Lightpanda is faster on
    navigation but has no graphical renderer (no screenshots).
    """
    _bt = _origin()
    def compute() -> str:
        engine = _bt._browser_cfg("engine", "auto", lambda v: str(v).strip().lower() if v and str(v).strip() else "auto", "browser.engine from config")
        if engine == "auto":
            engine = os.environ.get("AGENT_BROWSER_ENGINE", "").strip().lower() or engine
        # agent-browser only accepts "chrome" and "lightpanda".
        _VALID_ENGINES = {"auto", "lightpanda", "chrome"}
        if engine not in _VALID_ENGINES:
            _bt.logger.warning("Unknown browser engine %r (valid: %s), falling back to 'auto'", engine, ", ".join(sorted(_VALID_ENGINES)))
            engine = "auto"
        return engine
    return _memo(_bt, "_browser_engine_resolved", "_cached_browser_engine", compute)


def _is_headed_mode() -> bool:
    """Resolve the active profile's migrated ``browser.headed`` setting; cached."""
    _bt = _origin()
    def compute() -> bool:
        return _bt._browser_cfg("headed", False, lambda v: False if v is None else str(v).strip().lower() in ("true", "1", "yes"), "browser.headed from config")
    return _memo(_bt, "_headed_mode_resolved", "_cached_headed_mode", compute)


def _should_inject_engine(engine: str) -> bool:
    """True when ``--engine`` should be added: explicit (non-``auto``) engine on a non-cloud, non-camofox local session."""
    _bt = _origin()
    return engine != "auto" and not _bt._is_camofox_mode() and _is_local_mode()


def _auto_local_for_private_urls() -> bool:
    """``browser.auto_local_for_private_urls`` (default True), cached: route private/LAN URLs to a local sidecar even with a cloud provider."""
    _bt = _origin()
    return _memo(
        _bt, "_auto_local_for_private_urls_resolved", "_cached_auto_local_for_private_urls",
        lambda: _bt._browser_cfg("auto_local_for_private_urls", _bt._cached_auto_local_for_private_urls, bool, "auto_local_for_private_urls from config"),
    )


def _use_real_profile() -> bool:
    """Whether the user consented to real-profile local browsing.

    Read on EVERY call: it is a consent switch (flipping it off must not need a restart) and each multiplexed
    profile must decide for itself. One YAML load per local session creation, so no hot-path cost.
    """
    return _origin()._browser_cfg("use_real_profile", False, bool, "use_real_profile from config")


def _allow_private_urls() -> bool:
    """Whether the browser may navigate to private/internal addresses (default False: SSRF protection on).

    Single-profile calls cache for the process lifetime; multiplexed profile turns (ContextVar-scoped config)
    resolve on every call so one profile's opt-out is never reused by another.
    """
    _bt = _origin()
    if get_hermes_home_override() is not None:
        return _resolve_allow_private_urls()
    return _memo(_bt, "_allow_private_urls_resolved", "_cached_allow_private_urls", _resolve_allow_private_urls)


def _resolve_allow_private_urls() -> bool:
    """Read the browser private-URL toggle from the active config scope."""
    _bt = _origin()
    return _bt._browser_cfg("allow_private_urls", False, lambda v: is_truthy_value(v, default=False), "allow_private_urls from config")
