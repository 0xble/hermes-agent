"""Durable native update notices and conservative finalized-receipt interpretation."""
from __future__ import annotations

import json
import os
import re
import threading
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

_MARKER_LOCK = threading.RLock()


@contextmanager
def locked_update_marker(home: Path):
    """Serialize admissions and marker writes across threads/processes; lock a stable sibling inode."""
    with _MARKER_LOCK:
        with (home / ".update_pending.lock").open("a+b") as handle:
            if os.name == "nt":  # pragma: no cover - Windows CI
                import msvcrt
                if handle.seek(0, os.SEEK_END) == 0:
                    handle.write(b"\0")
                    handle.flush()
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
            else:
                import fcntl
                fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if os.name == "nt":  # pragma: no cover - Windows CI
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def same_update(path: Path, pending: dict) -> bool:
    """A watcher may finish after a new admission replaced the marker."""
    try:
        current = json.loads(path.read_text(encoding="utf-8-sig"))
        if pending.get("request_id") or current.get("request_id"):
            return bool(pending.get("request_id") and current.get("request_id") == pending["request_id"])
        current.setdefault("timestamp", datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat())
        return current.get("timestamp") == pending.get("timestamp") and current.get("reason") == pending.get("reason")
    except (OSError, ValueError, AttributeError):
        return False


def read_pending(home: Path) -> tuple[Path, dict] | None:
    for name in (".update_pending.claimed.json", ".update_pending.json"):
        path = home / name
        try:
            data = json.loads(path.read_text(encoding="utf-8-sig"))
            if isinstance(data, dict):
                data.setdefault("timestamp", datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat())
                return path, data
        except (OSError, ValueError):
            continue
    return None


def notice(heading: str, pending: dict, detail: str) -> str:
    reason = pending.get("reason")
    paragraph = reason.strip() if isinstance(reason, str) else ""
    return heading + "\n\n" + " ".join(part for part in (paragraph, detail) if part)


def save_pending(path: Path, pending: dict) -> None:
    with locked_update_marker(path.parent):
        if not same_update(path, pending):
            return
        temporary = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        try:
            temporary.write_text(json.dumps(pending), encoding="utf-8")
            temporary.replace(path)
        finally:
            temporary.unlink(missing_ok=True)


def _timestamp(value: str) -> float:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        parsed = parsed.astimezone()
    return parsed.timestamp()


def expected_revision(home: Path, receipt: dict) -> tuple[str | None, str | None]:
    """``(sha, disagreement)`` naming the revision a finished update must leave running.

    Immutable homes run the release ``current`` points at. The updater runs from the
    journal-bound source checkout, whose HEAD is frozen by design, so the receipt's
    ``post_update`` can name that checkout rather than the runtime. A recorded release
    transition that contradicts the pointer is a real disagreement. Legacy homes keep
    the receipt's post-update checkout identity.
    """
    post = (receipt.get("post_update") or {}).get("sha") or None
    try:
        from hermes_cli.immutable_releases import resolved_release
        release = resolved_release(home)
    except Exception:
        release = None
    if release is None:
        return post, None
    transition = receipt.get("release_transition") or {}
    to_sha = transition.get("to_sha") if isinstance(transition, dict) else None
    if to_sha and to_sha != release.name:
        return None, (f"The update recorded release {str(to_sha)[:12]}, but the active release is "
                      f"{release.name[:12]}. Runtime state is unverified.")
    return release.name, None


def update_receipt_path(home: Path) -> Path:
    """The update's receipt: ``latest.json`` unless a ``pm`` receipt replaced it.

    ``pm`` sync and plugin-check receipts (they carry ``kind``) also replace the shared
    ``latest.json`` pointer, so a sync that finishes after the update would otherwise be
    read as the update's own outcome. Then fall back to the newest per-run
    ``update_*.json``, which only the updater writes. An update-owned ``latest.json`` stays
    authoritative because a live-fleet settle rewrites only that pointer.
    """
    directory = home / "logs" / "update_receipts"
    latest = directory / "latest.json"
    try:
        data = json.loads(latest.read_text(encoding="utf-8-sig"))
        if not (isinstance(data, dict) and data.get("kind")):
            return latest
    except (OSError, ValueError):
        return latest
    newest: tuple[float, Path] | None = None
    for path in directory.glob("update_*.json"):
        try:
            stamp = path.stat().st_mtime
        except OSError:
            continue
        if newest is None or stamp > newest[0]:
            newest = (stamp, path)
    return newest[1] if newest else latest


