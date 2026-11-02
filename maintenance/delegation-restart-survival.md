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
  `goal` plus `recovery_context` replacement contract. On the legacy TUI/API/CLI
  path, one unfinished child result does not turn the unit into a fan-out or a
  partial-success refusal; a successfully completed child remains ineligible.
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
- A lifecycle cut lists unfinished indexes from goals/task_indexes, including
  children whose result is missing. It gives the exact `action='resume'` command
  only while the same durable scalar/fan-out/retry admission gates would allow it;
  missing rows, completed work, spent claims, user stops, and exhausted budgets
  cannot advertise recovery, including through a previously serialized retry note.
  That admission is evaluated off the event loop where the event is built, claimed,
  or replayed (`stamp_resume_availability` -> `evt["resume_available"]`); a ledger
  read failure stamps False. Notification formatters read only that stamp and never
  open state.db, so a locked ledger cannot raise inside a drain (losing unrelated,
  already-popped completions) or block a gateway loop. A notice carries exactly one
  instruction: a rendered retry note (resume, or "Hermes will retry; do not
  re-dispatch") replaces the body's own resume line.
  An unscoped stop (`interrupt_all`) cancels owed retries everywhere, but revokes a
  claimed legacy recovery (`retry_state='none'`) only for the stopping session's
  selectors; CLI `/stop` passes its own `parent_session_id`.
  CLI exits are labeled as CLI exits, not gateway shutdowns.
  `is_trusted_restart_cut` remains useful for canonical wording and the one-notice
  continuation path, but recovery authority is based on positive evidence of a user
  stop, not on a restart token. Legacy phase-specific gateway-shutdown reasons and
  unplanned shutdowns therefore remain recoverable. `/stop`, `stop_command`,
  cancellation, and session end do not offer or admit recovery; a sweep that revokes
  one records `retry_reason='stopped by user'` and settles any already-owed terminal
  report once. The accepted restart-cut completion/continuation owns the one parent
  notice and requires explicit resume, not an automatic rerun or a second wake.
- An unspent cut dispatch claim re-arms quietly after the normal grace window.
  Unresumed cuts expire at the existing 24h lineage deadline and settle `reported`
  without another parent wake; normal retention then prunes the row and orphaned
  child-claim evidence. This keeps the notice promise and sweep contract aligned.
- Schema reconciliation owns the additive child table. Older releases may ignore
  it while reading the same state database.

## Tests

Contract tests: `tests/tools/test_async_delegation_checkpoint_completion.py`,
  `tests/tools/test_async_delegation_restart_cut.py`

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
`test_delegation_scalar_cut_recovery.py` (TUI/API/CLI notice-to-scalar-resume,
replacement admission, completed-child and spent-claim refusal), and canonical
state reconciliation tests.

Fork patch identity: `delegation-restart-survival`.
