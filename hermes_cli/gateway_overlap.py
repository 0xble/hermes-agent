"""Opt-in pinned generation promotion. The coordinator lease, never a release pointer,
controls who can admit work. Ambiguous process or token state fails closed.
"""
from __future__ import annotations

import os
import subprocess
import time
from pathlib import Path

from gateway.generation import GenerationCoordinator, GenerationIdentity, generation_paths
from gateway.run_generation import _generation_request, handover_to_generation
from gateway.status import _get_process_start_time, _pid_exists
from hermes_cli.gateway_launchd_generation import (bootstrap_generation_plist,
    generation_launchd_label, render_generation_launchd_plist)
from hermes_cli.immutable_releases import ReleasePaths, activate_release, _release_is_ready


def _identity(row: dict) -> GenerationIdentity:
    required = set(GenerationIdentity.__dataclass_fields__)
    missing = required - row.keys()
    if missing:
        raise RuntimeError(f"generation identity missing fields: {sorted(missing)}")
    return GenerationIdentity(**{key: row[key] for key in required})


def _live(row: dict) -> bool:
    pid = row["pid"]
    observed = _get_process_start_time(pid) if type(pid) is int and _pid_exists(pid) else None
    return (observed is not None and row["start_fingerprint"] == f"{pid}:{observed}")


def _active_and_prior(home: Path) -> tuple[GenerationCoordinator, dict, dict | None, int]:
    coordinator = GenerationCoordinator(home)
    leases = [lease for lease in coordinator.leases() if lease["resource"] == "active_generation"
              and lease["state"] == "active"]
    if len(leases) != 1:
        raise RuntimeError("overlap requires exactly one active generation lease")
    lease = leases[0]
    rows = {row["id"]: row for row in coordinator.generations()}
    active = rows.get(lease["generation_id"])
    if active is None or not _live(active):
        raise RuntimeError("active generation identity cannot be proved")
    prior = next((row for row in rows.values() if row["state"] == "draining"), None)
    return coordinator, active, prior, lease["epoch"]


def _ready_successor(coordinator: GenerationCoordinator, label: str, sha: str,
                     *, timeout: float = 30) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        rows = [row for row in coordinator.generations()
                if row["label"] == label and row["release_sha"] == sha and row["state"] == "ready"]
        if len(rows) == 1 and _live(rows[0]):
            return rows[0]
        time.sleep(.2)
    raise RuntimeError("pinned standby did not report a live ready identity")


def _generation_socket(home: Path, row: dict) -> Path:
    return generation_paths(home, _identity(row))["socket"]


def _observe_poller(home: Path, row: dict, *, timeout: float = 5) -> dict:
    result = _generation_request(_generation_socket(home, row), "polling_status", timeout=timeout)
    if (result.get("generation_id") != row["id"] or result.get("polling") is not True
            or not result.get("tokens")):
        raise RuntimeError("successor polling has not progressed on a real token")
    return result


def rollback_overlap(home: Path, failed_id: str, old_id: str, epoch: int,
                     *, drain_seconds: float = 7200) -> dict:
    """Stop B's wire before restoring A's fresh lease, then restore the pointer.

    A failed B that cannot answer its control socket is intentionally not taken
    over automatically: a dead PID alone does not prove token-lock release.
    """
    coordinator = GenerationCoordinator(home)
    rows = {row["id"]: row for row in coordinator.generations()}
    failed, old = rows.get(failed_id), rows.get(old_id)
    if failed is None or old is None or not _live(old) or not _live(failed):
        raise RuntimeError("rollback blocked: generation process identity unknown")
    if old["state"] != "draining":
        raise RuntimeError("rollback blocked: prior generation is not draining")
    paths = ReleasePaths.for_home(home)
    old_release = (paths.releases / old["release_sha"]).resolve()
    if not _release_is_ready(old_release, old["release_sha"]):
        raise RuntimeError("rollback blocked: prior release is not intact")
    stopped = _generation_request(_generation_socket(home, failed), "stop_for_rollback", timeout=10)
    if (stopped.get("generation_id"), stopped.get("epoch"), stopped.get("poller_stopped")) != (
            failed_id, epoch, True):
        raise RuntimeError("rollback blocked: successor wire-stop receipt invalid")
    try:
        restored = coordinator.rollback_transfer(failed_id, old_id, epoch, poller_stopped=True,
                                                 drain_seconds=drain_seconds)
        if restored is None:
            raise RuntimeError("rollback blocked: active lease changed")
    except Exception:
        # A stopped B can be re-armed only if B still owns the same lease.
        lease = next((item for item in coordinator.leases()
                      if item["resource"] == "active_generation"), None)
        if lease and (lease["generation_id"], lease["epoch"], lease["state"]) == (
                failed_id, epoch, "active"):
            resumed = _generation_request(_generation_socket(home, failed),
                                          "resume_uncommitted_transfer",
                                          params={"epoch": epoch}, timeout=15)
            if resumed.get("generation_id") != failed_id or resumed.get("polling") is not True:
                raise RuntimeError("rollback blocked: successor did not re-arm")
        raise
    response = _generation_request(_generation_socket(home, old), "restore_after_rollback",
                                   params={"epoch": restored}, timeout=15)
    if response.get("generation_id") != old_id or response.get("polling") is not True:
        raise RuntimeError("rollback blocked: prior generation has not restored polling")
    activate_release(home, old_release, operation="rollback")
    return {"from_id": failed_id, "to_id": old_id, "from_sha": failed["release_sha"],
            "to_sha": old["release_sha"], "from_label": failed["label"],
            "to_label": old["label"], "epoch": restored,
            "cursor": coordinator.transfer_receipts(old_id, epoch - 1)}


