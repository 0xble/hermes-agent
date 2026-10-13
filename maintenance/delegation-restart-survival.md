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

`tests/tools/test_async_delegation_restart_cut.py` and the existing
`tests/tools/test_async_delegation*.py` suites.

Fork patch identity: `delegation-restart-survival`.
