"""Durable records about gateway launchd definitions: removal audit lines and the reload fence.

Both live in the profile, outside the gateway process. A planned restart's reload helper and the
independent guardian read them to agree on who owns a label while it is briefly unloaded.
"""
from __future__ import annotations

from datetime import datetime
import json
import os
from pathlib import Path
import sys
import time
import uuid

RELOAD_PENDING_NAME = "launchd-reload-pending.json"
# Two drain-sized waits at the largest drain timeout a deployment plausibly configures.
MAX_RELOAD_FENCE_SECONDS = 2 * 3600


def _home(home: Path | None) -> Path:
    if home is not None:
        return Path(home)
    from hermes_constants import get_hermes_home
    return get_hermes_home()


def lifecycle_log_path(home: Path | None = None) -> Path:
    return _home(home) / "logs" / "launchd-reload.log"


def append_lifecycle_log(message: str, *, home: Path | None = None) -> None:
    """Append one timestamped line; launchd recovery must never fail on its audit trail."""
    path = lifecycle_log_path(home)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
        with path.open("a", encoding="utf-8") as stream:
            stream.write(f"[{stamp}] {message}\n")
    except OSError:
        pass


def _call_path(depth: int = 4) -> str:
    """Name the Hermes frames that asked for a removal, nearest first."""
    names = []
    frame = sys._getframe(2)
    while frame is not None and len(names) < depth:
        module = frame.f_globals.get("__name__", "")
        if module.startswith(("hermes_cli.", "gateway.")) and module != __name__:
            names.append(f"{module.rsplit('.', 1)[-1]}.{frame.f_code.co_name}")
        frame = frame.f_back
    return " < ".join(names) or "unknown"


def stop_owning_guardian(path: Path) -> None:
    """Before a deliberate service removal, record the stopped intent for the definition's own home
    so its guardian cannot regenerate the service in the window before its own uninstall."""
    import plistlib
    try:
        owner = plistlib.loads(path.read_bytes()).get("EnvironmentVariables", {}).get("HERMES_HOME")
    except Exception:  # noqa: BLE001 - plistlib has no single error class; no owner, no intent
        return
    if isinstance(owner, str) and Path(owner).is_dir():
        from hermes_cli.gateway_guardian import set_intent
        set_intent(Path(owner), stopped=True)


def remove_definition(path: Path, *, reason: str, home: Path | None = None,
                      generation_id: str | None = None, missing_ok: bool = False) -> bool:
    """Unlink a LaunchAgents plist and record who removed it, for which generation and why."""
    path = Path(path)
    try:
        path.unlink()
    except FileNotFoundError:
        if not missing_ok:
            raise
        return False
    append_lifecycle_log(
        f"Removed launchd definition {path} (caller={_call_path()}, "
        f"generation={generation_id or 'none'}, reason={reason})", home=home)
    return True


def reload_pending_path(home: Path) -> Path:
    return Path(home) / RELOAD_PENDING_NAME


def write_reload_pending(home: Path, *, label: str, generation_id: str | None,
                         seconds: float) -> str:
    """Fence ``label`` for one deferred reload; return the nonce its helper clears."""
    nonce = uuid.uuid4().hex
    now = time.time()
    record = {"label": label, "generation_id": generation_id, "nonce": nonce,
              "created_at": now, "expires_at": now + seconds}
    path = reload_pending_path(home)
    temporary = path.with_name(f".{path.name}.{nonce}.tmp")
    temporary.write_text(json.dumps(record, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, path)
    return nonce


def clear_reload_pending(home: Path, nonce: str) -> None:
    """Remove only this reload's record; a newer reload owns its own fence."""
    path = reload_pending_path(home)
    try:
        if json.loads(path.read_text(encoding="utf-8-sig")).get("nonce") == nonce:
            path.unlink()
    except (OSError, ValueError, AttributeError):
        pass


def reload_pending(home: Path) -> dict | None:
    """The unexpired reload fence, if any. A malformed record cannot fence repair forever."""
    try:
        record = json.loads(reload_pending_path(home).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        return None
    if not isinstance(record, dict) or not isinstance(record.get("label"), str):
        return None
    expires, created, now = record.get("expires_at"), record.get("created_at"), time.time()
    if type(expires) not in (int, float) or type(created) not in (int, float):
        return None
    # A record from the future or with an implausible lifetime is clock damage, not a reload.
    if expires <= now or created > now + 60 or expires - created > MAX_RELOAD_FENCE_SECONDS:
        return None
    return record
