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

The frozen hypothesis is: repeated automatic judge WAITs can spend one turn every
minute even when the external condition is unchanged, because timed WAITs clamp
to the 60-second minimum, `consecutive_no_progress` only advances on CONTINUE,
and `clear_wait()` erases the parked-notice identity during an idle lift. The
same boundary also needs explicit lifecycle coverage for live pid/session waits,
delegation waits, and every CLI, gateway, and TUI wake surface. A core patch is
required because this behavior spans judge semantics, durable goal state, barrier
liveness, and idle wake admission; instructions or a plugin cannot atomically own
those boundaries.

Related upstream PRs reviewed before implementation: #106925, #118705,
#129380, and #107130. None supplied a released equivalent for the complete
external-wait, no-progress, and live-barrier contract.

## Design

### Production entry points and turn provenance

`GoalManager.evaluate_after_turn()` is the shared evaluator entry point. The CLI
calls it from `CLILoopsMixin._maybe_continue_goal_after_turn`; the gateway calls
it from `GatewayGoalsMixin._post_turn_goal_continuation`; and the TUI calls it
from `tui_gateway.prompt_turn._goal_followup_after_turn` after
`_dispatch_followup_turn` routes the follow-up through
`_run_prompt_submit(user_turn=False)`. Each caller must pass the provenance of
the turn that just completed, rather than letting the evaluator infer it from
prose or queue state:

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
accepted by their explicit tool-name allowlist. The judge-facing evidence ledger
remains capped at `_EVIDENCE_MAX_ENTRIES`, but no-progress classification reads a
separate complete per-turn slice before that cap, filtered by `timestamp >
previous_turn_at`. The per-turn scan is bounded; if it may have omitted rows from
the current turn, classification fails closed as actionable rather than treating
an incomplete read-only suffix as proof of no progress. Quality-gate rows are
excluded from the progress fingerprint, while still being shown to the judge.

For an automatic CONTINUE, empty evidence or evidence consisting only of
approved read-only/status operations increments the shared no-progress streak. A
write/actionable result or a real user turn resets it. Explicit pause and resume
also reset `consecutive_no_progress` and `backoff_level`, because they are user
lifecycle actions and the next automatic continuation starts a fresh streak. Three
consecutive qualifying turns call `_no_progress_wait()`, persist a timed barrier,
and return `should_continue=False`; the backoff waits are 5, 15, and 30 minutes
(the last is bounded by `_MAX_BARRIER_WAIT_S`). Judge WAITs use the same streak
state rather than a second counter. A timed WAIT's effective duration is the
bounded maximum of the judge request and the current escalation floor: automatic
turns impose a 300-second floor and use 300 → 900 → 1800 seconds as
`backoff_level` advances; user turns keep the ordinary 60-second minimum. Active
delegations impose an additional 600-second floor, with every timed wait capped
at 1800 seconds. A user turn or new actionable evidence resets the shared streak
and `backoff_level`. Judge WAITs and CONTINUE-triggered no-progress parks therefore
share one durable escalation path, including mixed CONTINUE/WAIT sequences. The
parked key for delegation waits is `delegations|reason:<r>` and excludes the active
count and seconds. Judge wording and changing status output do not convert a
read-only poll into progress.

### Repeated judge WAITs

Only automatic turns participate in judge-WAIT escalation. The effective wait is
computed before parking: 300 seconds for the first no-new-evidence WAIT, then
900, then 1800 seconds for later WAITs in the same shared no-progress streak.
The judge's requested `wait_for_seconds` remains bounded to 60..1800 for the
ordinary path, but it cannot reduce this automatic no-new-evidence floor or the
current escalation level. A user turn or a new actionable evidence fingerprint
resets the shared streak and allows a later WAIT to start again at 300 seconds.

The timed parked-notice identity is `target-type + reason`: `timed|reason:<r>`
for timed waits, `session:<id>|reason:<r>` for session waits, and
`pid:<pid>|reason:<r>` for pid waits. It never includes the deadline,
`waiting_seconds`, or the escalated duration. The identity survives every timer
lift path—evaluator expiry before `_evaluate_after_turn`, conditional
`clear_lifted_wait`/`clear_goal_wait_if_since`, and gateway or TUI idle wakes—when
the next timed WAIT has the same reason. `clear_wait()` may clear
`last_wait_notice_key` only for a non-WAIT verdict (including CONTINUE), a user
turn, pause, resume, done, or a changed target/reason; a timer lift preserves
the key when re-parking would reuse it. A changed target or reason starts a new
notice. In particular, a CONTINUE verdict must clear the key so a later
same-reason WAIT is announced again.

