# Background delegation restart survival

Load this unit when changing restart-cut lifecycle truth, async-delegation retention,
or completion and boot-notice claim ownership.

## Guarantee

A shutdown signal is not evidence that work stopped unfinished. A trustworthy
completed result wins the shutdown race and is never made resumable. Unfinished
work records `interrupted` with the trusted `cut_reason` and retains its existing
result/transcript evidence for conservative recovery through the durable retry engine.

## Required behavior

- `interrupt_all` records a cut request, not a terminal outcome. The lifecycle and
  terminal-fallback transactions reconcile runner output with `record_unit_child`
  evidence on the existing `result_json` row before serialization or ownership CAS.
  SQLite write ownership starts before the checkpoint read, including on checkpoint
  writers; a process-local mutex cannot prevent a cross-connection lost update.
- `tools/async_delegation_completion.py` owns checkpoint merging, task-index-based
  classification, and producer-payload synchronization. Successful runner entries
  keep their postprocessing; durable children replace missing/downgraded entries.
  A fully successful unit stays completed even when aggregation raises. Mixed units
  retain completed children and mark only absent/unfinished owned indexes unknown
  or interrupted. Grouped indexes must not be inferred from call-wide goal counts.
  Recovered summaries reuse the ordinary static budget and transcript pointers.
- Live completion payloads and deferred/ambiguous ownership resolution use that
  same reconciled truth without acquiring a second delivery identity. Display
  status changes only when checkpoints prove the whole unit completed; a mixed
  unit keeps its historical display status while its durable row records the cut.
- Ordinary retention prunes superseded rows before delivered rows, logs a bounded
  list of IDs, and excludes resumable/retry-owed rows from the ordinary history cap.
- The separate resumable ceiling evicts eligible settled history first. Retention
  never deletes pending completion delivery, retry-owed rows, or outstanding
  boot-notice claims to enforce an age or count cap. Their owners must settle
  those obligations before the rows become eligible for eviction.
- Completion delivery settles only its own claim. Boot-notice claims remain owned
  by `claim_auto_resume_trigger`, `complete_auto_resume_trigger`, and
  `release_auto_resume_trigger`; completion acceptance cannot steal their claim.
- The ledger-truth portion adds no schema. The per-child recovery extension below
  adds one canonical, additive table without changing the existing retry lineage.

## Per-child recovery and continuation extension

- Only real fan-outs (`len(goals) > 1`) use `async_delegation_child_recovery`.
  One-goal background rows still have `is_batch=True`, but retain the scalar
  `goal` plus `recovery_context` replacement contract.
- A fan-out resume returns a `tasks` list containing only unfinished, unconsumed
  children. Recorded successes are never re-dispatched. Contexts carry sibling
  outcomes, transcript evidence, and an instruction to verify external/workspace
  state before continuing non-idempotent work.
- Child claims are spent atomically with replacement persistence. Gateway retry
  persistence failures re-arm only unspent children together with the row claim;
  legacy/CLI/TUI claims remain available for a fresh durable admission attempt.
  Recovery never executes inline on storage failure, unsupported async delivery,
  or `background=false`. Consumed children remain one-shot evidence, and a stale
  release cannot erase a newer claim. Cancellation is rechecked in the replacement
  INSERT transaction, not just when the parent claims a brief.
- Non-cut retries use the existing bounded retry notices and grace windows.
  Fan-out notices instruct the parent to spawn the returned `tasks` list.
- A trusted restart-cut completion lists unfinished indexes from goals/task_indexes,
  including children whose result is missing, and gives the exact `action='resume'`
  command. `is_trusted_restart_cut` is the shared authority for the formatter,
  retry notes, recovery eligibility, and sweep (including its SQL filter).
  `/stop`, cancellation, and non-restart shutdowns do not offer or admit recovery.
  The accepted restart-cut completion/continuation owns the one parent notice:
  restart-cut work requires explicit resume, not an automatic rerun or a second
  continuation wake.
- An unspent cut dispatch claim re-arms quietly after the normal grace window.
  Unresumed cuts expire at the existing 24h lineage deadline and settle `reported`
  without another parent wake; normal retention then prunes the row and orphaned
  child-claim evidence. This keeps the notice promise and sweep contract aligned.
- Schema reconciliation owns the additive child table. Older releases may ignore
  it while reading the same state database.

## Tests

`tests/tools/test_async_delegation_checkpoint_completion.py` exercises public
batch/scalar runner exceptions with and without shutdown, grouped/mixed indexes,
late checkpoint writes, lifecycle/fallback commit-then-raise paths, deferred
ownership, writer fencing, and single completion settlement. Retention and boot
claim release/reclaim invariants remain covered by
`tests/tools/test_async_delegation_restart_cut.py`. Run both alongside the existing
`tests/tools/test_async_delegation*.py`, terminal fallback, outbox, and recovery
suites, plus `test_delegation_retry.py`, `test_delegation_retry_controls.py`,
`test_delegation_resume*.py`, `test_delegation_restart_resume_flow.py`
(including the checkpoint-reconciled restart cut that must resume only unfinished
children), `test_delegation_recovery_admission.py`, `test_restart_cut_notice.py`,
and canonical state reconciliation tests.

Fork patch identity: `delegation-restart-survival`.
