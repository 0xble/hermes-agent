"""Typed config failures and once-per-revision operator diagnostics."""
from __future__ import annotations

import threading


class ConfigValidationError(ValueError):
    """The authored config cannot be used, not a provider or sign-in failure."""


_lock = threading.Lock()
_logged: dict[tuple[str, str], tuple | None] = {}


def config_error_reason(exc: BaseException) -> str | None:
    seen: set[int] = set()
    while id(exc) not in seen:
        seen.add(id(exc))
        if isinstance(exc, ConfigValidationError):
            return str(exc)
        link = exc.__cause__ or exc.__context__
        if link is None:
            return None
        exc = link
    return None


def config_error_notice(reason: str) -> str:
    return f"Hermes config is invalid: {reason}; fix config.yaml"


def log_config_error_once(logger, reason: str, *, context: str = "config") -> None:
    """A bad revision can reject many turns/ticks without flooding the operator log."""
    from hermes_cli.config import get_config_path

    path = get_config_path()
    try:
        stat = path.stat()
        signature = (stat.st_mtime_ns, stat.st_size, stat.st_ino, stat.st_ctime_ns)
    except OSError:
        signature = None
    key = (str(path), context)
    with _lock:
        if key in _logged and _logged[key] == signature:
            return
        _logged[key] = signature
    logger.error("%s (%s)", config_error_notice(reason), context)