def promote_overlap(home: Path, candidate: Path, sha: str, *, drain_seconds: float = 7200,
                    timeout: float = 60) -> dict:
    """Bootstrap B without touching A's label, then promote and observe real polling."""
    from hermes_cli.gateway_guardian import _gateway_domain, _launch_state
    paths = ReleasePaths.for_home(home)
    old_release = paths.current.resolve()
    coordinator, old, prior, epoch = _active_and_prior(home)
    if prior is not None:
        raise RuntimeError("prior generation still draining; do not reuse its label")
    if old["label"] == generation_launchd_label("a"):
        slot = "b"
    elif old["label"] in {"ai.hermes.gateway", generation_launchd_label("b")}:
        slot = "a" if old["label"] != "ai.hermes.gateway" else "b"
    else:
        raise RuntimeError("active generation label is not a recognized overlap slot")
    label = generation_launchd_label(slot)
    domain = _gateway_domain(old["label"], None)
    if _launch_state(domain, label) != "unloaded":
        raise RuntimeError("inactive generation label is already loaded; inspect before promotion")
    if old_release.name != old["release_sha"] or not _release_is_ready(old_release, old["release_sha"]):
        raise RuntimeError("active generation does not match an intact current release")
    plist = home / "generation-plists" / f"{label}.plist"
    plist.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    body = render_generation_launchd_plist(slot=slot, release_sha=sha, release_root=candidate,
                                           interpreter=candidate / ".venv/bin/python", hermes_home=home)
    plist.write_text(body, encoding="utf-8")
    bootstrap_generation_plist(domain=domain, plist_path=plist, label=label)
    try:
        successor = _ready_successor(coordinator, label, sha, timeout=min(timeout, 30))
    except Exception:
        # A still owns the lease; the newly installed standby is ours to retire.
        # A failed bootout is not a successful cleanup and must block retry.
        subprocess.run(["launchctl", "bootout", f"{domain}/{label}"], check=True, timeout=15)
        raise
    activate_release(home, candidate)  # No relaunch of either live generation.
    try:
        promoted_epoch = handover_to_generation(home, successor["id"], timeout=min(timeout, 45),
                                                drain_seconds=drain_seconds)
        poller = _observe_poller(home, successor)
    except Exception as failure:
        # A committed lease cannot be inferred from a missing handover reply.
        # Record a rollback only after readback proves A owns the wire and the
        # immutable pointer was restored; an unprovable recovery is blocked.
        try:
            lease = next((row for row in coordinator.leases() if row["resource"] == "active_generation"), None)
            if lease and lease["generation_id"] == successor["id"]:
                rollback = rollback_overlap(home, successor["id"], old["id"], lease["epoch"],
                                            drain_seconds=drain_seconds)
            else:
                if not lease or (lease["generation_id"], lease["epoch"]) != (old["id"], epoch):
                    raise RuntimeError("active lease became unknown during failed overlap promotion")
                coordinator.abort_transfer(old["id"], successor["id"], epoch)
                resumed = _generation_request(_generation_socket(home, old), "resume_uncommitted_transfer",
                                              params={"epoch": epoch}, timeout=15)
                if resumed.get("generation_id") != old["id"] or resumed.get("polling") is not True:
                    raise RuntimeError("old generation did not prove restored polling")
                activate_release(home, old_release, operation="rollback")
                rollback = {"to_id": old["id"], "epoch": epoch, "polling": True}
            current = next((row for row in coordinator.leases()
                            if row["resource"] == "active_generation"), None)
            if (current is None or current["generation_id"] != old["id"] or
                    current["epoch"] != rollback["epoch"] or paths.current.resolve() != old_release):
                raise RuntimeError("rollback readback did not prove old lease and release")
            return {"outcome": "rolled_back", "failure": str(failure), "rollback": rollback,
                    "old_id": old["id"], "new_id": successor["id"],
                    "old_sha": old["release_sha"], "new_sha": sha}
        except Exception as rollback_error:
            raise RuntimeError(f"overlap blocked after {failure}: {rollback_error}") from rollback_error
    return {"old_id": old["id"], "new_id": successor["id"],
            "old_sha": old["release_sha"], "new_sha": sha,
            "old_label": old["label"], "new_label": label, "epoch": promoted_epoch,
            "poller": poller, "previous": str(old_release), "current": str(candidate)}