A judge WAIT on a pid or session uses the live-barrier lifecycle: the target
stays armed through its 5/15/30-minute rechecks, emits one age notice, pauses at
the six-hour ceiling if still live, and lifts only when the target exits. A
judge WAIT with active delegations is a timed delegation barrier: it parks for
at least ten minutes, records the active count, and lifts early when that count
decreases. Neither lifecycle spends a judge turn while its barrier still holds.

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

The grammar rejects shell composition and execution syntax even when an allowed
prefix appears first: newline, `>`, `>>`, `|`, `&&`, `||`, `;`, backticks, `$(`,
`&`, `<(`, `>(`, and `find` `-exec`, `-execdir`, `-ok`, `-fprint`, and
`-fprint0`. It also rejects `rg --pre`, `--output`, git `--ext-diff` and
`--textconv`, destructive options such as `-delete`, `--delete`,
branch deletion, redirects, and any command that does not consume the complete
argument string. The parser consumes the complete JSON argument representation,
extracts `command`, and applies the grammar only to that value; truncated,
malformed, or redacted shell calls are actionable by default.

### Parked, age, continuation, and delivery notice keys

These durable keys have separate domains:

- `last_wait_notice_key` identifies the parked barrier (`session`, `pid`, or
  `timed`/delegation target plus reason). For timed judge waits it is stable
  across deadline and backoff changes; it deduplicates the initial judge-WAIT
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
`GatewayGoalsMixin._goal_wakeup_fire_one()`, the TUI/session-owner poller enters
`_maybe_resume_tui_parked_goal()` → `_notif_loop_status`, and the CLI process loop
enters `_maybe_resume_parked_goal()`. They call `rearm_live_barrier()` first. A
successful re-arm persists the next deadline and, only for the first age
threshold, returns the age notice; gateway sends it through
`_send_goal_status_notice(..., notice_kind="wait-age")`, while TUI and CLI emit
their status/console update. The pure `is_waiting()`/`lifted_barrier_prompt()`
checks never write state. A notice already committed by the evaluator is
suppressed by `last_age_notice_key` on every idle scan, preventing a duplicate.

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
| Per-turn evidence keeps a write actionable after nine read-only calls and across four automatic turns | `SessionDB.append_message` → uncapped per-turn `collect_goal_evidence` → evaluator classifier | `test_real_write_survives_eight_read_only_results_per_automatic_turn` | yes | judge response |
| Evidence storage shape is JSON and parsed before classification | `SessionDB.append_message` → `collect_goal_evidence` | `test_real_collector_preserves_json_argument_representation` | yes | none |
| `&&`, `||`, `;`, backticks, `$(`, `-exec`, `-execdir`, and `-ok` are rejected | same CLI/evaluator path with real terminal rows | `test_real_evidence_classifier_rejects_shell_operators` | yes | judge response |
| `find -fprint0`, git `--ext-diff`, and git `--textconv` are rejected as actionable | same CLI/evaluator path with real terminal rows | `test_real_evidence_classifier_rejects_shell_operators` | yes | judge response |
| Passing quality-gate rows do not reset no-progress state | CLI post-turn hook → real gate execution → evaluator fingerprint | `test_real_passing_quality_gate_rows_do_not_reset_no_progress` | yes | judge response |
| Actionable SessionDB evidence or a real user turn resets the shared WAIT/CONTINUE streak | `SessionDB.append_message` / `_tui_process_one_input` → evaluator streak update | `test_real_actionable_evidence_and_user_turn_reset_shared_wait_streak` | yes | judge response |
| Pause and resume reset a level-2 no-progress park before one automatic read-only status turn | CLI evaluator path → `GoalManager.pause()` / `resume()` → evaluator classifier | `test_real_pause_resume_resets_no_progress_backoff_before_status_turn` | yes | judge response |
| Four repeated timed judge WAITs lift through the CLI idle path with 300 → 900 → 1800 → 1800 seconds and one parked notice | `evaluate_after_turn` → `_apply_wait_directive` → `_maybe_resume_parked_goal` → `clear_lifted_wait` | `test_real_repeated_judge_waits_use_idle_lift_backoff_and_one_notice` | yes | judge response, wall clock |
| Timed WAIT expiry before the next evaluator turn preserves the parked key, advances 300 → 900, and emits exactly one parked notice | evaluator `_evaluate_after_turn` → `_apply_wait_directive` | `test_real_timed_wait_expiry_reparks_through_evaluator_with_one_notice` | yes | judge response, wall clock |
| Full-string evidence grammar rejects newline, `&`, process substitution, `--output`, `rg --pre`, `find` print actions, and truncated/unparseable JSON | `SessionDB.append_message` → `collect_goal_evidence` → CLI evaluator classifier | `test_real_evidence_classifier_rejects_shell_operators` + `test_real_truncated_terminal_arguments_fail_closed_as_actionable` | yes | judge response |
| CONTINUE and WAIT share one durable no-progress streak and `backoff_level`; mixed turns use the 300/900 ladder and a requested 1200-second wait is honored | evaluator → `_apply_wait_directive` / `_no_progress_wait` | `test_real_judge_wait_mixed_continue_and_wait_uses_shared_backoff_formula` | yes | judge response |
| Judge WAIT with active delegations uses the 600-second floor, 1800-second cap, and `delegations|reason:<r>` notice key | evaluator → `_apply_wait_directive` → delegation barrier | `test_real_judge_wait_with_active_delegations_uses_delegation_floor_and_key` | yes | judge response, delegation count |
| Timed WAIT notice identity excludes deadline and escalated seconds and survives lift/re-park | `_wait_notice_key` → `clear_wait` → `_apply_wait_directive` | same repeated-WAIT test plus direct state assertions | yes | judge response, wall clock |
| Plain, contract, subgoal, and quality-gate continuation templates are synthetic | `GoalManager.next_continuation_prompt` / real `_check_gates` → CLI and gateway provenance matchers | `test_real_continuation_builders_are_synthetic_to_cli_and_gateway` | yes | none |
| Gateway synthetic continuation, gate-failed continuation, and idle-wake event reach the evaluator as automatic | `GatewayRunner._run_post_turn_hooks` → `_is_user_turn_event` → `_post_turn_goal_continuation` | `test_real_gateway_post_turn_hooks_mark_continuation_gate_failure_and_idle_wake_automatic` | yes | judge response |
| CLI input provenance is resolved by the TUI entry point, not a preset flag | `_tui_process_one_input` → `_tui_after_turn` → `_maybe_continue_goal_after_turn` | `test_real_cli_tui_input_provenance_reaches_goal_evaluator_as_automatic` | yes | judge response, runtime shell/UI |
| TUI follow-up dispatch passes `user_turn=False` into `_goal_followup_after_turn` | `_dispatch_followup_turn` → `_run_prompt_submit` → `tui_gateway.prompt_turn._goal_followup_after_turn` | `test_real_tui_followup_dispatch_reaches_goal_followup_as_automatic` | yes | judge response, turn submit |
| Judge WAIT session park uses real JSON parsing and durable barrier | CLI post-turn hook → `judge_goal` → `_apply_wait_directive` | `test_real_judge_wait_then_evaluator_age_notice_reaches_cli_user` | yes | judge response, session liveness |
| Evaluator-path 30-minute age notice reaches the user once | `_stage_live_barrier` → evaluator decision → CLI output | `test_real_judge_wait_then_evaluator_age_notice_reaches_cli_user` | yes | judge response, session liveness |
| Gateway idle age notice is delivered with `notice_kind="wait-age"`, deduped on a second due scan, and suppressed after evaluator commit | `_goal_wakeup_fire_one` → `rearm_live_barrier` → `_send_goal_status_notice` | `test_real_gateway_idle_age_notice_delivery_dedupes_on_second_due_scan` | yes | session liveness, capturing adapter |
| TUI idle age notice reaches `_notif_loop_status` once | `_maybe_resume_tui_parked_goal` → `_notif_loop_status` | `test_real_tui_idle_age_notice_reaches_status_and_dedupes` | yes | session liveness, status sink |
| CLI idle age notice reaches the console once | `_maybe_resume_parked_goal` → CLI status output | `test_real_cli_idle_age_notice_reaches_user_and_dedupes` | yes | session liveness, console sink |
| Live barrier rechecks at 5/15/30 minutes and `lifted_barrier_prompt()` stays `None` while target is alive | `rearm_live_barrier` → `lifted_barrier_prompt` | `test_real_exited_after_rearm_lifts_only_after_live_barrier_rechecks` | yes | wall clock, session liveness |
| Exited target after at least one re-arm admits exactly one idle continuation and clears only that wait | CLI idle hook → `lifted_barrier_prompt` → `clear_lifted_wait` | `test_real_exited_after_rearm_lifts_only_after_live_barrier_rechecks` | yes | session liveness, console sink |
| Six-hour live target pauses without a second judge | evaluator live-barrier stage | `test_real_judge_wait_at_six_hours_pauses_without_a_second_judge` | yes | judge response, wall clock, session liveness |
| A live delegation plus real SessionDB status evidence parks an automatic no-action CONTINUE for ten minutes and lifts when one returns | `SessionDB.append_message` → CLI post-turn hook → real `count_active_delegations` → `_delegation_no_progress_wait` → CLI idle hook | `test_real_delegation_no_progress_uses_sessiondb_evidence_and_lifts_early` | yes | judge response, delegation registry lifecycle |
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

