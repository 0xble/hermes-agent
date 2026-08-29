"""Named real-profile browser identity configuration and resolution."""

from __future__ import annotations

import hashlib
import errno
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Mapping

_SUPPORTED_BROWSERS = frozenset({"chrome", "edge", "brave", "chromium"})
_ALIAS_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")


class BrowserIdentityError(ValueError):
    """A named browser identity is missing, invalid, or incompatible."""


@dataclass(frozen=True)
class BrowserIdentity:
    """Validated operator-facing alias mapped to one Chromium source profile."""

    alias: str
    browser: str
    source_profile: str
    runtime_key: str


def browser_identity_runtime_key(
    alias: str,
    browser: str,
    source_profile: str = "",
) -> str:
    """Return a stable opaque key for identity-owned runtime resources."""

    payload = f"{browser}\0{alias}\0{source_profile}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


def browser_identity_scope_key(runtime_key: str) -> str:
    """Scope an identity runtime to the active Hermes home/profile."""

    from hermes_constants import hermes_home_key

    payload = f"{hermes_home_key()}\0{runtime_key}".encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:20]


class BrowserIdentityProcessLock:
    """Owner-only, bounded cross-process lock for snapshot mutation and launch."""

    def __init__(self, runtime_key: str, *, timeout: float = 30.0) -> None:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", runtime_key):
            raise BrowserIdentityError("invalid browser identity runtime lock key")
        if timeout < 0:
            raise BrowserIdentityError(
                "browser identity lock timeout must be non-negative"
            )
        from hermes_constants import get_hermes_home

        self.path = (
            get_hermes_home() / "browser-profile" / "locks" / f"{runtime_key}.lock"
        )
        self.timeout = timeout
        self._handle = None
        self._windows = os.name == "nt"

    def __enter__(self) -> "BrowserIdentityProcessLock":
        lock_dir = self.path.parent
        lock_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(lock_dir, 0o700)
        except OSError:
            pass
        handle = open(self.path, "a+b")
        self._handle = handle
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        if self._windows:
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()

        deadline = time.monotonic() + self.timeout
        while True:
            try:
                if self._windows:
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                return self
            except (BlockingIOError, OSError) as exc:
                contention = {
                    errno.EACCES,
                    errno.EAGAIN,
                    getattr(errno, "EDEADLK", errno.EAGAIN),
                    getattr(errno, "EWOULDBLOCK", errno.EAGAIN),
                }
                if getattr(exc, "errno", None) not in contention:
                    handle.close()
                    self._handle = None
                    raise BrowserIdentityError(
                        f"could not acquire browser identity lock: {exc}"
                    ) from exc
                if time.monotonic() >= deadline:
                    handle.close()
                    self._handle = None
                    raise BrowserIdentityError(
                        f"timed out acquiring browser identity lock after {self.timeout:.1f}s"
                    ) from exc
                time.sleep(min(0.05, max(deadline - time.monotonic(), 0.0)))

    def __exit__(self, exc_type, exc, traceback) -> None:
        handle = self._handle
        self._handle = None
        if handle is None:
            return
        try:
            if self._windows:
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        except OSError:
            pass
        finally:
            handle.close()


def read_browser_identity_config() -> dict[str, Any]:
    """Read the active profile's browser mapping without process caching."""

    try:
        from hermes_cli.config import read_raw_config

        config = read_raw_config()
    except Exception as exc:
        raise BrowserIdentityError(
            f"could not read browser identity configuration: {exc}"
        ) from exc
    if not isinstance(config, Mapping):
        raise BrowserIdentityError("Hermes configuration root must be a mapping")
    browser_cfg = config.get("browser")
    if browser_cfg is None:
        return {}
    if not isinstance(browser_cfg, Mapping):
        raise BrowserIdentityError("browser configuration must be a mapping")
    return dict(browser_cfg)


