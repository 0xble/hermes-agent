# Delegation restart drain

Load this unit when changing the in-band gateway restart wait, shutdown drain
accounting, the CLI exit-wait budget for `hermes update` / `hermes gateway restart`,
or how an interrupted child reports why it stopped.

## Required behavior

- A planned gateway restart waits for live background delegations before `stop()`.
  Live async delegations count in `_active_work_count()` (busy status,
  `gateway_state.json` `active_agents`, restart wait) while `stop()`'s drain counters
  deliberately exclude them so `interrupt_all` is still reached at the cap.
- `gateway.restart_delegation_timeout` (default 900s, `0` skips the wait; env
  override `HERMES_RESTART_DELEGATION_TIMEOUT`) is independent of
  `agent.restart_after_turn_timeout`. Each kind has its own entry-fixed deadline; a
  stuck kind cannot borrow the other's budget, and `restart_after_turn_timeout=0`
  still enters forced drain immediately for non-delegation work.
- The CLI exit-wait budget is `drain + max(after_turn, delegation) + headroom`
  (`resolve_restart_exit_wait_budget`), read from the `gateway.*` section on the CLI
  side, so `hermes update` never `kickstart -k`s a gateway that is legitimately
  holding for a delegation. With this fork's live config (after_turn=30, drain=5)
  the budget is 920s, not 50s.
- The drain report names delegations (`delegation deleg_x (12s)`) and cites the
  config knob that caps each kind.
- A child interrupted by shutdown or `/stop` carries the reason:
  `interrupt_all(reason)` -> `interrupt_fn(reason)` -> `_signal_child_stop` ->
  `entry["interrupt_reason"]`, shown in the batch completion header as
  `status=interrupted, reason=gateway shutdown (final-cleanup)`. `_call_interrupt`
  selects the legacy zero-arg vs reason ABI by signature and never retries a
  callback's own `TypeError`.
- Composes with [Restart continuation](restart-continuation.md): the restart first
  waits for children under this unit, then the parent turn resumes under
  `gateway.restart_resume_policy`. Neither unit resumes a killed child
  automatically; explicit parent-driven recovery is the section below.

## Explicit one-shot recovery (slice 2, narrow form)

- `delegate_task(action='resume', subagent_id=<delegation_id>)` is a control
  action on the existing synchronous control path (`_handle_control_action`), not
  a new tool and not a spawn. It hands back a `recovery_context` string; the
  parent then spawns normally, so every existing spawn gate (pause switch, depth,
  capacity, guardrail spawn cap) still applies and the resumed run is visible in
  the parent's own transcript. `resume` counts 0 against the spawn cap because it
  spawns nothing.
- Eligibility is durable and deliberately minimal (`tools/delegation_resume.py`):
  a SINGLE-task row in state `unknown`/`interrupted`/`stalled`, with a recorded
  goal, a routable origin, and no recorded per-child partial results. Batches,
  partial-result units, and stateless (cron/one-shot) origins are refused with a
  distinct truthful reason rather than guessed at.
- The claim is durable and ONE-SHOT: `async_delegations.resume_state` /
  `resume_attempts` / `resume_claim` / `resume_claimed_at`, taken by a guarded
  `UPDATE ... WHERE resume_state='none' AND resume_attempts=0` inside the same
  transaction as the eligibility re-check. A restart loop, a re-delivered
  completion, or two racing consumers yield at most one recovery brief per
  delegation, ever. An ineligible row never spends its attempt.
- Ownership is checked BEFORE the claim, over the same durable spine as
  `_owns_subagent_record` tier 2 (`parent_session_id`/`origin_session_id` with
  compression-lineage resolution, failing closed with no session id), so a foreign
  conversation can neither read another's brief nor burn its single claim.
- The brief always requires state verification first: the interrupted child may
  have completed side effects its lost summary never reported. No credentials are
  read or persisted; `task_json` holds only goal/context/role/model metadata and
  the resumed spawn resolves credentials through the normal delegation path.
- Boot auto-trigger (slice 3) stays DISABLED: `AUTO_RESUME_ON_BOOT` is `False`
  with no call site, and `recover_abandoned_delegations()` is unchanged. Automatic
  re-spawning of abandoned work at process start waits for field evidence from
  this explicit path.

## Provenance and patches

- Fork patch identities: `restart-delegation-drain` (gateway wait, CLI budget, drain
  report), `delegation-interrupt-reason` (reason plumbing),
  `delegation-stall-reason` (truthful stall reason on the stale-monitor interrupt),
  and `delegation-explicit-resume` (one-shot recovery claim and the
  `action='resume'` control path). All are upstream contribution candidates; no
  upstream PR filed. Upstream's documented position is
  that a process restart does not resume a running child (`delegation.md`), which
  this unit does not change.
