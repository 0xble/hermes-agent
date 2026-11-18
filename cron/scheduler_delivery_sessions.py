"""Cron transcript mirroring and continuable delivery-session seeding."""

from __future__ import annotations

import logging
from typing import Optional


logger = logging.getLogger('cron.scheduler')


def _resolve_cron_surface_mode(pconfig, logical_platform_name: str) -> str:
    """Return ``"in_channel"`` or ``"thread"`` (default) for a platform config.

    Native: flat ``platforms.<p>.extra.cron_continuable_surface``. Relay-fronted:
    ``platforms.relay.extra.<logical>.cron_continuable_surface`` (same sub-block as the relay's
    Slack knobs); the sub-block wins and is scoped to its logical platform. Unlike
    _relay_slack_extra, a sub-block that omits the knob falls back to the flat key (deliberate —
    the flat key must keep working), so a flat value applies to EVERY platform the relay fronts.
    """
    from cron.scheduler_delivery import contextlib
    with contextlib.suppress(Exception):
        extra = getattr(pconfig, "extra", None) or {}
        sub = extra.get(str(logical_platform_name or "").lower())
        raw = sub.get("cron_continuable_surface") if isinstance(sub, dict) else None
        if raw is None:
            raw = extra.get("cron_continuable_surface")
        if raw is not None and str(raw).strip().lower() == "in_channel":
            return "in_channel"
    return "thread"


def _resolve_origin(job: dict) -> Optional[dict]:
    """Extract origin info from a job. Non-dict origins (provenance strings, hand-edited
    jobs.json) are treated as missing — otherwise every fire crashed on ``origin.get``.

    Without this guard, a job tagged with e.g. ``"combined-digest-replaces-x-and-y"`` crashed every fire
    attempt with ``'str' object has no attribute 'get'`` — ``mark_job_run`` recorded the failure, but the
    next tick re-loaded the same poisoned origin and crashed identically until the field was patched
    manually (#18722).
    """
    from cron.scheduler_delivery import _NON_PUSH_ORIGIN_PLATFORMS
    origin = job.get("origin")
    if isinstance(origin, dict) and origin.get("platform") and origin.get("chat_id"):
        # Jobs stamped before non-push origins stopped being captured (#69304): the api_server
        # adapter's send() is a stub, so honouring this origin fails every fire with
        # last_status=ok. Treat it as missing so deliver=origin takes the home-channel fallback.
        if str(origin["platform"]).lower() in _NON_PUSH_ORIGIN_PLATFORMS:
            return None
        return origin
    return None


def _cron_mirror_delivery_enabled(job: dict, cfg: Optional[dict] = None) -> bool:
    """Whether a cron delivery is also mirrored into the target chat's session transcript.

    Default OFF. Precedence: per-job ``attach_to_session`` (bool) → global
    ``cron.mirror_delivery`` → False. CARVE-OUT: the ``in_channel`` surface seeds its session
    independently of this knob (the seed IS that feature) — this governs only the thread-surface
    mirror. ``mirror_to_session`` runs at a turn boundary, so it is alternation- and cache-safe.
    """
    from cron.scheduler_delivery import _sched
    per_job = job.get("attach_to_session")
    if isinstance(per_job, bool):
        return per_job
    try:
        if cfg is None:
            cfg = _sched.load_config() or {}
        return bool((cfg.get("cron", {}) or {}).get("mirror_delivery", False))
    except Exception:
        return False


def _target_matches_origin(origin: dict, platform_name: str, chat_id: str,
                           thread_id: Optional[str]) -> bool:
    """True when a delivery target is the job's own origin conversation. A pinned origin
    thread_id must match — a target without it is a different lane. Mirror eligibility for
    non-origin targets is decided by ``_target_mirror_eligible``."""
    if (
        not origin
        or str(origin.get("platform", "")).lower() != str(platform_name).lower()
        or str(origin.get("chat_id", "")) != str(chat_id)
    ):
        return False
    origin_thread = origin.get("thread_id")
    return origin_thread is None or str(origin_thread) == str(thread_id or "")


