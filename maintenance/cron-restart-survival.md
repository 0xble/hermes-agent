# Cron restart survival on macOS

Patch identities: `cron-macos-detached`, `cron-restart-survival`.

## Contract

Under a launchd-managed macOS gateway, hand each cron execution to the existing external worker in a new session, rather than running it in the gateway process. The worker adopts the durable execution claim, owns its (PID, process-start fingerprint) liveness, completes the ledger, and queues delivery for the replacement gateway. Honor `cron.require_restart_safe_scope` without requiring systemd on macOS. Foreground/desktop invocations and Linux systemd dispatch retain their current behavior. A worker that has recorded an inactivity timeout but still has an abandoned non-daemon executor thread must retain its hard-wall watchdog until the process exits; a terminal delivery receipt from normal send, restart recovery, or wait timeout must project onto its execution row without replaying an uncertain send.

This is a core scheduler/dispatch invariant; a plugin or skill cannot atomically own cron's claim, worker handoff and recovery. Revert this unit's dispatch selection and regression when an upstream release proves the same launchd restart-survival contract. The Linux transient-scope worker and delivery queue are upstream-owned infrastructure reused here.

## Proof and limitation

Run `tests/cron/test_restart_safe_worker.py`, `tests/cron/test_bounded_worker_recovery.py`, `tests/cron/test_delivery_queue.py`, `tests/cron/test_hard_wall_real_path.py`, `tests/cron/test_double_fork_sweep.py` and `tests/cron/test_hard_wall_completion_race.py` on macOS, then `./bin/ci preflight` and the exact-SHA `gate`. The SQLite execution row is the sole completion fence: worker output is composed and, for gateway-queued notices, an idempotent gated queue item is admitted before the terminal result commits. The gateway cannot claim a gated item until that result commits with a matching outcome. At the cap the watchdog CAS-writes failed(timeout) only if the row remains running. A pre-committed success notice is suppressed when timeout wins, and the occurrence is never retried. When completion won, the watchdog grants a config-derived, finite post-commit allowance (up to 60s including descendant cleanup), then exits with the recorded result. An abandoned executor never disarms the cap. Once a queue receipt exists its status projects onto the execution row; an eligible delivery survives a worker exit after terminal commit and drains once. Only unprojected terminal queue receipts/tombstones are reconciled, in one indexed scan and one execution-ledger connection per drain. The E2E uses a disposable profile, script and fake `ai.hermes-test.*` identity; never touch the live gateway job.

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