On the pre-fix head, the exact real-path result is **8 failed, 19 passed** in
27 collected cases (`8 failed, 19 passed in 176.31s (0:02:56)`). The eight red
cases are intentional acceptance blockers:

- `test_real_repeated_judge_waits_use_idle_lift_backoff_and_one_notice`: every
  timed judge WAIT remains 60 seconds and reposts its parked notice instead of
  using 300 → 900 → 1800 → 1800 seconds with one notice;
- `test_real_session_evidence_drives_three_status_continuations_to_backoff`:
  JSON terminal arguments are not parsed before classification, so read-only
  status polls are treated as actionable and the no-progress streak stays 0;
- `test_real_continuation_builders_are_synthetic_to_cli_and_gateway`: the
  quality-gate continuation prefix is not recognized as synthetic;
- `test_real_gateway_post_turn_hooks_mark_continuation_gate_failure_and_idle_wake_automatic`:
  the gate-failed gateway event reaches the evaluator as `user_initiated=True`;
- `test_real_gateway_idle_age_notice_delivery_dedupes_on_second_due_scan` and
  `test_real_tui_idle_age_notice_reaches_status_and_dedupes`: an age notice
  committed by idle re-arm was vulnerable to a second delivery through the
  evaluator's transient notice field, while the parked key could also suppress
  the evaluator's own age delivery. Age notices now have an independent durable
  key, idle re-arm returns the notice directly without retaining it for a later
  evaluator pass, and the second idle-scan fixtures reload a fresh durable row
  before forcing a due scan so they do not overwrite CAS-written state;
