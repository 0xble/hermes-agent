# Zero-Cut Gateway Handover

Patch identity: `zero-cut-handover`.

Published commit `c016f92bbbca7b37162d17ac3384df47d181ae29` (PR #630) omitted its `Fork-Patch` trailer during squash merge. This exact stable patch ID classifies only that published content, so `main` is not rewritten.

Fork-Patch-Backfill: adf8f9ace4f2f1c05c2230edef450a41edf71d06; zero-cut-handover

## Scope And Adoption

The handover implementation is opt-in and defaults to disabled:

```yaml
gateway:
  zero_cut_handover:
    enabled: false
```

This unit documents the source contract, not evidence of activation or native
acceptance. Do not enable it merely because isolated tests pass. Read
[runtime ownership](runtime-ownership.md) and
[immutable releases](seamless-restart-s2.md) before any authorized activation.
The legacy single-slot drain/restart path remains available when the switch is
off. Explicit stop and crash recovery are not planned handovers.

With the switch on, each resolved `HERMES_HOME` has 2 fixed launchd slot labels.
The default home uses `ai.hermes.gateway` and `ai.hermes.gateway.b`. A named or
custom home uses the existing home-derived label convention and appends `.b`
for the second slot. Both jobs use `KeepAlive: {SuccessfulExit: false}`.
Slots alternate roles, so the label ending in `.b` is not always the standby.
There are no per-generation labels or additional service dependencies.

## Planned Lifecycle Contract

`hermes_cli.gateway_handover.planned_handover` is the shared opt-in boundary for
normal immutable-update plist reload, launchd restart, and the gateway's in-band
planned restart. Only a disabled flag permits the legacy restart fallback.
An enabled handover that refuses or fails must not fall through to a destructive
restart. The update fleet path must not restart either slot a second time after
promotion or a serving-release acknowledgement.

The idle slot is pinned to the selected immutable release before bootstrap.
Standby defers adapter construction and shared-store activation until promotion.
Its local readiness is not proof of polling, model inference, or external
delivery. The orchestration arms a successor-owned promotion watcher before
asking the predecessor to hand over, so the updater is not the sole owner of
the continuation.

The predecessor stops cron and new autonomous dispatch, settles already-fetched
Telegram updates, awaits poller shutdown while retaining its token lock, then
releases that lock. The successor connects its deferred adapters after release.
A failed adapter connection must not report successful promotion or acknowledge
a serving release. A live but unanswered predecessor is uncertainty, not proof
that it exited and not authority to steal a lock or kill it.

The predecessor is not placed into ordinary shutdown drain while original work
continues. Its retirement check includes active turns, local follow-ups,
commands, delegation completion obligations, approvals, completion-event
routing, batches, retained finite tasks, and pending delivery. Counts come from
the existing registries and ledgers. Failure to read an owner prevents retirement.
New autonomous work stays suppressed on the predecessor, while existing finite
work and its delivery may finish without a retirement deadline. This does not
raise a shutdown or restart drain timeout.

Retirement does not mutate ambiguous sends. A cancelled send can leave a
`pending` or `attempting` ledger row for existing dead-owner recovery. Such rows
hold retirement only within the existing `ATTEMPT_ORDER_HOLD_SECONDS` window
since their last progress, not their creation. Actual tracked sends and adapter
sessions remain separate owners even after that window. Failed rows parked
outside the runtime retry budget likewise remain intact for recovery.

A session whose turn is still owned by the predecessor queues normal follow-ups
on the successor. `/approve`, `/deny`, and `/stop` use the owning session marker
to reach the original runner instead of joining that blocked FIFO. Telegram
approval, slash-confirmation, and clarification callbacks use the original
adapter's callback state and authorization scope. A submitted control with a
lost acknowledgement is not replayed. This favors avoiding duplicate effects
over pretending the resolution is known.

The successor does not adopt command pipes or delegation completions still
owned by a live predecessor. Existing checkpoint records retain the registry
owner, and completion replay excludes live owners. After the predecessor exits,
the successor may claim its host role and recover durable outbox work through
the existing recovery paths. No new coordinator database or turn-replay scheme
is introduced.

File-backed persistent and restart-surviving commands transfer through the
existing checkpoint. Late recovery arms their completion watchers immediately,
using the same detached descriptor drain as startup and post-turn wiring. A
foreground turn is not required. Non-file-backed persistent commands, including
PTY and sandbox pipe-backed jobs, cannot transfer durable I/O and still hold
retirement. They are not killed or silently excluded to force a handover.

Normal update/restart may return once the successor is promoted, without waiting
for predecessor retirement. This avoids deadlocking an updater that is itself
an old-owned command. The explicit handover CLI waits for retirement. While
both slots are occupied, another handover refuses without killing either slot
or creating a third. An operator must distinguish transfer completion from
predecessor exit when reading receipts or investigating occupied slots.

## Release Identity And Recovery

Keep both the predecessor's immutable code and its environment available while
it owns work, including late imports. Garbage collection must respect live
slot pins. A changed `current` pointer alone does not identify the serving
process.

The successor owns promotion and attempts release acknowledgement after it
reports non-standby status. A transaction acknowledgement must identify the
candidate code SHA, the actual serving slot's launchd label and supervised
process, and the pinned release root. The fleet reader must validate that live
identity before crediting the acknowledgement. Legacy acknowledgements without
a slot label retain their original label interpretation.

An updater disappearing after handover acceptance, release, or promotion must
not strand successor promotion. Socket silence does not authorize rollback of
a possibly serving slot. Failed or ambiguous transfer leaves the processes
available for inspection rather than claiming a successful restart.

## Regression And Native Evidence

The isolated regression boundary is `scripts/run_tests.sh` with these focused
files:

- `tests/gateway/test_planned_lifecycle_continuity.py`: actual update/restart
  entry points, registry and delivery retirement checks, real control-server
  approval resolution, occupied-slot refusal, and successor-owned continuation.
- `tests/gateway/test_zero_cut_owner_controls.py`: text controls bypass an
  already-queued follow-up, and inline callbacks preserve original approval
  state and authorization.
- `tests/hermes_cli/test_gateway_handover.py`,
  `tests/gateway/test_zero_cut_handover_verb.py`,
  `tests/gateway/test_zero_cut_standby_promote.py`, and
  `tests/gateway/test_zero_cut_busy_routing.py`: orchestration and ownership
  regressions.

Release transaction, fleet restart, delegation, process-checkpoint, and delivery
suites remain required affected coverage. Test output and native receipts, not
this document, establish which checks passed for an exact candidate. The
installed request-restart bridge test is opt-in and may be skipped in a source
checkout. It is not evidence about every external plugin installation.

`scripts/zero_cut_drills.py --drills native` runs ordinary immutable updates and
marker-bearing in-band restarts. Its separate acceptance oracle never consumes
legacy direct-handover/crash `XFAIL` results. Fakes test the observer contract,
not native continuity. Do not reuse its fixed native home, bot or topic IDs
without reconciling their current owner and reservation first.

### Native Acceptance Invocation

Only the top-level operator may execute the native route, after exact-candidate
qualification and reservation release. No install, profile creation or service
setup occurs automatically. Prepare the isolated home with 2 slots, 1 active
native launchd owner, immutable `current`, and 4 distinct bot topic IDs named
`work`, `approval`, `control` and `admission`. The Telegram observer must expose
sender IDs, raw text and message IDs. English queue receipts default to
`Queued`, with `queue_receipt` and `restart_receipt` overrides for the actual
served locale. Qualify a harmless real approval-triggering command template
containing `{worker}`. The harness will substitute the finite effect helper,
then resolve that native pending request through `/approve` after promotion.
No approval bypass or fabricated approval state is accepted.

The parent-authored JSON plan contains absolute `source`, a nonempty
`reservation`, `topics`, `approval_command`, and exactly 3 ordered route
scenarios in `passes`. These scenarios form 1 complete suite invocation, not
3 complete suite passes. Every scenario has `route`, literal full `before` and `after` SHAs. `update` also
has the real ordinary `branch`, must advance to a nonempty qualified descendant,
and must begin at the exact clean source checkout. Restarts preserve their
release and explicitly select `request_restart` (the installed external tool)
or `slash_restart` (the built-in command). Adjacent identities must agree.
The sequence must cover ordinary update, external `request_restart`, and
built-in `/restart` once each. Neither restart route substitutes for the other. Never
manufacture revisions, empty commits or another slot to satisfy this plan.
The updater's read-only fleet inventory must contain only the reserved home.
A foreign fleet, stale source, occupied slot or absent route is a non-pass.

```sh
ZERO_CUT_DRILLS_LIVE=1 ZERO_CUT_DRILLS_NATIVE_PARENT=1 \
  .venv/bin/python scripts/zero_cut_drills.py --drills native \
  --home "$RESERVED_HOME" --peer "$VERIFIED_BOT_PEER" \
  --native-plan "$QUALIFIED_PLAN" --native-output "$FRESH_OUTPUT"
```

The harness sends finite mixed-work instructions, but credits only observed
native execution: a living 150-second foreground tool with its pending durable
tool call, a registry-owned background command, running durable delegation,
pending approval, active goal/loop and scheduled cron. `/queue` must be admitted
while the original tool is still live. At promotion the original long tool,
background command and delegation must still be running. New-message work must
run under the successor while predecessor work remains live. The old owner
must preserve its release until effects, results and delivery finish, then
retire. Worker receipts are append-only. Repetition is detected, not hidden.

Acceptance requires 3 consecutive successful complete native suite invocations,
each covering all 3 routes with fresh evidence. An incomplete or failed suite
breaks the consecutive-pass sequence. The 3 route scenarios inside 1 invocation
do not satisfy this gate. Every workload requires exactly 1
start/finish effect pair, durable tool-call/result pair and Telegram/DB final
per workload. Background completion uses the native process-result receipt,
not its immediate launch result. Loop ticks, goal settlement, cron execution
and delivery, delegation delivery, final-delivery obligations, restart markers
and update correlation receipts are checked against their native stores.
The normal final-delivery ledger's `attempts=0` means no recovery/retry claim,
not no external send. Canonical transcript rows live in `state.db`, with JSONL
used only as fallback. Artifacts contain effect receipts, per-check outcomes,
process/code identities and hashed DB-row locators, never credentials or a
profile DB copy.

An ambiguous send/effect is never retried. An incomplete witness, model/provider
failure or refused precondition is an explicit non-pass. A partial run retains
`operation.json`, latest evidence and the last check set. Reconcile the existing
Telegram messages, finite jobs and updater PID before any further action.
Choose a fresh output directory only after reconciliation, not to replay an
unknown effect. Native proof does not follow from the focused fake-driven
pytest suite or from a legacy crash diagnostic exit code.

Before activation, independently reviewed exact-candidate evidence must exercise
normal immutable update and in-band planned restart on native launchd with mixed
original work: an active turn, delegation, background command, approval plus
follow-up, and pending final delivery. Show new work starting on the successor,
no premature predecessor retirement, truthful serving-release acknowledgement,
release retention, occupied-slot refusal, and updater death at each transfer
barrier. No native, live, or sustained-window acceptance follows from mocked
launchd effects or isolated control sockets.

## Crash Boundary And Retirement

Planned-handover acceptance requires 0 cut turns, 0 duplicate finals, and 0 lost
messages. A hard kill can interrupt in-memory work. Recovery must report that
interruption without automatically replaying non-idempotent turns. Telegram
provides no idempotency key for final sends, so a kill after acceptance by the
platform but before recording the receipt cannot prove both zero duplication
and zero loss. Keep that ambiguity diagnostic, not a planned-transfer pass.

The polling and control-transfer path described here is Telegram-focused.
Do not infer equivalent native acceptance for every other adapter, Linux
supervisor, Windows service, multiplexed profile combination, or external
request-restart plugin from these regressions.

Retire this fork divergence only when the adopted upstream lifecycle preserves
these ownership and release-identity invariants through the same behavioral
entry points and native evidence. Do not retire it because an upstream flag or
similarly named handover command exists.
