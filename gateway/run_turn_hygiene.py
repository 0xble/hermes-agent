"""Hygiene-compression outcome copy and diagnostics for the gateway turn loop."""
from __future__ import annotations

from agent.i18n import t


def _hygiene_compression_timeout_message(
    *, total_exhausted: bool, elapsed: float, idle_timeout: float, progress_observed: bool) -> str:
    """Describe the host timeout that actually ended hygiene compression. Chat users cannot edit
    model config, so the copy names /compress, /new and `hermes doctor`, never a config key or the
    raw second counts (those stay in the gateway log)."""
    lead = t("gateway.compress.hygiene_timeout")
    if total_exhausted:
        return lead
    return lead + t("gateway.compress.hygiene_timeout_doctor_hint")


def hygiene_no_commit_reason(agent) -> str:
    """Name WHY a hygiene compression left the session id unchanged with no in-place commit.
    The terminal ``else`` used to blame "no session_db on the hygiene agent" for every route into it,
    but that is one of several causes (#71097): an attempt that ABORTED before any commit boundary
    (lock skip, transient cooldown, summary timeout, codex thread interrupted) leaves
    ``_last_compression_attempt_in_place`` at ``None``; a DB-less agent is only the case when
    ``_session_db`` really is missing. Read the per-attempt signals the compressor sets, in that order."""
    if not bool(getattr(agent, "_last_compression_attempt_recorded", False)):
        return "compression did not run"
    lock_skip = getattr(agent, "_compression_skipped_due_to_lock", None)
    if lock_skip is True or isinstance(lock_skip, str):
        return "attempt skipped: compression lease held by another process"
    blocked = getattr(agent, "_compression_blocked_transient", None)
    if blocked:
        return f"attempt blocked: {blocked}"
    if getattr(agent, "_last_compression_attempt_in_place", None) is None:
        detail = "summary timed out" if getattr(agent, "_last_compression_timed_out", False) else "aborted before commit"
        warning = getattr(agent, "_last_compression_summary_warning", None)
        return f"attempt {detail}" + (f": {warning}" if warning else "")
    if getattr(agent, "_session_db", None) is None:
        return "no session_db on the hygiene agent"
    return "in-place commit did not complete"
