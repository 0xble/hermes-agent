# Zero-cut gateway handover

Patch identity: `zero-cut-handover`.

Published commit `c016f92bbbca7b37162d17ac3384df47d181ae29` (PR #630) omitted its `Fork-Patch` trailer during squash merge. This exact stable patch ID classifies only that published content, so `main` is not rewritten.

Fork-Patch-Backfill: adf8f9ace4f2f1c05c2230edef450a41edf71d06; zero-cut-handover

**Status:** opt-in source implementation. This record does not activate or migrate
a gateway. The design sections after the received-work repair are historical.

## Received Telegram Work During Handover

The active-side handover implementation is in `gateway/zero_cut_handover_drain.py`.
The original design below is historical, not an activation receipt. In particular,
cooperative polling settlement failure retains the token lock and reports failure,
rather than releasing it through the ordinary disconnect timeout path.

External drain is engaged before stopping cron reviews or polling. Keep that order:
`gateway/run.py`'s cron `can_dispatch` closure reads `_external_drain_active`.
This is best-effort early cron quiescence, not a persistent handover-owned fence:
`gateway/run_shutdown.py`'s dashboard marker watcher clears the shared flag when
no marker exists. The early flag still closes the cron gate when observed, and
`stop_reviews()` separately stops review admission. While this existing handover
owns the old Telegram transport, its live received events remain admissible through
the normal auth and session admission path. The allowance requires a registered
receiving Telegram adapter via `_transport_owner`, not a restored source, relay
event or profile-name guess. Failed-handover recovery keeps that allowance until
late queue, batch and held-redispatch owners have dispatched their received input.
It does not override a real stop. Starting a handover inside an ordinary drain is
refused, not converted to this allowance. Ordinary external-drain new-turn refusal
remains unchanged.

Stop and settle every served Telegram adapter, including secondary profile bots.
Join PTB's unfinished update queue because a dequeued update need not have entered
the concurrent processor yet. Also join existing batch and held-redispatch tasks,
including `_committed_flush_tasks` in both drain and failed-handover recovery:
text, photo and album tasks leave their pending maps at the pop, before identity
recovery or dispatch finishes. Adapter session guards count as outstanding work
even before the runner has claimed an agent slot and while it delivers a final
or starts a follow-up.
Those old-owner tasks may finish after token transfer without being mistaken for
fresh external admissions. Use the existing `_polling_teardown_started` fence while
stopping polling, without setting the delayed-delivery drop guard. Otherwise 2
stopped-updater heartbeat probes schedule recovery during a long old-owner turn.
No new ingress queue, synthetic delivery/turn identity, registry, schema, dependency
or service is introduced.

A failed popped batch is retained by the existing held-input owner. A handover with
held input reports failure, never successful idle, and does not release the token
lock. Before full token transfer, failure or cancellation restores normal old-runner
admission, preserves a concurrent dashboard marker and resumes stopped pollers only
after a successful PTB stop and proof of the original lock nonce. Held input uses
the existing redispatch path. Served adapters recover independently. A partial token
release can be reclaimed only while no other acquire owns it. An uncertain stop or
lost lock keeps the old polling fence and notifies the existing retryable-fatal
adapter recovery owner, rather than starting a competing old poller. A pending
PTB stop remains owned even after `updater.running` becomes false: its final
getUpdates cleanup must finish before normal disconnect can close its app or
release its token. The adapter publishes that shielded retirement task before
fatal notification. Both primary and secondary reconnects join the predecessor's
actual retirement task inside the existing bounded connect owner, because the
scoped lock intentionally permits same-PID reacquisition. Disconnect and connect
deadlines detach their waits, not that retirement owner. Late completion releases
the old nonce-protected lock and allows the retained reconnect queue to rebuild
and transfer held input. Other adapters without an outstanding retirement owner
keep their existing queue-before-disconnect recovery behavior. After full
transfer, cancellation leaves the old fence intact and continues waiting for its
turns to finish. It does not report a failure that would tell the control client to
boot out the promoted standby. Review admission remains stopped, as there is no
existing review-resume lifecycle hook. A failed handover remains a failed control
operation, not an in-process retry of that operation.

Held and adapter-pending events remain in memory until dispatched or the existing
shutdown spool runs. This is not a new arbitrary-process-kill durability or
exactly-once guarantee.

### Source Evidence And Regression

On frozen fork base `9c465b1d10085f3d57ce33599f58ca84096be1c2`, a real PTB
Application queue/processor plus real Telegram batching, `handle_message` and runner
admission returns `draining_maintenance` for the original message during polling
settlement. Admission parked until after token transfer takes the ordinary drain
path instead of executing. Secondary bots are not settled or counted by the old
primary-only enumeration. An exception after a batch pop also loses its held-input
ownership. These reproduce in isolated homes without a bot network or model call.
No particular lost real user request has been established.

`tests/plugins/test_received_handover.py` exercises original native message ID/text
execution once across text/photo/album batching, primary/secondary transports,
pre-admission identity recovery, both handover admission phases, cancellation and
failed popped dispatch. Cancellation before transfer checks that the old owner
resumes admission and executes the original ask once, instead of remaining in an
unserviced drain spool. Failure regressions also cover held redispatch, multiple
served bots, dashboard markers, partial release, ambiguous stop recovery and
post-transfer cancellation without reopening polling. Real PTB stop regressions
also drive production primary/secondary fatal retirement and bounded reconnect
while a cancellation-resistant final cleanup is pending. They check foreign-PID
lock refusal (identifying the pytest owner as the test gateway), no same-process
replacement consumer before late cleanup finishes, then original event identity
and one execution after normal recovery. No test contacts Telegram.
`tests/gateway/test_zero_cut_handover_verb.py` also refuses pre-existing ordinary
drains. Run through `scripts/run_tests.sh`, alongside external-drain, reconnect,
Telegram update-admission, batching and multiplex/profile-isolation suites. Network
and model execution are replaced at their boundaries, not the runner admission
guards or PTB queue/processor. Partial-release regressions use real scoped locks in
a temporary rendezvous directory. The test belongs under `tests/plugins/` because
`tests/gateway/conftest.py` replaces PTB modules with MagicMocks. The optional PTB
imports skip collection cleanly when the messaging extra is absent.

Falsifier: an existing supported route that durably replays the same maintenance-
refused native input exactly once would invalidate the loss diagnosis. A successful
outbox admission alone does not: the maintenance response is terminal admission
output without agent execution.

### Upstream Preflight (2026-10-22)

The independent proposal was recorded before tracker searches. Governing guidance
was compared at NousResearch `main` pin
`3d7e687462ca8edfa52142a4dafad12def723bd7`: extend the existing owner, keep the core
admission narrow waist, use real imports/temp homes and invariant regressions, and
retain the repository test runner. The pinned subtree instructions were read with
the fork's current instructions. Fork PM setup and CI delivery differences remain
unchanged. This repair extends the existing fork-only zero-cut owner, not a claimed
upstream zero-cut defect.

Read-only open/closed/merged issue and PR searches covered handover, zero-cut,
external drain, Telegram batch shutdown and held input. Related, not equivalent:

- [#140135](https://github.com/NousResearch/hermes-agent/pull/140135), open: journals
  inbound intent before admission awaits. Its diff does not preserve an input that
  reaches a terminal maintenance refusal instead of executing.
- [#138760](https://github.com/NousResearch/hermes-agent/pull/138760), open: cron
  worker waits for gateway readiness across a swap, not old-bot received admission.
- [#138795](https://github.com/NousResearch/hermes-agent/pull/138795), open: serial
  PTB dispatch stall/rebuild work, not the fork's handover drain gate. No code was
  borrowed from these proposals.

No equivalent solution was found within those bounded searches. Upstream guidance
supports joining existing task owners and preserving the ordered admission path.
The intentional fork divergence is the existing opt-in handover state granting its
registered old Telegram transport an admission allowance. The early shared drain
flag gates cron when observed, but the dashboard watcher can clear it as described
above. This extension does not add a persistent handover-owned cron fence.
Retire this extension with the handover owner, or when upstream provides equivalent
lossless cooperative transfer and these regressions pass without the fork patch.
Rollback is the source repair commit with `Fork-Patch: zero-cut-handover`, not a
configuration migration. Stack fit: existing Python/asyncio and adapter/runner
ownership, no dependencies.

This source repair authorizes no runtime activation. Later activation still needs
the parent-owned exact-head gate, review, authorized integration and runtime-owner
procedure. In particular, replacing a live home plugin can affect separately
running cron interpreters before gateway reload. Account for every old gateway,
cron worker and other importer before plugin deployment or storage migration.
The regression covers registered live transports, not already-parked reconnect
entries or arbitrary concurrent gateway-level adapter replacement. Those transport
owners still need separate runtime preflight, not an inference from chat idle.
Gateway chat idle or a terminal execution row is not process-exit proof. The old
owner must already run this repair for its handover to gain this behavior. Starting
a fixed successor does not repair the currently unpatched owner's drain path, so
this candidate alone is not a lossless first-activation procedure. Do not use
withdrawn `gateway.forward_only_handover` as an activation route.

## Scope

Add one opt-in switch:

```yaml
gateway:
  zero_cut_handover:
    enabled: false
```

With the switch off, behavior is unchanged: one gateway label, drain-first
shutdown, restart recovery, and the existing token lock and outbox paths. No
handover state, standby process, or second launchd slot is initialized.

With the switch on, each resolved `HERMES_HOME` has two fixed launchd slot labels.
The default home uses `ai.hermes.gateway` and `ai.hermes.gateway.b`; a named or
custom home uses the existing per-profile/per-home label convention and appends
`.b` for the second slot. Both jobs use `KeepAlive: {SuccessfulExit: false}`.
There are no per-generation UUID labels and no consumed-scope `SystemExit(0)`.

## Cooperative handover

A new immutable release starts idle slot B in standby. Standby has no poller,
cron, autonomous dispatch, or user admission. Its bounded local readiness check
only proves that adapters are built, databases are open, and the control socket
answers; it does not run a model turn or perform network delivery. If readiness
fails, B is booted out and A is untouched.

When B is ready, the updater sends `handover` over A's control socket. A then:

1. stops cron and autonomous dispatch;
2. journals raw inbound updates before handing them to the runner;
3. awaits `updater.stop()` while retaining the token lock;
4. keeps handling turns it already fetched as A-owned work; and
5. releases the token lock only after updater shutdown completes.

B acquires the lock and starts polling only after A releases it. A continues
sending its own final responses through the existing outbox and exits `0` once
idle. A failed or timed-out `updater.stop()` reports handover failure. A pending
stop remains owned, and fatal retirement joins its token-using cleanup before
disconnect or lock release. Supported gateway reconnect owners wait for that
retirement before starting a replacement consumer. Gateway-facing waits remain
bounded without treating their deadline as completed cleanup.

A follow-up for a session with a turn still running on A remains queued behind
that owner rather than starting on B. New sessions may be admitted by B after
ownership transfer. The handover is cooperative: an arbitrary hard kill can
interrupt an in-memory operation, but recovery must not replay unsafe external
side effects.

## Durable state

Only two new durable pieces are required:

- **Raw-update ingress journal:** lives in the existing per-profile outbox DB
  (`gateway/outbox.py`) and is written before an update is handed to the runner.
  Thus an update acknowledged by PTB still survives A being SIGKILLed.
- **Cross-process session owner:** records the process that owns a still-running
  session turn, so B queues follow-ups behind A. The existing
  `gateway/turn_lease.py` lease is process-local and is not sufficient.

Existing outbox delivery, immutable releases, and restart recovery remain the
recovery substrate. The journal must be idempotent by source update identity;
this PR does not claim arbitrary-crash exactly-once transport delivery from a
local receipt alone.

## Acceptance split

For a planned handover, the drill must show 0 cut turns, 0 duplicate finals,
and 0 lost messages. For SIGKILL or crash drills, the pass criteria are 0 lost
inbound messages, 0 duplicate finals, and no parking. A turn killed in memory is
reported as interrupted and recovered by today's restart-recovery path without
automatically replaying a non-idempotent turn. Telegram has no idempotency key,
so a kill after a final is accepted but before Hermes records its receipt cannot
prove both zero duplicate and zero loss; that boundary is a diagnostic, not a
planned-handover guarantee.

## Explicitly rejected

- A permanent router/worker split. That is the S4.2 design that failed spikes
  #215 and #224; each slot remains a complete native gateway.
- A coordinator database with leases and epochs. PR1 does not introduce one;
  the two durable records above are the complete new state for this sequence.
- Generation labels or UUID-labelled launchd jobs.
- Raised drain or restart timeouts.

## PR sequence

1. **Flag and isolation seam:** add the default-off flag, derive the two fixed
   slot labels from resolved `HERMES_HOME`, and correct token-lock ordering under
   the flag. Add this design document.
2. **Ingress journal:** add the outbox-backed raw-update journal and its replay /
   crash-boundary tests.
3. **Standby and transfer:** add standby boot, local readiness, the `handover`
   control command, and the cross-process session-owner record.
4. **launchd drill:** add a disposable-home launchd harness and verify ownership,
   polling order, queued follow-ups, and clean A exit.

The switch stays false until the later PRs and the launchd drill pass. The legacy
single-slot drain/restart path remains canonical until then.
