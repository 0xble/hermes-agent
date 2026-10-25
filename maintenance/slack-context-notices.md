# Slack Context Notices

## Contract

Fork patch identity: `slack-context-notices`.

FYI producers explicitly carry `notice_style="context"` to Slack. Single-chunk
notices whose converted mrkdwn fits 3,000 characters use a context block and a
formatting-stripped accessibility fallback. Larger content uses the existing
renderer. Unmarked replies and action-needed failures remain normal size.
Other platforms retain their existing content and metadata. Presentation must
not change notice visibility, privacy, persistence, or suppression policy.
Context notices never finalize a foreground stream.

## Classified Routes

| Producer/Route | Context | Full Size |
| --- | --- | --- |
| Goal/loop committed tool receipts, `agent/inline_tool_executors.py` | Successful persisted `notice` receipts | Errors, unpersisted results |
| Goal manager and command output, `hermes_cli/goals.py`, `hermes_cli/goal_command.py` | Active/parked status, set/revise/replace/remove/resume, achieved | Paused, blocked, persistence failures, authoring findings, waiting for user input |
| Loop manager and command output, `hermes_cli/loops.py` | Set/revise/replace/clear/stop/resume, done, count cap completion | Safety cap pauses and failures |
| Agent lifecycle producers, `agent/status_output.py` | Explicitly marked fallback/recovery/compaction/lease/memory/review progress | Unmarked diagnostics and action-needed errors |
| Credit producers, `agent/credits_tracker.py` | Usage, grant spent, restored | Depleted credits and payment action |
| Attended routing, `gateway/run_turn_runner.py` | Switch to/from attended, successful automatic retries | Failure prompts and dead subagents |
| Command producers, `gateway/slash_commands_model.py` | Model/reasoning/fast/personality/runtime setting confirmations | Invalid selections, failed saves, guard/approval prompts |
| Resume producer, `gateway/slash_commands_session.py` | Successful resume/already-current confirmations | Missing or disallowed target and empty selection lists |
| Background task producer, `gateway/slash_commands.py`, `gateway/run_turn.py` | Start and successful completion receipt | Actual result body, execution errors |
| Startup/update producers, `gateway/run_notifications.py` | Online, restart complete, operation progress and successful update receipt | Failed updates and restart resend/recovery decisions |
| Moved-out-of-core producers, `hermes_cli/left_core_migration.py`, `hermes_cli/memory_provider_migration.py` | Successful installation receipts | Installation failures |

Native Slack task-progress cards are already native renderings. Scheduled-job
reports, delegation results, and background-task results remain real content,
not context-sized agent replies. Suppressed no-change ticks stay suppressed.
Unmarked plugin command strings and arbitrary plugin notices retain full-size
rendering, because their shared routes mix confirmations and action prompts.
Only their producers may opt into context styling. String-like marked plugin
command replies retain their marker at the gateway command boundary.

## Proof

Run `scripts/run_tests.sh tests/gateway/test_slack_context_notices.py` and the
Slack, goal/loop receipt, status callback, and command-delivery suites.

## Retirement

Retire when upstream has equivalent producer-owned metadata classification and
context rendering across sends and status edits. Never replace classification
with text matching in the Slack adapter.