def _target_mirror_eligible(
    job: dict, target: dict, *, global_mirror: bool, origin_match: Optional[bool] = None) -> bool:
    """Whether a resolved delivery target may receive the transcript mirror. Origin targets:
    always. ``origin_fallback`` (deliver=origin with no captured origin → home channel, standing
    in for the primary conversation) and ``home`` (user-written bare-platform token, e.g.
    ``deliver: slack`` — deliberately addresses that platform's home channel): same flags as a
    true origin. ``explicit`` ``platform:chat_id``: ONLY with per-job ``attach_to_session: true``
    — the global flag must never write transcripts into arbitrary explicitly-addressed chats.
    Untagged broadcast expansions (``all``) are never eligible. ``origin_match`` may be
    precomputed."""
    from cron.scheduler_delivery import _resolve_origin, _target_matches_origin
    if origin_match is None:
        origin = _resolve_origin(job) or {}
        origin_match = _target_matches_origin(
            origin, target.get("platform", ""), target.get("chat_id", ""), target.get("thread_id"))
    if origin_match:
        return True
    resolved_from = target.get("_resolved_from")
    if resolved_from in ("origin_fallback", "home"):
        # Same precedence as _cron_mirror_delivery_enabled (keep in sync): a per-job False must
        # beat a global True even for callers that don't pre-merge `global_mirror`.
        per_job = job.get("attach_to_session")
        return per_job if isinstance(per_job, bool) else bool(global_mirror)
    if resolved_from == "explicit":
        return job.get("attach_to_session") is True
    return False


def _inchannel_seed_allowed(*, is_dm: bool, user_id: Optional[str]) -> bool:
    """Whether the flat in_channel seed may run. Group keys are user-isolated
    (``…:group:<chat_id>:<user_id>``): seeding without a real user_id creates an orphan session no
    reply resolves to — worse than no seed. DM keys omit user_id, so DMs are always seedable."""
    return bool(is_dm or user_id)


def _redact_cron_payload(text: str, what: str) -> str:
    """Fail-closed secret redaction for anything a cron job emits outward.

    Every outward lane — chat message, session mirror, bot-chat turn — must apply the same policy,
    so the policy lives in one place. ``force=True`` because this is a safety boundary, not
    logging: the ``security.redact_secrets`` preference governs how much is scrubbed from the
    user's own logs and must not be able to turn scrubbing off on the way out to a chat (same
    reasoning as ``tools/delegation_live_log.py``). Empty input is returned as-is; any failure
    inside the redactor replaces the payload entirely rather than letting an unscanned value out.
    """
    if not text:
        return text
    try:
        from agent.redact import redact_sensitive_text
        return redact_sensitive_text(text, force=True)
    except Exception as e:
        logger.warning("Failed to redact secrets from cron %s: %s", what, e)
        return "[REDACTED - redaction failed]"


def _cron_display_name(job: dict) -> str:
    """Job name/id as it appears in outward-facing text. The mirror sinks and the thread title
    splice the job *name* around the redacted payload, and the name is user-controlled config — a
    name embedding a credential would re-leak it next to the scrubbed body."""
    from cron.scheduler_delivery import _redact_cron_payload
    return _redact_cron_payload(job.get("name") or job.get("id", "cron"), "job name")


def _cron_mirror_message(job: dict, text: str) -> str:
    from cron.scheduler_delivery import _cron_display_name
    return f"[Cron delivery: {_cron_display_name(job)}]\n{text}"