def _finalized_receipt(home: Path, pending: dict, exit_path: Path) -> dict | None:
    receipt = json.loads(update_receipt_path(home).read_text(encoding="utf-8-sig"))
    started = _timestamp(receipt["started_at"])
    finished = _timestamp(receipt["finished_at"])
    requested = _timestamp(pending["timestamp"]) if pending.get("timestamp") else exit_path.stat().st_mtime
    if finished < requested or finished < started:
        return None
    if pending.get("notification_version") == 2 and started < requested:
        return None
    return receipt


def _failure_explanation(reason: str) -> str:
    from hermes_cli.debug_redaction import redact_debug_support_text

    explanation = []
    for line in reason.splitlines():
        line = line.strip()
        # Nothing after either boundary explains the failure safely, including
        # traceback frame/source lines and command-boundary exit-code wrappers.
        if line.startswith("Traceback (most recent call last):") or re.fullmatch(r"sys\.exit\(-?\d+\)", line):
            break
        # PM can append progress to the exception head on the same line. Keep
        # that head and the actual diagnostic, not timings or a generic retry hint.
        line = re.sub(r"\b(?:Checked|Audited|Resolved|Installed) \d+ packages? in \S+$", "", line).rstrip()
        if (not line or re.fullmatch(r"Found \d+ incompatibilit(?:y|ies)", line)
                or line.startswith("— retry,")):
            continue
        explanation.append(line)
    summary = " ".join(explanation)
    conflicts = [line for line in explanation if line.startswith("The package `")]
    if conflicts:
        # A verbose preamble must not push the offending requirement out of view.
        head = redact_debug_support_text(explanation[0], max_chars=80) if explanation[0] not in conflicts else ""
        summary = " ".join(part for part in (head, *conflicts) if part)
    # Scrub before truncating, so a cut credential cannot evade the redactor.
    return redact_debug_support_text(summary, max_chars=240).rstrip()


def _failure_cause(receipt: dict, exit_code: int) -> str:
    for reason in (receipt.get("stop_reason"), (receipt.get("gateway_restart") or {}).get("phase_error")):
        if isinstance(reason, str) and (explanation := _failure_explanation(reason)):
            return explanation
    stop_class = receipt.get("stop_class")
    if isinstance(stop_class, str) and stop_class.strip():
        explanations = {
            "syntax_rollback": "The downloaded update contains a syntax error.",
            "target_syntax_error": "The selected update contains a syntax error.",
        }
        return explanations.get(stop_class, stop_class.splitlines()[0].strip()[:240])
    if exit_code:
        return f"The updater exited with code {exit_code}."
    return f"Updater outcome: {receipt.get('outcome') or 'unknown'}."


def _update_recovery_pending(home: Path) -> bool:
    """HEAD may already be restored while checkout repair or completion is still owed."""
    from hermes_cli._early_recovery import interrupted_pull_marker
    from hermes_cli.immutable_releases import update_source_checkout
    from hermes_cli.update_host_obligation import HOST_OBLIGATION_NAME, host_obligation_path
    from hermes_cli.venv_sync import completion_pending_path

    root = update_source_checkout(home, Path(__file__).resolve().parents[1])
    if root is None:
        # ZIP/non-git installs cannot rule out source-keyed recovery debt.
        return True
    host_record = host_obligation_path(root)
    return any(path.exists() for path in (
        interrupted_pull_marker(root), completion_pending_path(root),
        host_record, host_record.with_name(HOST_OBLIGATION_NAME),
    ))


def _local_changes_evidence(receipt: dict) -> tuple[bool, str]:
    """HEAD equality says nothing about edits removed from the tree by autostash."""
    for step in reversed(receipt.get("steps") or []):
        if step.get("name") != "local_changes_stash":
            continue
        detail = step.get("detail") or ""
        if step.get("ok") is True and detail.startswith(("restored: ", "not_needed: ")):
            return True, ""
        if detail.startswith("parked: "):
            stash = detail.removeprefix("parked: ").split(" ", 1)[0]
            if re.fullmatch(r"[0-9a-fA-F]{40}", stash):
                return False, f"Local changes remain in stash {stash}; restore with git stash apply {stash}."
            return False, "Local changes remain in stash; see the update output before restoring them."
        if detail.startswith("unverified: "):
            return False, ""
        if detail.startswith("discarded: "):
            return False, "Local changes were discarded."
        return False, "Local changes were not confirmed restored; see the update output."
    return next((step.get("ok") is True for step in reversed(receipt.get("steps") or [])
                 if step.get("name") == "immutable_source_untouched"), False), ""


