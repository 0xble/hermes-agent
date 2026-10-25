# Compaction Display-Fold Writer Cost

Patch identity: `compaction-write-amplification`.

## Required Behavior

A WAL snapshot may plan display identities before compaction takes the writer
lock. Under the lock, reuse a planned identity only when the stored identity
still matches. Content, role, timestamp, tool fields and display-kind edits
invalidate the stored identity through the existing trigger. Unseen or stale
rows still decode under the lock. Full-fold fallback remains available without
a WAL plan. Preserve transactions, rollback, clone behavior and existing row
counters.

User handoff identity depends on the split live view's content, not its copied
presentation metadata. Do not reread historical user metadata to validate that
identity. Reactions and model-only membership remain authoritative on the read
path, not cached in a fold plan.

## Evidence And Verification

`tests/hermes_state/test_archive_compact_off_writer_lock.py` covers metadata-key
invariance, visible metadata and membership, unchanged-row SQL traces, stale and
unseen rows, planned/full-fold equivalence and the no-WAL fallback. The former
metadata-recomputation test asserted an incorrect premise and is replaced with
these semantic and resource assertions. `test_compaction_write_metrics.py`
checks existing amplification counters and bounded display subphase timings.

Reproduce the real SQLite comparison with the repository's test Python:

```sh
python scripts/bench_display_fold.py --baseline c972a26e2057aa1cb082c1b2213b83c4529b6f06 --output "$TMPDIR/display-fold.json"
```

The fixed protocol uses 2,000 initial user rows, 64 KiB content and 128 KiB
metadata per row, a 128 KiB writer page cache, 1 warmup pair and 21 alternating
pairs on the same disposable WAL database. Stored-fold and visible-projection
digests must agree before/after both implementations and their full folds.
Observed medians were 110.774 ms before and 1.227 ms after, a 98.892% reduction
for the unchanged planned fold. Median transaction time was 110.861 ms versus
1.348 ms, with 1 candidate transaction outlier at 2,660 ms outside the fold,
retained in raw samples. This is a bounded workload, not a live hold-time
claim. Off-lock planning remains O(history), measured at about 0.6 seconds in
this workload. Clone elimination is not implemented.

## Incremental Display-Fold Contract

Both watermark and named-coverage compaction preserve the existing canonical identity and smallest surviving row-id order. The fold population remains `active = 1 OR compacted = 1`, including model-only rows and excluding removed rewind rows. The newest active representative and paged display projections are unchanged. `hermes_state_display_fold.py` owns affected-group enumeration and the existing planned/full fold.

The off-lock snapshot records each visible row's stored identity, canonical identity and order, canonical-to-stored aliases, and every group needing identity or order repair. Unrelated legacy drift is not skipped.

Before the reader snapshot, briefly hold the existing process writer guard and capture the writer connection object, `total_changes`, and `PRAGMA main.data_version`. After `BEGIN IMMEDIATE`, before any callback mutation, compare all 3 signals on the acquired writer. This rejects same-connection writes, rolled-back writes counted by SQLite, other-connection commits, visibility/order/metadata changes, and connection replacement. Capturing before the snapshot is essential. A write between capture and snapshot is a conservative rejection, never evidence of freshness.

For a fresh attempt, seed the closure with repair groups, canonical and stored preimages of rows leaving the population, actual inserted rows and clones, and every remaining NULL-identity or NULL-order row. Query `idx_messages_display_identity` by session and `display_identity IN (...)`, with at most 500 identities per query. Validate each fetched row against the snapshot and recompute unseen or changed payloads. Expand through newly discovered canonical groups, their stored aliases, and foreign/colliding aliases until closed. Reuse the existing fold on selected rows in id order. No schema, index, identity-function or projection change is involved.

Preimage seeding and stored-alias expansion are defence in depth, not the guarantee. The visibility trigger already recomputes order for a removed row's stored-identity peers, and any alias or NULL mismatch already puts its canonical key in `repair_keys`. Mutation testing on 2026-10-25 found no current scenario that depends on either step. Keep them so a later trigger or repair-key change cannot silently break the closure. The freshness guard also assumes each pooled reader's `BEGIN` opens a new snapshot, which holds while no caller returns a reader with an open statement.

### Fallback And Limits

Unavailable or changed freshness signals select the existing planned whole-index fold. No WAL reader or no plan selects the full payload fold. Missing display indexes also fall back to the full payload fold. Non-compaction reconciliation keeps its existing generic/full behavior. Every transaction retry checks the original snapshot again and creates a new attempt-local selection. Rollback remains atomic.

Eligible in-lock display work scales with affected canonical/alias groups, their surviving members, repair work and NULL backlog, not unrelated healthy history. It is not constant-time when a repeated group itself grows, legacy repair or NULL backlog grows, or freshness fails. Off-lock planning remains O(history), with additional memory for alias sets. Any intervening database write can conservatively force fallback, including an unrelated session's write. This does not remove FTS reinsertion amplification, lock admission, commit, checkpoint or disk latency.

### Diagnostics And Regression Evidence

Existing attempt-local metrics emit `display_incremental=1` or `display_fallback=1`. `display_identity_rows`, `display_backfill_rows`, `display_new_rows` and `display_preimage_rows` count cursor enumeration, while `display_materialized_rows` counts unique selected rows. Cursor counts can overlap. Existing index, stale-decode, fold, update and thread-CPU phases remain available. No payloads or identities are logged.

