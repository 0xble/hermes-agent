# External-wait goal backoff

## Fork patch identity

This maintenance unit owns the fork patch identity `goal-external-wait-backoff`.

## Required behavior

A standing goal parks when its next progress depends on work outside the current
session, including an external service, cron/watchdog, scheduled time, or an
elapsed hold, when the current response has no actionable step. Repeated
automatic turns with no recorded progress use durable escalating timed backoff
rather than busy-polling. A pid/session barrier whose target is still running
re-arms after its bounded probe window instead of lifting and waking the agent;
target exit or the process session's watch trigger remains the wake condition.

## Independent hypothesis and upstream comparison

The frozen hypothesis is recorded at
`~/.hermes/cache/scratch/goal-busy-poll-hypothesis-20261004.md`. The native
judge prompt omitted external prerequisites, so it defaulted to CONTINUE, and
pid/session barriers treated the 30-minute probe cap as permission to resume
judging even when the target was still alive. A core patch is required because
this behavior spans judge semantics, durable goal state, barrier liveness, and
idle wake admission; instructions or a plugin cannot atomically own those
boundaries.

Related upstream PRs reviewed before implementation: #106925, #118705,
#129380, and #107130. None supplied a released equivalent for the complete
external-wait, no-progress, and live-barrier contract.

## Design

### Production entry points and turn provenance

`GoalManager.evaluate_after_turn()` is the shared evaluator entry point. The
CLI calls it from `CLILoopsMixin._maybe_continue_goal_after_turn`; the gateway
calls it from `GatewayGoalsMixin._post_turn_goal_continuation`; and the TUI
calls it from `tui_gateway.prompt_turn._post_turn_goal_continuation`. Each
caller must pass the provenance of the turn that just completed, rather than
letting the evaluator infer it from prose or queue state:

- admitted human input is `user_initiated=True`;
- a goal continuation, idle wake, heartbeat, loop wakeup, process/delegation
  notification, browser/system note, or other runtime injection is
  `user_initiated=False`;
- gateway `_is_user_turn_event()` is the admission authority. It rejects
  `internal`, control-disallowed, heartbeat, and goal-continuation events;
- CLI `_is_self_injected_turn()` is the equivalent guard for synthetic text.

The complete standing-goal continuation-builder inventory is:

1. `CONTINUATION_PROMPT_TEMPLATE`;
2. `CONTINUATION_PROMPT_WITH_CONTRACT_TEMPLATE`;
3. `CONTINUATION_PROMPT_WITH_SUBGOALS_TEMPLATE`;
4. `CONTINUATION_PROMPT_GATE_FAILED_TEMPLATE`, whose distinct prefix is
   `[Continuing toward your standing goal — a quality gate failed]`;
5. `KANBAN_GOAL_CONTINUATION_TEMPLATE`, a separate kanban worker loop that is
   still runtime-injected and must not be mistaken for human input; and
6. `KANBAN_GOAL_FINALIZE_TEMPLATE`, the kanban terminal-action nudge.

The first four are produced by `GoalManager.next_continuation_prompt()` or the
real `_check_gates()` path and enter the CLI pending-input queue, gateway FIFO
via `_synthetic_prompt_event()`, or TUI follow-up dispatch. The gateway and CLI
provenance matchers must recognize all four standing-goal forms, including the
quality-gate prefix. Heartbeat (`[Heartbeat — ...]`), loop (`[/loop wakeup ...]`),
async delegation (`[ASYNC DELEGATION ...]`), process (`[IMPORTANT: Background
process ...]`), system/browser (`[System note: ...]`, `[System: ...]`), and
other runtime prompt builders are also synthetic and are covered by the same
caller-level admission rules; they do not become user progress merely because
their text is stored as a user-role row.

The resulting data path is:

```text
real input or runtime injection
→ caller marks provenance
→ GoalManager.evaluate_after_turn(..., user_initiated=...)
→ isolated evaluate_goal_snapshot()
→ durable state CAS commit
→ caller prints/sends decision message and admits continuation if allowed
```

### Evidence representation and no-progress decision

`collect_goal_evidence(session_id, since=...)` reads the real `SessionDB` rows.
It matches assistant `tool_calls[*].id` to tool-result rows and stores each
eligible result as:

```python
{
    "tool": "terminal",
    "call": '{"command": "gh run view 1"}',
    "output": "...secret-redacted one-line/tail output...",
    "timestamp": 123.0,
}
```

The `call` field is intentionally the bounded, one-line JSON argument string
written by the collector; it is not a bare shell command. The classifier must
parse JSON arguments first (for terminal/shell calls), extract the `command`
field, and only then apply its command grammar. Non-shell read-only tools are
accepted by their explicit tool-name allowlist. Evidence is filtered to the
current turn by `timestamp > previous_turn_at`; quality-gate rows are excluded
from the progress fingerprint, while still being shown to the judge.