def configured_identity_aliases(
    browser_cfg: Mapping[str, Any] | None = None,
) -> tuple[str, ...]:
    """Return validated aliases advertised to the model, sorted for stability."""

    cfg = browser_cfg if browser_cfg is not None else read_browser_identity_config()
    identities = (
        cfg.get("real_profile_identities") if isinstance(cfg, Mapping) else None
    )
    if not isinstance(identities, Mapping):
        return ()
    return tuple(
        sorted(
            alias
            for alias in identities
            if isinstance(alias, str) and _ALIAS_RE.fullmatch(alias)
        )
    )


def _validate_source_profile(alias: str, raw: Any) -> str:
    if not isinstance(raw, str) or not raw.strip():
        raise BrowserIdentityError(f"browser identity {alias!r} has no source_profile")
    profile = raw.strip()
    if (
        profile in {".", "..", "Guest Profile", "System Profile"}
        or "/" in profile
        or "\\" in profile
    ):
        raise BrowserIdentityError(
            f"browser identity {alias!r} source_profile must be a direct, non-guest profile directory"
        )
    return profile


def resolve_browser_identity(
    requested: str | None,
    *,
    browser_cfg: Mapping[str, Any] | None = None,
) -> BrowserIdentity | None:
    """Resolve an explicit/default named identity, or return None for legacy mode.

    Once ``real_profile_identities`` is non-empty, resolution never consults the
    source browser's mutable ``profile.last_used``. Unknown and malformed
    identities fail closed. ``require_identity`` rejects omission even when a
    default is configured.
    """

    cfg = browser_cfg if browser_cfg is not None else read_browser_identity_config()
    if not isinstance(cfg, Mapping):
        cfg = {}
    raw_identities = cfg.get("real_profile_identities")
    if raw_identities is not None and not isinstance(raw_identities, Mapping):
        raise BrowserIdentityError("browser.real_profile_identities must be a mapping")
    identities = raw_identities if isinstance(raw_identities, Mapping) else {}

    require_identity = cfg.get("require_identity", False)
    if not isinstance(require_identity, bool):
        raise BrowserIdentityError("browser.require_identity must be true or false")

    requested_clean = requested.strip() if isinstance(requested, str) else ""
    if not identities:
        if requested_clean:
            raise BrowserIdentityError(
                f"unknown browser identity {requested_clean!r}: no real_profile_identities are configured"
            )
        if require_identity or cfg.get("default_identity"):
            raise BrowserIdentityError(
                "browser identity configuration requires a non-empty real_profile_identities mapping"
            )
        return None

    invalid_aliases = [
        alias
        for alias in identities
        if not isinstance(alias, str) or not _ALIAS_RE.fullmatch(alias)
    ]
    if invalid_aliases:
        raise BrowserIdentityError(
            "browser identity aliases must use 1-64 lowercase letters, digits, '_' or '-'"
        )

    if require_identity and not requested_clean:
        raise BrowserIdentityError(
            "browser identity is required by browser.require_identity; pass identity explicitly"
        )

    selected = requested_clean
    if not selected:
        default = cfg.get("default_identity")
        selected = default.strip() if isinstance(default, str) else ""
    if not selected:
        raise BrowserIdentityError(
            "no browser identity was supplied and browser.default_identity is not configured"
        )

    raw = identities.get(selected)
    if not isinstance(raw, Mapping):
        raise BrowserIdentityError(f"unknown browser identity {selected!r}")

    browser_raw = raw.get("browser")
    browser = browser_raw.strip().lower() if isinstance(browser_raw, str) else ""
    if browser not in _SUPPORTED_BROWSERS:
        raise BrowserIdentityError(
            f"browser identity {selected!r} browser must be one of: brave, chrome, chromium, edge"
        )
    source_profile = _validate_source_profile(selected, raw.get("source_profile"))
    return BrowserIdentity(
        alias=selected,
        browser=browser,
        source_profile=source_profile,
        runtime_key=browser_identity_runtime_key(selected, browser, source_profile),
    )