The 3 invariant tests in `test_display_fold_incremental.py` compare randomized archives with the full-fold oracle, exercise real snapshot/writer interleavings, and bound indexed work as unrelated history grows. Coverage includes both archive paths, split generations, colliding aliases across groups and sessions, order-only drift, NULLs, oldest-member removal, gap clones, handoff normalization, stable tool identities, paging, unavailable guards, missing indexes, connection replacement and a real writer retry after the first fold rolls back. The full-index diagnostic test explicitly selects fallback so both diagnostic paths retain coverage.

Reproduce measurements through the repository test environment and `heavy-slot`:

```sh
python scripts/bench_display_fold_incremental.py --base 9cc95ec4b933e877c58e15357b194e7b016b1506 --output "$TMPDIR/display-fold-incremental.json"
```

This uses only synthetic databases, checks exact stored-fold equivalence, alternates the baseline and candidate, retains all timing samples, and reports planning separately. The component arm follows the warm 21-pair protocol at 30k/60k/100k history rows, with a 2 MiB cache, 256-byte payload suffix and fixed 100-row affected population. It times enumeration plus fold inside an already-held transaction, not cold-cache or gateway latency. The archive arm also measures actual archive/insert/FTS/commit work for both archive modes.

Measured on Python 3.14.7 and SQLite 3.53.1 against base `9cc95ec4`, with every stored fold equal to the full-fold oracle. Component arm, median (p95) in ms for the index enumeration plus fold:

- 30k rows: full 24.54 (25.35), incremental 0.62 (0.72); approx VM steps 214,700 vs 5,900.
- 60k rows: full 60.49 (63.71), incremental 0.73 (0.87); 424,700 vs 5,900.
- 100k rows: full 119.09 (149.57), incremental 0.79 (0.98); 704,700 vs 5,900.

The incremental cost stays flat as unrelated history grows, while the full index scales with it. Archive arm writer hold in ms over 3 rounds, watermark and named coverage: 30k 33.2→7.7 and 39.5→12.8; 60k 69.2→8.8 and 77.8→18.1; 100k 123.0→9.1 and 138.6→25.0. The remaining hold is archive, insert, FTS and commit work. These are synthetic bounds, not a live hold-time claim.

## Upstream Disposition And Retirement

Fork-only correction to the off-writer fold plan, whose metadata revalidation
was introduced in `9f26c9af94c411b8abcf720d4b949b3f0e8e4fc3`.
Guidance checked at upstream `5141460529ad320140f9916be0b88ab1f0af563e`:
root `AGENTS.md` and `CONTRIBUTING.md`. Prior-art searches for display identity
and compaction writer cost found related transcript-index work, including
[split display generations](https://github.com/NousResearch/hermes-agent/pull/128296)
and [display-order replay drift](https://github.com/NousResearch/hermes-agent/issues/133880),
not a reason to retain metadata rereads. Existing invalidation is preserved.
No upstream contribution is associated with this fork-only correction.

The incremental-fold preflight checked current upstream guidance at `571a5911ae6aaa05ef3df53bf0f6720c4556c24a`. Related [#130499](https://github.com/NousResearch/hermes-agent/pull/130499), [#119002](https://github.com/NousResearch/hermes-agent/pull/119002), and [#129592](https://github.com/NousResearch/hermes-agent/pull/129592) concern NULL ordering, NULL-safe paging and content-rewrite healing. They support preserving full-fold and projection semantics but do not provide affected-group enumeration. Generic rewrite reconciliation deliberately remains on the existing full path.

Retire after upstream supplies the complete off-writer planning contract and
passes these regressions without the fork implementation. The live bottleneck
hypothesis is falsified if the affected call has no off-lock plan or remaining
index enumeration/stale decoding still dominates the hold. Existing timing
phases distinguish planned, full, indexed, decode and update work.

## GIL-Bound Scans

The off-lock plan, the in-lock full fold and the stale-row decode read rows through `fold_rows`. It fetches each page (up to 1,000 rows) in one SQLite step, as a `json_group_array` that decodes to the exact values and types `sqlite3` returns. BLOBs travel as hex. REALs use `printf('%!.17g')`, because the runtime's JSON REAL rendering keeps only 15 digits. A row with a REAL zero is refetched conventionally, since no text form keeps the sign of zero. `display_identity` (`hermes_state_identity.py`) feeds sha256 in chunks of 2,047 bytes, so hashlib never releases the GIL. The digest is unchanged.

Every row a cursor yields, and every large hash, releases the GIL and must take it back. On the loaded gateway each re-take waits a switch interval. A live session's plan, measured on a 16,969-row snapshot (76 MB) on 2026-10-25:

| Plan scan | Behind 1 CPU-bound thread |
|---|---:|
| Before (alone: 0.5 s) | 80 s |
| One-step fetch only | 11.6 s |
| One-step fetch and chunked hash | 1.5 s |

Committed compressions spent 2,393 s of 7,245 s of worker time in commit, of which only 258 s was SQLite wait or hold. That pinned compression workers past the 600 s ceiling and drove pool-saturation refusals.
