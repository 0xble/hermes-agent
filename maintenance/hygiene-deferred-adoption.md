# Hygiene deferred adoption

Load this unit when changing gateway hygiene turn-hold handling (`gateway/run_turn.py::_hmwa_hygiene_on_turn_hold`),
deferred summary adoption, the hygiene token count stored on the session entry, or the lease-less
`archive_and_compact` callers (proactive prune, micro-compaction).

## Required behavior

- When the turn-hold expires but the watermark-fenced worker keeps its commit admission, the user sees no notice.
  The turn-hold is logged at INFO only, because the summary is adopted later at the fenced commit.
- The cancel path, where nothing is adopted, keeps the `turnhold_deferred` notice. Sometimes a kept-admission worker
  stops on a real summary abort (`_last_compress_aborted`). It then posts the ordinary `hygiene_failed` warning once
  and escalates the failure cooldown, the same way the inline abort path does. A no-op or superseded worker records
  only the flat retry spacing and stays silent.
- Deferred adoption resets `last_prompt_tokens` to 0 on the entry and in the store, and it bumps a per-session
  adoption epoch. When a turn's epoch moved while it ran, that turn's end-of-turn prompt count measured the
  pre-compaction transcript, so the turn stores 0 instead. The next hygiene check then estimates the compacted
  transcript and does not re-fire on it. The turn's count is written off-loop, so the epoch is checked again after
  that write lands and the count is re-zeroed if an adoption landed meanwhile; otherwise the turn's stale count can
  overwrite the adoption's 0.
- A lease-less `archive_and_compact` caller passes `held_messages`. If an active summary row is absent from the held
  history, the commit raises `StaleHeldHistory` inside the write transaction and writes nothing. That rewrite was built
  from the generation a compaction already replaced, and committing it would re-insert the whole old transcript beside
  the summary. A gateway projection carries each row's `message_uid` and stored-row CAS snapshot, and the summary is
  matched by that exact version; only legacy projections without them fall back to role and loaded-view content. The
  gateway replay builder (`gateway/run.py::_build_replay_entry`) must therefore keep `_db_row_snapshot` on every row
  whose content it leaves unchanged. A timestamp render is a model-only view: it keeps the snapshot and
  `_replay_persisted_content`, so compaction writes the stored text while the normal flush skips the already-durable
  row. Other rewritten views (mirror prefix, expired confirmation redaction) keep `_db_persisted` to prevent
  marker-only flush from inserting a duplicate, but drop `_db_row_snapshot` and row metadata so lease-less compaction
  cannot treat the rewritten view as held. Tool rows pass through whole with their snapshot. A summary row stripped
  of its snapshot looks unheld and every lease-less prune and defrag after the first compaction is refused as
  "another compaction already committed this session". `held_messages` must also be a per-dict snapshot taken before any in-place rewrite.
  Micro-compaction defrag edits the live marker dict, and an aliased snapshot would refuse every defrag of the current
  generation as stale.

A compaction commit must leave only 1 active row per `message_uid`, including late adoption and rotation.
The carried handoff is authoritative (it may contain pruned content). Concurrent-tail clones skip uids
already carried and keep only the newest physical source per remaining uid. Handoffs themselves keep
the last occurrence of a uid. Concurrent carried slots never consume positional rewind slots below the
watermark. Counters count only inserted survivors. Historical payloads stay intact.
Active model replay of a single session also keeps the newest-positioned uid for already-corrupt
sessions, without writing back or collapsing unknown uids, distinct logical messages with identical
content, lineage reads, or forensic reads. This can hide divergent payloads under a reused uid, which
violates the durable logical-identity contract. The caller's in-memory handoff list is collapsed the
same way after the commit, on both direct `SessionDB` and the worker facade, so a dropped duplicate dict
never flushes back as a second active copy. The worker owner's history read mirrors the replay rule.

Fork patch identity: `hygiene-deferred-adoption`.

## Why

On 2026-10-13, one Telegram session posted the deferral notice four times in an hour. Every summary was adopted about
two minutes later. The same session re-triggered hygiene on almost every turn. After each deferred commit, the active
transcript grew back to the full pre-compaction size, because the stale-generation rewrite re-inserted it. The stored
prompt count was also stale, so hygiene ran a 93-second compression for a 33-to-31-message trim.

## Verification

- `scripts/run_tests.sh tests/gateway/test_session_hygiene_turnhold_adoption.py tests/hermes_state/test_stale_held_generation_guard.py tests/agent/test_micro_compaction.py tests/agent/test_proactive_prune_restart_safety.py tests/gateway/test_replay_entry_fields.py`
- `scripts/run_tests.sh tests/hermes_state/test_compaction_active_uid.py tests/hermes_state/test_compression_watermark_commit.py tests/hermes_state/test_repaired_history_commit_coverage.py tests/hermes_state/test_compression_lease_refresh_before_publish.py tests/hermes_state/test_display_fold_incremental.py tests/hermes_state/test_runtime_worker_compression_annotations.py`
- Live: a deferred adoption logs `summary adopted at the watermark-fenced commit boundary` and sends no chat notice.
  The next turn's hygiene check does not report a pre-compaction `actual` token count.

## Retirement

Retire after upstream silences adopted turn-holds and fences lease-less rewrites against a newer summary generation.
Roll back the patch commits and their regression tests. No persistent-data migration is required.
