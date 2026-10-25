# Slack Progress Card Recovery

Load when changing native Slack task-card publication, retry disposition, or fallback titles.

## Required Behavior

Retry transient append failures using the existing stream timestamp and latest projection. An idle tool must not prevent retry. Honor Retry-After. A start timeout or missing timestamp is ambiguous, so do not automatically restart or emit a second fallback card. Disable the SDK connection retry for startStream only, without mutating the shared client. A positive rate-limit rejection permits a safe later start. Permanent rejection logs the cause and falls back to human-readable tool labels without command or code previews.

## Provenance And Patches

Fork patch identity: `slack-progress-card-recovery`. Base `fdcba2ab110d` ignored SendResult.retryable and permanently latched native_failed. Core publication owns the repair. An extension cannot restore a native stream after that latch. Existing destination suppression and cleanup behavior remain intact. Related upstream #137803 covers explicit stream expiry rather than transient transport failures.

## Verification

Run `scripts/run_tests.sh tests/gateway/test_slack_task_card_recovery.py tests/gateway/test_decline_fallback_suppression.py tests/gateway/test_run_progress_topics.py tests/gateway/test_task_card_destination_preflight.py tests/gateway/test_slack_native_streaming.py tests/gateway/test_stream_final_contract.py -q --file-retries 0`. The 12-case regression fails on the base production modules. Per-conversation aggregate outbound admission remains an existing limitation, documented in the patch register.

## Retirement And Rollback

Retire when upstream supplies equivalent recovery and presentation coverage. Revert the patch commit, its matching tests, index row and register entry to roll back. No stored state, schema or configuration migration.