For an automatic CONTINUE, empty evidence or evidence consisting only of
approved read-only/status operations increments
`consecutive_no_progress`. A write/actionable result or a real user turn resets
it. Three consecutive qualifying turns call `_no_progress_wait()`, persist a
timed barrier, and return `should_continue=False`; the backoff waits are 5,
15, and 30 minutes (the last is bounded by `_MAX_BARRIER_WAIT_S`). Judge
wording and changing status output do not convert a read-only poll into
progress.

### Read-only command grammar

The grammar is an allowlist, not a prefix regex. It accepts only complete,
single commands in these families:

- `gh pr|run view|checks|status|list` with ordinary arguments;
- `git status|diff|log|show|rev-parse`;
- `git branch` and its read-only listing forms `--show-current`, `--list`,
  `-a`, and `--all`;
- bounded inspection commands `ls`, `pwd`, `rg`, `grep`, `cat`, `head`,
  `tail`, and `find` only when no destructive option/operator is present; and
- the explicit non-shell read-only tool allowlist.

The parser rejects shell composition and execution syntax even when an allowed
prefix appears first: `>`, `>>`, `|`, `&&`, `||`, `;`, backticks, `$(`, and
`find` `-exec`, `-execdir`, and `-ok`. It also rejects destructive options such
as `-delete`, `--delete`, branch deletion, redirects, and any command that does
not consume the complete argument string. The classifier consumes the parsed
`command`, never the raw JSON representation.

### Parked, age, continuation, and delivery notice keys

These durable keys have separate domains:

- `last_wait_notice_key` identifies the parked barrier (`session`, `pid`, or
  timed/delegation target plus reason). It deduplicates the initial judge-WAIT
  or deterministic no-progress parked notice only;
- `last_age_notice_key` identifies the live target and reason after the first
  30-minute threshold. It emits exactly one `⏳ ... after 30 minutes ...`
  notice for that live barrier;
- `last_continuation_notice_key` identifies the judge reason for a normal
  CONTINUE notice. It must not be reused for parked or age notices.

On the evaluator path, a user or automatic turn enters
`evaluate_goal_snapshot()`. A held live barrier is staged by
`_stage_live_barrier()` before any judge call. A due age notice or six-hour
pause is placed on the isolated state, committed by the normal optimistic CAS,
and returned directly. The evaluator returns without incrementing the turn or
calling the judge. The CLI prints `decision["message"]`; the gateway defers
that message until visible response delivery; the TUI emits its goal status
update.

On the idle path, the gateway ticker enters
`GatewayGoalsMixin._goal_wakeup_fire_one()` and the TUI/session-owner poller
uses the same parked-goal check. They call `rearm_live_barrier()` first. A
successful re-arm persists the next deadline and, only for the first age
threshold, returns the age notice; gateway sends it through
`_send_goal_status_notice(..., notice_kind="wait-age")`, while TUI emits its
status update. The pure `is_waiting()`/`lifted_barrier_prompt()` checks never
write state. A notice already committed by the evaluator is suppressed by
`last_age_notice_key` on the idle scan, preventing a duplicate.

### Live-barrier lifecycle

Judge WAIT parsing is the only model-dependent park path. The real
`judge_goal()` parser accepts `wait_on_session`, `wait_on_pid`, or bounded
`wait_for_seconds`; only the LLM response is stubbed in tests. A session/pid
WAIT calls `wait_on_session()`/`wait_on()`, records `waiting_since`, and keeps
`waiting_until=0`. Timed WAIT uses `waiting_until` and is not subject to the
live-target age ceiling.

For a still-live session or pid:

1. before 30 minutes, the implicit first recheck is
   `waiting_since + _MAX_BARRIER_WAIT_S` and no age notice is emitted;
2. at the 30-minute probe, `rearm_live_barrier()` or `_stage_live_barrier()`
   emits one age notice and persists the next liveness deadline 5 minutes later;
3. subsequent due probes re-arm at 15 minutes and then 30 minutes, retaining
   the live barrier and never treating probe expiry as target completion;
4. at six hours (`_MAX_LIVE_BARRIER_S`), a still-live target is paused with a
   blocker naming the pid/session. This transition is durable and bypasses the
   judge; and
5. when liveness is false, the live barrier clears, the existing completion or
   receipt-based wake note is used, and the next continuation is admitted only
   after the normal busy/queue/session fences. `clear_lifted_wait(waiting_since)`
   is conditional, so a newer re-park cannot be erased.

