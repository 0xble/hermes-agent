# Delegation result follow-through

Child execution, result presentation, parent disposition, and final delivery are distinct.

## Nested callbacks

The live-transcript wrapper preserves the exact return from its lifecycle relay, including
handling acknowledgements and missing-result queries. Logging failures remain best-effort;
relay exceptions propagate. Display-only CLI callbacks still have no card ledger.

## Deferred results

A newly presented completion triggers at most one follow-through page in its owner's
processing turn, including inline delegation tool results. Plain turns do not scan history.
The ledger offers up to eight deferred exact attempts, oldest-offered first. The runtime
reads their retained payloads using the same immutable-owner accessors as `action=result`,
and adds at most 48,000 characters of complete results to the new context. It does not
truncate results to manufacture a presentation. Unavailable or oversized results remain
unresolved for deliberate `delegate_task(action="result", delegation_id=...)` retrieval.

Only actual retrieved attempts gain current-turn presentation authority. Their previous
deferral remains recorded, but a current disposition is required. Re-deferral for genuine
user approval or outstanding work is valid; reading is not acceptance. Existing correction
bounds, exact owner/attempt checks, and final-delivery retirement remain intact. Successor
success never auto-closes predecessors. No dependency graph, timer, expiry, or cosmetic hide
is introduced. A native review phase boundary does not trigger inline follow-through.

## Admission versus durable presentation

Push-adapter admission sets `delivery_state=admitted`, not `delivered`. Admitted events are
non-claimable while that gateway owns their volatile queue slots. The final gateway path
settles only when the exact internal receipt is recorded on a canonical user row. Queued
recursive turns carry the same metadata as direct turns. Restart checks persisted receipts
through the native compression chain; otherwise it requeues admitted events. Repeated
failed presentation consumes the existing delivery/recovery budgets and parks retained
results in `pending_recovery`, rather than repeatedly resetting the budget or expiring
unresolved payloads. Non-push API self-post retains its existing full-request contract.

This prevents future instances of the observed BS boundary: a busy parent admitted a
completion, restart discarded its pending slot, and the old admission-level delivered
state suppressed replay. It does not retroactively guess presentation for old rows.

## Scope

This source repair does not change independent completion defaults, historical card
state, runtime configuration, or provider routes. Tests use isolated homes and local
transports; they are not live Telegram/provider acceptance. Runtime promotion is a
separate parent-owned action.

## Code-enforced managed downgrade boundary

Managed source replacement requires `runtime-compatibility.json` schema 1 with both
`delegation-admitted-v1` and `managed-downgrade-floor-v1`. These are release contracts:
the runtime recovers this persisted state, and its updater/installer preserves this
same floor. Recovery alone is insufficient: an unguarded intermediate version would
permit a second incompatible downgrade. Capability declarations must be reviewed with
the implementation; they are not signatures or proof against a malicious release.

The floor is unconditional. An incompatible target is refused **even with zero admitted
rows**. This intentionally trades some otherwise-safe downgrades for a small guard that
does not discover stores, hold database locks during deployment, or stop writers.
All served profiles/stores, including undiscovered or unreadable stores and writers
admitting after preflight, are protected by never selecting an incompatible reader.
Compatible updates and compatible downgrades remain allowed. No ledger conversion,
receipt fabrication, owner relaxation, retention change, or new orchestration is used.

The Git runner checks source-changing checkout, fast-forward merge, hard reset (including
syntax rollback and orphan reset), and autostash reset. References are resolved once to
immutable commits; execution uses that exact commit. Local branch checkout preserves
its branch name. Non-fast-forward composition and automatic stash reapplication are
refused because the resulting executable tree is not the verified target. The stash
and existing source rollback reference remain available for deliberate investigation;
a rollback reference is not permission to execute an incompatible rollback. Use a
separately reviewed compatible revision rather than a live unverified merge/overlay.

ZIP replacement verifies the extracted target and current rollback tree before staging
or swapping. Failure restores the already-compatible source; a failed fleet activation
must be retried with compatible code, never used as clearance to downgrade. This does
not make the existing per-entry ZIP swap a whole-process atomic activation mechanism,
nor guarantee arbitrary mixed-version runtime availability. It prevents incompatible
reader selection during partial activation and subsequent managed recovery.

The current Bash and PowerShell bootstrap installers refuse direct Git/ZIP replacement
of an existing installation carrying the capability manifest and point to the managed
updater. Fresh installs retain their normal bootstrap route. Missing/malformed target
capability evidence or failed Git reads fail closed with an actionable error.

Scope begins when this updater/installer code is installed. A previously launched legacy
updater, manual Git, manual file copying, old downloaded installers, OS/Nix package
management, and malicious or incorrectly declared release contents cannot be policed
by replaceable Python code. First promotion from a legacy runtime must retain the
supported exact-revision procedure: it preserves the rollback reference and does not
perform an automatic post-startup source downgrade. No live activation occurs in tests.
