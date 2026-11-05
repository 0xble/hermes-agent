"""Shared checks for delayed person-facing sends.

Checksums and freshness targets are rejected at create/update, not at the
later send. A declared freshness contract is re-checked before dispatch so a
late job is held instead of sent into a changed thread.
"""
from __future__ import annotations

import json
import hashlib
import re
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Optional

_SHA256_DECL = re.compile(
    r"(?i)\b(?P<expected>expected\s+)?sha-?256[ \t]*[:=][ \t]*"
    r"[`\"\']?(?P<value>[^\s`\"\'\),;]*)"
)
_SHA256_HEX = re.compile(r"^[0-9a-fA-F]{64}$")
_FRESHNESS_FIELDS = ("channel", "conversation", "script", "recipient", "approved_at")

FreshnessRunner = Callable[[dict[str, Any], dict[str, Any]], dict[str, Any]]


def declared_checksums(prompt: Optional[str], payload_sha256: Any = None) -> list[str]:
    """Return checksum tokens a scheduled send declared, in source order."""
    found: list[str] = []
    if isinstance(prompt, str):
        for match in _SHA256_DECL.finditer(prompt):
            token = match["value"]
            # Prompt text is also prose, shell commands and unexpanded templates.
            # Only literal expected values or hash-shaped values declare a digest;
            # structured payload_sha256 below is always an explicit declaration.
            if token.startswith(("$", "<", "{")):
                continue
            if match["expected"] or re.fullmatch(r"[0-9a-fA-F]+", token):
                found.append(token)
    if payload_sha256 not in (None, ""):
        found.append(str(payload_sha256))
    return found


