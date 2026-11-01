# Cron restart survival on macOS

Patch identities: `cron-macos-detached`, `cron-restart-survival`, `cron-detached-claim-release`.

## Contract

Under a launchd-managed macOS gateway, hand each cron execution to the existing external worker in a new session, rather than running it in the gateway process. The worker adopts the durable execution claim, owns its (PID, process-start fingerprint) liveness, completes the ledger, and queues delivery for the replacement gateway. Honor `cron.require_restart_safe_scope` without requiring systemd on macOS. Foreground/desktop invocations and Linux systemd dispatch retain their current behavior. A worker that has recorded an inactivity timeout but still has an abandoned non-daemon executor thread must retain its hard-wall watchdog until the process exits; a terminal delivery receipt from normal send, restart recovery, or wait timeout must project onto its execution row without replaying an uncertain send.

The gateway's in-process dedupe claim is owned by the detached worker waiter until the worker process actually exits. A terminal execution row is not sufficient proof that teardown and delivery have stopped: the waiter must keep blocking a concurrent fire while the PID is live, then release only the owner-fenced claim on process exit. The shared release path fences every ordinary waiter/pool cleanup while that worker claim is live; only the owner-authorized reaper or forced-release path may override it. Forced-release and shutdown-drain paths remain authoritative if the waiter itself is lost.

This is a core scheduler/dispatch invariant; a plugin or skill cannot atomically own cron's claim, worker handoff and recovery. Revert this unit's dispatch selection and regression when an upstream release proves the same launchd restart-survival contract. The Linux transient-scope worker and delivery queue are upstream-owned infrastructure reused here.

Canonical admission does not move inference back into the gateway. A durable
`owner_kind=detached` assignment selects the existing external executor, including
when that executor subsequently dies. The gateway only observes it, never falls
back to a second execution. The external PID registers its already-started cron
admission through the birth-fenced worker protocol and uses `RuntimeSessionStore`
for canonical persistence. It never opens the owner's `state.db`. Owner loss
pauses persistence until authenticated epoch adoption, without replaying inference
or tools. Explicit cron cancellation uses the admission's durable cancellation
marker. Gateway shutdown is not that cancellation.

Worker owner discovery and authentication retry pre-send lifecycle failures for
up to 90 seconds per RPC call, rediscovering the endpoint and minting a fresh ticket after
owner replacement. Discovery, identify, ticket and WebSocket-open calls use the
remaining connection budget. The event-loop synchronous-RPC guard still fails
before discovery or waiting. Permission, protocol and self-RPC refusals remain
terminal. The existing closed-on-entry send retry stays limited to 1. A chained
send exception may follow a partial or complete socket write, so it is now
outcome-unknown, just like a successful send followed by a lost response. Neither
is automatically resent by WorkerRPC. The connection-reuse regression covers
both a lost response and a write exception after the owner receives the frame.

RuntimeSessionStore journals the exact operation before sending. An explicit
`stale_epoch` refusal permits authenticated `worker.adopt`, then retry of that
same pending sequence and payload. Owner receipt identity remains
`(execution_id, sequence, payload_digest)`, with epoch and generation fencing.
Non-adoptable managed workers and retired children remain refused. Exhausted
connection patience reports `owner_restart`, not disk-full/lock guidance, and
leaves pending writes in the existing journal for explicit retry or verified
adoption. This does not claim that a queued operation is canonically committed.
The detached executor retains its existing pause-until-adoption contract across
multiple bounded connection windows, including exhaustion while adopting. Its
outer reconnect wrapper handles escaped ConnectionClosed and receipt timeouts by
authenticating `worker.adopt`, then replaying only the identical journal entry.
The owner returns the stored receipt for a matching execution, sequence and
payload digest, so even a committed write with a lost reply produces only 1
canonical row and 1 receipt. This does not relax WorkerRPC's inner no-resend guard
or replay inference or tools. Discovery resets and broken pipes retain the
interrupted-control-response cause instead of becoming invalid-control-peer
refusals. Unclassified inaccessible lock state, invalid runtime state and stale
foreign locks remain retriable while replacement settles. Explicit protocol
mismatches, malformed peers, authorization failures and assignment refusals remain
terminal.
Regression coverage is `tests/agent/test_worker_owner_restart.py`, alongside the
connection-reuse, outbox-adoption and persistence-explainer suites. Detached
multi-window and real-owner exactly-once receipt replay coverage is
`tests/cron/test_detached_owner_restart.py`, including lost replies, double
closed-on-entry, chained writes and receipt timeouts. Discovery transport,
transient lock/state and terminal protocol/peer coverage is
`tests/agent/test_worker_discovery_recovery.py`.

