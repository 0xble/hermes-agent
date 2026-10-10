# Durable delegation terminal fallback

Load this unit when changing how async delegations persist child-failure
notices or terminal results that could not be written to the lifecycle row
(`tools/async_delegation.py::_persist_outbox_event`, `_persist_completion`,
`restore_undelivered_completions`), or how session recovery copies the
`async_delegation_events` table.

## Required behavior

- An interim child-failure notice (`task_failure`) and a terminal result whose
  lifecycle write raised (`terminal_fallback`) are stored as durable outbox
  rows in `async_delegation_events`, each with its own event id and delivery
  state. Restart and orphan recovery replay them like lifecycle rows.
- A `terminal_fallback` row claims the delegation lifecycle row in the same
  transaction as its outbox insert. A zero-row conditional transition rolls
  the insert back, so a competing terminal writer cannot create a redundant
  deliverable.
- Ambiguous completion writes reconcile the lifecycle payload and fallback
  outbox before queueing, comparing the canonical persisted status (a queued
  cancellation persists `cancelled`) while the live event keeps its intended
  status. The authoritative payload is delivered once; a losing terminal
  writer does not enqueue its result beside the winner.
- Ownership that cannot be proven (reconciliation unavailable, or a still
  active row) is never inferred from `delegation_id`. The event carries the
  in-memory `_terminal_ownership` marker, and `claim_event_delivery` (every
  consumer's claim; the TUI also checks before its status row) resolves it
  first: this writer's lifecycle or outbox identity, or a conditional fallback
  acquisition. A proven loser remains permanently rejected in that same marker,
  even when a failed batch requeues it or a consumer copies it. No later ledger
  change, removal, or read failure can revive a rejected copy. A still-unreadable
  ledger holds the event off the queue with backoff; the existing drain and
  orphan-sweep paths re-offer it. Only a proven-missing lifecycle row delivers
  in memory only, without a phantom outbox identity.
- Index-less task notices get collision-safe event ids.
- Session recovery registers `async_delegation_events`, so a rebuilt
  `state.db` keeps undelivered outbox rows.

## Why

A child failure notice or a terminal result that failed to persist was
in-memory only. If the owning process exited before delivery, the parent never
learned the unit failed, and a dead-owner restart reported it as `unknown`
instead of its real result.

## Provenance

Fork patch identity: `durable-delegation-terminal-fallback`.

Upstream-owned code. No upstream issue or PR covered durable outbox delivery
for child failure notices or terminal-write fallbacks when this patch landed.
The gateway settlement side is owned by
[Async delegation ledger off the loop](async-delegation-ledger-off-loop.md),
and the interim notice contract by
[Internal notification silence](internal-notification-silence.md).

## Resolved race coverage

- A commit-then-raise completion is reconciled against the terminal lifecycle
  row before fallback creation, so it cannot gain a second outbox replay.
- A competing terminal transition owns the durable payload, including a winner
  committed after the initial active-state read. Raised and zero-row writes use
  the same conditional fallback path, and fallback exceptions reconcile again
  before offering an event. The losing worker does not enqueue a notification.
- A fallback commit-then-raise recovers its existing outbox delivery identity
  before queueing, so acknowledging the live copy also settles restart replay.
- A fallback insert and its lifecycle claim are one conditional transaction.
- Unavailable reconciliation cannot claim or settle a competing winner's
  lifecycle row, and a fallback commit-then-raise behind unavailable
  reconciliation still settles its own outbox row once recovered.
- Ordinary terminal failures, interim notices, duplicate fallback invocation,
  and missing persistence retain their existing behavior and are covered by
  focused regression tests.

## Event-route closure inventory

Inspected before the permanent-rejection repair on `9ccfb780a572e764a89af335680ab216a482c80a`.
The routes below retain the whole event, not a whitelist of public fields.
The only destructive ownership-marker removal found was in
`tools/async_delegation.py::resolve_event_ownership`.

| Route / exact owner | Marker transport and claim boundary | Regression coverage |
|---|---|---|
| `tools/async_delegation.py::_push_completion_event` | `persist_evt` and the marker's `event` snapshot are copied **before** marker attachment; the queued live event carries the marker. `_resolve_terminal_ownership` writes only these producer snapshots, never consumer-enriched events. | `test_async_terminal_fallback_ownership.py`: ambiguous primary/fallback writes, queued cancellation, late competitor, unreadable DB recovery |
| `tools/async_delegation.py::_hold_unresolved`, `reoffer_unresolved_completions`, `sweep_orphaned_completions`, `maybe_sweep_orphaned_completions` | Hold and reoffer the same dict. The retry metadata stays in the marker; drain/sweep invokes reoffer, not a fresh delegation-id reconstruction. | ownership recovery tests; `test_async_delegation_orphan_sweep.py` |
| `tools/process_registry.py::ProcessRegistry.drain_notifications` | Foreign events accumulate in `requeue` and are put back unchanged; owned results are `(evt, text)` references. `restore_completions` and reoffer run before draining. | permanent rejected-copy/requeue invariant; `test_process_registry.py`, `test_restored_delegation_ownership.py` |
| `gateway/run.py::_drain_gateway_watch_events`; `gateway/run_turn.py` post-turn watch drain | Async delegations are detached then requeued as the same objects, never converted to watch messages. | permanent rejected-copy/requeue invariant; `test_background_process_notifications.py` |
| `gateway/run_notifications.py::_async_delegation_watcher`, `_enrich_async_delegation_routing`, `_enqueue_async_delegation_group` | Routing enrichment mutates the live dict only; grouped lists and held batches retain references. False or exceptional group delivery requeues **the whole original group**. | failed-primary batch retry invariant; `test_completion_delivery.py`, `test_autonomous_wake_pacing.py` |
| `gateway/run_notifications.py::_flush_async_delegation_batch`, `_requeue_completion_events`, `_cancel_process_completion_batch_tasks` | Failed, exceptional, cancelled and shutdown/orphaned batches requeue the original dicts. A skipped unclaimable sibling is not necessarily discarded: a failed primary requeues it too. | failed-primary batch retry invariant; `test_autonomous_wake_pacing.py` |
| `gateway/run_notifications.py::_deliver_async_delegation_group_scoped`, `_preflight_completion_delivery`; `gateway/run_notifications_ledger.py::claim_siblings_off_loop`, `claim_off_loop` | Entries/siblings keep dict references; shared claim resolves ownership before either lifecycle or outbox claim. A None primary requeues claimed siblings unchanged. Cancellation refunds claim tokens, not reconstructed events. API-origin groups deliver events individually through the same preflight. | failed-primary batch retry invariant; `test_completion_delivery.py` |
| `tui_gateway/session_notifications.py::_notif_handle_event`, `_notif_handle_ready`, `_notification_poller_scoped_loop`; `tui_gateway/prompt_turn.py::_run_post_turn_followups` post-turn drain | Foreign/busy events and remaining ready snapshots are put back directly, or appended to `deferred` and later put back. No event projection. TUI checks shared ownership before status emission and again in `_notif_dispatch_event`; `_notif_dispatch_completions` uses shared claim for batch entries. | TUI ownership test in `test_async_terminal_fallback_ownership.py`; `test_notification_turn_release.py`, `test_tui_gateway_server.py` |
| `hermes_cli/cli_process_notifications.py::CLIProcessNotificationsMixin._drain_process_notifications`; `hermes_cli/quiet_single_query.py::continue_quiet_notify_completions` | Both claim the raw event from registry drain before constructing model/UI notification text; no rejected event is cloned from rendered text. | CLI ownership test; `test_cli_async_delegation_delivery.py`, `test_process_notification_display.py`, `test_quiet_turn_author.py` |
| `tools/async_delegation.py::push_task_failure_notice`, `_persist_outbox_event` | Interim notice producer takes a whole-event durable snapshot and queues its live outbox identity; it does not create a terminal-ownership marker. Copies pass through the same shared claim without terminal lifecycle acknowledgement. | `test_async_batch_task_failure_notice.py` |
| `tools/async_delegation.py::restore_undelivered_completions`, `_replay_pending`, `_replay_outbox_pending`; `gateway/run_notifications.py::_restore_secondary_completion_ledgers`, `_sweep_orphaned_completion_ledgers` | JSON replay reconstructs the durable **authoritative** event. The private marker is never persisted. This is not a retry of a rejected live event; no rejected payload can own a row merely by being requeued. Session DB recovery copies durable tables, not queue events. | lifecycle/outbox recovery tests; `test_session_recovery.py`, `test_async_batch_task_failure_notice.py` |
| Whole-dict shallow/deep copies | `dict(evt)` retains the marker (and shares its metadata); `deepcopy(evt)` retains its recorded disposition independently. Copies made before resolution must themselves resolve; once rejected, every later copy retains rejection even if the winner is delivered, purged, or the ledger becomes unreadable. | permanent rejected-copy/requeue invariant |

Read-only queue-depth observers, `hermes_cli/oneshot.py` (no queue consumer),
process-only producers and separate `delegation_auto_resume` notices do not
copy/retry terminal delegation payloads. No other production route drops the
marker. Consumer transports and lifecycle/outbox CAS remain their existing owners.

## Known gaps

- A stale pending `task_failure` notice can replay after the unit's final
  result on restart. This patch does not resolve interim-notice ordering.
- A held unproven event lives only in the producing process. If that process
  exits before the ledger is readable again, the result is lost unless its
  write committed; a lifecycle row left active is then classified by
  abandoned-delegation recovery, not delivered with the real result.

## Verification

Run the focused consumer set through the canonical per-file runner:

```sh
scripts/run_tests.sh -j 2 --file-retries 0 \
  tests/tools/test_async_terminal_fallback_ownership.py \
  tests/tools/test_async_batch_task_failure_notice.py \
  tests/tools/test_async_delegation.py \
  tests/tools/test_async_delegation_orphan_sweep.py \
  tests/tools/test_async_delegation_stale_profile_scope.py \
  tests/tools/test_restored_delegation_ownership.py \
  tests/tools/test_process_registry.py \
  tests/hermes_cli/test_session_recovery.py \
  tests/gateway/test_completion_delivery.py \
  tests/gateway/test_background_process_notifications.py \
  tests/gateway/test_autonomous_wake_pacing.py \
  tests/tui_gateway/test_notification_turn_release.py \
  tests/tui_gateway/test_tui_gateway_server.py \
  tests/hermes_cli/test_cli_async_delegation_delivery.py \
  tests/hermes_cli/test_process_notification_display.py \
  tests/hermes_cli/test_quiet_turn_author.py \
  tests/hermes_cli/test_subagent_notification_display.py \
  tests/tui_gateway/test_heartbeat_wakes.py
```

The permanent-rejection invariants are
`test_rejected_terminal_copies_never_claim_after_winner_changes` and
`test_failed_gateway_primary_requeues_permanently_rejected_sibling` in the
ownership test file. The latter uses real gateway group delivery and batch
flush, SQLite claims and settlement; only route readiness and adapter admission
are replaced with synthetic boundaries.

## Retirement and rollback

Retire when upstream persists interim and fallback delegation events durably.
To roll back, revert the patch commits. Existing `async_delegation_events`
rows are then ignored.
