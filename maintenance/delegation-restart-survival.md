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
- This unit adds no schema or child recovery table. Per-child resume payloads and
  fixed parent continuation notices belong to the follow-on recovery change.

## Tests

`tests/tools/test_async_delegation_checkpoint_completion.py` exercises public
batch/scalar runner exceptions with and without shutdown, grouped/mixed indexes,
late checkpoint writes, lifecycle/fallback commit-then-raise paths, deferred
ownership, writer fencing, and single completion settlement. Retention and boot
claim release/reclaim invariants remain covered by
`tests/tools/test_async_delegation_restart_cut.py`. Run both alongside the existing
async delegation, terminal fallback, outbox, and recovery suites.

Fork patch identity: `delegation-restart-survival`.
