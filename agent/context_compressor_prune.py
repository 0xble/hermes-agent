"""The proactive tool-result prune: deterministic passes plus the in-place row commit (#124102).

Split out of :mod:`agent.context_compressor` (FILE_LINES ratchet): the prune is a self-contained
reclamation feature. The origin module keeps the ``ContextCompressor`` class; this sibling owns
``prune_tool_results_only`` and its in-place commit helper. Log name parity with the origin
module, and every origin-module symbol is read through ``_cc()`` at call time so tests that patch
``agent.context_compressor.X`` stay effective.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Optional

logger = logging.getLogger("agent.context_compressor")


def _cc():
    """The origin module, resolved lazily: avoids the import cycle and keeps tests that patch
    ``agent.context_compressor.X`` effective (attributes are read at call time)."""
    from agent import context_compressor
    return context_compressor


class ProactivePruneMixin:
    """``ContextCompressor`` mixin: the deterministic, no-LLM tool-result prune and its commit."""

    def _record_optional_archive_capacity_failure(self) -> None:
        """Back off optional rewrites only; mandatory full compaction must stay available.

        Keep this local: writing the refusal to the bounded outbox could itself poison it.
        """
        seconds = _cc()._next_timeout_cooldown(self, "_consecutive_archive_capacity_failures")
        self._optional_archive_cooldown_until = max(self._optional_archive_cooldown_until, time.monotonic() + seconds)

    def would_proactively_prune(self, current_tokens: int | None = None) -> bool:
        """Configured on AND above its trigger: the two state-free preconditions
        ``prune_tool_results_only`` checks first, published so the host can tell a withheld prune
        from a no-op. Not a prediction that a prune would commit (tail, runway and reclaim gate
        need the scan)."""
        return self.proactive_prune_tokens > 0 and (
            current_tokens is None or current_tokens >= self.proactive_prune_tokens
        )

    def prune_tool_results_only(
        self, messages: list[dict[str, Any]], current_tokens: int | None = None, *, cold_cache: bool = False,
        turn_start: int | None = None,
    ) -> tuple[list[dict[str, Any]], int]:
        """Prune old tool results on an expired cache, or above the opt-in warm trigger.

        Never shorten the running turn. An invalid anchor means the running turn
        cannot be identified, so skip rather than guessing from the tail size.

        Protects the tail by message COUNT only. A warm commit breaks the prompt cache, so it requires
        ``proactive_prune_min_reclaim_tokens`` (less what cold prunes freed since the last warm one) and a
        full regrowth runway; otherwise returns the INPUT object as ``(messages, 0)``. ``cold_cache`` (the
        provider cache has already expired) has nothing to break: the gate drops to
        ``_COLD_CACHE_MIN_RECLAIM_TOKENS`` and the runway is neither checked nor moved. The rearm gate is measured on message bodies only, so it is
        bypassed (never the reclaim gate) when a provider-billed ``current_tokens`` reading already
        puts the request over ``threshold_tokens`` (#101889); every no-op taken while over threshold
        is logged once per distinct reason.

        The underlying planner deduplicates identical results, summarizes oversized old result
        bodies, and retires old tool images. Canonical tool-call arguments stay unchanged.
        Dedup and image retirement may otherwise touch the protected tail; the running turn and
        the tool round at the tail boundary are restored whole before counting token savings.
        """
        if self._optional_archive_cooldown_until > time.monotonic():
            return messages, 0
        if not (cold_cache or self.would_proactively_prune(current_tokens)):
            return messages, 0
        if turn_start is not None and not (type(turn_start) is int and 0 <= turn_start < len(messages)
                and messages[turn_start].get("role") == "user"):
            self._warn_reclamation_no_op("prune:running_turn_unknown", current_tokens)
            return messages, 0
        if len(messages) <= self.protect_last_n + self._protect_head_size(messages) + 1:
            self._warn_reclamation_no_op("prune:tail_only", current_tokens)
            return messages, 0
        _estimate_msg_budget_tokens = _cc()._estimate_msg_budget_tokens
        before = sum(_estimate_msg_budget_tokens(m) for m in messages)
        # Under-threshold runway skip is ordinary hysteresis (silent); above it the lockout is the bug.
        if (not cold_cache and before < self._proactive_prune_rearm_tokens
                and not self._billed_basis_over_threshold(current_tokens)):
            return messages, 0
        # Capability gate first: a store without archive_and_compact makes every prune a no-op.
        session_db = getattr(self, "_session_db", None)
        session_id = getattr(self, "_session_id", "")
        if session_db and session_id and not callable(getattr(session_db, "archive_and_compact", None)):
            self._warn_reclamation_no_op("prune:store_cannot_persist", current_tokens)
            return messages, 0
        boundary = len(messages) - self.protect_last_n
        if 0 < boundary < len(messages) and messages[boundary].get("role") == "tool":
            boundary = self._align_boundary_backward(messages, boundary)
        pruned_msgs, _ = self._prune_old_tool_results(
            messages, protect_tail_count=len(messages) - boundary, protect_tail_tokens=None,
            min_prune_chars=self.proactive_prune_min_result_chars,
        )
        if turn_start is not None:
            pruned_msgs[turn_start:] = messages[turn_start:]
        # A protected parallel round stays whole, including dedup and image retirement.
        if 0 <= boundary < len(messages) and messages[boundary].get("tool_calls"):
            end = boundary + 1
            while end < len(messages) and messages[end].get("role") == "tool":
                end += 1
            pruned_msgs[boundary:end] = messages[boundary:end]
        pruned_count = sum(a != b for a, b in zip(messages, pruned_msgs))
        from agent.context_compressor_tail import with_pruned_row_skill_safety_rule
        pruned_msgs = with_pruned_row_skill_safety_rule(messages, pruned_msgs)
        if not pruned_count:
            # No-op contract: return the INPUT object so callers can gate on `result is not input`.
            self._warn_reclamation_no_op("prune:nothing_eligible", current_tokens)
            return messages, 0
        # Prompt-cache hysteresis: commit only when the reclaim is meaningful.
        after = sum(_estimate_msg_budget_tokens(m) for m in pruned_msgs)
        reclaimed = max(0, before - after)
        gate = self.proactive_prune_min_reclaim_tokens
        cold_gate = _cc()._COLD_CACHE_MIN_RECLAIM_TOKENS
        if self.proactive_prune_tokens > 0:
            cold_gate = min(gate, cold_gate)
        if reclaimed < (cold_gate if cold_cache else max(cold_gate, gate - self._proactive_prune_cold_credit)):
            self._warn_reclamation_no_op("prune:reclaim_below_minimum", current_tokens, before=before)
            return messages, 0
        if cold_cache:  # Nothing to protect: keep the warm runway and credit the reclaim to the next warm gate.
            next_rearm_tokens, cold_credit = self._proactive_prune_rearm_tokens, self._proactive_prune_cold_credit + reclaimed
        else:  # Require a full trigger-sized regrowth before the next cache-breaking rewrite.
            next_rearm_tokens, cold_credit = after + max(reclaimed, self.proactive_prune_tokens, gate), 0
        if session_db and session_id:
            _cc_ = _cc()
            from hermes_state_errors import HeldRowRewrittenError
            model_config_patch = {_cc_.PROACTIVE_PRUNE_REARM_MODEL_CONFIG_KEY: next_rearm_tokens}
            StaleHeldHistory = _cc_.StaleHeldHistory
            _DB_PERSISTED_MARKER = _cc_._DB_PERSISTED_MARKER
            rewritten = None
            try:
                # Raises StaleHeldHistory when another compaction already won, for both commit paths.
                watermark = _cc_._archive_watermark_for(session_db, session_id, messages)
                rewritten = self._rewrite_pruned_rows_in_place(
                    session_db, session_id, messages, pruned_msgs, model_config_patch)
                if rewritten is None:
                    from agent.conversation_compression_archive import coverage_for_commit, optional_archive_method
                    covered_ids, unresolved_held = coverage_for_commit(session_db, session_id, messages)
                    optional_archive_method(session_db)(
                        session_id, pruned_msgs,
                        model_config_patch=model_config_patch,
                        watermark=watermark,
                        covered_ids=covered_ids, unresolved_held=unresolved_held,
                        held_for_cas=messages, held_messages=messages)
            except StaleHeldHistory:
                # Another compaction already committed this session's history; a lease-less prune of the
                # generation this process holds would publish beside the winner. Leave the input alone.
                logger.info("Proactive tool-result prune skipped: another compaction already committed this session")
                self._warn_reclamation_no_op("prune:stale_generation", current_tokens, before=before)
                return messages, 0
            except HeldRowRewrittenError:
                # The full writer's in-transaction CAS fence tripped: a held row changed between the
                # precheck and the commit. Same disposition as StaleHeldHistory — nothing was written.
                logger.info("Proactive tool-result prune skipped: a held row was rewritten in place before the commit")
                self._warn_reclamation_no_op("prune:stale_generation", current_tokens, before=before)
                return messages, 0
            except Exception as exc:  # health: allow BLE001 -- the commit boundary keeps the original transcript on any store failure (#124102)
                from agent.runtime_session_journal import OutboxCapacityError
                if isinstance(exc, OutboxCapacityError):
                    self._record_optional_archive_capacity_failure()
                    self._warn_reclamation_no_op("prune:archive_capacity", current_tokens, before=before)
                logger.warning("Proactive tool-result prune DB commit failed; keeping the original transcript: %s", exc)
                return messages, 0
            # Shared post-commit stamp site with the in-place commit and micro-compaction sync.
            # See #98450. The in-place rewrite wrote no new rows, so only already-durable messages
            # carry the marker; unwritten ones reach the DB through the next flush.
            stamp_db_persisted_markers = _cc_.stamp_db_persisted_markers
            if rewritten is None:
                stamp_db_persisted_markers(pruned_msgs)
            else:
                stamp_db_persisted_markers([
                    replacement for original, replacement in zip(messages, pruned_msgs)
                    if original.get(_DB_PERSISTED_MARKER)])
        self._consecutive_archive_capacity_failures = 0
        self._optional_archive_cooldown_until = 0.0
        self._proactive_prune_rearm_tokens, self._proactive_prune_cold_credit = next_rearm_tokens, cold_credit
        # Reclamation just ran: let a future lockout warn again.
        self._last_reclaim_block_warn = None
        return pruned_msgs, pruned_count

    def _rewrite_pruned_rows_in_place(
        self, session_db: Any, session_id: str, messages: list[dict[str, Any]],
        pruned_msgs: list[dict[str, Any]], model_config_patch: dict[str, Any],
    ) -> Optional[int]:
        """Commit a prune by rewriting only its changed rows (#124102); ``None`` means use the full rewrite.

        A prune edits the content of a few existing messages and never adds, drops or reorders one, so the
        durable rows it touched can be updated where they are. That needs a store with
        ``rewrite_pruned_rows`` and unwritten messages only at the end (the next flush appends those in
        order; one sitting before durable rows would land out of order). A row the store cannot name
        exactly also falls back: ``archive_and_compact`` handles every shape. A row it *can* name whose
        stored body changed is not ambiguity but a lost race, and raises ``StaleHeldHistory`` so the
        caller skips the pass rather than republishing the held generation.
        """
        rewrite = getattr(session_db, "rewrite_pruned_rows", None)
        if not callable(rewrite) or len(messages) != len(pruned_msgs):
            return None
        from hermes_state_errors import PruneRowStaleError, PruneRowUnresolvedError

        _DB_PERSISTED_MARKER = _cc()._DB_PERSISTED_MARKER
        ignored = {_DB_PERSISTED_MARKER, "_row_id"}
        changes: list[tuple] = []
        unwritten_seen = False
        for original, replacement in zip(messages, pruned_msgs):
            if not isinstance(original, dict) or not isinstance(replacement, dict):
                return None
            if not original.get(_DB_PERSISTED_MARKER):
                unwritten_seen = True
                continue
            if unwritten_seen:
                return None
            if ({k: v for k, v in original.items() if k not in ignored}
                    != {k: v for k, v in replacement.items() if k not in ignored}):
                changes.append((original, replacement))
        try:
            return rewrite(session_id, changes, model_config_patch=model_config_patch)
        except PruneRowStaleError as exc:
            # A named row whose stored body changed is another writer's generation, which is exactly
            # what StaleHeldHistory exists to catch — the in-place prune preserved the row ids, so
            # _archive_watermark_for could not see it. Falling back would hand this stale transcript to
            # archive_and_compact, which archives rows it never compared and republishes the held text
            # over the newer one. Fail closed; the caller skips the pass.
            raise _cc().StaleHeldHistory(str(exc)) from exc
        except PruneRowUnresolvedError as exc:
            logger.info("Proactive prune: in-place rewrite refused (%s); using the full rewrite", exc)
            return None
