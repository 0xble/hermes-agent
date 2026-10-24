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

Retire after upstream supplies the complete off-writer planning contract and
passes these regressions without the fork implementation. The live bottleneck
hypothesis is falsified if the affected call has no off-lock plan or remaining
index enumeration/stale decoding still dominates the hold. Existing timing
phases distinguish planned, full, indexed, decode and update work.
