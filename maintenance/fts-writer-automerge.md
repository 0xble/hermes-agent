# FTS writer-side automatic merge

- **Status:** Candidate.
- **Problem:** SQLite FTS5 automatic segment merges run from the `messages` insert
  triggers. On the live 100 GB macOS `state.db`, two post-activation one-row
  transcript writes held the shared writer for 3.7 s and 6.0 s, with 3.684 s
  and 5.945 s respectively attributed to `insert_sql_fts` and negligible CPU
  time. This delays unrelated response-critical writes.
- **Fix:** Disable FTS5 automatic merges for the shipped word and trigram indexes
  at schema ensure time. Hermes already runs bounded explicit `merge` commands
  from its maintenance connection outside the application writer guard, so
  index searchability and eventual segment maintenance remain owned by that
  path.
- **Verification:** `test_fts_automerge_is_disabled_for_writer_transactions`
  checks both live indexes in a fresh store. The existing bounded-merge test
  continues to require explicit `usermerge` and `merge` commands and forbids
  `optimize`.
- **Contract tests:** `tests/hermes_state/test_fts_automerge.py`.
- **Rollback:** Revert this source and test change. No schema migration or
  state rewrite is needed. Existing indexes keep their current data.
- **Retirement:** Remove the private setting when an upstream release prevents
  automatic FTS5 merge work from running in the response-critical insert path
  while retaining bounded maintenance merges.
