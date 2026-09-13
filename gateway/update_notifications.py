"""Durable native update notices and conservative finalized-receipt interpretation."""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path


def read_pending(home: Path) -> tuple[Path, dict] | None:
    for name in (".update_pending.claimed.json", ".update_pending.json"):
        path = home / name
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                data.setdefault("timestamp", datetime.fromtimestamp(path.stat().st_mtime, timezone.utc).isoformat())
                return path, data
        except (OSError, ValueError):
            continue
    return None


def request_identity(pending: dict) -> str:
    """Stable across pending-to-claimed moves and delivery checkpoints."""
    progress = {"output_offset", "output_batch", "updating_notified", "restarting_notified", "timeout_notified"}
    request = {key: value for key, value in pending.items() if key not in progress}
    return hashlib.sha256(json.dumps(request, sort_keys=True).encode()).hexdigest()


def notice(heading: str, pending: dict, detail: str) -> str:
    reason = pending.get("reason")
    paragraph = reason.strip() if isinstance(reason, str) else ""
    return heading + "\n\n" + " ".join(part for part in (paragraph, detail) if part)


def save_pending(path: Path, pending: dict) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(pending), encoding="utf-8")
    temporary.replace(path)


def _timestamp(value: str) -> float:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed.timestamp()


def process_completed(home: Path, pending: dict) -> bool:
    """A notification deadline cannot stand in for the wrapper's termination proof."""
    name = ".update_process_exit_code" if pending.get("notification_version") == 2 else ".update_exit_code"
    try:
        int((home / name).read_text(encoding="utf-8").strip())
        return True
    except (OSError, ValueError):
        return False


def final_outcome(home: Path, pending: dict) -> tuple[bool, str] | None:
    """None means still waiting; success needs process completion AND runtime proof.

    Older manual records lack the wrapper completion marker, so retain receipt-based
    compatibility. Receipt timestamps bind evidence to this request, not to an exit
    file whose shell wrapper overwrites its mtime after the receipt is finalized.
    """
    try:
        process_exit = home / ".update_process_exit_code"
        exit_path = process_exit if pending.get("notification_version") == 2 else home / ".update_exit_code"
        if not exit_path.exists():
            return None
        exit_code = int(exit_path.read_text(encoding="utf-8").strip())
        if exit_code:
            return False, f"The updater exited with code {exit_code}. Runtime state is unverified; see the update output."
        receipt_path = home / "logs" / "update_receipts" / "latest.json"
        receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
        started = _timestamp(receipt["started_at"])
        finished = _timestamp(receipt["finished_at"])
        requested = _timestamp(pending["timestamp"]) if pending.get("timestamp") else exit_path.stat().st_mtime
        if finished < requested or finished < started:
            return None
        if pending.get("notification_version") == 2 and started < requested:
            return None
        outcome = receipt.get("outcome")
        restart = receipt.get("gateway_restart") or {}
        if outcome != "success" or restart.get("incomplete") or restart.get("phase_error"):
            cause = restart.get("phase_error") or receipt.get("stop_reason") or f"Updater outcome: {outcome or 'unknown'}."
            return False, f"{cause} Runtime completion is unverified."
        if (home / "fleet_restart_pending").exists():
            return None
        fleet = receipt.get("fleet")
        expected = (receipt.get("post_update") or {}).get("sha")
        if not expected or not isinstance(fleet, list) or not fleet:
            return False, "The updater finalized without verified runtime evidence. Runtime state is unknown."
        if any(not isinstance(row, dict) or row.get("state") != "current" or row.get("code_sha") != expected for row in fleet):
            return False, "The runtime verification did not confirm the updated revision. Runtime state is unverified."
        return True, f"Update finalized and the running gateway revision was verified ({expected[:12]}). Interrupted work may still need recovery."
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return None
