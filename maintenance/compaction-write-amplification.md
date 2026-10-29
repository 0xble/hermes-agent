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

Stored-alias expansion is load-bearing. A canonical repair key enters `repair_keys`, but an index lookup for that key alone cannot find rows stored only under distinct, non-NULL aliases. For example, 2 rows with the same recomputed identity, different stored aliases, and `display_order = 99` need `plan.aliases` to enqueue both stored identities. Without that expansion, neither the new-row scan nor the NULL-backfill scan finds them, and the incremental fold leaves the group unrepaired. `test_alias_only_repair_requires_stored_alias_expansion` compares this group with the full-fold oracle and includes an alias-expansion-disabled negative control.

Preimage seeding is defence in depth: the visibility trigger already recomputes order for a removed row's stored-identity peers, and alias or NULL mismatches seed canonical repair keys. That does not replace stored-alias expansion from those keys. The freshness guard assumes each pooled reader's `BEGIN` opens a new snapshot, which holds while no caller returns a reader with an open statement.

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

The off-lock plan and every in-lock display-index enumeration use `one_step_rows`. Payload reads in the plan, no-plan full fold and stale-row decode share a boundary of 200 rows and 1 MiB of input, admitting an oversized row alone through a direct fetch. The boundary query retains only ids and byte counts, not a page of large payloads. Repair writes use one `UPDATE FROM (VALUES ...)` per 200 rows, with at most 600 parameters, instead of stepping once per repaired row. `one_step_rows` fetches each page in one SQLite step, as a `json_group_array` that decodes to the exact values and types `sqlite3` returns. BLOBs travel as hex. REALs use `printf('%!.17g')`, because the runtime's JSON REAL rendering keeps only 15 digits. A page containing a REAL zero is refetched conventionally, since no text form keeps the sign of zero. `display_identity` (`hermes_state_identity.py`) feeds sha256 in chunks of 2,047 bytes, so hashlib never releases the GIL. The digest is unchanged.

Every row a cursor yields, and every large hash, releases the GIL and must take it back. On the loaded gateway each re-take waits a switch interval. A live session's plan, measured on a 16,969-row snapshot (76 MB) on 2026-10-25:

| Plan scan | Behind 1 CPU-bound thread |
|---|---:|
| Before (alone: 0.5 s) | 80 s |
| One-step fetch only | 11.6 s |
| One-step fetch and chunked hash | 1.5 s |

Committed compressions spent 2,393 s of 7,245 s of worker time in commit, of which only 258 s was SQLite wait or hold. That pinned compression workers past the 600 s ceiling and drove pool-saturation refusals.

Live, after #799 shipped (13:29-13:48), every slow compaction hold took `display_fallback=1`, with 10 fallbacks and 0 incremental folds. The writer-wide freshness token changes on any write to the database, so under constant writes the affected-group path almost never qualifies. The fallback's in-lock 19k-row index enumeration then held the writer for 5-9 s. With one-step fetch, the same 16,969-row index takes 0.07 s behind 1 CPU-bound thread, against 3.1 s before. This makes the fallback cheap no matter how often freshness fails.

## Writer-Side Repair Batching

[PR #853](https://github.com/0xble/hermes-agent/pull/853) applies the same handoff boundary to affected-group indexes, NULL backfill, stale-payload decode and repair UPDATEs. Keep both the row and byte budget: a row-only page caused the existing large-payload display-read memory regression to fail, so a bounded id/byte-count query chooses the payload prefix before payloads are fetched. An oversized row must still make progress alone, without JSON expansion. Do not replace the existing memory regression with a looser threshold.

`tests/hermes_state/test_display_fold_writer_steps.py` checks page-level SQLite result stepping, batched repair writes, exact full-fold equivalence, both archive paths, and oversized/sparse/unordered selections. Retain `test_legacy_page_retains_only_bounded_payloads` in `test_get_messages_include_compacted.py` and the exact-type tests in `test_display_fold_one_step.py`.

`python scripts/bench_display_writer_hold.py --baseline 82cfb861e2b6487c7b92f0fef4b3d7780a599572 --rows 400 --rounds 100 --output "$TMPDIR/display-writer-hold.json"` measures real archive/FTS/commit hold time with 1 CPU-bound thread, alternating baseline/candidate pairs. Every sample is checked against the stored fold and a full-payload oracle. The output retains samples, source hash, environment, p50 and nearest-rank p99. This is a synthetic contention test, not live or cold-cache p99 for a large store. The PR records measured results and their runner provenance.

The generic bounded-reader/batched-writer proposal is [prepared for upstream](compaction-upstream-proposal.md). Fetched upstream `c714c42431e518fcd1c8870eff5c667d5de33b57` still streams full-fold payloads and uses `executemany`; it lacks this fork's incremental mixin. Port the common full-fold boundary without requiring the fork planner, and preserve the upstream display-correctness tests.

## Compaction Insert Boundaries

The shared `_insert_message_rows` path uses bounded multi-row `INSERT ... VALUES` statements rather than 1 Python/SQLite call per message. Pages admit at most 200 rows and 1 MiB of serialized parameters, respect the connection's actual variable limit, and admit an oversized row alone. All existing message and full-text-search triggers still run synchronously inside the original transaction. No index is bypassed, deferred, or backfilled, and writer patience is unchanged.

Each page reads the actual inserted ids and logical message ids through the existing one-step reader before assigning caller row markers. It does not infer ids from `MAX(id)` plus a contiguous offset, because `sqlite_sequence` can run ahead. A cardinality or identity mismatch raises and rolls back instead of adopting an uncertain row. The caller's existing transaction retry wrapper restores row ids, timestamps, and snapshots on failure.

`insert_incl_fts` includes preparation, SQL plus trigger work, checkpoint pruning, and stored-row snapshot hashing. Its nested phases now separate these costs as `insert_prepare`, `insert_sql_fts`, `insert_checkpoints`, and `insert_snapshots`. Snapshot hashing feeds the unchanged framed bytes to BLAKE2b in chunks of 2,047 bytes, matching the display-identity hash boundary. The digest is byte-identical, without a large-buffer hashlib GIL release at every long field. A low callback CPU time alone does not distinguish I/O wait from GIL reacquisition.

`tests/hermes_state/test_compaction_insert_batches.py` drives both archive paths and checks a deterministic insert-step budget, 999-variable compatibility, row and byte budgets, AUTOINCREMENT gaps, caller snapshots, synchronous FTS integrity, and rollback. It reuses the native CJK fixture to exercise the third search index. `tests/agent/test_transcript_snapshot_hash_steps.py` checks digest equivalence and hash-update sizes across the 2,048-byte boundary, Unicode, BLOBs, numeric values, and surrogate text.

`scripts/bench_compaction_insert.py` compares the base inserter and snapshot hash with batch-only, hash-only, and combined variants. It alternates variants on the same synthetic 400-row scratch store, compacts 201 rows per sample, performs 1 warmup and 5 measured rounds, and measures both idle execution and contention from 1 CPU-bound thread. Every sample must have identical stored rows and correct MATCH results and integrity checks for all 3 FTS tables. This tests scheduling amplification, not cold-cache behavior or the absolute hold bound of the live 73 GB store. If the combined variant leaves the loaded insert phase and hold cost unchanged, the scheduling hypothesis is falsified for this workload.
