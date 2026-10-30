# Heavy-Slot Ticket Handoff

Fork patch identity: `heavy-slot-ticket-handoff`.

## Required Behavior

A foreground heavy terminal command still waiting for admission must keep its
original live shell and helper ticket when it becomes a tracked background
command. The existing 10–120-second foreground admission budget bounds foreground
ownership, not helper admission. The local wait loop uses the existing registry
adoption path at that budget. The helper keeps waiting without
`HEAVY_SLOT_MAX_WAIT`, so there is no exit-75 resubmission and compound-shell
side effects are not replayed. The result explains that the command remains
queued with the same shell and ticket and will notify on completion. It does not
claim a user message caused an admission-budget handoff.

Already-admitted work retains its normal foreground deadline. Ordinary commands
and non-local backends retain their existing behavior. Gate commands follow the
existing heavy-slot path without git-guard rerouting or ownership checks.
Explicit helper prefixes and options are unchanged. The admission trampoline
uses the helper's existing exact-head label form to retain gate classification
when its original argv is hidden. No helper protocol, slot count or FIFO order
is changed.

## Provenance And Upstream Alignment

The earlier exit-75 retry was added in `cfdd1856916feb4073efa2fb8b4e974f95978b62`
to avoid requiring a model retry, but recreated the ticket and could not replay
compound shells safely. This patch replaces that path rather than adding another
scheduler or queue. Root upstream guidance at
`fcc26fbd9f1641755e49793d7ed608f3b7214c3d` favors extending the existing owner and
behavior-contract tests. Local heavy-slot admission remains fork-only host policy,
not a proposed upstream capability.

## Verification

Run through the checkout's canonical test runner with
`HERMES_TEST_WORKERS=1 HERMES_TEST_FILE_RETRIES=0 HEAVY_SLOT_HELD=1`:

- `tests/tools/test_terminal_heavy_slot_handoff.py`
- `tests/tools/test_terminal_tool_heavy_slot.py`
- `tests/tools/test_terminal_foreground_timeout_cap.py`
- `tests/tools/test_base_environment.py`
- `tests/tools/test_local_interrupt_cleanup.py`
- `tests/tools/test_terminal_bounded_execute.py`

The handoff regression uses a stub helper blocked on a private FIFO. It verifies
1 ticket, original-shell side effects exactly once, live registry adoption,
accurate handoff text and successful completion after explicit admission. It
covers ordinary heavy work and an exact-head `./bin/ci gate` command without
running a real gate. Compare generated argv with the installed helper's
`is_gate` classifier against the base dispatcher, including shell and env
prefixes, short/invalid SHAs and `CI_EXPECTED_SHA`. No live queue is involved.
Also run `scripts/check-windows-footguns.py --all` and `git diff --check`.

## Retirement And Rollback

Retire when upstream or the host no longer requires terminal admission handoff.
Revert the commit carrying `Fork-Patch: heavy-slot-ticket-handoff`.
No persistent-state migration or live queue changes are involved.
