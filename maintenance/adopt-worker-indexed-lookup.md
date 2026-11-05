# Indexed worker-admission lookup

Fork patch identity: `adopt-worker-indexed-lookup`.

## Contract

`adopt_worker_execution` must not materialize historical admissions while holding
its writer transaction merely to find the active worker's linked admission.
Use the existing all-status `session_admissions_pending` covering index to build
candidate primary keys, then check generation, epoch and direct-target/JSON
lineage against those rows. The index still scans: status is not its leading
column. This patch removes the wide table scan, not all history-dependent work.

Keep both assignment revalidations, retirement visibility, secret and epoch
checks, conflict cardinality and conditional-update row-count checks inside the
same transaction. Assignment validation needs only `runtime_generation` from
`sessions`; its returned worker row still retains the public execution fields.

## Provenance and upstream disposition

- Fork base: `0fd2eeeefd0dfb0e2d9fbb63bee0aecfca841835`, which includes the
  callback timing diagnostics from fork PR #1042.
- Upstream `NousResearch/hermes-agent` main inspected at
  `6602031910d06715d8811b7a9498300d6524dd29` still contains the same
  `_linked_worker_admission` scan and wide assignment-session lookup.
- Related, not adopted source: [upstream #141125](https://github.com/NousResearch/hermes-agent/pull/141125)
  narrows other admission-history reads using existing indexes. It does not
  replace this helper. This change is upstreamable; this delivery targets only
  the fork, with no new upstream PR or schema/index change.
- The independent proposal was the candidate-key/PK query with unchanged
  predicates and transactional CAS, plus a narrow generation projection through
  the existing session validator. Current contribution guidance and prior-art
  checks did not require a different owner or mechanism.

## Maintenance decision

When upstream changes worker adoption, compare its full conflict, lineage,
epoch, generation and retirement behavior, not only its SQL shape. Preserve the
within-transaction checks during any port. An upstream query that scopes
candidates only by target session loses active ancestor-lineage matches.

Contract tests: `tests/hermes_state/test_adoption_indexed_lookup.py`,
`tests/hermes_state/test_runtime_adoption.py`,
`tests/hermes_state/test_runtime_worker.py`.

The indexed-lookup test checks SQLite's actual execution plan; the session-read
test traces the actual adoption transaction. The seeded equivalence test covers
1,050 query cases, including pending rows, stale generations/epochs and
conflicting matches. Concurrent adoption/resolution and a refused admission CAS
protect atomicity. Use the broader `tests/hermes_state` suite for shared callers.

Retire this divergence when a released upstream implements the complete contract
and the planner, equivalence and atomicity tests pass against its adopted code.
Rollback is code-only; no database migration, new index or runtime activation is
part of this patch.
