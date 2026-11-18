# Slack Progress Card Recovery

Load when changing native Slack task-card publication, retry disposition, or fallback titles.

## Required Behavior

Retry transient append failures using the existing stream timestamp and latest projection. An idle tool must not prevent retry. Honor Retry-After. A start timeout or missing timestamp is ambiguous, so do not automatically restart or emit a second fallback card. Disable implicit SDK retries for adapter clients so every explicit retry re-enters outbound admission. The stream start in `task_cards.py`, which also records the card in the cleanup ledger, disables retries on its client copy for injected clients. A positive rate-limit rejection permits a safe later start. Permanent rejection logs the cause and falls back to human-readable tool labels without command or code previews.

Turn cleanup intentionally cancels progress consumers. Cancellation drains queued events and publishes only if the drain changes the projection, the run is still current and the agent is not interrupted, subject to the existing publication cooldown. An empty queue does not trigger a retry. Both paths finalize the native card. The consumer may swallow cancellation and return normally, so a normal return alone does not prove idle recovery.

The Bolt app client, authenticated client and shallow copies share a workspace-bot-token budget at the SDK `api_call` seam for Slack's canonical `https://slack.com/api/` endpoint. Alternate API endpoints retain SDK transport behavior without Slack-specific pacing, cosmetic shedding or cooldown propagation. All current chat writes, reactions, thread status/title changes and file-publication calls share one 2-second channel gap across threads. Workspace/method spacing is 4 seconds except appendStream at 0.8 seconds. Real 429s honor method-scoped Retry-After and double spacing for 60 seconds after cooldown, then restore baseline spacing. Nonempty status and task-only cosmetic appends are shed under contention, retaining the latest task projection in existing recovery. Status clears and actual output wait for ordinary admission. A server cooldown above 10 seconds is returned to the caller with Retry-After instead of parking output. This cap never rejects routine spacing. This budget is in-process, not a coordinator across different bot tokens or processes.

## Provenance And Patches

Fork patch identity: `slack-progress-card-recovery`. Base `fdcba2ab110d` ignored SendResult.retryable and permanently latched native_failed. Core publication owns the repair. An extension cannot restore a native stream after that latch. Existing destination suppression and cleanup behavior remain intact. Related upstream #137803 covers explicit stream expiry rather than transient transport failures.

## Verification

Run `scripts/run_tests.sh tests/plugins/platforms/slack/test_outbound_budget.py tests/gateway/test_slack_task_card_recovery.py tests/gateway/test_decline_fallback_suppression.py tests/gateway/test_run_progress_topics.py tests/gateway/test_task_card_destination_preflight.py tests/gateway/test_slack_native_streaming.py tests/gateway/test_stream_final_contract.py -q --file-retries 0`. Idle-consumer coverage observes retryable publication failure and subsequent successful publication through events, ends the run on recovery and uses a non-cancelling outer deadline. It verifies no cancellation request, stream-timestamp reuse and the unchanged tool projection. Separate turn-end coverage gates cancellation after failed publication with an empty queue and an ineligible retry deadline, then verifies swallowed cancellation, stop and deletion without another append. These harness contracts are distinct from the original base-production recovery regressions. The shared-client transport regression first failed against the ungated SDK factory. Controlled-clock SDK tests count aggregate channel and workspace/method attempts, isolate workspaces/channels, exercise real SDK retries, and verify 429 adaptation expires. The patch register records published-limit arithmetic and the remaining live qualification boundary.

## Retirement And Rollback

Retire when upstream supplies equivalent recovery and presentation coverage. Revert the patch commit, its matching tests, index row and register entry to roll back. No stored state, schema or configuration migration.
