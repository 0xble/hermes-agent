# Slack Context Notices

## Contract

Fork patch identity: `slack-context-notices`.

FYI producers explicitly carry `notice_style="context"` to Slack. Single-chunk
notices whose converted mrkdwn fits 3,000 characters use a context block and a
formatting-stripped accessibility fallback. Larger content uses the existing
renderer. Unmarked replies and action-needed failures remain normal size.
Other platforms retain their existing content and metadata. Presentation must
not change notice visibility, privacy, persistence, or suppression policy.
Context notices never finalize a foreground stream. Native Slack finalization
also requires the consumer's existing draft identity in `metadata["_draft_id"]`
to match the open stream. Equal text, thread routing and `notify=True` do not
authorize a background or stale turn to commit a foreground stream.

Classification belongs to producers through `ContextNotice` or the existing
notice's `notice_style` field, carried by `notice_metadata`. Never infer it from
prose, notice-key prefixes, severity, or command names. `EphemeralReply` retains
its command identity, lifetime and presentation marker through unwrapping.
Both helpers live in `agent/notice_style.py`, outside `gateway/`, so storage and
agent producers never load the gateway package
(`tests/hermes_state/test_storage_import_boundary.py`).

## Classified Routes

| Producer/Route | Context | Full Size |
| --- | --- | --- |
| Goal/loop committed tool receipts, `agent/inline_tool_executors.py` | Successful persisted `notice` receipts | Errors, unpersisted results |
| Goal manager and command output, `hermes_cli/goals.py`, `hermes_cli/goal_command.py` | Active/parked status, set/revise/replace/remove/resume, achieved | Paused, blocked, persistence failures, authoring findings, waiting for user input |
| Session-control outcome producer/outbox, `gateway/run_session_controls.py` | Applied goal replace/resume/clear and loop resume/stop target receipts | Pause, pending approval, denied, expired, failed, unknown/uncommitted controls |
| Goal/heartbeat commands, `gateway/slash_commands_goals.py` | Successful subgoal add/remove/clear and heartbeat set/resume/clear | Status, pause, missing/no-op state, invalid input and persistence errors |
| Loop manager and command output, `hermes_cli/loops.py` | Set/revise/replace/clear/stop/resume, done, count cap completion | Safety cap pauses and failures |
| Agent lifecycle producers, `agent/status_output.py` | Explicitly marked fallback/recovery/compaction/lease/memory/review progress | Unmarked diagnostics and action-needed errors |
| Credit producers, `agent/credits_tracker.py` | Usage, grant spent, restored | Depleted credits and payment action. Unmarked credit notices stay full, even at info/success severity |
| Attended routing, `gateway/run_turn.py`, `gateway/run_turn_runner.py` | Switch to/from attended, successful automatic retries, attended unavailable while a working default continues | Failure prompts and dead subagents |
| Busy/delegation acknowledgement producer, `gateway/run_busy.py` | Queued, steered, redirected, interrupting, subagent-working and compressing acknowledgements, including optional onboarding hints | Unmarked busy replies, escalation, failures and requests for user action |
| Display/busy settings, `gateway/slash_commands.py` | Saved verbose, busy and footer receipts, retaining busy's `EphemeralReply` contract | Status, usage, unknown values, disabled controls and failed saves |
| Command producers, `gateway/slash_commands_model.py` | Model/reasoning/fast/personality/runtime setting confirmations | Invalid selections, failed saves, guard/approval prompts |
| Resume producer, `gateway/slash_commands_session.py` | Successful resume/already-current confirmations | Missing or disallowed target and empty selection lists |
| Background task producer, `gateway/slash_commands.py`, `gateway/run_turn.py` | Start receipt | Completion attribution and actual result in one send, execution errors |
| Cron-store transition producer, `gateway/cron_store_notices.py` | Recovered/writable-again receipt within the owning profile's language, home-channel route and warning opt-out | Unwritable/degraded warning. Scheduled-job reports remain real content |
| Startup/update producers, `gateway/run_notifications.py` | Online, restart complete, operation progress and successful update receipt | Failed updates and restart resend/recovery decisions |
| Moved-out-of-core producers, `hermes_cli/left_core_migration.py`, `hermes_cli/memory_provider_migration.py` | Successful installation receipts | Installation failures |

