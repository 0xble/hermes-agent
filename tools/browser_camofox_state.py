"""Hermes-managed Camofox state and named-identity helpers."""

from __future__ import annotations

import hashlib
import os
import shutil
import threading
import uuid
from pathlib import Path
from typing import Dict, Optional

from hermes_constants import get_hermes_home, hermes_home_key


class CamofoxIdentityError(ValueError):
    """A Camofox identity is missing, invalid, or conflicts with global state."""


def get_camofox_state_dir() -> Path:
    """Return the active Hermes-home root for Camofox state and claims."""
    return get_hermes_home() / "browser_auth" / "camofox"


def get_camofox_identity(task_id: Optional[str] = None) -> Dict[str, str]:
    """Legacy managed-persistence identity, scoped to the active Hermes home."""
    scope_root = str(get_camofox_state_dir())
    user_digest = uuid.uuid5(uuid.NAMESPACE_URL, f"camofox-user:{scope_root}").hex[:10]
    session_digest = uuid.uuid5(uuid.NAMESPACE_URL, f"camofox-session:{scope_root}:{task_id or 'default'}").hex[:16]
    return {"user_id": f"hermes_{user_digest}", "session_key": f"task_{session_digest}"}


def resolve_camofox_identity(alias: Optional[str], task_id: Optional[str] = None) -> Dict[str, str]:
    """Resolve one configured browser alias to opaque Camofox identifiers.

    Alias validation is intentionally delegated to the shared browser identity registry.
    The server-visible identifiers are derived from the active Hermes home and immutable
    registry runtime key; aliases and source-profile names never leave Hermes.
    """
    from hermes_cli.browser_identity import BrowserIdentityError, resolve_browser_identity

    try:
        identity = resolve_browser_identity(alias)
    except BrowserIdentityError as exc:
        raise CamofoxIdentityError(str(exc)) from exc
    if identity is None:
        raise CamofoxIdentityError("Camofox requires an explicit configured browser identity")
    scope = f"{hermes_home_key()}:{identity.runtime_key}"
    user_digest = hashlib.sha256(f"camofox-user:{scope}".encode()).hexdigest()[:24]
    task_digest = hashlib.sha256(f"camofox-tab:{scope}:{task_id or 'default'}".encode()).hexdigest()[:24]
    return {
        "alias": identity.alias,
        "identity_key": hashlib.sha256(scope.encode()).hexdigest()[:24],
        "user_id": f"hermes_camofox_{user_digest}",
        "session_key": f"task_{task_digest}",
    }


def _binding_dir(task_id: str) -> Path:
    digest = hashlib.sha256((task_id or "default").encode()).hexdigest()
    return get_camofox_state_dir() / "bindings" / digest


def read_camofox_binding(task_id: Optional[str]) -> Optional[Dict[str, str]]:
    """Return the immutable Camofox task binding; corrupt claims fail closed."""
    claim = _binding_dir(task_id or "default")
    if not claim.exists():
        return None
    try:
        values = {name: (claim / name).read_text(encoding="utf-8").strip() for name in ("backend", "alias", "identity_key", "user_id", "session_key")}
    except OSError as exc:
        raise CamofoxIdentityError("Camofox task binding is unreadable; start a new task") from exc
    if values["backend"] != "camofox" or any(not value or len(value) > 128 or any(ch.isspace() for ch in value) for value in values.values()):
        raise CamofoxIdentityError("Camofox task binding is corrupt; start a new task")
    return values


def reject_non_camofox_binding(task_id: Optional[str]) -> None:
    """Refuse a Camofox attach when the task already owns a Chrome identity.

    The established real-profile binding predates Camofox and has a deliberately
    different on-disk shape.  Inspect only its existence here: its own reader is
    the authority for corruption and identity validation on the Chrome path.
    """
    digest = hashlib.sha256((task_id or "default").encode("utf-8")).hexdigest()
    claim = get_hermes_home() / "browser-profile" / "agent-browser-bindings" / digest
    if claim.exists():
        raise CamofoxIdentityError(
            "browser task is already bound to another backend or identity; start a new task instead of switching cookie jars")


def claim_camofox_binding(task_id: Optional[str], identity: Dict[str, str]) -> Dict[str, str]:
    """Atomically bind a task to Camofox plus one identity across process restarts."""
    from hermes_cli.browser_identity import BrowserIdentityProcessLock

    digest = hashlib.sha256((task_id or "default").encode()).hexdigest()
    with BrowserIdentityProcessLock(digest, task_binding=True):
        reject_non_camofox_binding(task_id)
        existing = read_camofox_binding(task_id)
        expected = {"backend": "camofox", **{key: identity[key] for key in ("alias", "identity_key", "user_id", "session_key")}}
        if existing is not None:
            if existing != expected:
                raise CamofoxIdentityError("browser task is already bound to another backend or identity; start a new task instead of switching cookie jars")
            return existing
        claim = _binding_dir(task_id or "default")
        root = claim.parent
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            os.chmod(root, 0o700)
        except OSError:
            pass
        temporary = root / f".{claim.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        try:
            temporary.mkdir(mode=0o700)
            for key, value in expected.items():
                (temporary / key).write_text(value + "\n", encoding="utf-8")
            try:
                temporary.rename(claim)
            except FileExistsError:
                pass
            except OSError:
                # A populated competing claim can produce ENOTEMPTY, not EEXIST.
                # Its existence permits only the strict winner verification below.
                if not claim.exists():
                    raise
        finally:
            if temporary.exists():
                shutil.rmtree(temporary, ignore_errors=True)
        verified = read_camofox_binding(task_id)
        if verified != expected:
            raise CamofoxIdentityError("browser task is already bound to another backend or identity; start a new task instead of switching cookie jars")
        return expected


# ---- BEGIN PLUGIN-COMPAT (revert-scheduled; see COMPAT_MANIFEST.md) ----
CAMOFOX_STATE_DIR_NAME = "browser_auth"
CAMOFOX_STATE_SUBDIR = "camofox"
# ---- END PLUGIN-COMPAT ----
