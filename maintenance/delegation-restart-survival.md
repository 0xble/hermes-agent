# Background delegation restart survival

Load this unit when changing restart-cut lifecycle truth, async-delegation retention,
per-child recovery, or parent-facing continuation notices.

## Guarantee

A gateway restart does not turn a live background unit into a completed or failed
history row. The durable row records `interrupted` with the trusted `cut_reason`,
keeps restart-cut rows outside the ordinary history cap, and retains enough
per-child evidence for a parent to resume only unfinished work.

## Required behavior

- `interrupt_all` marks a running unit `interrupted` before its aggregate result can
  report `completed` or `error`; the event carries `cut_reason`.
- Ordinary retention prunes superseded rows before delivered rows, logs each prune
  with a bounded list of IDs, and never counts resumable/retry-owed rows against
  the ordinary cap. A separate hard ceiling prevents unbounded protected history.
- Batch recovery claims each unfinished child once in `async_delegation_child_recovery`.
  Completed children are never included in the replacement `tasks` payload. Every
  child context includes recorded sibling results and any transcript evidence.
- A cut completion renders one fixed continuation block with the cut time, the
  unfinished child indexes, and the exact `delegate_task(action='resume', ...)`
  instruction. Delivery settlement marks the boot/continuation claim delivered.
- The schema change is additive and lives in the canonical reconciliation path;
  older releases may read the same `state.db` while ignoring the new table.

## Tests

`tests/tools/test_async_delegation_restart_cut.py` and
`tests/tools/test_delegation_resume_children.py`.

Fork patch identity: `delegation-restart-survival`.
