"""Ephemeral filesystem for one incognito interval.

The directory is deliberately outside ``HERMES_HOME``.  The incognito registry is the
authority for ownership; a ContextVar only supplies the session identity to a resolver.
"""
from __future__ import annotations

from pathlib import Path
import hashlib
import logging
import os
import secrets
import shutil
import sys
import threading
from typing import Mapping

from ruamel.yaml.error import YAMLError

PRIVATE_DIR_PREFIX = "hermes-incognito-"
logger = logging.getLogger(__name__)
_lock = threading.RLock()
_session_dirs: dict[str, Path] = {}


def _configured_root() -> Path:
    """Parent of this Hermes home's interval directories.

    The default lives in the account cache root, not the OS temp root (macOS prunes temp
    files untouched for three days, which would break a long private interval), and is
    scoped to the process Hermes home: the startup sweep removes everything under this
    parent, so a parent shared across homes would let any other gateway on the account
    (a test gateway, another profile) delete a live interval. An explicit
    ``incognito.private_dir`` is used as given and must not be shared between homes.
    """
    from hermes_constants import get_process_hermes_home
    home = get_process_hermes_home()
    config_path = home / "config.yaml"
    try:
        from hermes_cli.config_backend import config_exists
        if config_exists(config_path):
            from hermes_cli.config import load_config_readonly
            data = load_config_readonly() or {}
            value = ((data.get("incognito") or {}).get("private_dir")
                     if isinstance(data, Mapping) else None)
            if isinstance(value, str) and value.strip():
                return Path(value).expanduser()
    except (OSError, UnicodeDecodeError, YAMLError):
        logger.debug("Could not read incognito private-dir configuration")
    if sys.platform == "darwin":
        cache = Path.home() / "Library" / "Caches"
    else:
        cache = Path(os.environ.get("XDG_CACHE_HOME", "").strip() or Path.home() / ".cache")
    scope = hashlib.sha256(os.path.realpath(home).encode()).hexdigest()[:16]
    return cache / "Hermes" / "incognito" / scope


def _remove(path: Path) -> None:
    try:
        if path.is_symlink() or path.is_file():
            path.unlink()
        elif path.is_dir():
            shutil.rmtree(path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        # A failed cleanup must not make /incognito off fail after the privacy boundary
        # has already been removed. Startup sweep retries it on the next process.
        from agent.incognito import log_private_failure
        log_private_failure(logger, exc, operation="private cleanup", reason="remove_failed", force=True)


def sweep_private_dirs() -> int:
    """Remove leftover private interval directories before accepting ingress."""
    roots = {_configured_root()}
    removed = 0
    for root in roots:
        try:
            entries = list(root.iterdir())
        except OSError:
            continue
        for path in entries:
            if path.name.startswith(PRIVATE_DIR_PREFIX) or path.name == "unattributed":
                before = path.exists() or path.is_symlink()
                _remove(path)
                if before and not (path.exists() or path.is_symlink()):
                    removed += 1
    with _lock:
        _session_dirs.clear()
    logger.info("Incognito startup sweep removed %d interval directories", removed)
    return removed


def _new_dir() -> Path:
    root = _configured_root()
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{PRIVATE_DIR_PREFIX}{secrets.token_hex(16)}"
    path.mkdir(mode=0o700)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path


def private_dir_for_session(session_id: str | None, *, create: bool = True) -> Path | None:
    """Return the interval directory for a registered private session."""
    if not isinstance(session_id, str) or not session_id:
        return None
    from agent.incognito import is_registered
    if not is_registered(session_id):
        return None
    with _lock:
        path = _session_dirs.get(session_id)
        if path is None or not path.exists():
            if not create:
                return None
            path = _new_dir()
            _session_dirs[session_id] = path
        return path


def bind_private_session(session_id: str | None, path: Path | None) -> None:
    """Bind a routing key alias to an existing interval directory."""
    if isinstance(session_id, str) and session_id and path is not None:
        with _lock:
            _session_dirs[session_id] = path


def discard_private_dir(session_id: str | None) -> None:
    """Delete the interval directory after the registry has been disabled."""
    if not isinstance(session_id, str) or not session_id:
        return
    with _lock:
        path = _session_dirs.pop(session_id, None)
        aliases = [key for key, value in _session_dirs.items() if value == path] if path else []
        for key in aliases:
            _session_dirs.pop(key, None)
    from agent.incognito import any_incognito_sessions, register_session
    for key in aliases:
        register_session(key, False)
    root = path.parent if path is not None else _configured_root()
    if path is not None:
        _remove(path)
    if not any_incognito_sessions():
        with _lock:
            leftovers = {value for value in _session_dirs.values() if value.parent == root}
            for key, value in list(_session_dirs.items()):
                if value.parent == root:
                    _session_dirs.pop(key, None)
        for leftover in leftovers:
            _remove(leftover)
        _remove(root / "unattributed")


def private_path_for_current_session() -> Path | None:
    """Resolve only a positively bound private session; public or unknown turns never inherit it."""
    from agent.incognito import current_session_id
    return private_dir_for_session(current_session_id())


def disposable_path_for_current_session() -> Path | None:
    """Resolve disposable storage for a private identity, or an unattributed backstop."""
    path = private_path_for_current_session()
    if path is not None:
        return path
    from agent.incognito import any_incognito_sessions, current_session_id
    if current_session_id() is not None or not any_incognito_sessions():
        return None
    with _lock:
        paths = set(_session_dirs.values())
    if not paths:
        return None
    root = next(iter(paths)).parent / "unattributed"
    root.mkdir(parents=True, exist_ok=True)
    return root


def private_subdir_for_current_session(relative: str) -> Path | None:
    root = private_path_for_current_session()
    if root is None:
        return None
    path = root / relative
    path.mkdir(parents=True, exist_ok=True)
    return path
