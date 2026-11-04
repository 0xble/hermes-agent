# Worker outbox capacity

Patch identity: `worker-outbox-capacity`.

## Independent hypothesis (2026-11-04)

The synchronous worker store admits only one outstanding operation. A successful
receipt removes that entry before another write starts. The failed cron's retained
`pending.json` is 350 bytes with an empty pending list and next sequence 1622;
it is not a backlog. Proactive prune failed at 02:51:28 with `outbox_full`.
`compression.archive` repeats compacted/held/CAS history in one operation, so a
legitimate snapshot can exceed the 4 MiB raw JSON bound. The exact rejected
payload was not retained; the operation is inferred from the prune call path.

The narrow fix is a lossless compressed representation for oversized journals,
retaining the existing 4 MiB disk bound, single outstanding operation, atomic
private writes, scope verification and identical receipt replay. Decode must be
bounded too. A definitely-unsent capacity rejection of an explicitly best-effort
archive must leave the prior journal and sequence usable rather than poisoning
subsequent usage/lease/finalization. Mandatory writes must latch `outbox_full`,
so a swallowed append error still fails the turn at its flush/finish barrier.
Unknown outcomes and filesystem failures must remain sticky. Raising the disk
cap alone or dropping transcript/CAS fields would not solve the contract.

Native worker persistence is core behavior; configuration, plugins and hooks do
not own its atomic journal/receipt boundary. Source-only delivery is selected:
no runtime installation, restart, cron mutation or merge is authorized.

## Verification and lifecycle

Regression: `scripts/run_tests.sh tests/agent/test_worker_outbox_capacity.py
 tests/agent/test_runtime_session_store.py tests/agent/test_worker_outbox_reopen.py
 tests/agent/test_worker_outbox_adoption.py`.

Retire when the maintained baseline implements bounded lossless large-operation
journaling and safe pre-send rejection without changing receipt identity. Roll
back by reverting the commit with this patch identity; a compressed pending
journal must first be replayed by a compatible worker. Existing small JSON
journals remain readable and are not migrated proactively.

The first real-owner regression also exposed its separate 4 MiB UTF-8 payload
limit (`hermes_state_runtime.apply_worker_receipt`). Lossless journaling is only
useful for operations that owner accepts: JSON ASCII escaping/envelope overhead
can cross the disk bound before canonical UTF-8 does. Worker admission now checks
both the existing owner payload bound and the 8 MiB RPC frame bound before
journaling. Ineligible compaction retains its original transcript through the
existing best-effort caller; it does not advance sequence or poison the store.
No owner behavior, schema, payload identity or bound is changed.

Compressed decode is limited to min(2 * disk budget, 8 MiB), matching the existing
frame budget and preventing gzip bombs. Disk budget remains exactly 4 MiB by
default. Only journals whose raw representation does not fit are compressed.
Unknown outcomes, write/rename failures and ack-save failures remain fatal until
explicit identical receipt replay, as before. Oversized mandatory writes still
raise and latch honestly; this is not an assertion of canonical persistence.
Malformed gzip headers/trailers and truncated streams report `outbox_corrupt`.

## Capacity policy by call site

One mechanism owns the policy: `_apply(..., best_effort=False)` latches capacity
failures by default, including when a caller catches the exception. Only the
worker's `archive_and_compact_best_effort` capability opts in to non-sticky
admission. Non-worker stores keep their existing `archive_and_compact` call.
Neither opt-in bypasses existing pending/fatal failures, filesystem errors or
uncertain RPC outcomes. No mandatory call site needs a separate return-value rule.

