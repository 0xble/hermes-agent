"""Publish a generated compaction without repeating the summary phase."""
from __future__ import annotations

import contextlib
import copy
import functools
import logging
import sqlite3
import time
from typing import Any, Optional, TYPE_CHECKING

if TYPE_CHECKING:
    from agent.conversation_compression import _CompressionLease, _Attempt, _CommitOutcome, CompressionCommitFence

from hermes_state_errors import is_sqlite_lock_error
from hermes_state_publication import publication_metrics, record_publication_attempt, raise_if_nonretryable_contention

logger = logging.getLogger("agent.conversation_compression")


def _publication_stages(fn):
    """Keep publication accounting out of preparation, memory jobs and post-commit writes."""
    @functools.wraps(fn)
    def measured(*args, publication_seed=None, **kwargs):
        with publication_metrics(20.0) as metrics:
            try:
                return fn(*args, **kwargs)
            finally:
                if publication_seed is not None:
                    publication_seed.update({k: v for k, v in metrics.items() if k != "patience_s"})
    return measured


@_publication_stages
def retry_publication(fn, *args, **kwargs):
    """Replay only lock/busy publication failures, whose transaction was rolled back.

    Keep the same candidate and holder. Lease loss, corruption and ambiguous I/O
    settlement propagate, never regenerate a summary or publish a stale snapshot.
    """
    for attempt in range(3):
        record_publication_attempt()
        try:
            return fn(*args, **kwargs)
        except sqlite3.OperationalError as exc:
            raise_if_nonretryable_contention(exc)
            if attempt == 2 or not is_sqlite_lock_error(exc):
                raise
            logger.info("Compaction publication busy, retrying the generated candidate (%s/3)", attempt + 2)


@contextlib.contextmanager
def _hold_attempt_telemetry(compressor: Any):
    """Restore the live attempt's telemetry after a call that may reset it (the memory flush)."""
    from agent.conversation_compression import _ATTEMPT_TELEMETRY_FIELDS
    held = tuple(getattr(compressor, name, None) for name in _ATTEMPT_TELEMETRY_FIELDS)
    try:
        yield
    finally:
        for name, value in zip(_ATTEMPT_TELEMETRY_FIELDS, held):
            with contextlib.suppress(Exception):
                setattr(compressor, name, value)


def _prepare_in_place(agent, messages, compressed, tail_tagged_ids, verbatim_tail, original_messages):
    from hermes_cli.partial_compress import rejoin_compressed_head_and_tail
    def _tail_row_is_still_verbatim(message: dict) -> bool:
        row_id = message.get("_row_id")
        if not isinstance(row_id, int) or isinstance(row_id, bool) or row_id <= 0:
            return True
        source = next(
            (candidate for candidate in messages
             if isinstance(candidate, dict) and candidate.get("_row_id") == row_id),
            None,
        )
        if source is None:
            return True
        return (
            source.get("role") == message.get("role")
            and source.get("content") == message.get("content")
            and source.get("tool_calls") == message.get("tool_calls")
        )
    # The rewind below is positional (newest `tail_count` durable rows), so a rewritten carrier can
    # only be left out of the count at the OLDEST end of the tail, where the summary seam folds into
    # it. Excluding one from the middle would shift the window onto a still-verbatim neighbour.
    _tail_rows = [m for m in compressed if id(m) in tail_tagged_ids and isinstance(m, dict)]
    _leading_rewritten = next(
        (i for i, m in enumerate(_tail_rows) if _tail_row_is_still_verbatim(m)), len(_tail_rows))
    tail_count = len(_tail_rows) - _leading_rewritten
    _tail_held = messages[max(0, len(messages) - tail_count):]
    # The rewind takes the newest `tail_count` durable rows as the tail's originals, so a tail row
    # with none (this turn's user row, which the CLI and gateway persist after preflight; unflushed
    # scaffolding) would flag a summarized row superseded instead: gone from display and search.
    # Only while a turn holds the session: between turns (manual /compress, gateway hygiene) the
    # anchor is the last turn's, and the rows it points at are durable, just unmarked.
    _turn_idx = getattr(agent, "_persist_user_message_idx", None)
    if (getattr(agent, "_active_session_turn_lease_holder", None) is not None
            and isinstance(_turn_idx, int) and 0 <= _turn_idx < len(messages)):
        from agent.context_compressor import _DB_PERSISTED_MARKER
        tail_count -= sum(
            1 for m in messages[max(_turn_idx, len(messages) - tail_count):]
            if isinstance(m, dict) and not m.get(_DB_PERSISTED_MARKER)
            and not isinstance(m.get("_row_id"), int))
    # A tail dict stands for every row a repair retired into it (merged users, dropped orphan
    # tool rows): counted once, the oldest carried original stays compacted=1 beside its live
    # copy and is recalled twice. Only rows still active: a live list keeps the ids after an
    # earlier compaction archived them.
    from agent.conversation_compression_archive import ABSORBED_ROW_IDS, _positive_id
    tail_count += len({
        r for m in _tail_held if isinstance(m, dict)
        for r in map(_positive_id, m.get(ABSORBED_ROW_IDS) or ())
        if r is not None and agent._session_db.get_message_role(agent.session_id, r) is not None})
    persisted = compressed
    if verbatim_tail:
        # The kept exchanges are durable rows under the watermark, so the archive below covers
        # them too. Store them after the head in the same transaction, with the seam the caller
        # would build, and count their originals as carried duplicates like compress()'s tail.
        persisted = rejoin_compressed_head_and_tail(compressed, verbatim_tail)
        tail_count += len(verbatim_tail)
    from agent.conversation_compression_archive import coverage_for_commit
    covered_ids, unresolved_held = coverage_for_commit(
        agent._session_db, agent.session_id,
        original_messages,
        verbatim_tail)
    return persisted, tail_count, covered_ids, unresolved_held