def validate_scheduled_send_inputs(
    prompt: Optional[str] = None,
    *,
    payload_sha256: Any = None,
    payload_path: Any = None,
    send_freshness: Any = None,
) -> None:
    """Raise ValueError when a scheduled-send declaration cannot be sent safely.

    A 61-character checksum must never become a job. A freshness target must
    name the thread and the shared checker that will re-read it.
    """
    checksums = declared_checksums(prompt, payload_sha256)
    for token in checksums:
        if not _SHA256_HEX.fullmatch(token):
            raise ValueError(
                f"Scheduled-send checksum must be 64 hex characters, got {len(token)}."
            )
    path_text = "" if payload_path in (None, "") else str(payload_path)
    if path_text:
        if not checksums:
            raise ValueError("Scheduled-send payload_path requires a 64-character SHA-256 checksum.")
        file_path = Path(path_text).expanduser()
        if not file_path.is_file():
            raise ValueError(f"Scheduled-send payload file does not exist: {path_text}")
        digest = hashlib.sha256(file_path.read_bytes()).hexdigest()
        if digest != checksums[-1].lower():
            raise ValueError("Scheduled-send payload checksum does not match the file.")
    if send_freshness in (None, ""):
        return
    if not isinstance(send_freshness, dict):
        raise ValueError("send_freshness must be an object with channel, conversation, and script.")
    missing = [key for key in _FRESHNESS_FIELDS
               if not isinstance(send_freshness.get(key), str) or not send_freshness[key].strip()]
    if missing:
        raise ValueError(
            "send_freshness missing required field(s): " + ", ".join(missing)
        )
    channel = send_freshness["channel"]
    if channel not in {"slack", "telegram", "imessage", "email", "quo"}:
        raise ValueError("send_freshness.channel has no supported shared reader.")
    if channel != "imessage" and (
            not isinstance(send_freshness.get("account"), str) or not send_freshness["account"].strip()):
        raise ValueError("send_freshness.account is required for this provider.")
    conversation = send_freshness["conversation"]
    if channel == "slack" and (not re.fullmatch(r"[CDG][A-Z0-9]+", conversation)
                               or not re.fullmatch(r"[UW][A-Z0-9]+", send_freshness["recipient"])):
        raise ValueError("send_freshness requires exact Slack conversation and recipient IDs.")
    if channel in {"telegram", "imessage"} and not re.fullmatch(r"-?[0-9]+", conversation):
        raise ValueError("send_freshness.conversation must be the exact numeric chat ID.")
    # No shared Slack binding is not a failed read. The communications reader
    # records N/A coverage with a reason, while declared contexts must resolve.
    if "activity_context" in send_freshness:
        context = send_freshness["activity_context"]
        if not isinstance(context, dict) or any(not isinstance(context.get(k), str) or not context[k]
                for k in ("slack_account", "slack_conversation", "slack_recipient")):
            raise ValueError("send_freshness.activity_context must contain resolved Slack IDs.")
    if channel == "email":
        reviewed = send_freshness.get("reviewed_message_ids")
        if (not re.fullmatch(r"[0-9a-fA-F]+", conversation)
                or not isinstance(reviewed, list)
                or any(not isinstance(v, str) or not re.fullmatch(r"[0-9a-fA-F]+", v) for v in reviewed)
                or len(set(reviewed)) != len(reviewed)):
            raise ValueError("send_freshness email requires exact thread and reviewed_message_ids.")
    if channel == "quo" and (not re.fullmatch(r"PN[A-Za-z0-9]+", conversation)
                              or not re.fullmatch(r"\+[1-9]\d{6,14}", send_freshness["recipient"])):
        raise ValueError("send_freshness Quo requires PN number ID and E.164 recipient.")
    if "activity_check" in send_freshness:
        check = send_freshness["activity_check"]
        if (not isinstance(check, dict) or check.get("read_only") is not True
                or not isinstance(check.get("argv"), list) or not check["argv"]
                or any(not isinstance(a, str) or not a for a in check["argv"])):
            raise ValueError("send_freshness.activity_check must be an explicit read-only argv.")
    for key in ("transport_sha256", "payload_sha256", "text_sha256"):
        if key in send_freshness and not _SHA256_HEX.fullmatch(str(send_freshness[key])):
            raise ValueError(f"send_freshness.{key} must be 64 hex characters.")
    if send_freshness.get("payload_path"):
        validate_scheduled_send_inputs(payload_path=send_freshness["payload_path"],
                                       payload_sha256=send_freshness.get("payload_sha256"))
    if send_freshness.get("text_file"):
        validate_scheduled_send_inputs(payload_path=send_freshness["text_file"],
                                       payload_sha256=send_freshness.get("text_sha256"))
    if "text_file" in send_freshness and "payload_path" in send_freshness and (
            send_freshness["text_file"] != send_freshness["payload_path"]
            or send_freshness.get("text_sha256") != send_freshness.get("payload_sha256")):
        raise ValueError("send_freshness has conflicting approved payload bindings.")
    try:
        approved = datetime.fromisoformat(str(send_freshness["approved_at"]))
        if approved.tzinfo is None:
            raise ValueError("timezone required")
    except ValueError as exc:
        raise ValueError("send_freshness.approved_at must be a timezone-aware ISO timestamp.") from exc
    _resolve_freshness_script(send_freshness["script"])


def _resolve_freshness_script(script: str) -> Path:
    """Apply cron's script owner, with one installed communications resource.

    A deployed skill directory may itself be a symlink. That does not make
    other files in that skill (or a checker symlink escaping it) executable.
    Project-local skills are deliberately not an installation trust source.
    """
    from cron.lifecycle_guard import check_gateway_lifecycle
    from cron.scheduler_script import _resolve_script_path

    requested = Path(script).expanduser()
    if ".." in requested.parts:
        raise ValueError("send_freshness.script must not contain '..' traversal.")
    resolved, script_error = _resolve_script_path(script)
    if resolved is None:
        from agent.skill_utils import get_skill_search_roots
        from tools.skills_tool import locate_skill

        error, skill_dir, _skill_md = locate_skill(
            "communications", None, get_skill_search_roots(include_project=False))
        expected = (skill_dir.resolve() / "scripts/send_freshness.py") if skill_dir else None
        if (error or expected is None or not requested.is_absolute()
                or requested.resolve() != expected or expected.resolve() != expected):
            raise ValueError(
                "send_freshness.script must be inside HERMES_HOME/scripts or the installed "
                "communications/scripts/send_freshness.py checker. "
                f"Checker is unavailable or does not exist: {script_error}"
            )
        resolved = expected
    if not resolved.is_file():
        raise ValueError(f"send_freshness.script does not exist: {script}")
    try:
        check_gateway_lifecycle("", str(resolved))
    except ValueError as exc:
        raise ValueError(f"send_freshness.script: {exc}") from exc
    return resolved


