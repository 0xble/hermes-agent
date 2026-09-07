"""Visibility control for a verified Hermes-owned named-identity browser."""
from __future__ import annotations

import json
import os
import secrets
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


def _write_state_path(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.parent.chmod(0o700)
    temporary = path.with_suffix(".tmp")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as stream:
        json.dump(value, stream)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def _write_state(identity, value: dict) -> None:
    _write_state_path(_state_path(identity), value)


@contextmanager
def activity(identity, *, timeout: float = 0) -> Iterator[None]:
    """Serialize execution and visibility changes for one identity across processes."""
    with BrowserIdentityProcessLock(identity.runtime_key + "-visibility", timeout=timeout):
        yield


def mark_executing(
    identity,
    endpoint: str,
    *,
    daemon_name: str = "",
    runtime_owner: str = "",
    daemon_identity: tuple[int, float] | None = None,
) -> bool:
    """Record uncertain activity before starting Browser Use code."""
    if _read_state(identity).get("pending"):
        return False
    state: dict = {
        "pending": str(endpoint or "unknown"),
        "daemon": daemon_name,
        "runtime_owner": runtime_owner,
        "generation": secrets.token_hex(16),
    }
    if daemon_identity is not None:
        state["daemon_pid"], state["daemon_created"] = daemon_identity
    _write_state(identity, state)
    return True


def mark_finished(identity, *, daemon_name: str = "", runtime_owner: str = "") -> bool:
    """Clear only this completed invocation; timeouts remain fail-closed."""
    state = _read_state(identity)
    if not state.get("pending"):
        return False
    if daemon_name and (state.get("daemon") != daemon_name or state.get("runtime_owner") != runtime_owner):
        return False
    _write_state(identity, {})
    return True


def pending_daemon_recovery_state(runtime_owner: str, daemon_name: str) -> list[dict]:
    """Snapshot recovery claims for an exact daemon before it is reloaded."""
    from hermes_constants import get_hermes_home

    root = get_hermes_home() / "browser-use" / "visibility-handoff"
    if not root.is_dir():
        return []
    states = []
    for path in root.glob("*.json"):
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if (isinstance(state, dict) and state.get("pending")
                and state.get("daemon") == daemon_name
                and state.get("runtime_owner") == runtime_owner):
            states.append(state)
    return states


def clear_pending_after_daemon_reload(
    runtime_owner: str,
    daemon_name: str,
    daemon_identity: tuple[int, float],
    *,
    expected_generation: str | None = None,
) -> bool:
    """Clear a timeout marker only after its exact daemon was successfully reloaded.

    The caller supplies the exact PID/start-time pair it proved terminated. Missing,
    corrupt, old-format, different-daemon, or newer-generation markers remain
    fail-closed.
    """
    from hermes_constants import get_hermes_home

    root = get_hermes_home() / "browser-use" / "visibility-handoff"
    if not root.is_dir():
        return False
    cleared = False
    for path in root.glob("*.json"):
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(state, dict):
            continue
        runtime_key = path.stem
        try:
            with BrowserIdentityProcessLock(runtime_key + "-visibility", timeout=0):
                state = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(state, dict):
                    continue
                if (state.get("pending") and state.get("daemon") == daemon_name
                        and state.get("runtime_owner") == runtime_owner
                        and state.get("daemon_pid") == daemon_identity[0]
                        and state.get("daemon_created") == daemon_identity[1]
                        and (expected_generation is None or state.get("generation") == expected_generation)):
                    _write_state_path(path, {})
                    cleared = True
        except BrowserIdentityError:
            continue
    return cleared


def _assert_no_pending(identity) -> None:
    if _read_state(identity).get("pending"):
        raise HandoffError(
            "A prior browser execution is unfinished or uncertain; its daemon termination cannot be verified, "
            "so visibility was not changed"
        )



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
