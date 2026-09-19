"""Candidate-bound review tool for the Hermes Agent Next build."""

from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_SCHEMA = {
    "name": "review_candidate",
    "description": "Review an exact Git candidate once. Supply repository, base_sha, head_sha, and an optional repository-relative scope. The tool records a durable receipt and never edits the candidate.",
    "parameters": {
        "type": "object",
        "properties": {
            "repository": {"type": "string", "description": "Absolute Git repository path."},
            "base_sha": {"type": "string", "description": "Exact review base commit."},
            "head_sha": {"type": "string", "description": "Exact candidate commit."},
            "scope": {"type": "array", "items": {"type": "string"}, "description": "Optional repository-relative paths."},
        },
        "required": ["repository", "base_sha", "head_sha"],
    },
}

# Upper bound on the candidate handed to one review child. A candidate larger than this is
# REFUSED rather than truncated: a silently shortened diff would produce a receipt claiming
# coverage the reviewer never saw.
_MAX_DIFF_BYTES = 2_000_000

# Only a provider-level AVAILABILITY failure may fall back to the secondary reviewer. A child
# that started and then failed, timed out, or returned an unknown status is recorded as
# "not reviewed" — re-running it on another model would review a different thing and is not
# what the delivery gate promises.
_AVAILABILITY_PATTERN = re.compile(
    r"\b(401|403|429|50[0-9]|auth|authentication|unauthorized|credential|quota|rate.?limit|"
    r"overloaded|unavailable|capacity|no such model|model not found|could not start|failed to start|"
    r"connection|network|dns)\b",
    re.IGNORECASE,
)
_NON_AVAILABILITY_PATTERN = re.compile(r"\b(timeout|timed out|unknown|interrupted|cancell?ed)\b", re.IGNORECASE)

# Secondary reviewer, used only on a dispatch-time availability failure. Read from
# ``auxiliary.review.fallback_providers[0]`` (same entry shape as the delegation chain) so the
# route follows the profile's provider naming; these literals are the last resort.
_FALLBACK_PROVIDER = "anthropic"
_FALLBACK_MODEL = "claude-opus-5"


def _fallback_credentials(primary: dict[str, Any] | None) -> dict[str, Any]:
    fallback = dict(primary or {})
    provider, model = _FALLBACK_PROVIDER, _FALLBACK_MODEL
    try:
        from hermes_cli.config import load_config_readonly
        chain = ((load_config_readonly().get("auxiliary") or {}).get("review") or {}).get("fallback_providers") or []
        first = next((e for e in chain if isinstance(e, dict) and e.get("model")), None)
        if first:
            provider, model = str(first.get("provider") or provider), str(first["model"])
            for key in ("base_url", "api_key", "api_mode"):
                if first.get(key):
                    fallback[key] = str(first[key])
    except Exception:
        pass
    fallback["provider"], fallback["model"] = provider, model
    return fallback


def _json(**fields: Any) -> str:
    return json.dumps(fields, sort_keys=True)


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(repo), *args], check=True,
                            capture_output=True, text=True)
    return result.stdout.strip()


def _receipt_path(head_sha: str = "") -> Path:
    """One receipt per candidate head, so an earlier candidate's coverage is never overwritten."""
    from hermes_constants import get_hermes_home
    name = f"{head_sha}.json" if head_sha else "review-candidate.json"
    return get_hermes_home() / "review_receipts" / name


def _existing_receipt(path: Path, *, base_sha: str, head_sha: str, scope: list[str]) -> dict[str, Any] | None:
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return None
    if (receipt.get("status") == "reviewed" and receipt.get("base_sha") == base_sha
            and receipt.get("head_sha") == head_sha and receipt.get("scope") == scope):
        return receipt
    return None


def _is_availability_failure(error: Any) -> bool:
    """True only for provider-level failures that mean the reviewer never ran."""
    text = str(error or "")
    if _NON_AVAILABILITY_PATTERN.search(text):
        return False
    return bool(_AVAILABILITY_PATTERN.search(text))