- `test_age_notice_does_not_repost_parked_notice`: after idle re-arm commits the
  age key, an automatic evaluator pass must not replay the 30-minute notice;
- `test_judge_repark_after_continue_reannounces`: a CONTINUE verdict clears the
  parked key, so a later same-reason WAIT announces its new park;
- `test_real_delegation_no_progress_uses_sessiondb_evidence_and_lifts_early`:
  despite a real active delegation count of 1, the JSON status evidence was
  misclassified, so the automatic CONTINUE did not enter the ten-minute
  delegation wait;
- `test_real_judge_wait_then_evaluator_age_notice_reaches_cli_user`: an aged
  live barrier returned the generic parked notice instead of the 30-minute age
  notice.

The full collected set that must turn green is:

- `test_real_actionable_evidence_and_user_turn_reset_shared_wait_streak`;
- `test_real_session_evidence_drives_three_status_continuations_to_backoff`;
- `test_real_collector_preserves_json_argument_representation`;
- all eight cases of `test_real_evidence_classifier_rejects_shell_operators`;
- `test_real_passing_quality_gate_rows_do_not_reset_no_progress`;
- `test_real_continuation_builders_are_synthetic_to_cli_and_gateway`;
- `test_real_repeated_judge_waits_use_idle_lift_backoff_and_one_notice`;
- `test_real_gateway_post_turn_hooks_mark_continuation_gate_failure_and_idle_wake_automatic`;
- `test_real_cli_tui_input_provenance_reaches_goal_evaluator_as_automatic`;
- `test_real_tui_followup_dispatch_reaches_goal_followup_as_automatic`;
- `test_real_gateway_idle_age_notice_delivery_dedupes_on_second_due_scan`;
- `test_real_tui_idle_age_notice_reaches_status_and_dedupes`;
- `test_real_cli_idle_age_notice_reaches_user_and_dedupes`;
- `test_real_exited_after_rearm_lifts_only_after_live_barrier_rechecks`;
- `test_real_delegation_no_progress_uses_sessiondb_evidence_and_lifts_early`;
- `test_real_judge_wait_then_evaluator_age_notice_reaches_cli_user`;
- `test_real_judge_wait_then_idle_rearm_delivers_age_notice`;
- `test_real_timed_wait_expiry_reparks_through_evaluator_with_one_notice`;
- `test_real_judge_wait_mixed_continue_and_wait_uses_shared_backoff_formula`;
- `test_real_judge_wait_with_active_delegations_uses_delegation_floor_and_key`;
- `test_real_truncated_terminal_arguments_fail_closed_as_actionable`;
- `test_real_judge_wait_rearms_live_barrier_at_five_fifteen_and_thirty_minutes`;
- `test_real_judge_wait_at_six_hours_pauses_without_a_second_judge`;
- `test_real_exited_target_lifts_barrier_for_a_continuation`.

The implementation lives in `hermes_cli/goals.py`, `gateway/run_goals.py`,
`gateway/run_busy.py`, the CLI/TUI goal mixins and `tui_gateway/` notification paths,
and all of the regressions above pass against it.

## Retirement and rollback

Retire when a released upstream implementation satisfies the complete contract
and passes the focused regressions. Rollback is a source revert of every commit
carrying `Fork-Patch: goal-external-wait-backoff` (production code, tests and this
record together). The durable fields remain optional, so old rows stay readable.
