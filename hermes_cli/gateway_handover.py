"""Zero-cut gateway slot handover orchestration.

The orchestration is deliberately separate from the large gateway lifecycle module.  All
launchd and process effects are passed as small callables so the state machine can be tested
without touching a live gateway or launchctl.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess
import time
from typing import Any, Callable

from gateway.control_socket import query_gateway_control
from gateway.gateway_slot import SLOT_ACTIVE, SLOT_STANDBY
from gateway.config import is_zero_cut_handover_enabled
from hermes_constants import get_hermes_home

# Standby boot-to-ready, A's updater stop (_UPDATER_STOP_TIMEOUT 15s) and promotion. A keeps serving
# for all of it and is never cut, so this bounds only how long a stuck handover waits before aborting.
STANDBY_READY_BUDGET_S = 180.0
# Promote opens state.db and connects Telegram (up to the adapter's own connect budget).
PROMOTE_TIMEOUT_S = 120.0


def _pid_exists(pid: int) -> bool:
    from gateway.status import _pid_exists as status_pid_exists
    return bool(status_pid_exists(pid))


@dataclass(frozen=True)
class HandoverEffects:
    """Injectable external effects used by :func:`run_handover`."""

    query: Callable[..., dict[str, Any] | None] = query_gateway_control
    bootstrap: Callable[[str, Path], None] | None = None
    bootout: Callable[[str], None] | None = None
    process_exists: Callable[[int], bool] = _pid_exists
    monotonic: Callable[[], float] = time.monotonic
    sleep: Callable[[float], None] = time.sleep
    write_plist: Callable[[Path, str], None] | None = None


def _launchctl_runner(args: list[str]) -> None:
    subprocess.run(["launchctl", *args], check=True, timeout=30)


def _default_bootstrap(label: str, plist_path: Path) -> None:
    from hermes_cli.gateway_launchd import bootstrap_launchd_label
    bootstrap_launchd_label(label, plist_path, runner=_launchctl_runner)


def _default_bootout(label: str) -> None:
    from hermes_cli.gateway_launchd import bootout_launchd_label
    bootout_launchd_label(label, runner=_launchctl_runner)


def _default_write_plist(path: Path, contents: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(contents, encoding="utf-8")


def _effects(effects: HandoverEffects | None) -> HandoverEffects:
    if effects is not None:
        return effects
    return HandoverEffects(
        process_exists=_pid_exists,
        bootstrap=_default_bootstrap,
        bootout=_default_bootout,
        write_plist=_default_write_plist,
    )


def _query(effects: HandoverEffects, home: Path, verb: str, slot: str, *, timeout: float = 2.0) -> dict[str, Any] | None:
    try:
        return effects.query(home, verb, slot=slot, timeout=timeout)
    except TypeError:
        # Keep the seam friendly to tiny test doubles and older local callers.
        return effects.query(home, verb, slot=slot)


def _slot_snapshot(effects: HandoverEffects, home: Path, slot: str) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    """Read both identity and live status; roles are never inferred from filenames."""
    return _query(effects, home, "identify", slot), _query(effects, home, "status", slot)


def _deadline(effects: HandoverEffects, budget: float) -> float:
    return effects.monotonic() + max(float(budget), 0.0)


def _poll(effects: HandoverEffects, deadline: float, fn: Callable[[], bool]) -> bool:
    while True:
        if fn():
            return True
        if effects.monotonic() >= deadline:
            return False
        effects.sleep(min(0.1, max(0.0, deadline - effects.monotonic())) if deadline != float("inf") else 0.5)


def _slot_plist_path(label: str) -> Path:
    from hermes_cli.gateway import get_launchd_plist_path
    return get_launchd_plist_path().with_name(f"{label}.plist")


def _failure(effects: HandoverEffects, idle_label: str, message: str, *, cleanup: bool = True) -> int:
    if cleanup:
        try:
            assert effects.bootout is not None
            effects.bootout(idle_label)
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:  # cleanup must not hide the handover reason
            print(f"✗ {message}; also failed to boot out standby: {exc}")
            return 1
    print(f"✗ {message}")
    return 1


def run_handover(*, home: Path | None = None, enabled: bool | None = None,
                 timeout: float, effects: HandoverEffects | None = None) -> int:
    """Perform one A→B handover and return a CLI exit code."""
    home = Path(home or get_hermes_home()).expanduser().resolve()
    fx = _effects(effects)
    if enabled is False:
        print("✗ Zero-cut gateway handover is disabled (set gateway.zero_cut_handover.enabled: true).")
        return 2

    from hermes_cli.gateway_launchd import generate_launchd_plist, get_launchd_slot_labels
    labels = get_launchd_slot_labels()
    label_by_slot = {SLOT_ACTIVE: labels[0], SLOT_STANDBY: labels[1]}
    snapshots = {slot: _slot_snapshot(fx, home, slot) for slot in (SLOT_ACTIVE, SLOT_STANDBY)}
    active_slots = [slot for slot, (identity, _status) in snapshots.items()
                    if identity is not None and identity.get("standby") is False]
    if len(active_slots) != 1:
        return _failure(fx, labels[1], f"refusing handover: expected exactly one active slot, found {len(active_slots)}", cleanup=False)
    active_slot = active_slots[0]
    idle_slot = SLOT_STANDBY if active_slot == SLOT_ACTIVE else SLOT_ACTIVE
    active_label = label_by_slot[active_slot]
    idle_label = label_by_slot[idle_slot]

    idle_identity, idle_status = snapshots[idle_slot]
    idle_snapshot = idle_identity or idle_status
    if idle_snapshot is not None and idle_snapshot.get("standby") is not True:
        return _failure(fx, idle_label, "refusing handover: idle slot is active", cleanup=False)
    reused = idle_identity is not None or idle_status is not None
    if reused and not bool((idle_status or idle_identity or {}).get("ready")):
        return _failure(fx, idle_label, "refusing handover: standby slot is running but not ready")

    if not reused:
        assert fx.bootstrap is not None and fx.write_plist is not None
        plist_path = _slot_plist_path(idle_label)
        fx.write_plist(plist_path, generate_launchd_plist(slot=idle_slot))
        try:
            fx.bootstrap(idle_label, plist_path)
        except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
            # A slot that exited cleanly stays loaded (KeepAlive restarts only failures), and bootstrap
            # of a loaded label fails with EIO. Nothing answers on that slot (reused is False), so
            # booting the stale label out and bootstrapping once more is safe (found in a real drill).
            try:
                assert fx.bootout is not None
                fx.bootout(idle_label)
                fx.bootstrap(idle_label, plist_path)
            except (OSError, RuntimeError, subprocess.SubprocessError) as retry_exc:
                print(f"✗ could not bootstrap standby {idle_label}: {exc}; retry after bootout: {retry_exc}")
                return 1

    budget_deadline = _deadline(fx, timeout)
    if not _poll(fx, budget_deadline, lambda: bool(
        (_query(fx, home, "status", idle_slot) or {}).get("ready") is True and
        (_query(fx, home, "status", idle_slot) or {}).get("standby") is True)):
        return _failure(fx, idle_label, "standby did not become ready before the handover budget expired")

    accepted = _query(fx, home, "handover", active_slot)
    if not accepted or accepted.get("accepted") is not True:
        # A lost answer may still have been accepted, and an accepted handover is not cancellable: the
        # old slot will release Telegram and exit once idle. Boot the standby out only when the old slot
        # proves it did not accept; otherwise carry on as accepted.
        status = _query(fx, home, "handover-status", active_slot)
        if not (status or {}).get("accepted"):
            return _failure(fx, idle_label, f"active gateway refused handover: {accepted or 'no response'}")

    old_identity, old_status = snapshots[active_slot]
    old_pid = ((old_identity or {}).get("pid") or (old_status or {}).get("pid"))
    state_holder: list[dict[str, Any] | None] = [None]
    # Accepted: the old slot settles and releases with no budget of ours (it is never cut), so wait for
    # it without a deadline. Only its own reported failure may boot the standby out.
    _poll(fx, float("inf"), lambda: _handover_released(fx, home, state_holder, active_slot, old_pid))
    handover_state = state_holder[0]
    if (handover_state or {}).get("failed") or (handover_state or {}).get("status") == "failed":
        return _failure(fx, idle_label, str((handover_state or {}).get("failed") or "handover failed"))
    budget_deadline = _deadline(fx, timeout)  # fresh budget for promotion, after the release

    # Promote connects Telegram and opens the shared store, which outlasts a 2s query. A promote whose
    # answer is lost may still have taken effect, so never boot the standby out on silence: ask its
    # status, and treat it as promoted once it reports standby:false.
    promoted = _query(fx, home, "promote", idle_slot, timeout=PROMOTE_TIMEOUT_S)
    if not promoted:
        _poll(fx, budget_deadline, lambda: (_query(fx, home, "status", idle_slot) or {}).get("standby") is False)
        if (_query(fx, home, "status", idle_slot) or {}).get("standby") is False:
            promoted = {"promoted": True, "failed": [], "recovered_from_lost_answer": True}
        else:
            print(f"✗ standby {idle_label} did not answer promotion; leaving it running for inspection")
            return 1
    failed = (promoted or {}).get("failed") or []
    if failed:
        print(f"⚠ promotion reported failed platforms: {', '.join(map(str, failed))}")
    if not promoted or promoted.get("promoted") is not True:
        # A has already released Telegram and will exit once idle: booting B out here would leave
        # nothing serving. B stays up (its reconnect queue heals failed platforms); report and stop.
        print(f"✗ standby promotion failed: {promoted or 'no response'}; leaving {idle_label} running")
        return 1
    # From here the idle slot owns Telegram: no failure path may boot it out.
    if not _poll(fx, budget_deadline, lambda: bool(
        (_query(fx, home, "status", idle_slot) or {}).get("standby") is False)):
        print(f"⚠ {idle_label} accepted promotion but has not reported active status yet; leaving it running")

    # The old slot finishes its own in-flight turns with no drain budget (zero-cut), then exits 0.
    print(f"… {idle_label} is serving; waiting for {active_label} to finish its in-flight turns and exit")
    _poll(fx, float("inf"), lambda: _old_active_exited(fx, home, old_pid, active_slot))
    assert fx.bootout is not None
    try:
        fx.bootout(active_label)
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        if _query(fx, home, "identify", active_slot) is None:
            print(f"⚠ {active_label} was already gone after promotion; continuing")
        else:
            print(f"✗ new standby is promoted, but could not boot out old slot {active_label}: {exc}")
            return 1
    print(f"✓ Gateway handover complete: {active_label} → {idle_label}")
    return 0


def _handover_released(fx: HandoverEffects, home: Path, holder: list[Any], slot: str,
                      old_pid: Any) -> bool:
    state = _query(fx, home, "handover-status", slot)
    holder[0] = state
    if state and (state.get("failed") or state.get("status") == "failed"):
        return True
    if state and state.get("token_lock_released") is True:
        return True
    identity = _query(fx, home, "identify", slot)
    dead = identity is None
    if not dead and old_pid is not None:
        try:
            dead = not fx.process_exists(int(old_pid))
        except (TypeError, ValueError, OSError):
            dead = False
    if dead:
        if not isinstance(holder[0], dict) or not holder[0].get("active_died"):
            print(f"⚠ active slot {slot} died mid-drain; treating its token lock as released")
            holder[0] = {**(state or {}), "active_died": True}
        return True
    return False


def _old_active_exited(fx: HandoverEffects, home: Path, pid: Any, slot: str) -> bool:
    identity = _query(fx, home, "identify", slot)
    if identity is None:
        return True
    try:
        return pid is not None and not fx.process_exists(int(pid))
    except (TypeError, ValueError, OSError):
        return False


def cmd_handover(args) -> int:
    from hermes_cli.config import load_config_readonly
    config = load_config_readonly()
    if not is_zero_cut_handover_enabled(config):
        return run_handover(enabled=False, timeout=0)
    # Budget for the standby to boot and for A's updater stop; A serves throughout, so this is not a
    # drain timeout (restart_drain_timeout can be 0, which aborted every handover instantly).
    return run_handover(timeout=STANDBY_READY_BUDGET_S)
