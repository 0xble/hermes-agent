"""Process slot identity for zero-cut gateway handover.

A handover runs two launchd slots against ONE ``HERMES_HOME``: slot ``a`` and slot ``b``. Only when
``gateway.zero_cut_handover.enabled`` is on AND this process's sibling reports a live active gateway
does its runtime role become standby. Slot identity still controls suffixed runtime lock, PID file and
control socket names; role and slot are independent. Readers that look up "the" gateway of a home keep
resolving slot ``a`` unless they pass a slot.
"""

from __future__ import annotations

import os
from typing import Any

SLOT_ACTIVE = "a"
SLOT_STANDBY = "b"

# Pinned once by gateway startup so every state path in one process uses one slot and role.
_PINNED_GATEWAY_SLOT: str | None = None
_PINNED_GATEWAY_STANDBY: bool | None = None


def pin_gateway_slot(config: Any = None) -> str:
    """Pin this process to its slot and active/standby role for the lifetime of the boot."""
    global _PINNED_GATEWAY_SLOT, _PINNED_GATEWAY_STANDBY
    if _PINNED_GATEWAY_SLOT is None:
        _PINNED_GATEWAY_SLOT = _effective_gateway_slot_uncached(config)
        _PINNED_GATEWAY_STANDBY = _resolve_gateway_role(_PINNED_GATEWAY_SLOT, config)
    return _PINNED_GATEWAY_SLOT


def reset_gateway_slot_for_tests() -> None:
    """Clear the process slot and role pins for isolated tests."""
    global _PINNED_GATEWAY_SLOT, _PINNED_GATEWAY_STANDBY
    _PINNED_GATEWAY_SLOT = None
    _PINNED_GATEWAY_STANDBY = None


def get_gateway_slot() -> str:
    """This process's launchd slot: ``b`` only when its environment says so, else ``a``.

    Read at call time (never cached at import). Anything but ``b`` is the active slot.
    """
    # health: allow HX002 -- per-process launchd slot identity; both slots share one config.yaml
    value = os.environ.get("HERMES_GATEWAY_SLOT", "").strip().lower()
    return SLOT_STANDBY if value == SLOT_STANDBY else SLOT_ACTIVE


def _effective_gateway_slot_uncached(config: Any = None) -> str:
    """Resolve the slot before the process-wide pin is applied.

    ``config`` is a GatewayConfig or config mapping; ``None`` loads this home's config, and only
    after the env already says ``b``, so slot ``a`` never pays for a config read.
    """
    if get_gateway_slot() != SLOT_STANDBY:
        return SLOT_ACTIVE
    from gateway.config import is_zero_cut_handover_enabled
    if config is None:
        from hermes_cli.config import load_config_readonly
        config = load_config_readonly()
    return SLOT_STANDBY if is_zero_cut_handover_enabled(config) else SLOT_ACTIVE


def _query_other_slot(home, slot: str, timeout: float) -> dict[str, Any] | None:
    """Read one sibling slot's live status without making slot resolution recursive."""
    from gateway.control_socket import query_gateway_control
    return query_gateway_control(home, "status", slot=slot, timeout=timeout)


def _resolve_gateway_role(slot: str, config: Any = None, *, timeout: float = 0.5) -> bool:
    """Return whether ``slot`` should boot standby based on the sibling's live status."""
    from gateway.config import is_zero_cut_handover_enabled
    if config is None:  # same source the slot resolver reads; None alone would mean "flag off"
        from hermes_cli.config import load_config_readonly
        config = load_config_readonly()
    if not is_zero_cut_handover_enabled(config):
        return False
    from hermes_constants import get_hermes_home
    other_slot = SLOT_STANDBY if slot == SLOT_ACTIVE else SLOT_ACTIVE
    home = get_hermes_home()
    try:
        status = _query_other_slot(home, other_slot, timeout)
    except (OSError, RuntimeError, ValueError, TypeError, ImportError):
        status = None
    if not status:
        # No answer is not "not standby" while the sibling holds its runtime lock: a slow or wedged
        # serving gateway would otherwise have this one boot active and take its runtime epoch.
        return slot_lock_held(home, other_slot)
    return status.get("standby") is False