Native Slack task-progress cards are already native renderings. Scheduled-job
reports, delegation results, and background-task results remain real content,
not context-sized agent replies. Suppressed no-change ticks stay suppressed.
Unmarked plugin command strings and arbitrary plugin notices retain full-size
rendering, because their shared routes mix confirmations and action prompts.
Only their producers may opt into context styling. String-like marked plugin
command replies retain their marker at the gateway command boundary.
Session-mode/autopilot producer classification belongs to the separate
agents-plugin change, not this core patch.

`/goal resume extra-text` intentionally stays full-size: `_bare_control` adds
an ignored-argument usage hint that asks the user to correct their input. Do
not preserve a context marker merely to shrink that warning. Approval/security
policy controls such as `/yolo`, and all failure, approval, blocked, paused-for-user
or action-needed content stay full-size.

## Proof

Run `scripts/run_tests.sh` with `tests/gateway/test_slack_context_notices.py`
and `tests/gateway/test_slack_notice_coverage.py`, plus the related Slack,
goal/loop receipt, session-control watcher, ephemeral reply, warning conservation,
cron-health, heartbeat and busy-routing suites. Coverage drives actual settings
and autonomy handlers through command delivery, persisted control outcomes
through the outbox drain, scoped cron home-channel delivery, busy composition
and sent metadata, and real credit producers through the threaded callback.
Transport clients are faked, not formatters. Cached attended-unavailable coverage
drives runtime resolution, one-shot session consumption, the request-boundary
callback, and actual Slack payloads. The producer marks only the recoverable
working-default notice. Tests preserve diagnostic suppression, session isolation,
repeat-boundary deduplication, unchanged Telegram text, and full-size unmarked
credentials, login, and model-access prompts.

`tests/gateway/test_background_attribution_delivery.py` forces concurrent
completions and rejected sends through the actual background producer, requiring
one attributed payload per result. `tests/gateway/test_slack_native_streaming.py`
rejects equal-text finalization without the owning draft identity, including a
stale turn. Its real stream-consumer fixture posts an equal background body
without touching the foreground stream, then commits the owning final in place.

`tests/gateway/test_telegram_ingress_nonblocking.py` drives busy acknowledgements
through the native ingress-consumer boundary with a real `MessageEvent` and
`SessionSource`. Both unmarked and context-marked Telegram acknowledgements return
before the outbound budget opens and retain the same text and metadata.
`tests/gateway/test_restart_resume_admission_identity.py` uses native session states
and the replay scheduler to verify each interrupted resume marker is admitted
exactly once and reaches a successful terminal outcome. Do not replace the
runner's `_session_states()` method with a dictionary in this fixture, since that
breaks scheduler admission before a turn can start.

For checkouts nested beneath a real Hermes home, use the portable CI environment
(`bin/ci check --lane python`) rather than the machine's inherited home. Targeted
runs can use the same `scripts.ci.portable.environment` with `scripts/run_tests.sh`
and the candidate's already-provisioned `HERMES_PYTHON`. This keeps credentials,
runtime plugins and profile state out of qualification. Do not disable the
real-home I/O guard: a source payload-manifest probe outside the checkout can
otherwise be mistaken for profile I/O in a hidden-home worktree.

The footer handler's legacy argument route reads `event.message`, while native
`MessageEvent` supplies `text`. Coverage exercises native no-argument toggling
and separately the existing compatibility argument route. This presentation-only
patch does not change footer argument parsing or other platforms' behavior.

## Retirement

Retire when upstream has equivalent producer-owned metadata classification and
context rendering across sends and status edits. Never replace classification
with text matching in the Slack adapter.
