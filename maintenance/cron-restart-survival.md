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
finish `unknown` marker. It distinguishes the pre-enqueue gap from a terminal
queue projection; old releases read the status as ordinary `unknown` and ignore
the additive column. The queue projection clears the flag in the same SQL
UPDATE, and terminal states remain fenced.

| Write site | Transitions | Guard / invariant |
| --- | --- | --- |
| `executions.finish_execution` | `NULL → unknown(provisional)` | Detached terminal finish only; never rewrites an existing delivery state. |
| `executions.record_delivery_status` | `NULL/pending → pending`; `unknown(provisional) → pending`; `NULL/unknown/provisional/pending/delivering → delivered/failed/unknown/suppressed` | Conditional SQL UPDATE; pending cannot replace terminal unknown (`provisional=0`) or any terminal state; terminal write clears provisional. |
| `delivery_queue.reconcile_terminal_deliveries` | `NULL/unknown(provisional)/pending/delivering → delivered/failed/unknown/suppressed` | Conditional SQL UPDATE; terminal statuses are immutable and projection marker is cleared atomically. |

The executions schema migration is additive and idempotent. Existing releases use
named INSERT/UPDATE columns and `SELECT *` into named rows, so the new column is
ignored safely; the queue's existing additive tombstone `projected` column is
also named in retention INSERT/SELECT and defaults safely for old rows.


Related upstream PRs [#123893](https://github.com/NousResearch/hermes-agent/pull/123893) and [#123878](https://github.com/NousResearch/hermes-agent/pull/123878) concern restart identity and drain waiting; neither isolates a macOS cron worker. Roll back by reverting this patch's launchd dispatch branch and associated docs/tests, without removing the upstream Linux handoff or execution ledger.