def _failed_publication(agent, messages, compressed, *, in_place, old_session_id, split_status,
                        messages_before_compression, made_progress, attempt, e):
    from agent.conversation_compression import (
        _restore_prune_rearm_tokens, _swallow, _SPLIT_FAILURE_COOLDOWN_SECONDS,
    )
    # Rotation: atomic publication failed (including lease loss) — keep the parent live and discard the stale
    # compacted snapshot. In-place: archive_and_compact is atomic so old rows stay active, but marker-swept
    # `compressed` would re-INSERT on top of them (doubling each try); gate on split_status (set right after
    # commit). Either way the deepcopy keeps markers/identity and only the prune runway rolls back (the full
    # snapshot restore is for pre-commit cancels; telemetry keeps the failed values). _db_flush_scan_prefix is
    # intentionally NOT cleared: the scan is identity-based and the deepcopy replaces every row.
    rotation_rollback = not in_place and old_session_id and agent.session_id == old_session_id
    if rotation_rollback or (
        in_place and split_status != "in_place_committed" and messages_before_compression is not None
    ):
        if rotation_rollback:
            old_session_id = None
        # In-place sibling of the rotation rollback above (#99477). archive_and_compact() is atomic,
        # so a raise before it returned means EVERY pre-compaction row is still ``active = 1`` in
        # state.db — nothing was archived and the compacted set was never inserted. But
        # ``compressed`` is the marker-swept output of compress() (_strip_persistence_markers,
        # #57491) and the post-commit ``stamp_db_persisted_markers`` never ran, so handing it back
        # makes the next append-only flush treat the whole compacted transcript as new and INSERT it
        # ON TOP of the rows it was supposed to replace. The active set then holds the summary AND
        # the turns it summarized; the next resume reloads both, the token count goes UP, preflight
        # fires again, and each failed attempt appends another copy of the protected head + tail
        # (#99477: ~15 real turns stored as 3,814 rows, the first user message repeated 893 times).
        # Gate on ``split_status`` rather than ``compacted_in_place``: it is assigned on the
        # statement immediately after the atomic commit returns, so a committed compaction can never
        # be rolled back into a live/durable mismatch of the opposite sign. The deepcopy carries
        # each row's _DB_PERSISTED_MARKER from the pre-compression snapshot, so the restored
        # transcript is correctly skipped by the flush, and replacing every dict breaks
        # _db_flush_scan_prefix identity (same reasoning as the rotation branch — no explicit clear
        # needed).
        messages[:] = copy.deepcopy(messages_before_compression)
        compressed = messages
        made_progress = False
        _restore_prune_rearm_tokens(agent.context_compressor, attempt.snapshot)
    split_status = "aborted" if old_session_id is None and not in_place else "failed_not_indexed"
    # If rotation rolled back to the parent, agent.session_id is the indexed parent
    # and old_session_id was cleared: recovery, not an un-indexed orphan.
    if old_session_id is None and not in_place:
        logger.warning(
            "Compression rotation aborted and rolled back to the parent session (%s): %s",
            agent.session_id or "?", e,
        )
    else:
        logger.warning("Session DB compression split failed — new session will NOT be indexed: %s", e)
    # Arm the failure cooldown so the next turn can't rerun the doomed compression;
    # try/except so a stub compressor can't mask the original error in this handler.
    with _swallow('could not record split-failure cooldown', exc_info=True):
        # See #97948.
        agent.context_compressor._record_compression_failure_cooldown(
            _SPLIT_FAILURE_COOLDOWN_SECONDS, f"session_split_failed: {e}"
        )
    return compressed, made_progress, split_status, old_session_id