def _maybe_mirror_cron_delivery(
    job: dict, platform_name: str, chat_id: str, mirror_text: str, thread_id: Optional[str] = None,
    user_id: Optional[str] = None, *, enabled: bool = False,
) -> None:
    """Best-effort mirror of a cron delivery into the origin chat's session. No-op unless
    ``enabled`` (caller resolves it, scoped to the origin target). Rides the same
    ``mirror_to_session`` path as ``send_message``, passing ``user_id`` so user-isolated group
    chats resolve to the scheduling member. All failures swallowed — a successful delivery must
    never be reported failed because the mirror broke."""
    from cron.scheduler_delivery import (
        _cron_mirror_message,
    )
    if not enabled:
        return
    text = (mirror_text or "").strip()
    if not text:
        return
    try:
        from gateway.mirror import mirror_to_session
        # USER role + labelled prefix, NOT assistant: an assistant-role mirror lands
        # assistant→assistant and breaks strict alternation; consecutive user turns merge safely.
        # The brief is not the agent speaking; an assistant-role mirror lands as assistant→assistant after
        # the agent's last turn and breaks strict alternation (issue #2221, the exact failure #2313
        # removed). A user-role turn collapses safely via repair_message_sequence's consecutive-user merge
        # on every provider, and the prefix preserves the "this came from cron" context that the dropped
        # SQLite mirror metadata would otherwise lose on replay.
        # The invisible review already ends with an assistant decision. A labelled user
        # mirror keeps that protocol reply separate from the report on transcript replay.
        review_session = job.get("_review_mirror_session")
        ok = mirror_to_session(
            platform_name, str(chat_id), _cron_mirror_message(job, text),
            source_label="cron", thread_id=thread_id, user_id=user_id,
            role="user",
            **({"session_id": review_session} if review_session else {}))
        if ok:
            logger.info(
                "Job '%s': mirrored delivery into %s:%s session transcript",
                job.get("id", "?"), platform_name, chat_id)
        else:
            logger.debug(
                "Job '%s': delivery mirror skipped for %s:%s "
                "(no matching gateway session — cold start)",
                job.get("id", "?"), platform_name, chat_id)
    except Exception as e:
        logger.debug(
            "Job '%s': delivery mirror failed for %s:%s: %s", job.get("id", "?"), platform_name,
            chat_id, e,
        )


def _open_continuable_cron_thread(job: dict, adapter, chat_id: str, loop) -> Optional[str]:
    """Open a thread for a continuable cron job via ``adapter.create_handoff_thread``. Returns the
    thread_id, or ``None`` (no thread primitive / failed) = caller falls back to the DM mirror."""
    from cron.scheduler_delivery import (
        _cron_display_name,
    )
    create_thread = getattr(adapter, "create_handoff_thread", None)
    if not callable(create_thread) or loop is None:
        return None
    thread_name = f"Hermes — {_cron_display_name(job)}"
    try:
        from agent.async_utils import safe_schedule_threadsafe
        coro = create_thread(str(chat_id), thread_name)
        future = safe_schedule_threadsafe(coro, loop)  # type: ignore[arg-type]
        if future is None:
            return None
        new_thread_id = future.result(timeout=30)
        return str(new_thread_id) if new_thread_id else None
    except Exception as e:
        logger.debug(
            "Job '%s': create_handoff_thread failed on %s — falling back to "
            "DM-session mirror: %s",
            job.get("id", "?"), getattr(adapter, "name", "?"), e)
        return None


def _seed_cron_session(
    job: dict, adapter, platform_name: str, chat_id: str, text: str, *, thread_id: Optional[str],
    chat_type: str, user_id: Optional[str], user_name: Optional[str] = None,
    chat_name: Optional[str], scope_id: Optional[str], discord_keys_on_thread: bool = False,
) -> bool:
    """Create the session row (so the mirror has a target) and mirror the brief as a USER turn.
    The seeded key must equal the reply's ``build_session_key``: chat_type, user_id, thread_id and
    scope_id (Slack team id) are all part of it, so callers pass exactly what the reply carries."""
    from cron.scheduler_delivery import _cron_mirror_message
    from gateway.config import Platform
    from gateway.session import SessionSource
    from gateway.mirror import mirror_to_session
    seeded_session_id: Optional[str] = None
    session_store = getattr(adapter, "_session_store", None)
    if session_store is not None:
        try:
            platform_enum = Platform(platform_name.lower())
        except (ValueError, KeyError):
            platform_enum = None
        if platform_enum is not None:
            # Discord keys in-thread messages with chat_id == thread_id; Slack/Telegram use the
            # parent channel.
            seed_chat_id = (
                str(thread_id)
                if discord_keys_on_thread and platform_enum == Platform.DISCORD
                else str(chat_id)
            )
            dest_source = SessionSource(
                platform=platform_enum, chat_id=seed_chat_id, chat_name=chat_name,
                chat_type=chat_type,
                user_id=user_id, user_name=user_name, thread_id=thread_id,
                scope_id=str(scope_id) if scope_id else None)
            # Create the row and pass its exact id to the mirror — origin-heuristic rediscovery
            # bails on populated chats.
            _entry = session_store.get_or_create_session(dest_source)
            seeded_session_id = getattr(_entry, "session_id", None)
    return mirror_to_session(
        platform_name, str(chat_id), _cron_mirror_message(job, text),
        source_label="cron", thread_id=thread_id, user_id=user_id, role="user",
        session_id=seeded_session_id,
    )


