# Background delegation restart survival

Load this unit when changing restart-cut lifecycle truth, async-delegation retention,
or completion and boot-notice claim ownership.

## Guarantee

A shutdown signal is not evidence that work stopped unfinished. A trustworthy
completed result wins the shutdown race and is never made resumable. Unfinished
work records `interrupted` with the trusted `cut_reason` and retains its existing
result/transcript evidence for conservative recovery through the durable retry engine.

## Required behavior

- `interrupt_all` records a cut request, not a terminal outcome. Finalization marks
  a non-successful scalar result or a batch with interrupted/missing children
  `interrupted`; successful runners remain completed, including during CLI shutdown.
- Ordinary retention prunes superseded rows before delivered rows, logs a bounded
  list of IDs, and excludes resumable/retry-owed rows from the ordinary history cap.
- The separate resumable ceiling evicts eligible settled history first. It must
  never delete pending completion delivery or retry-owed rows to enforce the cap;
  those obligations are settled by the existing bounded delivery/retry engine.
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
- Child claims are spent atomically with replacement persistence. Failed persists
  and expired unspent dispatch claims re-arm only unspent children together with
  the row claim. Consumed children remain one-shot evidence, and a stale release
  cannot erase a newer claim.
- Non-cut retries use the existing bounded retry notices and grace windows.
  Fan-out notices instruct the parent to spawn the returned `tasks` list.
- A cut completion lists unfinished indexes from goals/task_indexes, including
  children whose result is missing, and gives the exact `action='resume'` command.
  The accepted cut completion/continuation owns the one parent notice: cut work
  requires explicit resume, not an automatic rerun or a second continuation wake.
- An unspent cut dispatch claim re-arms quietly after the normal grace window.
  Unresumed cuts expire at the existing 24h lineage deadline and settle `reported`
  without another parent wake; normal retention then prunes the row and orphaned
  child-claim evidence. This keeps the notice promise and sweep contract aligned.
- Schema reconciliation owns the additive child table. Older releases may ignore
  it while reading the same state database.

## Tests

`tests/tools/test_async_delegation_restart_cut.py`, the existing
`tests/tools/test_async_delegation*.py`, `test_delegation_retry.py`,
`test_delegation_retry_controls.py`, `test_delegation_resume*.py`,
`test_delegation_restart_resume_flow.py`, `test_restart_cut_notice.py`,
and canonical state reconciliation tests.

Fork patch identity: `delegation-restart-survival`.
