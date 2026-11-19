# Session admission adoption index

- **Patch identity:** `session-admission-adoption-index`
- **Contract tests:** `tests/hermes_state/test_worker_admission_index.py`, `tests/hermes_state/test_runtime_adoption.py`, `tests/hermes_state/test_runtime_admission.py`
- **Behavior:** `adopt_worker_execution` looks up its `started`/`unknown` candidate admission through a partial index, so the write transaction never walks terminal admission history. The query writes that status set as a literal, which SQLite needs in order to prove the partial-index predicate; any other status set keeps the parameterized query. Generation, owner-epoch, lineage and conflict checks are unchanged and stay inside the transaction. The index is declarative (`CREATE INDEX IF NOT EXISTS` in `SCHEMA_SQL`), so no schema-version bump and no data migration. On an existing store the first writable open builds it once, scanning `session_admissions` a single time, like the cron-request partial index before it.
- **Surfaces:** `hermes_state_common.py` (`idx_session_admissions_worker_adoption`), `hermes_state_runtime.py` (`_linked_worker_admission`).
- **Retirement:** Remove this patch when upstream bounds the adoption candidate lookup to non-terminal admissions.
- **Rollback:** Revert the commits carrying `Fork-Patch: session-admission-adoption-index`. An older release ignores the extra index.
