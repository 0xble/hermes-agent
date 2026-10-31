# Per-turn progress cleanup

## Required behavior

Post-delivery callbacks for overlapping queued turns retain independent generation ownership. A newer registration never overwrites an older pending turn. Same-generation callbacks chain in order, stale new registrations are rejected, and generation-less legacy callbacks remain supported. The adapter snapshots the handler's generation before subsequent delivery awaits can rebind the shared session event.

With `cleanup_progress: true`, temporary progress deletion is registered before pending-message inspection and awaited after the first response reaches the chat, before the next queued turn starts. A refused first response keeps the progress breadcrumbs and the queued event for retry. Deletion follows the live replacement adapter while callback ownership stays with the original adapter. Failed agent runs keep their progress.

Every turn the durable FIFO runs fires its own cleanup once its reply is sent, whichever path delivers it. A live native waiter's sender fires it: the first message's adapter lifecycle, or the observer of a follow-up sent while the session is busy (`dispatch_shared_busy`), which receives the turn's exact owner and generation from the drain (`fire_turn_post_delivery`). A waiter that stops waiting has the cleanup fired when its turn settles. A turn with no live waiter (producer wakes, recovered turns) has the drain fire it after `deliver_settled`.

A turn superseded by a newer run generation drops its deferred notices but still deletes its cards: `_hmwa_discard_stale_result` runs only the parts of its callback marked `_temporary_cleanup` (`temporary_cleanups`). Firing a generation sweeps an older unfired generation only when no deliverer has claimed it (`claim_post_delivery`), so a slow reply's cards are never deleted by a newer turn ahead of that reply. A waiter that registers after its turn started gets the cleanup with the reply instead of the drain firing it first. A queued or interrupting follow-up's busy acknowledgement is temporary status: it is deleted once that follow-up's own reply is delivered, or when an in-process follow-up's turn starts (`release_temporary_notices`). Steer and redirect acknowledgements, and an acknowledgement carrying the one-time busy-input hint, stay.

## Provenance and patches

Fork patch identity: `progress-cleanup-turns`.

Ported/adapted from archived HERMES-086 and HERMES-096 (gateway-delivery unit), originally implemented in the archived fork's `gateway/platforms/base.py` and `gateway/run.py`. The current gateway split the latter into `gateway/run_turn.py` and `gateway/turn_context.py`; the callback registry and run-boundary logic were adapted to those owners. Upstream issue [#100061](https://github.com/NousResearch/hermes-agent/issues/100061) and our existing [PR #100133](https://github.com/NousResearch/hermes-agent/pull/100133) track the upstream contribution. Related PR #100125 overlaps the awaitable deletion but not the complete queued delivery contract.

## Verification

Both current fork main and current upstream main failed the queued-handoff callback and queued-progress-deletion regressions before this patch: the former fired only the newer callback; the latter started turn two with zero deletions. `scripts/run_tests.sh tests/gateway/test_post_delivery_callback_chaining.py tests/gateway/test_run_cleanup_progress.py tests/gateway/test_status_command.py tests/gateway/test_run_progress_topics.py tests/gateway/test_active_session_text_merge.py -j 6 -q` exercises callback ownership, deletion order, and existing gateway progress behavior.

`scripts/run_tests.sh tests/gateway/test_turn_bubble_cleanup.py tests/gateway/test_drain_post_delivery_cleanup.py` is the behaviour contract for every turn path: the adapter lifecycle, busy follow-ups, producer wakes the drain delivers, a waiter that stopped waiting, a waiter released before its turn ended, /stop cancelling the adapter task, a shutdown release, an unknown settlement, a late waiter, a superseded turn, a newer turn's sweep during a slow reply, and the queued follow-up's busy acknowledgement. Every test also fails on a double fire. Before the busy-follow-up fix (2026-10-31) the follow-up's cleanup never fired, which left its tool cards in the chat.

## Retirement and rollback

Retire when a selected upstream release preserves both generations' callbacks across queued handoff and awaits per-turn progress cleanup before follow-ups, including failure and adapter replacement, with the regression passing on that release. Revert the scoped patch and remove its tests and this unit; no persisted schema or config changes are involved. Source publication does not restart or promote the live gateway.