The 2026-10-30 detached `maintain-filesystems` execution failed while its owner
was exiting: SIGTERM at 2:21:55 AM PT, zero-second drain timeout at 2:21:56 AM PT,
live writers retained at 2:22:43 AM PT, watchdog at 2:22:53 AM PT, and
`incomplete_control_response` from append/finalize at 2:22:54 AM PT. A successor
started at 2:22:57 AM PT. The control-identify EOF preceded the persistence send,
so waiting for rediscovery is safe. The old classifier instead selected the
unknown disk/lock explanation. Upstream main
`92213f976785c3582502d84495cb4e5aad5833c8` and related WorkerRPC PRs were checked,
including open [#140128](https://github.com/NousResearch/hermes-agent/pull/140128),
which concerns loop-thread poisoning, not owner-transition patience. Retire this
extension when a released upstream satisfies the same bounded pre-send retry,
authenticated adoption, pending-journal and truthful-error contracts.

Ambiguous canonical submission is observed over fresh gateway links with the original
job/request identity, never by submitting a new fire. Known admissions are reread by their
exact receipt. Four bounded fresh-link reads wait through a delayed commit; exhausted
patience retains the admission journal and an unverified pause. One `missing` lookup is a
snapshot, not proof of non-admission. To avoid an indefinitely paused never-sent fire,
reconciliation settles a missing submit as failed only after
`MISSING_SUBMIT_GRACE_SECONDS = 600` and `MISSING_SUBMIT_READS = 2` consecutive successful
missing owner reads. The journal records `submit_started_at` before sending (legacy journals
pin their original mtime before the first recovery write); non-missing observations and
failed/refused reads reset the streak. A saved admission receipt or worker result is never
aged into a never-committed failure. The failure records the exact grace/count fence, retires
the journal and lifts only the pause it caused, allowing the next scheduled occurrence.

This is the bounded-age fallback, not a hard epoch fence: existing journals do not bind a
submit to the owner's epoch, and observing a current epoch separately cannot prove which
owner received that frame. `cron.submit` is idempotent under the original job/request id;
the owner's shielded request table retains a received submit even when its socket leaves.
A read can overtake that task in the admission queue, so repeated missing reads alone are
not final. Ten minutes is twenty times the 30-second answer deadline and fifteen times
the combined 20-second SQLite write budgets for session creation and admission, leaving
ample time for ordered-admission queueing. Admission does not await model/tool execution.
A pathological owner stalled beyond ten minutes can still commit a late shielded frame:
this bound is an explicit operational liveness tradeoff, never a cancellation guarantee,
and no automatic resubmission of the uncertain occurrence is made.

A verified terminal result also lifts its uncertainty pause through the live scheduler's
tail, not just receipt reconciliation. Journal save, pause read/merge/write, canonical-settled
stamp and retirement share the existing jobs lock plus atomic replacement; a later operator
pause cannot be overwritten or lifted by an older result. Gateway RPC timeouts preserve
method, answer phase, timeout cause and stable identifiers instead of becoming connection
failures. Direct connection/RPC callers were audited: chat, discard, session pruning,
Kanban, web mutations, worker persistence and RPC-forwarding boundaries already handle
transport errors or deliberately propagate them to their transport boundary. Goal peer's
CLI boundary now also catches `OSError` (including `TimeoutError`), emits the original error
and exits nonzero without a traceback or an untrue "not submitted" claim. Recovery-read
refusals log the bounded owner reason without reclassifying the original submit failure.
Detached registration also retries only the same birth/generation/secret-fenced scope;
a lost register receipt never generates a replacement execution. This is an exception
owned by the idempotent worker handshake, not a relaxation of WorkerRPC's no-resend guard.
Proof: `tests/cron/test_ambiguous_admission.py`, `tests/cron/test_cron_recover_pause.py`,
`tests/cron/test_detached_owner_restart.py`, and
`tests/hermes_cli/test_gateway_client_failure_phase.py`,
`tests/cron/test_missing_admission_bound.py`, `tests/cron/test_recovery_refusal_diagnostics.py`,
and `tests/hermes_cli/test_goal_peer_transport_errors.py`.

The existing admission journal captures the worker's result until the canonical
owner commits the same admission's terminal receipt. Reconciliation imports that
completion before cleaning up a terminal execution journal, without requiring a
client status poll or repeating delivery. Captured-completion recovery retires
the linked worker under its original epoch before adopting the admission. A Stop
accepted between admission claim and submit-receipt persistence writes its marker
from the admitted identity, independent of that receipt. A live detached assignment
is excluded from unattended interruption recovery. A dead executor with no
completion receipt is reconciled interrupted, never resubmitted. Admission,
execution and delivery identities are unchanged, and release/activation holds
and the zero-drain policy remain authoritative.

## Proof and limitation

`tests/cron/test_canonical_detached_survival.py` additionally exercises real
canonical RPC and an actual loopback-provider agent tool in a disposable macOS
profile with a fake `ai.hermes-test.*` identity. Its process-ancestry witness must
contain the detached worker PID and exclude the gateway PID. It kills the gateway
and dispatch observer mid-tool, then verifies exactly 1 effect and 1 admission
across replacement owners. Its dead-worker case verifies interruption without
tool or inference replay. A surviving observer alone is not proof of this contract.

Run `tests/cron/test_restart_safe_worker.py`, `tests/cron/test_bounded_worker_recovery.py`, `tests/cron/test_delivery_queue.py`, `tests/cron/test_hard_wall_real_path.py`, `tests/cron/test_double_fork_sweep.py` and `tests/cron/test_hard_wall_completion_race.py` on macOS, then `./bin/ci preflight` and the exact-SHA `gate`. The SQLite execution row is the sole completion fence: worker output is composed and, for gateway-queued notices, an idempotent gated queue item is admitted before the terminal result commits. The gateway cannot claim a gated item until that result commits with a matching outcome. At the cap the watchdog CAS-writes failed(timeout) only if the row remains running. A pre-committed success notice is suppressed when timeout wins, and the occurrence is never retried. When completion won, the watchdog grants a config-derived, finite post-commit allowance (up to 60s including descendant cleanup), then exits with the recorded result. An abandoned executor never disarms the cap. Once a queue receipt exists its status projects onto the execution row; an eligible delivery survives a worker exit after terminal commit and drains once. Only unprojected terminal queue receipts/tombstones are reconciled, in one indexed scan and one execution-ledger connection per drain. The E2E uses a disposable profile, script and fake `ai.hermes-test.*` identity; never touch the live gateway job. The real-path test's default synthetic watchdog cap is 10 seconds, not the production profile cap; its completion-versus-delivery fence case uses 30 seconds because it tests terminal arbitration rather than timeout ownership. The watchdog starts before `run_one_job`, so native macOS process/import/SQLite startup consumes the same budget. A 3-second test cap passed in isolation but flaked under the nightly's four-file concurrency (runs `38548768361`, `38717597758`, and `38765700954`); keep the default test budget at or above 10 seconds unless the worker startup contract changes.

## Operator-selected job deadlines

The `cron-restart-survival` patch also permits a persisted, CLI-only per-job
`hard_wall_timeout_seconds` override for detached/external workers. Unset or
cleared jobs retain the profile cap; malformed legacy overrides fall back to it.
The in-process gateway path remains outside this process-termination guarantee.
Upstream adoption must preserve both per-job selection and watchdog ownership
after an inactivity future is abandoned. Additional proof:
`tests/cron/test_job_wall_timeout.py` and `tests/cron/test_immutable_worker_real.py`.
Pinned-release fixtures must stage the candidate scheduler with its job and
deadline dependencies, not mix its facade with older committed implementations.

## Cross-store state machine

| Boundary | Durable transition | Crash recovery |
| --- | --- | --- |
| Gateway dispatch → worker adoption | `claimed`/handoff → `running` CAS | Dead owner becomes `unknown`; live detached owner retains claim. |
| Worker result ↔ hard wall | Exactly one `running` → `completed`/`failed` CAS after preparation | Watchdog writes failed(timeout) if still running; if terminal, allows bounded grace then exits by recorded status. |
| Delivery preparation → terminal ledger | Gated idempotent queue insert keyed by execution ID → `running` → `completed`/`failed` CAS | Pending queue row cannot be sent before the matching terminal result. A success notice is suppressed if timeout wins, and an `unknown` result is never sent. A pre-admitted failure notice may still cite its earlier failure after a later watchdog timeout: it remains a genuine failure alert, while the ledger's error is the authoritative terminal reason. When the executions ledger is readable but an execution row is missing, its gated notice is suppressed after the 24-hour grace, never sent. If the ledger file itself is missing or unreadable, the queue defers gated rows indefinitely (fail closed), even past the grace. A committed eligible result drains once after worker death. Legacy already-queued notices remain on their established path. |
| Queue pending → delivering | Gateway atomically claims one pending item | Dead delivery owner becomes `unknown`, never retried after possible send. |
| Queue terminal → ledger projection | Queue receipt commits → execution `delivery_status` update → receipt `projected=1` | Unprojected indexed receipt (including tombstone) retries projection on drain; projected history is skipped. |
| Queue retention → tombstone | Terminal receipt moves with `projected` preserved | Tombstone prevents re-enqueue and unprojected tombstone still retries projection. |

## Delivery-status transition audit

`delivery_status_provisional=1` is written only atomically with the detached
finish `unknown` marker, and only when the delivery status is still NULL.
It distinguishes the pre-enqueue gap from a terminal queue projection; old
releases read the status as ordinary `unknown` and ignore the additive column.
Every accepted queue projection clears the flag in the same conditional SQL
UPDATE. The same transition table applies to both immediate and reconciliation
projections (including tombstones); no stale nonterminal projection can replace
a terminal one.

| Target projection | Allowed source states | Rejected source states |
| --- | --- | --- |
| `pending` | NULL, pending, unknown(provisional) | delivering, unknown(terminal), delivered, failed, suppressed |
| `delivering` | NULL, pending, delivering, unknown(provisional) | unknown(terminal), delivered, failed, suppressed |
| `unknown` (terminal) | NULL, pending, delivering, unknown(provisional) | unknown(terminal), delivered, failed, suppressed |
| `delivered`, `failed`, or `suppressed` | NULL, pending, delivering, unknown(provisional), unknown(terminal) | delivered, failed, suppressed |

Known terminal states (`delivered`, `failed`, `suppressed`) are final. The
terminal-to-terminal exception is that an unknown(terminal) may become a known
terminal receipt; another unknown cannot rewrite it. `finish_execution` alone
may mark NULL → unknown(provisional) on detached terminal finish, and never
rewrites an existing delivery state. Execution recovery updates run status only,
not delivery status. The 48-pair SQLite matrix and the idempotent-enqueue versus
wait-timeout interleaving exercise this contract. The worker admits a gated queue item before its terminal CAS if a gateway delivery is eligible. The queue consumer waits for the exact execution's terminal row; a success notice whose result was superseded by timeout, or an `unknown` run, is suppressed without sending. The terminal finish may still carry `delivery_status_provisional=1` and `unknown`, but a pre-admitted queue receipt projects `pending` after the ledger commit. Worker death after terminal commit cannot lose the notice because the queue item already exists. Legacy queue inserts without the new gate retain their old claim semantics. A queue receipt is idempotent by execution ID, and the gateway claims it at most once. A watchdog kill during the short interval between enqueue and terminal commit leaves a pending gated row that is classified against the eventual ledger outcome, never an automatic run retry. The worker still records its `delivery_outcome` after classification while owner-fenced. A bot-chat-only direct send is outside this queue guarantee.

The executions schema migration is additive and idempotent. Existing releases use
named INSERT/UPDATE columns and `SELECT *` into named rows, so the new column is
ignored safely; the queue's existing additive tombstone `projected` column is
also named in retention INSERT/SELECT and defaults safely for old rows.


Related upstream PRs [#123893](https://github.com/NousResearch/hermes-agent/pull/123893) and [#123878](https://github.com/NousResearch/hermes-agent/pull/123878) concern restart identity and drain waiting; neither isolates a macOS cron worker. Roll back by reverting this patch's launchd dispatch branch and associated docs/tests, without removing the upstream Linux handoff or execution ledger.