def _immutable_unchanged_proof(home: Path, receipt: dict) -> bool:
    """Positive proof that a failed immutable update never reached activation.

    Needs the pointer recorded before the candidate build, the same pointer at finalize and
    now, activation and any staging runtime-mutation path never started, and a candidate
    distinct from the active release. A receipt without the record proves nothing.
    """
    from hermes_cli.immutable_releases import release_identity

    proof = receipt.get("immutable_update")
    if (not isinstance(proof, dict) or proof.get("activation_started") is not False
            or receipt.get("staging_runtime_mutation_started") is not False):
        return False
    before, after = proof.get("before"), proof.get("after")
    if not isinstance(before, dict) or before != after or before != release_identity(home):
        return False
    return bool(proof.get("candidate")) and proof["candidate"] != before.get("path")


def _failed_update_detail(home: Path, receipt: dict | None, exit_code: int) -> str:
    if receipt is None:
        return f"The updater exited with code {exit_code}. Runtime state is unverified; see the update output."
    cause = _failure_cause(receipt, exit_code)
    previous = (receipt.get("pre_update") or {}).get("sha")
    post = (receipt.get("post_update") or {}).get("sha")
    expected, disagreement = expected_revision(home, receipt)
    if (home / "current").is_symlink():
        from hermes_cli.immutable_releases import resolved_release
        release = resolved_release(home)
        if release is None or release.name != expected:
            expected = None
    # Immutable source HEAD is frozen, so equality alone cannot rule out a promotion.
    changed_runtime = (any(receipt.get(key) for key in (
        "release_transition", "gateway_restart", "fleet", "staging_runtime_mutation_started"))
                       or any(step.get("name") == "zip_source_swap" for step in receipt.get("steps") or []))
    pending_runtime = any((home / name).exists() for name in ("release-txn.json", "fleet_restart_pending"))
    tree_verified, local_changes = _local_changes_evidence(receipt)
    immutable_apply = ("immutable_update" in receipt or any(
        step.get("name") == "immutable_source_untouched" for step in receipt.get("steps") or []))
    if ((home / "current").is_symlink() or immutable_apply) and not _immutable_unchanged_proof(home, receipt):
        tree_verified = False
    valid_sha = isinstance(previous, str) and re.fullmatch(r"[0-9a-fA-F]{40}", previous)
    if (receipt.get("outcome") in {"failed", "partial"} and valid_sha and previous == post
            and expected and not disagreement and not changed_runtime and not pending_runtime
            and tree_verified and not receipt.get("user_action") and not _update_recovery_pending(home)):
        return f"Nothing changed. Still on {expected[:12]}. Cause: {cause}"
    detail = f"Cause: {cause} Runtime state is unverified; see the update output."
    return f"{detail} {local_changes}" if local_changes else detail


def final_outcome(home: Path, pending: dict) -> tuple[bool, str] | None:
    """None means still waiting; success needs process completion AND runtime proof.

    Older manual records lack the wrapper completion marker, so retain receipt-based
    compatibility. Receipt timestamps bind evidence to this request, not to an exit
    file whose shell wrapper overwrites its mtime after the receipt is finalized.
    """
    exit_code = 0
    try:
        process_exit = home / ".update_process_exit_code"
        exit_path = process_exit if pending.get("notification_version") == 2 else home / ".update_exit_code"
        if not exit_path.exists():
            return None
        exit_code = int(exit_path.read_text(encoding="utf-8-sig").strip())
        receipt_path = update_receipt_path(home)
        if not receipt_path.exists() and pending.get("notification_version") != 2 and not exit_code:
            # Legacy gateway markers predate runtime receipts. Preserve their terminal
            # notification contract while v2 markers remain fail-closed.
            return True, "Hermes update finished successfully."
        receipt = _finalized_receipt(home, pending, exit_path)
        if receipt is None:
            return (False, _failed_update_detail(home, None, exit_code)) if exit_code else None
        outcome = receipt.get("outcome")
        restart = receipt.get("gateway_restart") or {}
        if exit_code or outcome != "success" or restart.get("incomplete") or restart.get("phase_error"):
            return False, _failed_update_detail(home, receipt, exit_code)
        if (home / "fleet_restart_pending").exists():
            return None
        fleet = receipt.get("fleet")
        post = (receipt.get("post_update") or {}).get("sha")
        previous = (receipt.get("pre_update") or {}).get("sha")
        expected, disagreement = expected_revision(home, receipt)
        if disagreement:
            return False, disagreement
        if post and previous == post and expected and not restart and not fleet:
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
        return (False, _failed_update_detail(home, None, exit_code)) if exit_code else None


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