def _seed_cron_thread_session(
    job: dict, adapter, platform_name: str, chat_id: str, thread_id: str, mirror_text: str,
    chat_name: Optional[str] = None, is_dm: bool = False, scope_id: Optional[str] = None,
) -> None:
    """Seed the freshly-opened cron thread's session with the brief (never raises), else the
    user's in-thread reply resolves to a transcript without it. Threads are participant-shared
    (no real user_id); a DM thread must seed ``chat_type="dm"`` — DM-thread replies route through
    the DM arm (``…:dm:<chat>:<thread>``), so a "thread"-typed seed is a row no DM reply hits.
    Non-DM threads seed the slot the platform's adapter puts on an in-thread reply
    (``_THREAD_REPLY_CHAT_TYPE``)."""
    from cron.scheduler_delivery import _THREAD_REPLY_CHAT_TYPE, _seed_cron_session
    text = (mirror_text or "").strip()
    if not text:
        return
    try:
        ok = _seed_cron_session(
            job, adapter, platform_name, chat_id, text,
            thread_id=str(thread_id),
            chat_type="dm" if is_dm else _THREAD_REPLY_CHAT_TYPE.get(platform_name.lower(), "thread"),
            user_id="system:cron", user_name="Cron", chat_name=chat_name, scope_id=scope_id,
            discord_keys_on_thread=True)
        if ok:
            logger.info(
                "Job '%s': opened continuable thread %s on %s:%s and seeded the brief",
                job.get("id", "?"), thread_id, platform_name, chat_id)
        else:
            logger.warning(
                "Job '%s': thread seed did NOT land on %s:%s thread=%s — an "
                "in-thread reply will not see this brief",
                job.get("id", "?"), platform_name, chat_id, thread_id)
    except Exception as e:
        # WARNING, not debug: a silent seed failure IS the continuation-amnesia bug.
        logger.warning(
            "Job '%s': seeding cron thread session failed for %s:%s:%s: %s",
            job.get("id", "?"), platform_name, chat_id, thread_id, e)


def _seed_cron_channel_session(
    job: dict, adapter, platform_name: str, chat_id: str, mirror_text: str, *, is_dm: bool,
    user_id: Optional[str], chat_name: Optional[str] = None, scope_id: Optional[str] = None,
) -> bool:
    """Seed the FLAT (thread_id=None) session for an ``in_channel`` delivery; True on success.
    ``mirror_to_session`` only APPENDS to an existing session and the flat row is only created by
    an inbound human message, so create the row first or the brief is silently dropped. Group keys
    are user-isolated (``…:group:<chat_id>:<user_id>``): the seed MUST carry the origin's real
    user_id, not ``system:cron``; DM keys omit user_id. chat_type mirrors the inbound handler."""
    from cron.scheduler_delivery import (
        _seed_cron_session,
    )
    text = (mirror_text or "").strip()
    if not text:
        return False
    try:
        chat_type = "dm" if is_dm else "group"
        ok = _seed_cron_session(
            job, adapter, platform_name, chat_id, text,
            thread_id=None,  # flat — the whole-channel/DM session
            chat_type=chat_type, user_id=str(user_id) if user_id else None,
            chat_name=chat_name, scope_id=scope_id,
        )
        if ok:
            logger.info(
                "Job '%s': seeded flat in_channel session on %s:%s (chat_type=%s)",
                job.get("id", "?"), platform_name, chat_id, chat_type)
        return bool(ok)
    except Exception as e:
        # WARNING, not debug: a silent seed failure IS the continuation-amnesia bug.
        logger.warning(
            "Job '%s': seeding in_channel session failed for %s:%s: %s",
            job.get("id", "?"), platform_name, chat_id, e)
        return False
