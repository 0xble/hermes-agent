# Zero-cut gateway handover

Patch identity: `zero-cut-handover`.

Published commit `c016f92bbbca7b37162d17ac3384df47d181ae29` (PR #630) omitted its `Fork-Patch` trailer during squash merge. This exact stable patch ID classifies only that published content, so `main` is not rewritten.

Fork-Patch-Backfill: adf8f9ace4f2f1c05c2230edef450a41edf71d06; zero-cut-handover

**Status:** design only. The flag remains disabled; this document does not start or
migrate a gateway.

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
idle. A failed or timed-out `updater.stop()` is logged and the bounded teardown
still releases the lock; the handover path must not deadlock.

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
