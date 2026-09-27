# Cron restart survival on macOS

Patch identities: `cron-macos-detached`, `cron-restart-survival`.

## Contract

Under a launchd-managed macOS gateway, hand each cron execution to the existing external worker in a new session, rather than running it in the gateway process. The worker adopts the durable execution claim, owns its (PID, process-start fingerprint) liveness, completes the ledger, and queues delivery for the replacement gateway. Honor `cron.require_restart_safe_scope` without requiring systemd on macOS. Foreground/desktop invocations and Linux systemd dispatch retain their current behavior. A worker that has recorded an inactivity timeout but still has an abandoned non-daemon executor thread must retain its hard-wall watchdog until the process exits; a terminal delivery receipt from normal send, restart recovery, or wait timeout must project onto its execution row without replaying an uncertain send.

This is a core scheduler/dispatch invariant; a plugin or skill cannot atomically own cron's claim, worker handoff and recovery. Revert this unit's dispatch selection and regression when an upstream release proves the same launchd restart-survival contract. The Linux transient-scope worker and delivery queue are upstream-owned infrastructure reused here.

## Proof and limitation

Run `tests/cron/test_restart_safe_worker.py`, `tests/cron/test_bounded_worker_recovery.py`, `tests/cron/test_delivery_queue.py`, `tests/cron/test_hard_wall_real_path.py` and `tests/cron/test_hard_wall_completion_race.py` on macOS, then `./bin/ci preflight` and the exact-SHA `gate`. The SQLite execution row is the sole completion fence: completion commits result and output before output-file writes, notification enqueue or teardown; at the cap the watchdog CAS-writes failed(timeout) only if the row remains running. When completion won, the watchdog grants a config-derived, finite post-commit allowance (up to 60s including descendant cleanup), then exits with the recorded result. A completion that has not committed cannot suppress timeout. An abandoned executor never disarms the cap. The commit-to-enqueue gap deliberately records `delivery_status='unknown'` in the terminal transaction and **does not replay** an absent queue item: this favors a missed notice over duplicate send. Once a queue receipt exists its status may replace the unknown marker. Only unprojected terminal queue receipts/tombstones are reconciled, in one indexed scan and one execution-ledger connection per drain. The E2E uses a disposable profile, script and fake `ai.hermes-test.*` identity; never touch the live gateway job.

## Cross-store state machine

| Boundary | Durable transition | Crash recovery |
| --- | --- | --- |
| Gateway dispatch → worker adoption | `claimed`/handoff → `running` CAS | Dead owner becomes `unknown`; live detached owner retains claim. |
| Worker result ↔ hard wall | Exactly one `running` → `completed`/`failed` CAS; result/output/error committed first | Watchdog writes failed(timeout) if still running; if terminal, allows bounded grace then exits by recorded status. |
| Terminal ledger → delivery queue | Ledger delivery status `unknown` → idempotent queue insert keyed by execution ID → `pending` | No queue receipt means **no resend**; status remains `unknown`. Existing receipt is never inserted twice. |
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
wait-timeout interleaving exercise this contract. The worker commits its immutable
result before delivery classification, then conditionally writes `delivery_outcome`
exactly once while still owner-fenced by process ID and PID. That second SQL
UPDATE uses the receipt transition predicate in the same write to resolve only
`unknown(provisional)` when no queue receipt will arrive. A missing target
(`not_configured`) and intentionally withheld notices (`suppressed` or
`suppressed_acked`) resolve to terminal `suppressed`; a direct send or transport
failure resolves to `delivered` or `failed`. Queued notices retain the queue's
`pending`/terminal receipt transition. The post-terminal update emits the final
outcome to monitoring; the earlier result-fence event can carry a NULL outcome.
A watchdog kill between the result commit and classification deliberately leaves
`delivery_outcome=NULL` and provisional `unknown`, with no inferred retry or send.
In-gateway runs still classify in their single terminal `finish_execution` write.

The executions schema migration is additive and idempotent. Existing releases use
named INSERT/UPDATE columns and `SELECT *` into named rows, so the new column is
ignored safely; the queue's existing additive tombstone `projected` column is
also named in retention INSERT/SELECT and defaults safely for old rows.


Related upstream PRs [#123893](https://github.com/NousResearch/hermes-agent/pull/123893) and [#123878](https://github.com/NousResearch/hermes-agent/pull/123878) concern restart identity and drain waiting; neither isolates a macOS cron worker. Roll back by reverting this patch's launchd dispatch branch and associated docs/tests, without removing the upstream Linux handoff or execution ledger.
