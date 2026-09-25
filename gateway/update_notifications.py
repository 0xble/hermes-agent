"""Durable native update notices and conservative finalized-receipt interpretation."""
from __future__ import annotations

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
        if not receipt_path.exists() and pending.get("notification_version") != 2:
            # Legacy gateway markers predate runtime receipts. Preserve their terminal
            # notification contract while v2 markers remain fail-closed.
            return True, "Hermes update finished successfully."
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
        previous = (receipt.get("pre_update") or {}).get("sha")
        if expected and previous == expected and not restart and not fleet:
            # "Already up to date": no code changed and nothing was restarted, so there is no
            # runtime to verify. The caller labels this result "Already Latest".
            return True, f"Hermes is already at revision {expected[:12]}."
        if not expected or not isinstance(fleet, list) or not fleet:
            return False, "The updater finalized without verified runtime evidence. Runtime state is unknown."
        verdicts = [_row_verdict(home, row, expected) for row in fleet]
        if any(verdict is False for verdict in verdicts):
            return False, "The runtime verification did not confirm the updated revision. Runtime state is unverified."
        if any(verdict is None for verdict in verdicts):
            return None  # the enclosing gateway accepted a self-restart and has not come back yet
        return True, f"Update finalized and the running gateway revision was verified ({expected[:12]}). Interrupted work may still need recovery."
    except (OSError, ValueError, TypeError, KeyError, AttributeError):
        return None


def _row_verdict(home: Path, row, expected: str) -> bool | None:
    """True when the fleet row proves the updated revision runs, None while that proof is still due.

    A ``restart_pending`` row is the gateway the updater ran inside (``request_update``, cron): it
    restarts only after the updater exits, so the receipt cannot prove the new code by construction.
    The proof is the replacement gateway for the same home, verified live, reporting the expected
    revision. While the recorded process still serves, the answer is pending, never a failure.
    """
    if not isinstance(row, dict):
        return False
    if row.get("state") == "current":
        return row.get("code_sha") == expected
    if row.get("state") != "restart_pending":
        return False
    from gateway.status import live_gateway_pid_for_home, read_runtime_status

    live_pid = live_gateway_pid_for_home(home)
    if live_pid is None or live_pid == row.get("pid"):
        return None
    runtime = read_runtime_status(home / "gateway_state.json") or {}
    if runtime.get("pid") != live_pid or not runtime.get("code_sha"):
        return None
    return runtime.get("code_sha") == expected
