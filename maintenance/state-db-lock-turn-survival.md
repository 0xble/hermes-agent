# Busy state.db does not kill turns or mask persistence failures

Load this unit when changing session turn-lease refresh (`agent/turn_facade_lease.py`,
`SessionDB.refresh_session_turn_lease`, `session_turn_lease_expires_at`) or the
silent-tool-tail branch of `agent/turn_finalizer.py`.

Fork patch identity: `state-db-lock-turn-survival`.

## Required behavior

- A turn-lease refresh that hits a SQLite lock is a missed tick, not a lost lease. The
  turn keeps running while the next renewal can still land before the row's committed
  expiry. Once it cannot, the turn stops before a successor can reclaim the row.
- When the post-tool flush fails and the tool round exits `session_persistence_failed`,
  the finalizer keeps that reason and its cause (`session_persistence_failed:locked`).
  The silent-tool-tail close must not overwrite it with `pending_tool_result`.

## 2026-10-09 incident

Cron `d8ddc06f5259` (brief-lpg-priorities) failed at 07:04 PDT with "No reply: the turn
stopped while a tool result was still pending". A bulk delete of 1,906 sessions on the
38 GB `state.db` held the write lock for 20-80 s per transaction. At 07:04:31 the lease
refresher hit `database is locked` and hard-interrupted the turn, as it did about 15
other live sessions. At 07:04:49 the post-tool flush failed with the same lock, and the
finalizer replaced `session_persistence_failed` with `pending_tool_result`.

## Patch

This unit is an exact upstream backport. Commits were cherry-picked with `-x` and
applied without conflicts on fork `main` `d2fa237e7078`.

- **A.** [NousResearch/hermes-agent#134577](https://github.com/NousResearch/hermes-agent/pull/134577),
  merged upstream 2026-10-07 as `c538ec5f402e`. Commits `9ba015eafa5c`, `96e2fef9a745`,
  `064baef6471f`, `2d89e0fd781e`. Source surfaces: `agent/turn_facade_lease.py` and
  `hermes_state_compression.py`. Proof surface: `tests/agent/test_turn_facade_lease.py`
  (`test_refresh_tick_sqlite_lock_keeps_the_turn`,
  `test_lock_tolerance_never_outlives_the_committed_row_expiry`).
- **B.** [NousResearch/hermes-agent#132559](https://github.com/NousResearch/hermes-agent/pull/132559),
  open upstream (author shanelic). Commits `cd2444653bde`, `857365582a8d`. Source
  surface: one guard in `agent/turn_finalizer.py`. Proof surface:
  `tests/agent/test_turn_finalizer_interrupt_alternation.py`
  (`test_tool_tail_preserves_incremental_persistence_failure`).

All three regressions fail on the fork base and pass with the backport.

**Retire A** when fork sync PR #396 (`candidate/fork-sync-v0.21.6-20261008`) or a later
sync lands, because it already contains #134577. Because these are the same upstream
commits, the sync should merge cleanly. **Retire B** when upstream merges #132559 and a
fork sync includes it. If upstream changes B before merging, take upstream's version.

**Rollback:** revert the six backport commits. Each touches only the files above.