def _commit_compaction(
    agent: Any, messages: list, compressed: list, *, in_place: bool, lease: _CompressionLease,
    new_system_prompt: str, system_message: str, compressed_user_turn_outcome: str,
    messages_before_compression: Optional[list], made_progress: bool, attempt: _Attempt,
    verbatim_tail: Optional[list] = None, carried_messages: Optional[list] = None,
    commit_fence: Optional[CompressionCommitFence] = None,
) -> _CommitOutcome:
    """Persist the compacted transcript: anti-growth guard, memory extraction, then the
    in-place archive or the parent->child rotation.

    Failures roll the live list back and arm the split-failure cooldown; a refused (would-grow) candidate returns
    ``refused_prompt`` so the caller hands back the input unchanged. ``verbatim_tail`` (``/compress here N``) is
    re-inserted after the compacted head by either commit and stamped once durable.
    """
    from agent.conversation_compression import (
        _CommitOutcome, _salvage_or_refuse_grown_transcript, _held_watermark,
        _publish_rotated_compaction,
    )
    session_commit_succeeded = False
    compacted_in_place = False
    commit_started_at = time.monotonic()
    split_status = "not_applicable"
    old_session_id: Optional[str] = None  # bound only once rotation begins
    if agent._session_db:
        split_status = "pending"
        try:
            # Pop _compaction_tail tags before the size estimate / rotation: they must not
            # inflate anti-growth or reach the provider. Track ids: salvage may subset list.
            _tail_tagged_ids = {id(m) for m in compressed if isinstance(m, dict) and m.pop("_compaction_tail", None)}
            original_messages = messages_before_compression if messages_before_compression is not None else messages
            compressed, _refused_sp = _salvage_or_refuse_grown_transcript(
                agent, original_messages, compressed, system_message=system_message, attempt=attempt,
            )
            if compressed is None:
                return _CommitOutcome(
                    compressed=messages, refused_prompt=_refused_sp, commit_started_at=commit_started_at
                )
            # Publish memory only after every refusal gate, from the immutable pre-compression snapshot.
            # The engine's session-end reset nulls the in-flight attempt telemetry the success emit
            # still needs (#118580), so hold it across the call.
            with _hold_attempt_telemetry(agent.context_compressor):
                agent.commit_memory_session(original_messages)
            from agent.context_compressor import PROACTIVE_PRUNE_REARM_MODEL_CONFIG_KEY, stamp_db_persisted_markers
            from hermes_cli.partial_compress import rejoin_compressed_head_and_tail
            if in_place:
                # In-place compaction: same session_id; soft-archive old turns (active=0, still
                # searchable) + insert `compressed` atomically; no pre-flush (tail already in).
                # Tail rows tagged by compress() are archived as superseded duplicates, not
                # compacted=1. Count against the FINAL list — salvage may have dropped rows.
                persisted, tail_count, covered_ids, unresolved_held = _prepare_in_place(
                    agent, messages, compressed, _tail_tagged_ids, verbatim_tail, original_messages)
                retry_publication(agent._session_db.archive_and_compact,
                    agent.session_id, persisted, publication_seed=attempt.seed,
                    model_config_patch={PROACTIVE_PRUNE_REARM_MODEL_CONFIG_KEY: None},
                    watermark=_held_watermark(agent, lease.watermark, messages, verbatim_tail),
                    lock_holder=lease.holder, tail_count=tail_count, carried_messages=carried_messages,
                    covered_ids=covered_ids, unresolved_held=unresolved_held,
                )
                compressed = persisted
                split_status = "in_place_committed"
                # compress() returned marker-swept copies; stamp them as persisted or the next
                # flush re-INSERTs the whole compacted transcript, doubling the live set. Reset
                # the flush identity set so next turn diffs against the COMPACTED transcript.
                # The verbatim tail is stamped as well: a seam fold drops its first row from
                # `compressed`, and the stamps tell the caller the tail is already in the list.
                stamp_db_persisted_markers([*compressed, *(verbatim_tail or ())])
                agent._flushed_db_message_ids = set()
                # Rotation-independent signal; the gateway reads this (not an id diff) to
                # re-baseline transcript handling.
                compacted_in_place = True
                # In-place still updates the current row's prompt; rotation published it atomically above.
                agent._session_db.update_system_prompt(agent.session_id, new_system_prompt)
                agent._last_flushed_db_idx = 0
            else:
                # Bind old_session_id first: it is the rollback key in the handler below.
                # ── Rotation (legacy): end this session, fork a continuation ─ Flush any un-persisted
                # current-turn messages to the OLD session before ending it, so they survive in the
                # preserved parent transcript (#47202). (In-place skips this — see above.) Pass the
                # already-durable prefix as conversation_history so the flush skips it by identity (#68196).
                # Preflight compression runs BEFORE the normal turn flush has stamped the cold-resumed
                # history dicts with _DB_PERSISTED_MARKER, so without a boundary
                # _flush_messages_to_session_db treats every restored row as new and re-appends the whole
                # transcript to the parent. turn_context anchors _persist_user_message_idx at the
                # current-turn user message before preflight runs, so messages[:idx] is exactly the
                # persisted prefix; only the current turn's new messages get written. Bound to
                # old_session_id, hoisted above the flush: the ``except`` handler below keys its in-memory
                # rollback off this name, so anything that fails from here on rolls the transcript back
                # instead of leaving the failed attempt's compacted snapshot in place.
                old_session_id = agent.session_id
                if verbatim_tail:
                    # Publish the kept exchanges with the head, as the in-place branch stores them: the
                    # gateway no longer rewrites a published child, so a head-only handoff loses the tail.
                    compressed = rejoin_compressed_head_and_tail(compressed, verbatim_tail)
                _publish_rotated_compaction(
                    agent, messages, compressed, new_system_prompt=new_system_prompt, lease=lease,
                    old_session_id=old_session_id, compressed_user_turn_outcome=compressed_user_turn_outcome,
                    publication_seed=attempt.seed,
                )
                if verbatim_tail:
                    stamp_db_persisted_markers(verbatim_tail)
                split_status = "rotated_committed"
                agent._last_flushed_db_idx = len(compressed)
                agent._flushed_db_message_session_id = agent.session_id
            session_commit_succeeded = True
        except Exception as e:
            compressed, made_progress, split_status, old_session_id = _failed_publication(
                agent, messages, compressed, in_place=in_place, old_session_id=old_session_id,
                split_status=split_status, messages_before_compression=messages_before_compression,
                made_progress=made_progress, attempt=attempt, e=e)
            if commit_fence is not None:
                commit_fence.mark_publication_failed()
    return _CommitOutcome(
        compressed=compressed, commit_started_at=commit_started_at, old_session_id=old_session_id,
        split_status=split_status, session_commit_succeeded=session_commit_succeeded,
        compacted_in_place=compacted_in_place, made_progress=made_progress,
    )
