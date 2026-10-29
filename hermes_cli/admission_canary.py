"""Post-ack native-admission qualification shared by update and the guardian."""
from __future__ import annotations

import json
import math
import time
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Any

from hermes_cli.immutable_releases import _atomic_json


class AdmissionCanaryError(RuntimeError):
    pass


def settings(home: Path) -> tuple[bool, float, int]:
    from hermes_cli.config import require_readable_config_before_write, _expand_env_vars, _merge_managed_overlay
    # The guardian needs its own policy even when an unrelated runtime setting is broken.
    # Keep raw-read and overlay scope intact without admitting this document for execution.
    config = require_readable_config_before_write(home / "config.yaml")
    config, _ = _merge_managed_overlay(_expand_env_vars(config))
    section = config
    for key in ("gateway", "guardian", "admission_canary"):
        section = section.get(key, {})
        if section is None and key != "admission_canary":
            section = {}
        if not isinstance(section, dict):
            raise ValueError("gateway.guardian.admission_canary must be a mapping")
    enabled = section.get("enabled", True)
    window = section.get("window_seconds", 180)
    threshold = section.get("refusal_threshold", 10)
    if type(enabled) is not bool:
        raise ValueError("admission_canary.enabled must be a boolean")
    if type(window) not in (float, int) or not math.isfinite(window) or not 10 <= window <= 300:
        raise ValueError("admission_canary.window_seconds must be between 10 and 300")
    if type(threshold) is not int or not 5 <= threshold <= 1000:
        raise ValueError("admission_canary.refusal_threshold must be between 5 and 1000")
    # 10 refusals in 3 minutes catches a broken ingress without tripping on an
    # isolated invalid input. Count-only avoids a misleading low-traffic ratio.
    return enabled, float(window), threshold


def state_path(home: Path) -> Path:
    return home / "release-admission-canary.json"


def read_state(home: Path) -> dict:
    try:
        return json.loads(state_path(home).read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return {}


def save(home: Path, state: dict, **fields) -> dict:
    result = dict(state, **fields)
    _atomic_json(state_path(home), result)
    return result


def finish_rollback(home: Path, state: dict, healthy: bool) -> None:
    failed = save(home, state, outcome="failed", rollback_healthy=healthy)
    from hermes_cli.gateway_guardian import receipt
    receipt(home, "admission_canary", "failed", **{k: v for k, v in failed.items() if k != "outcome"})
    import logging
    logging.getLogger(__name__).error("%s; rollback healthy=%s", state["reason"], healthy)


def _refusal_counts(home: Path, record: dict, start: float, end: float) -> Counter:
    from gateway.status import read_runtime_status
    runtime = read_runtime_status(home / "gateway_state.json") or {}
    signal = runtime.get("native_admission") or {}
    if runtime.get("code_sha") != Path(record["candidate"]).name or signal.get("pid") != record["reload_ack"].get("gateway_pid"):
        return Counter()
    return Counter(clause for at, clause in signal.get("refusals", []) if start <= at <= end)


def inspect(home: Path, *, persist: bool = True) -> tuple[str, dict | None, dict]:
    """Evaluate an acknowledged current promotion. Persist only under guardian.lock.

    Return its transaction for the existing rollback path. Pre-ack refusals and
    evidence from a different gateway PID/code identity never count.
    """
    if (home / "release-txn.json").exists():
        return "none", None, {}
    last = home / "release-last-txn.json"
    if not last.exists():
        return "none", None, {}
    record = json.loads(last.read_text(encoding="utf-8-sig"))
    ack = record.get("reload_ack") or {}
    if (record.get("operation") not in {"promote", "first-migration"}
            or not ack.get("at") or not record.get("candidate")
            or (home / "current").resolve() != Path(record["candidate"])):
        return "none", None, {}
    enabled, window, threshold = settings(home)
    if not enabled:
        return "disabled", None, {}
    start = datetime.fromisoformat(ack["at"]).timestamp()
    identity = {"candidate": record["candidate"], "ack_at": ack["at"]}
    state: dict[str, Any] = read_state(home)
    if any(state.get(key) != value for key, value in identity.items()):
        state = dict(identity, outcome="watching", window_seconds=window,
                     refusal_threshold=threshold)
        if persist:
            save(home, state)
    if state["outcome"] != "watching":
        return state["outcome"], record, state
    end = start + state["window_seconds"]
    clauses = _refusal_counts(home, record, start, end)
    count = sum(clauses.values())
    dominant = clauses.most_common(1)[0][0] if clauses else None
    detail = {"refusals": count, "dominant_clause": dominant}
    if count >= state["refusal_threshold"]:
        reason = (f"native-admission canary failed: {count} refusals in "
                  f"{state['window_seconds']:g}s, dominant clause={dominant}")
        state = dict(state, outcome="tripped", reason=reason, **detail)
        if persist:
            save(home, state)
        return "tripped", record, state
    if time.time() >= end:
        state = dict(state, outcome="passed", **detail)
        if persist:
            save(home, state)
            from hermes_cli.gateway_guardian import receipt
            receipt(home, "admission_canary", "passed", **{k: v for k, v in state.items() if k != "outcome"})
        return "passed", record, state
    return "watching", record, state


def observe(home: Path, candidate: Path) -> None:
    """Bounded update-side wait. Guardian ticks and the updater share its lock.

    The result survives rollback replacing release-last-txn. Another observer
    cannot turn a failed update into success or roll it back twice.
    """
    from hermes_cli import gateway_guardian as guardian
    status, record, state = inspect(home, persist=False)
    if status == "none":
        prior = read_state(home)
        if prior.get("candidate") == str(candidate) and prior.get("outcome") in {"failed", "rolling_back", "tripped"}:
            raise AdmissionCanaryError(prior["reason"])
        return
    if status == "disabled":
        return
    if not record or record["candidate"] != str(candidate):
        raise AdmissionCanaryError("native-admission canary release identity changed")
    if status == "failed":
        raise AdmissionCanaryError(state["reason"])
    # Quiet observation only reads evidence. Do not bootstrap or restart a
    # gateway just to sample its canary. Only a trip enters guardian recovery.
    end = datetime.fromisoformat(state["ack_at"]).timestamp() + state["window_seconds"]
    remaining = min(state["window_seconds"], max(0, end - time.time()))
    deadline = time.monotonic() + remaining + guardian.ROLLBACK_SECONDS
    while time.monotonic() < deadline:
        with guardian.inspection_lock(home) as acquired:
            if acquired:
                status, record, state = inspect(home)
                if status == "none":
                    state = read_state(home)
                    status = state.get("outcome", "none")
                if state.get("candidate") not in {None, str(candidate)}:
                    raise AdmissionCanaryError("native-admission canary observation changed")
                if status in {"passed", "disabled"}:
                    return
                if status == "failed":
                    raise AdmissionCanaryError(state["reason"])
        if acquired and status in {"tripped", "rolling_back"}:
            assert record is not None
            plist = Path(record["plist"]["path"])
            import plistlib
            label = plistlib.loads(plist.read_bytes())["Label"]
            result = guardian.run_once(home, plist, label, deadline=deadline)
            state = read_state(home)
            if state.get("outcome") == "failed":
                raise AdmissionCanaryError(state["reason"])
            if result not in {"locked", "waiting"}:
                raise AdmissionCanaryError(f"{state['reason']}; guardian recovery={result}")
        guardian._sleep_within(guardian.POLL_SECONDS, deadline)
    raise AdmissionCanaryError("native-admission canary observation timed out")
