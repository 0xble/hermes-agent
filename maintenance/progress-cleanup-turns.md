# Per-turn progress cleanup

## Required behavior

Post-delivery callbacks for overlapping queued turns retain independent generation ownership. A newer registration never overwrites an older pending turn. Same-generation callbacks chain in order, stale new registrations are rejected, and generation-less legacy callbacks remain supported. The adapter snapshots the handler's generation before subsequent delivery awaits can rebind the shared session event.

With `cleanup_progress: true`, temporary progress deletion is registered before pending-message inspection and awaited after the first response reaches the chat, before the next queued turn starts. A refused first response keeps the progress breadcrumbs and the queued event for retry. Deletion follows the live replacement adapter while callback ownership stays with the original adapter. Failed agent runs keep their progress.

## Provenance and patches

Fork patch identity: `progress-cleanup-turns`.

Ported/adapted from archived HERMES-086 and HERMES-096 (gateway-delivery unit), originally implemented in the archived fork's `gateway/platforms/base.py` and `gateway/run.py`. The current gateway split the latter into `gateway/run_turn.py` and `gateway/turn_context.py`; the callback registry and run-boundary logic were adapted to those owners. Upstream issue [#100061](https://github.com/NousResearch/hermes-agent/issues/100061) and our existing [PR #100133](https://github.com/NousResearch/hermes-agent/pull/100133) track the upstream contribution. Related PR #100125 overlaps the awaitable deletion but not the complete queued delivery contract.

## Verification

Both current fork main and current upstream main failed the queued-handoff callback and queued-progress-deletion regressions before this patch: the former fired only the newer callback; the latter started turn two with zero deletions. `scripts/run_tests.sh tests/gateway/test_post_delivery_callback_chaining.py tests/gateway/test_run_cleanup_progress.py tests/gateway/test_status_command.py tests/gateway/test_run_progress_topics.py tests/gateway/test_active_session_text_merge.py -j 6 -q` exercises callback ownership, deletion order, and existing gateway progress behavior.

## Retirement and rollback

Retire when a selected upstream release preserves both generations' callbacks across queued handoff and awaits per-turn progress cleanup before follow-ups, including failure and adapter replacement, with the regression passing on that release. Revert the scoped patch and remove its tests and this unit; no persisted schema or config changes are involved. Source publication does not restart or promote the live gateway.