def _structured_verdict(result: dict[str, Any]) -> dict[str, Any]:
    """The reviewer's verdict as data, parsed out of the child's final message.

    The delegate result wraps the child's prose in a summary string; a receipt whose verdict can
    only be found by a human reading that prose is not reusable by later delivery stages. A fenced
    ```json block is preferred, then any bare JSON object with a ``verdict`` key. When nothing
    parses, the verdict is ``unparsed`` and delivery must treat it as changes_requested.
    """
    tasks = result.get("results") if isinstance(result.get("results"), list) else [result]
    text = ""
    for task in tasks or []:
        if isinstance(task, dict):
            text = str(task.get("summary") or task.get("output") or task.get("content") or "")
            if text:
                break
    candidates: list[str] = []
    for match in re.finditer(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL):
        candidates.append(match.group(1))
    start = text.find("{")
    if start >= 0:
        candidates.append(text[start:text.rfind("}") + 1])
    for blob in candidates:
        try:
            parsed = json.loads(blob)
        except (TypeError, ValueError):
            continue
        if isinstance(parsed, dict) and "verdict" in parsed:
            parsed.setdefault("findings", [])
            parsed.setdefault("summary", "")
            return parsed
    return {"verdict": "unparsed", "findings": [], "summary": text[:2000]}


def _pending_path(head_sha: str) -> Path:
    """Durable marker for a review in flight: written before dispatch, consumed by the receipt writer."""
    from hermes_constants import get_hermes_home
    return get_hermes_home() / "review_receipts" / f"{head_sha}.pending.json"


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", dir=path.parent, delete=False, encoding="utf-8") as tmp:
        json.dump(payload, tmp, sort_keys=True, indent=2)
        tmp.write("\n")
        tmp_path = Path(tmp.name)
    os.replace(tmp_path, path)


_PENDING_MAX_AGE_SECONDS = 4 * 3600


def _pending_is_live(pending: dict[str, Any]) -> bool:
    """True when a pending marker still describes a running review."""
    try:
        dispatched = datetime.fromisoformat(str(pending.get("dispatched_at") or ""))
        if dispatched.tzinfo is None:
            return False  # a hand-edited naive stamp is not one this plugin wrote; treat as stale
        if (datetime.now(timezone.utc) - dispatched).total_seconds() > _PENDING_MAX_AGE_SECONDS:
            return False
    except (ValueError, TypeError):
        return False
    delegation_id = str(pending.get("delegation_id") or "")
    if not delegation_id:
        return False
    try:
        from tools.async_delegation import get_durable_delegation, list_async_delegations
        live = {str(d.get("delegation_id")) for d in list_async_delegations()
                if str(d.get("status")) in ("running", "stalling", "finalizing")}
        if delegation_id in live:
            return True
        row = get_durable_delegation(delegation_id)
        return bool(row and row.get("state") == "running")
    except Exception:
        return True  # cannot tell; do not re-dispatch on top of a possibly live review


def _dispatch_review(context: str, head: str, parent: Any, credentials: dict[str, Any] | None) -> dict[str, Any]:
    """Dispatch one review child in the BACKGROUND and return the native handle.

    A full-candidate review is a long task, and the parent's sequential tool deadline (420 s by
    default) is a ceiling on how long a tool call may block, not on how long a review may take.
    Running the child synchronously inside this tool both hit that deadline on the first
    integration review and froze the parent for the duration, which is the opposite of what the
    delivery gate wants. Background dispatch returns at once with a ``delegation_id``; the child's
    completion re-enters the parent as a message the way every other delegation does, and the
    receipt is written by the ``subagent_stop`` hook below when the child finishes.
    """
    from tools.delegate_tool import delegate_task
    raw = delegate_task(
        goal=f"Review candidate {head[:12]}", context=context,
        background=True, parent_agent=parent, credentials_cfg=credentials,
    )
    try:
        return json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return {"error": f"review dispatch returned unparsable output: {str(raw)[:400]}"}


