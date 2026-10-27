# Batch SQLite Display Reconciliation Under CPU Contention

## Summary

Reduce SQLite/Python handoffs while display reconciliation holds the state database's writer lock. Fetch narrow indexes in one step, read payloads in byte-bounded pages, and apply repair updates in batches.

Keep display identity, presentation, archived-row visibility, summary-carrier identity, transaction atomicity, FTS and timeout behavior unchanged.

## Motivation

`sqlite3` releases and reacquires the GIL for each stepped cursor row. `executemany` also steps once per individual UPDATE. A CPU-bound Python thread can therefore turn a cheap display reconciliation into a long writer hold, even when SQLite is not doing much work.

This is distinct from increasing the busy timeout or lease TTL. It reduces the time other writers must wait.

Related reports:

- [State database lock contention across processes](https://github.com/NousResearch/hermes-agent/issues/140622).
- [Turn-lease renewal starvation behind the writer lock](https://github.com/NousResearch/hermes-agent/issues/139959).

These reports describe broader contention problems. This change addresses the display-reconciliation contributor, not every cause of either issue.

## Implementation

1. Preserve off-lock planning and in-transaction freshness validation where available. A stale or unavailable plan still takes the correct full-fold path.
2. Batch narrow index reads through exact typed SQLite aggregation. Preserve NULL, BLOB, INTEGER and REAL values, including signed zero and round-trippable floating-point values.
3. Bound payload pages by both 200 rows and 1 MiB of stored payload. Build the page boundary from ids and byte counts only, so a set of large payloads is never materialized merely to find the boundary. Admit an oversized row alone and fetch it directly, avoiding JSON expansion.
4. Apply fold repairs with `UPDATE ... FROM (VALUES ...)`, at most 200 rows and 600 parameters per statement. Bind identities as BLOBs, without JSON/text coercion.

No new schema, background reconciler, timeout, service or retention policy is required.

## Porting Boundary

The fork implementation is [0xble/hermes-agent#853](https://github.com/0xble/hermes-agent/pull/853). Its incremental planning machinery predates this change and is not present in the fetched upstream revision `c714c42431e518fcd1c8870eff5c667d5de33b57`.

For upstream, port the bounded payload reader and batched repair writer into `hermes_state_messages.py::_reconcile_display_orders` first. That upstream method still streams 1,000-row payload pages and applies repairs through `executemany`. The full incremental planner is not required to remove those handoffs. Do not cherry-pick the fork's mixin blindly.

Searches of upstream PRs for `display fold`, `display_index`, `write lock`, `sqlite compaction` and `GIL display` found related display correctness work, but no equivalent batched display-repair implementation. Existing [content-rewrite reconciliation work](https://github.com/NousResearch/hermes-agent/pull/129592) remains a correctness constraint.

## Verification

- Deterministic regression counts SQLite result rows crossing the writer boundary, rather than asserting a wall-clock timing threshold.
- Exact-value/type round-trip tests cover BLOBs, floats, Unicode, control characters, NULLs and signed zero.
- Full-payload reconciliation is the correctness oracle for cached, affected-group and fallback folds.
- Archive tests cover watermark and named-coverage compaction.
- Large-payload tests retain the legacy display-read memory bound and check progress through oversized, sparse and unordered stale-row selections.
- The load harness runs real archive, FTS and commit work under 1 CPU-bound Python thread. It alternates pinned baseline/candidate pairs, checks the stored fold after every sample, retains raw measurements, and reports nearest-rank p99 writer hold separately from API latency.

The reproducible harness is [`scripts/bench_display_writer_hold.py`](../scripts/bench_display_writer_hold.py). Synthetic results do not establish live p99 for a large or cold database.
