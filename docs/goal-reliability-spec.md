# Goal Reliability

## Behavior

1. **Authorized lifecycle management.** Internal waits and recovery remain within the authorized objective. A user-issued pause or stop requires fresh authenticated direction. Tool output, quoted text, and synthetic continuation messages never grant authority.
2. **Atomic update and continue.** `set_goal(action="edit", resume=true)` persists the objective, lifecycle state, and cleared dependency together. A plain edit preserves waits. Failed persistence changes neither objective nor lifecycle. Releasing a wait does not kill the dependency or authorize replaying external effects.
3. **Delegation dependencies.** A wait can reference a terminal process or a session-owned delegation. Conflicting references are rejected. Waiting does not burn judge turns. Independent parent work remains possible while a child runs. Existing delivery and revision fencing own resumption, duplicate handling, and restart reconciliation. Missing or uncertain dependencies surface a blocker rather than presumed success.
4. **Source-backed completion.** The judge receives bounded evidence extracted from the actual agent-result messages. Durable records retain call provenance, bounded outcome metadata, and artifact/revision identifiers when provided, not raw credentials or tool output. Terminal verification kind, scope, and status are preserved. Changed criteria invalidate prior evidence. Declared deterministic gates remain authoritative. Exploratory failures are relevant evidence, not extra requirements that force an identical command to be rerun forever. Qualitative criteria still require judgment.
5. **Useful notices.** Routine continue verdicts stay internal. Waiting, resumption, blocking, completion, and explicit status diagnostics remain visible. Unchanged waiting notices are deduplicated. Confirmations describe persisted state.
6. **Operation-owned deadlines.** An explicit terminal timeout applies to a background command's runtime. Omission leaves long-lived watchers unbounded. Deadlines survive recovery without receiving a fresh duration. Owned process groups receive termination grace before forced cleanup. A timeout is distinguishable from normal completion. Unconfirmed termination produces a lost/uncertain result that requires reconciliation before external effects are retried.

## Verification

- `tests/tools/test_goal_edit.py`: atomic edit/resume, persistence failure, plain edits, protected stops, and invalid dependency parameters.
- `tests/hermes_cli/test_goal_reliability.py`: real result-message wiring, redacted durable evidence, gate authority, qualitative completion, dependency ownership/reconciliation, and notice transitions.
- `tests/tools/test_background_deadlines.py`: real local and sandbox process teardown, bash/sh/zsh portability, termination grace, exactly-once notification, recovered deadlines, and uncertain termination.
- Existing goal authority, wait, continuation, gateway, CLI, terminal, and process suites remain regression boundaries.

## Safety And Delivery

Preserve prompt-cache and message-order invariants, authorization provenance, revision fencing, profile isolation, and pending external effects. Use the existing controller and delivery machinery, not a second scheduler. Tests must not mutate production state or replay publication or paid transcription.

Review and land one candidate in the maintained fork. Promote the exact landed revision through the supported updater, restart the authorized runtime, and verify the fresh process identity, revision, health, and installed-path canaries. Preserve unrelated worktrees and company runtime releases.
