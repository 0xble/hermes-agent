"""Visibility control for a verified Hermes-owned named-identity browser."""
from __future__ import annotations

import json
import os
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from hermes_cli.browser_identity import BrowserIdentityError, BrowserIdentityProcessLock


class HandoffError(BrowserIdentityError):
    """A visibility request could not prove its managed-browser boundary."""


def enabled() -> bool:
    """The dormant config gate for this optional browser_exec extension."""
    from tools.browser_use_cli import _read_browser_cfg

    return _read_browser_cfg().get("visibility_handoff") is True


def _state_path(identity) -> Path:
    from hermes_constants import get_hermes_home

    return get_hermes_home() / "browser-use" / "visibility-handoff" / f"{identity.runtime_key}.json"


def _read_state(identity) -> dict:
    path = _state_path(identity)
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as exc:
        raise HandoffError("Cannot verify managed browser activity state") from exc
    if not isinstance(value, dict):
        raise HandoffError("Cannot verify managed browser activity state")
    return value


def _write_state(identity, value: dict) -> None:
    path = _state_path(identity)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    temporary = path.with_suffix(".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(value, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


@contextmanager
def activity(identity, *, timeout: float = 0) -> Iterator[None]:
    """Serialize execution and visibility changes for one identity across processes."""
    with BrowserIdentityProcessLock(identity.runtime_key + "-visibility", timeout=timeout):
        yield


def mark_executing(identity, endpoint: str) -> bool:
    """Record uncertain activity before starting Browser Use code."""
    if _read_state(identity).get("pending"):
        return False
    _write_state(identity, {"pending": str(endpoint or "unknown")})
    return True


def mark_finished(identity) -> None:
    """Clear only a completed Browser Use invocation; timeouts remain fail-closed."""
    _write_state(identity, {})


def _assert_no_pending(identity) -> None:
    if _read_state(identity).get("pending"):
        raise HandoffError("A prior browser execution is unfinished or uncertain; visibility was not changed")



def _assert_binding(identity, session: str) -> None:
    from tools import browser_use_cli as cli

    owner = cli._read_browser_exec_durable_binding(session)
    expected = cli._browser_exec_runtime_owner(identity)
    if owner != expected:
        raise HandoffError("This browser session is not verified as bound to the requested identity")


def handoff(identity, session: str, action: str) -> dict:
    """Reveal or minimize one verified headed, Hermes-owned browser window."""
    if action not in {"reveal", "minimize"}:
        raise HandoffError("Unknown visibility action")
    _assert_binding(identity, session)
    _assert_no_pending(identity)

    from hermes_cli import browser_connect as connect
    from tools import browser_tool as bt
    from tools import browser_tool_real_profile as real
    from tools.browser_handoff_cdp import HandoffCDP
    from tools.browser_use_cli import _browser_exec_runtime_owner

    copy_dir = connect.real_profile_copy_dir(
        identity.browser, identity=identity.alias, source_profile=identity.source_profile
    )
    _session_name, launch_lock, _cache_key = bt._real_profile_runtime_resources(identity)
    with launch_lock, BrowserIdentityProcessLock(_browser_exec_runtime_owner(identity)):
        endpoint = real._owned_profile_cdp(copy_dir)
        if not endpoint:
            raise HandoffError("No verified Hermes-owned browser is running for this identity")
        if real._read_real_profile_headed_mode(copy_dir) is not True:
            raise HandoffError("The verified managed browser is not headed; visibility was not changed")
        with HandoffCDP(endpoint) as cdp:
            cdp.assert_owned_headed_command_line(copy_dir)
            result = cdp.set_visibility(action)
    return {"status": action, "identity": identity.alias, "headed": True, **result}