def _freshness_subprocess_env() -> dict[str, str]:
    """Bind the owning fire profile even before run_one_job's worker handoff."""
    from hermes_constants import get_hermes_home
    from cron.scheduler import _fire_secret_scope
    from tools.environments.local import build_subprocess_env
    from tools.terminal_scope import install_profile_terminal_scope, reset_terminal_scope

    home = get_hermes_home()
    with _fire_secret_scope():
        token = install_profile_terminal_scope(home)
        try:
            return build_subprocess_env(strip_launch_profile=True)
        finally:
            reset_terminal_scope(token)


def parse_send_freshness(raw: Any) -> Optional[dict[str, Any]]:
    """Accept a dict or a JSON object string. None means unset."""
    if raw in (None, ""):
        return None
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError("send_freshness must be a JSON object.") from exc
        if not isinstance(parsed, dict):
            raise ValueError("send_freshness must be a JSON object.")
        return parsed
    raise ValueError("send_freshness must be an object.")


def run_freshness_script(spec: dict[str, Any], job: dict[str, Any]) -> dict[str, Any]:
    """Run the job's shared checker. A failed check is stale: do not send."""
    script = str(spec.get("script") or "")
    payload = {
        **{k: v for k, v in spec.items() if k != "script"},
        "scheduled_at": (job.get("schedule") or {}).get("run_at") or job.get("next_run_at"),
        "job_id": job.get("id"),
    }
    try:
        # Re-resolve and scan at execution as well: a persisted checker can have
        # disappeared, escaped via a replaced symlink, or changed since admission.
        script_path = _resolve_freshness_script(script)
        env = _freshness_subprocess_env()
        proc = subprocess.run(
            [sys.executable, str(script_path)],
            input=json.dumps(payload),
            env=env,
            capture_output=True,
            text=True,
            timeout=45,
            check=False,
        )
    except (OSError, ValueError, RuntimeError, subprocess.TimeoutExpired) as exc:
        return {"stale": True, "checked": False, "reason": f"freshness check failed: {exc}"}
    if proc.returncode not in (0, 2):
        detail = (proc.stderr or proc.stdout or "").strip()[:240]
        return {"stale": True, "checked": False, "reason": f"freshness check failed ({proc.returncode}): {detail}"}
    try:
        data = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return {"stale": True, "checked": False, "reason": "freshness check returned invalid JSON"}
    if not isinstance(data, dict):
        return {"stale": True, "checked": False, "reason": "freshness check returned invalid JSON"}
    valid = data.get("checked") is True and type(data.get("stale")) is bool
    stale = not valid or data["stale"] or proc.returncode == 2
    return {
        "stale": stale,
        "checked": valid,
        "reason": str(data.get("reason") or ("stale" if stale else "fresh")),
    }


def assess_freshness(
    job: dict[str, Any],
    *,
    runner: Optional[FreshnessRunner] = None,
) -> dict[str, Any]:
    """Return a verdict for a job that declared send_freshness. No declaration means not checked."""
    spec = job.get("send_freshness")
    if not isinstance(spec, dict):
        return {"stale": False, "checked": False, "reason": ""}
    try:
        verdict = (runner or run_freshness_script)(spec, job)
    except Exception as exc:
        # This boundary precedes dispatch: an unavailable reader is a hold, not
        # an unknown/possibly-sent run or an exception escaping the ledger path.
        return {"stale": True, "checked": False,
                "reason": f"freshness check failed: {type(exc).__name__}: {exc}"}
    if not isinstance(verdict, dict):
        return {"stale": True, "checked": False, "reason": "freshness check returned nothing"}
    return {
        "stale": verdict.get("checked") is not True or verdict.get("stale") is not False,
        "checked": bool(verdict.get("checked")),
        "reason": str(verdict.get("reason") or ""),
    }
