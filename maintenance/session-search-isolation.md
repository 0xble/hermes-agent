# Session Search Isolation

Load when changing search connection acquisition, reader budgets, result error handling, or search diagnostics. Read [Session-Search Ranking](session-search-ranking.md) before changing ordering and [Session-Link Rendering](session-link-rendering.md) before changing returned tool payloads.

## Required Divergence

Patch identity: `session-search-isolation`.

Ordinary state reads retain upstream's writer fallback. Recall does not: one reader-only scope covers MATCH, metadata, lineage resolution, and hydration. Admission reuses the existing per-file and process descriptor budgets, with at most 64 waiting searches per file and a 5-second reader-admission budget. Known background sources yield to interactive or unknown callers, FIFO within each class. Saturation and opening errors are explicit failures. Query-bearing recall has one 30-second monotonic SQLite progress-handler budget after admission, shared by nested queries, fallbacks, metadata reads and hydration. An owned interruption returns an explicit `search too broad, refine` error, never a miss or partial success. The callback is removed before returning the connection to its pool. No result cap is added, and ranking/pagination are unchanged.

Session rows read inside recall skip token-count draining. Metadata token totals may be slightly stale until queued deltas commit. Outside recall, `get_session` still drains deltas for exact accounting. The tool paths for discovery, title lookup, compression ancestors and tips, recent browse, transcript hydration, scrolling, and rebuild status all use the scoped reader without the writer lock or a token flush. Admission priority reads only task-bound session source, platform, turn origin, cron status, and delegated-child context, never ambient process environment. It is rechecked after connection opening, before handing out the reader. Queryless tool browse also skips the SQL deadline. Ordinary recent-session UI browse retains its existing deadline through the shared runtime helper.

Only recognized FTS5 parser diagnostics may become misses or language-route fallbacks. Busy, interruption, failed context SQL, and corruption propagate to the tool's error response. Search no longer detaches corrupt indexes, because that recovery acquires the writer lock. Existing writer, reopen, and maintenance recovery remains responsible. An index already marked stale still uses the existing canonical LIKE route.

The slow log retains the served profile's existing threshold. Timings use a monotonic clock: `reader_queue_ms` includes admission and connection opening, `sql_ms` measures read-statement execution/fetch, `hydration_ms` measures context preparation and tool shaping exclusive of SQL, and `total_ms` includes the entire recall scope. Nested calls reuse the scope and do not produce duplicate logs. The line identifies caller, filters, sort, route, and raw search-hit count. SQL runners record the actual route, overriding the query-only predictor. Filters containing `tool` require a canonical LIKE scan to preserve hits beyond the indexed 8,192-character tool-body prefix, so they must not be labeled `fts5`. Queryless browse, transcript reads, and scrolling identify their own route and successful result count instead of appearing as failed admission. Their direct SQL, including compression-tip lookup and anchored windows, uses the same SQL timer as MATCH and is excluded from hydration time.

## Upstream Disposition

The independent starting hypothesis was reader-pool exhaustion reaching the locked-writer fallback, not slow FTS computation. It follows the production convoy evidence and was tested before widening execution architecture.

Guidance and source were inspected at upstream `5894e750c3dba980d43b25aa29e3958149ba2c61`, with root and tool guidance rechecked at `bac34e9e9946c9c6a9ce7ce64efadd47f8fa375b` for the token-flush correction. Root and contribution guidance were rechecked at `a547240007877c7fd99c4f92069e402c672b7928` for the admission and stage-log review fixes, reusing the existing bounded preflight and timing owner rather than adding a new connection wrapper. The existing recall scope remains the narrow owner for skipping token drains, rather than changing accounting reads globally. Related sources read during the design preflight:

- [Issue #141966](https://github.com/NousResearch/hermes-agent/issues/141966): the same 5-second `get_session` token-writer wait in gateway handlers. Skipping drains in recall addresses that dependency without changing those handlers' accounting contract.

- [Issue #110610](https://github.com/NousResearch/hermes-agent/issues/110610): transcript writer starvation is related, not a reader-isolation implementation.
- [Issue #139959](https://github.com/NousResearch/hermes-agent/issues/139959): lease failures under SQLite write contention. Its proposed separate lease writer does not isolate recall.
- [PR #102621](https://github.com/NousResearch/hermes-agent/pull/102621): separate read/write pools proposed in another implementation. It does not supply this fork's bounded descriptor-budget reuse and complete tool-hydration scope.

No equivalent covering the complete contract was found in the bounded tracker search. Contribution route is a fork-only PR for this requested delivery, not an upstream publication or runtime promotion. The recurring cost is reconciliation of the read-scope seam with upstream connection and recovery changes.

## Ordering and Execution Decisions

The existing rank-only query keeps FTS5's ordered virtual-table plan (`INDEX 32:M3`). Explicit `newest` and `oldest` retain exact relevance-first timestamp/ID tie-breaking. SQLite uses `INDEX 0:M3` and a temporary order B-tree for that case. A fixed rank-candidate overfetch would miss arbitrarily large boundary ties. Reading through every boundary tie is exact, but can still scan the whole match set and transfer it into Python. No generally cheaper exact replacement was established, so this patch leaves SQL ordering and pagination unchanged rather than substitute approximate results.

A separate worker process is deferred. The reproducible load harness executes real writer SQL while holding both the application writer lock and a WAL write transaction, and completes searches and full tool hydration before releasing either. SQLite statement execution already releases the GIL. A process would add connection, serialization, cancellation, and lifecycle ownership without evidence it is needed for this failure.

Observed on macOS/Python 3.14 with 10,000 messages and 64 simultaneous search threads: serial median 5.201 ms, loaded median 1,601.850 ms, loaded p95 2,622.629 ms, maximum 2,632.872 ms, 64/64 complete. Full tool recall with hydration under the same held writer took 53.747 ms. The concurrent writer completed 6 SQL workload iterations. These are synthetic contention numbers, not a live-gateway latency guarantee.

## Verification and Retirement

`tests/hermes_state/test_search_isolation.py` exercises error classification, saturated admission with the writer held, language and LIKE routes, hydration errors, read-only handles, non-WAL selection, bounded admission, interactive FIFO priority, admission-wait/SQL-budget independence, stage logs, exact pagination through ties, and corruption without writer repair. `tests/hermes_state/test_search_isolation_review.py` holds the writer lock with busy, pending token deltas across every tool shape and a 3-ancestor walk. It also covers priority races during checkout, real cron and delegated-child boundaries, unbound process-environment contamination, partial-result supplement errors, and unavailable-store/read error responses. A controlled-clock test traces real SQLite reads across discovery, browse, transcript, and scroll shapes and requires every statement's time to be reported as SQL, with one honest route/count log per call. `tests/hermes_state/test_search_query_budget.py` exercises explicit errors on broad LIKE and FTS5 queries, shared nested budgets, full-body tool-tail recall, actual-route logging, callback removal before connection reuse, and deadline-free browse/read/scroll shapes. Existing state, pool, search-tool, CJK, fallback, registry, web search, and FTS-recovery suites remain the neighboring proof surfaces.

`python scripts/benchmark_session_search.py --messages 10000 --workers 64` creates and deletes only its own temporary store. Rerun it through the source checkout's normal PM activation. Compare the returned plans and completion counts, not only average latency.

Retire when released upstream satisfies the complete reader-only, honest-error and stage-log contract without this divergence. Reverting this patch changes no persisted schema or transcript data, but restores writer fallback and potentially hidden search failures.