def effective_gateway_standby(config: Any = None) -> bool:
    """Return this process's pinned role, resolving it once if startup has not pinned it yet."""
    if _PINNED_GATEWAY_STANDBY is not None:
        return _PINNED_GATEWAY_STANDBY
    return _resolve_gateway_role(effective_gateway_slot(config), config)


def effective_gateway_slot(config: Any = None) -> str:
    """Return the pinned slot, or resolve it dynamically before gateway startup pins it."""
    if _PINNED_GATEWAY_SLOT is not None:
        return _PINNED_GATEWAY_SLOT
    return _effective_gateway_slot_uncached(config)


def slot_filename(filename: str, slot: str = SLOT_ACTIVE) -> str:
    """``gateway.pid`` -> ``gateway.b.pid`` for slot ``b``; any other slot returns ``filename``.

    The suffix goes after the first dot, so ``gateway.sock.path`` -> ``gateway.b.sock.path``.
    """
    if slot != SLOT_STANDBY:
        return filename
    stem, dot, rest = filename.partition(".")
    return f"{stem}.{SLOT_STANDBY}{dot}{rest}" if dot else f"{filename}.{SLOT_STANDBY}"


def process_slot_filename(filename: str) -> str:
    """``filename`` as this process names it: a gateway's own slot, or for a client the serving slot."""
    return slot_filename(filename, serving_slot())


def serving_slot(home: Any = None) -> str:
    """The slot a caller means when it names none. For this process's own files inside a gateway: its
    pinned slot. Otherwise ``a``, unless only ``b`` holds its runtime lock (a promoted ``b`` after
    ``a`` exited), so status and control clients follow the gateway that serves the home."""
    if home is None and (_PINNED_GATEWAY_SLOT is not None or get_gateway_slot() == SLOT_STANDBY):
        return effective_gateway_slot()
    from pathlib import Path

    from gateway.status import _get_process_hermes_home
    root = Path(home) if home is not None else _get_process_hermes_home()
    # Probe b first: with the flag off ``gateway.b.lock`` never exists, so this costs one stat.
    if slot_lock_held(root, SLOT_STANDBY) and not slot_lock_held(root, SLOT_ACTIVE):
        return SLOT_STANDBY
    return SLOT_ACTIVE


def slot_lock_held(home: Any, slot: str) -> bool:
    """True while some live process holds ``slot``'s runtime lock in ``home`` (a direct flock probe;
    the status helpers resolve paths through this module and would recurse). A missing lock file is
    one ``stat`` and opens nothing, so flag-off readers pay no file handle for it. A lock file that
    exists but cannot be opened (another user's inode, an I/O error) is reported held, as
    ``is_gateway_runtime_lock_active`` does: unknown ownership never grants it."""
    return slot_lock_state(home, slot) != LOCK_FREE


#: :func:`slot_lock_state` answers.
LOCK_FREE, LOCK_HELD, LOCK_UNKNOWN = "free", "held", "unknown"


def slot_lock_state(home: Any, slot: str) -> str:
    """``free`` (no lock file, or one nobody holds), ``held`` (a flock probe lost), or ``unknown``
    (the file exists but cannot be opened). Callers that wait for a release tell ``unknown`` apart,
    since it may never change; ownership checks treat it as held."""
    import os
    from pathlib import Path

    from gateway.status import _probe_lock_file
    path = Path(home) / slot_filename("gateway.lock", slot)
    if not os.path.exists(path):
        return LOCK_FREE
    try:
        handle = open(path, "a+", encoding="utf-8")
    except FileNotFoundError:
        return LOCK_FREE  # removed since the stat: nobody holds it
    except OSError:
        return LOCK_UNKNOWN
    return LOCK_HELD if _probe_lock_file(handle) else LOCK_FREE


def sibling_lock_name(pid_filename: str, lock_filename: str) -> str:
    """Runtime-lock name paired with a PID file name: ``gateway.b.pid`` pairs with ``gateway.b.lock``.

    Keyed on the PID file's own name so explicit-path callers that probe a home's ``gateway.pid``
    keep the historical ``gateway.lock`` whatever slot the calling process runs as.
    """
    standby = pid_filename == slot_filename("gateway.pid", SLOT_STANDBY)
    return slot_filename(lock_filename, SLOT_STANDBY if standby else SLOT_ACTIVE)
