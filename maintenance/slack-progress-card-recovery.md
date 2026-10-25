# Slack Progress Card Recovery

Load when changing native Slack task-card publication, retry disposition, or fallback titles.

## Required Behavior

Retry transient append failures using the existing stream timestamp and latest projection. An idle tool must not prevent retry. Honor Retry-After. A start timeout or missing timestamp is ambiguous, so do not automatically restart or emit a second fallback card. Disable implicit SDK retries for adapter clients so every explicit retry re-enters outbound admission. The start helper also disables retries on its copy for injected clients. A positive rate-limit rejection permits a safe later start. Permanent rejection logs the cause and falls back to human-readable tool labels without command or code previews.

The Bolt app client, authenticated client and shallow copies share a workspace-bot-token budget at the SDK `api_call` seam. All current chat writes, reactions, thread status/title changes and file-publication calls share one 2-second channel gap across threads. Workspace/method spacing is 4 seconds except appendStream at 0.8 seconds. Real 429s honor method-scoped Retry-After and double spacing for 60 seconds after cooldown, then restore baseline spacing. Nonempty status and task-only cosmetic appends are shed under contention, retaining the latest task projection in existing recovery. Status clears and actual output wait for ordinary admission. A server cooldown above 10 seconds is returned to the caller with Retry-After instead of parking output. This cap never rejects routine spacing. This budget is in-process, not a coordinator across different bot tokens or processes.

## Provenance And Patches

Fork patch identity: `slack-progress-card-recovery`. Base `fdcba2ab110d` ignored SendResult.retryable and permanently latched native_failed. Core publication owns the repair. An extension cannot restore a native stream after that latch. Existing destination suppression and cleanup behavior remain intact. Related upstream #137803 covers explicit stream expiry rather than transient transport failures.

## Verification

Run `scripts/run_tests.sh tests/plugins/platforms/slack/test_outbound_budget.py tests/gateway/test_slack_task_card_recovery.py tests/gateway/test_decline_fallback_suppression.py tests/gateway/test_run_progress_topics.py tests/gateway/test_task_card_destination_preflight.py tests/gateway/test_slack_native_streaming.py tests/gateway/test_stream_final_contract.py -q --file-retries 0`. The 12-case recovery regression fails on the base production modules. The shared-client transport regression first failed against the ungated SDK factory. Controlled-clock SDK tests count aggregate channel and workspace/method attempts, isolate workspaces/channels, exercise real SDK retries, and verify 429 adaptation expires. The patch register records published-limit arithmetic and the remaining live qualification boundary.

## Retirement And Rollback

Retire when upstream supplies equivalent recovery and presentation coverage. Revert the patch commit, its matching tests, index row and register entry to roll back. No stored state, schema or configuration migration.
