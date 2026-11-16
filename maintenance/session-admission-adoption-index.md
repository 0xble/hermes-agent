# Session admission adoption index

Patch identity: `session-admission-adoption-index`.

## Contract tests

`env -u HERMES_HOME /Users/brianle/Repos/hermes-agent/.worktrees/feat-request-usage-ledger/.venv/bin/python -m pytest -q -p no:cacheprovider -o addopts='' tests/hermes_state/test_worker_admission_index.py tests/hermes_state/test_runtime_adoption.py tests/hermes_state/test_runtime_admission.py`

## Behavior

`adopt_worker_execution` keeps its generation, owner-epoch, lineage, and conflict checks inside the same write transaction. Its `started`/`unknown` candidate query uses the literal status predicate required for SQLite to use the partial index, so terminal admission history is not scanned as adoption candidates. Other status sets retain the prior parameterized query behavior.

## Surfaces

`hermes_state_common.py` declares `idx_session_admissions_worker_adoption` on `(generation, owner_epoch, status, seq)` with `WHERE status IN ('started', 'unknown')`; `hermes_state_runtime.py` renders the matching candidate predicate; `hermes_state_schema.py` applies the declarative schema on open; `tests/hermes_state/test_worker_admission_index.py` verifies the plan and adoption behavior.

## Retirement

Remove this fork patch when upstream carries an equivalent partial index and candidate-query predicate, with the same adoption behavior and plan regression.

## Rollback

Revert the commit carrying `Fork-Patch: session-admission-adoption-index`.