def _finalize_receipt(pending: dict[str, Any], result: dict[str, Any], *, reviewer_model: str,
                      fallback_reason: str) -> dict[str, Any]:
    receipt = {
        "status": "reviewed",
        "repository": pending["repository"],
        "base_sha": pending["base_sha"],
        "head_sha": pending["head_sha"],
        "scope": pending["covers"],
        "covers": pending["covers"],
        "reviewer_model": reviewer_model,
        "fallback_reason": fallback_reason,
        "result": _structured_verdict(result),
        "raw_child_result": result.get("results", result),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    _write_json(_receipt_path(pending["head_sha"]), receipt)
    return receipt


_REVIEW_GOAL = re.compile(r"^Review candidate ([0-9a-f]{12})$")
_CHILD_HEADS: dict[str, str] = {}  # child_session_id -> head12, from subagent_start


def _on_subagent_start(child_session_id: Any = None, child_goal: Any = None, **_: Any) -> None:
    """Remember which child is reviewing which candidate, keyed by the runtime's own session id.

    The stop payload carries no goal, and a child that timed out or errored has no summary at all,
    so text matching at stop time cannot identify a FAILED reviewer. Recording the link at start
    is what lets a failed review be written as not_reviewed instead of leaving its marker pending.
    """
    match = _REVIEW_GOAL.match(str(child_goal or "").strip())
    if match and child_session_id:
        _CHILD_HEADS[str(child_session_id)] = match.group(1)


def _on_subagent_stop(child_summary: Any = None, child_status: Any = None, child_session_id: Any = None,
                      parent_session_id: Any = None, **_: Any) -> None:
    """Turn a finished reviewer child into a durable receipt.

    Matched by the child's session id recorded at start; the summary text is a fallback for a
    runtime that did not fire subagent_start. Fires for every child stop, so it must be cheap and
    must ignore every child that is not a review it dispatched.
    """
    try:
        from hermes_constants import get_hermes_home
        pending_dir = get_hermes_home() / "review_receipts"
        if not pending_dir.is_dir():
            return
        summary = str(child_summary or "")
        head12 = _CHILD_HEADS.pop(str(child_session_id), "") if child_session_id else ""
        for marker in pending_dir.glob("*.pending.json"):
            try:
                pending = json.loads(marker.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            head = pending.get("head_sha", "")
            if not head:
                continue
            matched = (head12 and head.startswith(head12)) or (not head12 and summary and head[:12] in summary)
            if not matched:
                continue
            status = str(child_status or "")
            if status not in ("completed", "success", "ok") or not summary:
                _write_json(_receipt_path(head), {
                    "status": "not_reviewed", "error_code": "review_incomplete",
                    "repository": pending["repository"], "base_sha": pending["base_sha"], "head_sha": head,
                    "reviewer_model": pending.get("reviewer_model", ""),
                    "error": f"reviewer child ended with status {status!r}" + ("" if summary else " and no summary"),
                    "created_at": datetime.now(timezone.utc).isoformat(),
                })
            else:
                _finalize_receipt(pending, {"results": [{"status": status, "summary": summary}]},
                                  reviewer_model=pending.get("reviewer_model", ""),
                                  fallback_reason=pending.get("fallback_reason", ""))
            marker.unlink(missing_ok=True)
            return
    except Exception:
        # A receipt failure must never break the parent's delegation completion path.
        return


def review_candidate(args: dict[str, Any], **kwargs: Any) -> str:
    try:
        from agent.delegation_context import is_delegated_child_context
        if is_delegated_child_context():
            return _json(success=False, status="not_reviewed", error_code="parent_only",
                         error="review_candidate is available only to the owning parent session")
        repo = Path(str(args.get("repository") or "")).expanduser().resolve()
        base = str(args.get("base_sha") or "").strip()
        head = str(args.get("head_sha") or "").strip()
        scope = args.get("scope") or []
        if not repo.is_dir() or not base or not head or not isinstance(scope, list) or any(not isinstance(p, str) for p in scope):
            return _json(success=False, status="not_reviewed", error_code="invalid_candidate",
                         error="repository, base_sha, head_sha, and a string scope are required")
        base_resolved = _git(repo, "rev-parse", "--verify", f"{base}^{{commit}}")
        head_resolved = _git(repo, "rev-parse", "--verify", f"{head}^{{commit}}")
        if base_resolved == head_resolved:
            return _json(success=False, status="not_reviewed", error_code="empty_candidate",
                         error="base and head must differ")
        diff = _git(repo, "diff", "--no-ext-diff", "--unified=3", base_resolved, head_resolved, "--", *scope)
        if not diff:
            return _json(success=False, status="not_reviewed", error_code="empty_scope",
                         error="candidate scope has no diff")
        changed = [line for line in _git(repo, "diff", "--name-only", base_resolved, head_resolved,
                                         "--", *scope).splitlines() if line]
        covers = sorted(set(changed))

        receipt_path = _receipt_path(head_resolved)
        existing = _existing_receipt(receipt_path, base_sha=base_resolved, head_sha=head_resolved, scope=covers)
        if existing is not None:
            return _json(success=True, reused=True, **existing)

        if len(diff.encode("utf-8")) > _MAX_DIFF_BYTES:
            return _json(success=False, status="not_reviewed", error_code="diff_too_large",
                         base_sha=base_resolved, head_sha=head_resolved,
                         error="candidate diff exceeds the review context limit; split the candidate")

        context = (
            "You are the independent reviewer for this exact candidate. Do not edit files, create "
            "commits, or call review_candidate (it is parent-only and you are the reviewer it spawned). "
            "Inspect the repository read-only and run relevant tests when a terminal is available; "
            "if it is not, say so and review the diff statically. Your FINAL message must be a single "
            "fenced ```json block with keys head_sha (the exact head you reviewed), verdict (approve or "
            "changes_requested), findings (array of objects with severity, path, line, message), and "
            "summary. No prose outside the block.\n\n"
            f"Repository: {repo}\nBase: {base_resolved}\nHead: {head_resolved}\n"
            f"Covered paths: {json.dumps(covers)}\nFull diff:\n{diff}"
        )

        from agent.review_engine import _load_review_credentials_cfg
        from agent.subagent_lifecycle import get_active_subagent_parent
        parent = get_active_subagent_parent()
        if parent is None:
            return _json(success=False, status="not_reviewed", error_code="parent_context_unavailable",
                         base_sha=base_resolved, head_sha=head_resolved,
                         error="review child requires an active parent agent")
        credentials = _load_review_credentials_cfg()
        primary_model = str((credentials or {}).get("model") or "")

        pending_marker = _pending_path(head_resolved)
        if pending_marker.exists():
            try:
                pending = json.loads(pending_marker.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                pending = {}
            if _pending_is_live(pending):
                return _json(success=True, status="pending", reused=True,
                             delegation_id=pending.get("delegation_id", ""), head_sha=head_resolved,
                             note="a review of this exact candidate is already in flight; its result will arrive as a delegation completion")
            # The stop hook only fires from the normal finalize path; a stalled child or a gateway
            # crash mid-review leaves the marker behind. A marker whose delegation is no longer live
            # (or is older than the bound) is stale, and refusing forever would make the head
            # unreviewable. Record why, then re-dispatch.
            _write_json(_receipt_path(head_resolved), {
                "status": "not_reviewed", "error_code": "review_incomplete", "head_sha": head_resolved,
                "base_sha": base_resolved, "repository": str(repo),
                "error": "previous review of this head never finished (stale pending marker); re-dispatching",
                "created_at": datetime.now(timezone.utc).isoformat()})
            pending_marker.unlink(missing_ok=True)

        handle = _dispatch_review(context, head_resolved, parent, credentials)
        reviewer_model, fallback_reason = primary_model, ""
        if handle.get("error"):
            if not _is_availability_failure(handle["error"]):
                return _json(success=False, status="not_reviewed", error_code="review_incomplete",
                             base_sha=base_resolved, head_sha=head_resolved, reviewer_model=primary_model,
                             error=str(handle["error"]))
            fallback_reason = str(handle["error"])[:500]
            fallback = _fallback_credentials(credentials)
            handle = _dispatch_review(context, head_resolved, parent, fallback)
            reviewer_model = fallback["model"]
            if handle.get("error"):
                return _json(success=False, status="not_reviewed", error_code="review_unavailable",
                             base_sha=base_resolved, head_sha=head_resolved,
                             error=f"primary unavailable ({fallback_reason}); fallback failed: {handle['error']}")

        pending = {
            "repository": str(repo), "base_sha": base_resolved, "head_sha": head_resolved, "covers": covers,
            "reviewer_model": reviewer_model, "fallback_reason": fallback_reason,
            "delegation_id": str(handle.get("delegation_id") or ""),
            "dispatched_at": datetime.now(timezone.utc).isoformat(),
        }
        if handle.get("status") == "dispatched":
            _write_json(pending_marker, pending)
            return _json(success=True, status="pending", **pending,
                         note="review dispatched in the background; the receipt is written when the reviewer finishes "
                              "and the result re-enters this session as a delegation completion")
        # A direct (non-background) result: a depth>0 caller or a runtime that ran it inline.
        receipt = _finalize_receipt(pending, handle, reviewer_model=str(handle.get("review_model") or reviewer_model),
                                    fallback_reason=fallback_reason)
        return _json(success=True, **receipt)
    except (subprocess.CalledProcessError, OSError, ValueError) as exc:
        return _json(success=False, status="not_reviewed", error_code="candidate_read_failed", error=str(exc))
    except Exception as exc:
        return _json(success=False, status="not_reviewed", error_code="review_failed", error=str(exc))


def register(ctx: Any) -> None:
    ctx.register_tool(name="review_candidate", toolset="review_candidate",
                      schema=_SCHEMA, handler=review_candidate)
    ctx.register_hook("subagent_start", _on_subagent_start)
    ctx.register_hook("subagent_stop", _on_subagent_stop)