- Origin: 2026-09-19, a `hermes update` from one Telegram topic killed a fix worker
  mid-task in another topic because the restart drain counted only chat turns, cron,
  API runs, and deferred workers. The parent received a bare `status=interrupted`
  and misattributed the death to a tool timeout.
- Independent review: three rounds. Round 1 caught a single `max()` deadline that
  collapsed both budgets; round 2 caught the CLI exit-wait budget not covering the
  delegation budget.
- Surfaces: `gateway/run_shutdown.py` (`_active_work_count`,
  `_active_async_delegation_count`, `_active_async_delegation_records`,
  `_describe_active_work`, `_await_active_work_before_restart`), `gateway/restart.py`
  (`parse_restart_delegation_timeout`, `resolve_restart_exit_wait_budget`),
  `gateway/run_config_loaders.py` (`_load_restart_delegation_timeout`),
  `hermes_cli/gateway.py` (`_gateway_timeout_setting`,
  `_get_restart_exit_wait_budget`), `hermes_cli/update_cmd_drain_report.py`,
  `hermes_cli/config_defaults.py`, `tools/async_delegation.py` (`active_records`,
  `_call_interrupt`, `_interrupt_records`), `tools/delegate_tool_child_run.py`
  (`_signal_child_stop`, `_build_result_entry`), `tools/delegate_tool_dispatch.py`,
  `tools/process_registry_notifications.py`, `tools/delegation_resume.py`,
  `tools/delegate_tool_registry.py` (`_handle_resume_action`,
  `_owns_durable_delegation`), `tools/delegate_tool.py` (schema + action routing),
  `agent/tool_guardrails.py` (`_subagent_spawn_count`), `hermes_state_common.py`
  (`async_delegations` resume columns).

## Verification

`scripts/run_tests.sh tests/gateway/test_restart_after_turn.py
tests/gateway/test_restart_drain.py tests/gateway/test_cron_active_work_drain.py
tests/gateway/test_drain_active_work_report.py
tests/tools/test_delegate_interrupt_reason.py tests/tools/test_async_delegation.py
tests/tools/test_delegation_resume.py tests/tools/test_delegate_control_actions.py
tests/hermes_state/test_hermes_state.py -k AsyncDelegations
tests/hermes_cli/test_gateway_service.py tests/hermes_cli/test_update_wedged_gateway.py`.

After promotion, prove it live: start a throwaway delegation, run `hermes update`
or `/restart`, and confirm `gateway.log` shows `Restart deferred: waiting on ...
delegations ...` rather than `interrupted 1 background delegation(s)` before the
child finishes. Source tests alone do not prove the installed gateway waited.

## Follow-ups

Review lows deliberately left out of the reviewed head:

- `website/docs/getting-started/updating.md` sample drain-report block still shows
  the pre-patch footer; `format_drain_report()` now emits the per-kind line.
- `hermes_cli/update_cmd_drain_report.py` L17 comment says 10s but
  `DRAIN_REPORT_INTERVAL_S` is 30.0; reword as a deliberate terminal-noise cap or
  lower it.
- `interrupt_reason` is dropped from single (non-batch) delegation completion
  payloads, `_fabricated_entry`, and the stale-monitor interrupt.
- `tests/gateway/restart_test_helpers.py` hardcodes `900.0` instead of
  `DEFAULT_GATEWAY_RESTART_DELEGATION_TIMEOUT`.
- A delegation record with no `dispatched_at` reports `elapsed_s=0.0` instead of
  omitting the field like chat/cron units.

Planned, not started:

- Slice 3: on boot, `recover_abandoned_delegations()` re-spawns via the explicit
  recovery path when the owner session exists and the transcript is intact, retry
  cap 1. Deliberately NOT implemented (`AUTO_RESUME_ON_BOOT = False`, no call
  site): it needs field evidence that the explicit path produces correct resumes
  before anything re-spawns without a parent asking.
- The recovery brief is seeded from the durable goal/context only, not from the
  persisted child transcript. Transcript seeding is a follow-up; the archived
  fork's `delegate_tool_checkpoint.py` / `resume_authorization` system had a P1
  registry-pruning defect and was dropped in v2026.9.14, so it is not a source.

## Retirement and rollback

Retire when a released upstream version waits for background delegations on
restart with an independent budget, surfaces the interrupt reason, and offers an
equivalent explicit recovery path. Roll back by reverting the fork commits and
removing `gateway.restart_delegation_timeout` from configuration. The resume
columns on `async_delegations` are additive with safe defaults
(`resume_state='none'`, `resume_attempts=0`); leaving them in place after a
rollback is harmless and avoids a destructive table rebuild.