CAS re-arm and clear operations compare the original `waiting_since`; a
concurrent pause, clear, or re-park wins and the idle scan retries against the
new durable row. Old goal rows deserialize missing fields as empty/zero defaults;
no schema migration is required.

## Test matrix

Every row below drives a production entry point. The real-path file stubs only
the auxiliary judge response, wall-clock values where exact deadlines matter,
and pid/session liveness. Existing direct-manager tests remain supporting
coverage; they are not evidence for the rows below.

| Behaviour | Production entry point | Test name | Uses real path | Mocks allowed |
| --- | --- | --- | --- | --- |
| JSON tool arguments make three changing status polls back off | `SessionDB.append_message` → `collect_goal_evidence` → CLI `_maybe_continue_goal_after_turn` → `evaluate_after_turn` | `test_real_session_evidence_drives_three_status_continuations_to_backoff` | yes | judge response |
| Evidence storage shape is JSON and parsed before classification | `SessionDB.append_message` → `collect_goal_evidence` | `test_real_collector_preserves_json_argument_representation` | yes | none |
| `&&`, `||`, `;`, backticks, `$(`, `-exec`, `-execdir`, and `-ok` are rejected | same CLI/evaluator path with real terminal rows | `test_real_evidence_classifier_rejects_shell_operators` | yes | judge response |
| Passing quality-gate rows do not reset no-progress state | CLI post-turn hook → real gate execution → evaluator fingerprint | `test_real_passing_quality_gate_rows_do_not_reset_no_progress` | yes | judge response |
| Plain, contract, subgoal, and quality-gate continuation templates are synthetic | `GoalManager.next_continuation_prompt` / real `_check_gates` → CLI and gateway provenance matchers | `test_real_continuation_builders_are_synthetic_to_cli_and_gateway` | yes | none |
| Judge WAIT session park uses real JSON parsing and durable barrier | CLI post-turn hook → `judge_goal` → `_apply_wait_directive` | `test_real_judge_wait_then_evaluator_age_notice_reaches_cli_user` | yes | judge response, session liveness |
| Evaluator-path 30-minute age notice reaches the user once | `_stage_live_barrier` → evaluator decision → CLI output | `test_real_judge_wait_then_evaluator_age_notice_reaches_cli_user` | yes | judge response, session liveness |
| Idle-path 30-minute age notice reaches the user once | `rearm_live_barrier` (called by gateway/TUI idle poller) | `test_real_judge_wait_then_idle_rearm_delivers_age_notice` | yes | judge response, session liveness |
| Live barrier rechecks at 5/15/30 minutes | `rearm_live_barrier` after real judge WAIT | `test_real_judge_wait_rearms_live_barrier_at_five_fifteen_and_thirty_minutes` | yes | judge response, wall clock, session liveness |
| Six-hour live target pauses without a second judge | evaluator live-barrier stage | `test_real_judge_wait_at_six_hours_pauses_without_a_second_judge` | yes | judge response, wall clock, session liveness |
| Exited target clears the barrier and resumes judging | evaluator live-barrier stage → normal judge | `test_real_exited_target_lifts_barrier_for_a_continuation` | yes | judge response, session liveness |
| Gateway idle wake injects an admitted continuation after exit/receipt | `_goal_wakeup_fire_one` → `admit_internal_event` → `clear_lifted_wait` | `tests/gateway/test_goal_parked_idle_wake.py::test_watcher_resumes_goal_parked_on_restart_killed_process` | yes | process/session liveness |
| CAS loss preserves a concurrent re-park | `rearm_live_barrier` → `rearm_goal_barrier_if_since` | `tests/hermes_cli/test_goal_external_wait_backoff.py::test_live_barrier_rearm_respects_cas_loss` | yes | session liveness |

Legacy tests that patch `collect_goal_evidence`, call `evaluate_after_turn`
with a forced provenance default, or call `wait_on_session()` directly remain
useful for narrow state-machine assertions, but cannot replace their paired
real-path rows in this matrix.

## Verification

Expected-red design reset command:

```text
TMPDIR=~/.cache/hermes-s2-tmp .venv/bin/python -m pytest -q -p no:cacheprovider tests/hermes_cli/test_goal_external_wait_backoff_real_path.py
```

On the pre-fix head, the real-path file is intentionally red for the P1 JSON
argument classification, the P2 continuation provenance prefix, and the P2
evaluator age-notice delivery. The remaining matrix tests provide green
coverage for the already-correct paths and keep the expected behavior explicit.
Production code is intentionally unchanged in this design/test phase.

## Retirement and rollback

Retire when a released upstream implementation satisfies the complete contract
and passes the focused regressions. Rollback is a source revert of this patch's
doc/tests commit; the durable fields remain optional and old rows remain
readable.
