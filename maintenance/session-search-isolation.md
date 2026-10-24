# Session Search Isolation

Load when changing search connection acquisition, reader budgets, result error handling, or search diagnostics. Read [Session-Search Ranking](session-search-ranking.md) before changing ordering and [Session-Link Rendering](session-link-rendering.md) before changing returned tool payloads.

## Required Divergence

Patch identity: `session-search-isolation`.

Ordinary state reads retain upstream's writer fallback. Recall does not: one reader-only scope covers MATCH, metadata, lineage resolution, and hydration. Admission reuses the existing per-file and process descriptor budgets, with at most 64 waiting searches per file and a 5-second reader-admission budget. Known background sources yield to interactive or unknown callers, FIFO within each class. Saturation and opening errors are explicit failures. An admitted statement has no search timeout, progress-handler cancellation, or added result cap.

Only recognized FTS5 parser diagnostics may become misses or language-route fallbacks. Busy, interruption, failed context SQL, and corruption propagate to the tool's error response. Search no longer detaches corrupt indexes, because that recovery acquires the writer lock. Existing writer, reopen, and maintenance recovery remains responsible. An index already marked stale still uses the existing canonical LIKE route.

The slow log retains the served profile's existing threshold. Timings use a monotonic clock: `reader_queue_ms` includes admission and connection opening, `sql_ms` measures read-statement execution/fetch, `hydration_ms` measures context preparation and tool shaping exclusive of SQL, and `total_ms` includes the entire recall scope. Nested calls reuse the scope and do not produce duplicate logs. The line identifies caller, filters, sort, route, and raw hit count.

## Upstream Disposition

The independent starting hypothesis was reader-pool exhaustion reaching the locked-writer fallback, not slow FTS computation. It follows the production convoy evidence and was tested before widening execution architecture.

Guidance and source were inspected at upstream `5894e750c3dba980d43b25aa29e3958149ba2c61`. Its `_read_ctx` retains the locked-writer fallback. Related sources read during the design preflight:

- [Issue #110610](https://github.com/NousResearch/hermes-agent/issues/110610): transcript writer starvation is related, not a reader-isolation implementation.
- [Issue #139959](https://github.com/NousResearch/hermes-agent/issues/139959): lease failures under SQLite write contention. Its proposed separate lease writer does not isolate recall.
- [PR #102621](https://github.com/NousResearch/hermes-agent/pull/102621): separate read/write pools proposed in another implementation. It does not supply this fork's bounded descriptor-budget reuse and complete tool-hydration scope.

No equivalent covering the complete contract was found in the bounded tracker search. Contribution route is a fork-only draft for this requested delivery, not an upstream publication or runtime promotion. The recurring cost is reconciliation of the read-scope seam with upstream connection and recovery changes.

## Ordering and Execution Decisions

The existing rank-only query keeps FTS5's ordered virtual-table plan (`INDEX 32:M3`). Explicit `newest` and `oldest` retain exact relevance-first timestamp/ID tie-breaking. SQLite uses `INDEX 0:M3` and a temporary order B-tree for that case. A fixed rank-candidate overfetch would miss arbitrarily large boundary ties. Reading through every boundary tie is exact, but can still scan the whole match set and transfer it into Python. No generally cheaper exact replacement was established, so this patch leaves SQL ordering and pagination unchanged rather than substitute approximate results.

A separate worker process is deferred. The reproducible load harness executes real writer SQL while holding both the application writer lock and a WAL write transaction, and completes searches and full tool hydration before releasing either. SQLite statement execution already releases the GIL. A process would add connection, serialization, cancellation, and lifecycle ownership without evidence it is needed for this failure.

Observed on macOS/Python 3.14 with 10,000 messages and 64 simultaneous search threads: serial median 5.201 ms, loaded median 1,601.850 ms, loaded p95 2,622.629 ms, maximum 2,632.872 ms, 64/64 complete. Full tool recall with hydration under the same held writer took 53.747 ms. The concurrent writer completed 6 SQL workload iterations. These are synthetic contention numbers, not a live-gateway latency guarantee.

## Verification and Retirement

`tests/hermes_state/test_search_isolation.py` exercises error classification, saturated admission with the writer held, language and LIKE routes, hydration errors, read-only handles, non-WAL selection, bounded admission, interactive FIFO priority, unbounded admitted SQL, stage logs, exact pagination through ties, and corruption without writer repair. Existing state, pool, search-tool, CJK, fallback, registry, web search, and FTS-recovery suites remain the neighboring proof surfaces.

`python scripts/benchmark_session_search.py --messages 10000 --workers 64` creates and deletes only its own temporary store. Rerun it through the source checkout's normal PM activation. Compare the returned plans and completion counts, not only average latency.

Retire when released upstream satisfies the complete reader-only, honest-error and stage-log contract without this divergence. Reverting this patch changes no persisted schema or transcript data, but restores writer fallback and potentially hidden search failures.