| Call site | Class | Capacity behavior |
| --- | --- | --- |
| `context_compressor_prune.prune_tool_results_only` archive fallback | Best effort | Explicit capability; retain original transcript and prune state, cool down on `archive_capacity`, continue |
| `micro_compaction._sync_micro_compact_to_db` archive | Best effort | Explicit capability; return `archive_capacity` so caller discards tentative compaction, reports the real outcome and backs off |
| `conversation_compression_publication._commit_compaction` in-place archive and rotated `publish_compression_child` | Mandatory | Default sticky; do not report successful publication |
| `conversation_compression._adopt_grown_durable_parent` preflush and `_publish_rotated_compaction` follow-up | Mandatory | Transcript append stays sticky |
| `turn_context._persist_turn_start` / `_ensure_and_persist`; `build_turn_context` flush | Mandatory | Turn-start user row stays sticky |
| `turn_tool_round.run_tool_round`; `tool_executor._flush_session_db_after_tool_progress`; `turn_stop_gates._append_interim_answer` | Mandatory | Tool/interim transcript rows stay sticky |
| `turn_final_response.finish_text_response` persist and flush; `codex_runtime._persist_projected_messages` | Mandatory | Final assistant/provider transcript stays sticky |
| `conversation_loop._close_durable_failed_turn`, `_preflight_hard_window_result`, `_provider_overflow_exhausted_result`; `session_persistence._persist_session` | Mandatory | All flushes/appends retain failure for token-count flush |
| `runtime_session_store.append_messages_batch`; `runtime_session_compression._append_compression_messages` | Mandatory | `transcript.append` and compression-scoped append stay sticky |
| `runtime_session_store.queue_token_counts`, `record_auxiliary_usage`, turn/compression lease acquire/renew/release; session metadata and history writes | Mandatory | Default sticky, no opt-in |
| `managed_worker.execute`: `flush_token_counts` before result and `store.finish`; `runtime_session_store.finish` | Mandatory | Latched failure raises before successful result/final receipt; `execution.finish` rejection also latches |

Call-site inventory includes the indirect `retry_publication` in-place archive;
it is not an optional prune. Any new worker call is mandatory unless it explicitly
uses the optional capability and has an original-transcript rollback path.

Review regression: on the previous PR head, the final assistant append logged an
`outbox_full` warning but `_persist_session` and the managed-worker flush barrier
both returned normally. RED: all ten mandatory-capacity cases failed; two optional
micro-compaction rollback cases failed, while both proactive-prune cases passed.
The fix preserves mandatory failure visibility while retaining the incident fix.

Prior art: upstream open PR #146198 addresses the unrelated on-loop refusal
poisoning boundary and deliberately retains outbox_full as sticky. No upstream
outbox-capacity fix or fork PR was found by exact-term search on 2026-11-04.

Contract tests: `tests/agent/test_worker_outbox_capacity.py`, `tests/agent/test_worker_outbox_codec.py`, `tests/agent/test_worker_outbox_io_failure.py`, `tests/agent/test_worker_prune_capacity.py`, `tests/agent/test_worker_mandatory_capacity.py`, `tests/agent/test_worker_archive_followups.py`

Verification: RED on the unchanged baseline raised `outbox_full` on both large
snapshot paths and proved the failure latched; 3 tests failed. GREEN exercises
real-owner receipt deduplication after lost ack, compressed reopen, resource-bound
refusals, malformed journals, owner lock release, disk/ack-save errors and the
actual proactive-prune fallback followed by usage/lease persistence. This is
source delivery only; live incident data was read but never changed.

## Review follow-ups

`read_journal` reports `outbox_corrupt` for malformed deflate bodies, JSON and
UTF-8 in either the plain or gzip representation, preserving the original
exception as its cause. Resource overflows stay `outbox_full`; filesystem errors
and sticky mandatory-write behavior are unchanged.

The shared `optional_archive_method` statically checks for a declared best-effort
capability before binding it. Dynamic mock children and `__getattr__` proxies do
not advertise a capability, while explicitly assigned and real store methods do.
Both optional archive paths use this helper; mandatory callers still use their
original method.

Micro-compaction distinguishes `archive_capacity` from `stale_generation`, undoes
all tentative changes, and records a capacity failure instead of crediting a
successful summary. Proactive prune reports `prune:archive_capacity`. Both reuse
the compression failure cooldown ladder (60/300/900 seconds), locally only: a
mandatory cooldown write could itself fill the bounded outbox. Capacity refusal
must not spend an LLM call again on the next turn, increment summary-poison
strikes, skip healthy material, or poison mandatory usage/lease/finalization.
Capacity streaks reset with the session and successful recovery. Growth alone
does not lift this cooldown. A stale-generation refusal remains an immediate
no-op without a capacity strike.

RED on `5ef030d5e9e36dacb5a6aa13e5711c00fe663864`: 12 failures (five codec cases,
three optional caller capacity/retry cases, and four dynamic capability cases),
with nine existing/explicit-capability cases passing. The follow-up regressions
also exercise cumulative-marker rollback, mixed failure classes and session reset.
GREEN: 24 files, 330 tests passed, 0 failed through `scripts/run_tests.sh` under
heavy-slot with isolated HOME/HERMES_HOME/HERMES_RUNTIME_DIR; worker/store,
proactive pruning, micro-compaction, owner compaction, restart safety, concurrent
fork, rotation-state and progress-timeout suites included.
