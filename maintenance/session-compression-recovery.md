# Same-session compression recovery

Load this unit when changing hard-window recovery, preflight input admission,
archive-backed request projection or the gateway exhaustion boundary.

## Required behavior

Compression failure is not `/new`: never clear the conversation scope, replace
the durable session, rebind the Telegram topic, or copy a title onto a blank
successor. Preserve exact title/icon/manual provenance, settings, accepted
instructions, budget counters and stop state on the original identity.

Flush the accepted input with its platform ID through the existing native
turn-start flush before risky compaction. History and logical input remain
recoverable on a typed early exit or process restart. No second inbox, archive,
context engine, task store, service or provider client is introduced.

The fail-closed admission (`admit_before_compaction`) runs only when the turn
may compact: compression enabled and either the idle-compaction gap reached or
the cheap preflight pre-check (`_should_run_preflight_estimate`) passing. Those
are exactly the turns with a compaction early exit that could otherwise lose the
input. Other turns keep the ordinary log-and-continue turn-start flush, so a
transient storage error does not fail a small turn with "Input checkpoint could
not be acknowledged".

### Queue checkpoint lifecycle

A queued (FIFO) human input is checkpointed in the native pending spool before
it is acknowledged. The checkpoint is retired by its own path, never by a
session-id lookup, at exactly one of these points:

- consumed: the agent's turn-start flush durably admits the input and fires the
  one-shot, turn-scoped `_input_admitted_callback` the gateway installs for the
  consumed event (`notify_input_admitted`); proxy turns retire on return;
- discarded: `/stop` dropping a parked follow-up, `/new`/reset/resume clearing
  the overflow (`_clear_conversation_scope`), adapter cancel/stale-lock heal
  (`_discard_pending_message`), a pending slash command refused as agent input,
  or a follow-up merged into the head's refreshed checkpoint;
- expired: `QUEUE_CHECKPOINT_MAX_AGE_SECONDS` (7 days) bounds any crash window
  between admission and retirement;
- unauthorized owner at recovery: quarantined to `*.bad`, never replayed.

Goal continuations and heartbeat ticks are not checkpointed: goal state and the
heartbeat poller re-derive them. Older goal-continuation checkpoints are dropped
at recovery. Internal completions (delegation/watch notifications) are
claim-settled at admission, so their checkpoint is their only durable copy and
stays until consumed. The same holds for human input without a platform id
(leftover `/steer`): retirement is by path and does not need an id.

After the configured native auxiliary summarizer/fallback route cannot fit a
hard-window request, a no-network mechanical candidate may excerpt oversized
tool observations and discard prior-turn thinking. Preserve human instructions,
assistant visible receipts, tool call arguments and active-turn reasoning
verbatim. Every excerpt identifies its retained original session/message row;
use `session_search(session_id=..., around_message_id=...)` to locate it. Full
content remains in SessionDB and the native session exporter. Do not replay
completed effects, grant a summary authority, or silently clip giant mandatory
user/system instructions. Requests include system/tools/media estimates,
selected/ephemeral output cap and estimator/transport safety headroom.

Install candidates only through native `archive_and_compact`, its existing
lease/fence/watermark and turn-tail reconciliation. Explicit stop, lock
contention, required-checkpoint failure and unfittable mandatory context must
pause intact. A late summarizer cannot overwrite an admitted recovery boundary.
Do not rewrite global/saved model selections or auxiliary settings.

## Independent diagnosis and prior art

Fork-Patch identity: `session-compression-recovery`.

The reported boundary was not an executed summarizer timeout. The preserved
2026-10-12 17:28:41 gateway log records `352,895 >= 204,000` preflight pressure,
then `compression worker pool saturated (max_pending=16); skipping automatic
compression`, then the no-progress guard's `352,895 vs 272,000` wording under the
shared timeout exception. There was no before/after summary at this boundary.
The original input was then excluded by the typed early-return path and the
gateway deliberately reset the conversation and cleared scoped overrides.

The export of the original session contains 11,126 durable rows, of which 397
were active in that preserved snapshot. Active content character totals: user
324,605; assistant 1,683; tool 278,816. Additional assistant replay metadata is
large (199,890 reasoning-details characters; 74,255 Codex-reasoning-item
characters). This later preserved snapshot estimates 289,679 tokens without
tools/system; it is not the exact 352,895 request. Tool excerpts alone still
estimate 277,810, so thinking replay must be bounded too. Do not invent a
successful summarizer outcome or confuse this snapshot with the incident wire.

Independent hypothesis was frozen before read-only upstream comparison.
Upstream main checked: `63d0b94120027d487c9a5235b9ec64b696af1b26`.
Open [#137566](https://github.com/NousResearch/hermes-agent/pull/137566) bounds
safe input replay but still resets onto a fresh identity; it is not this contract.
Open [#113028](https://github.com/NousResearch/hermes-agent/pull/113028) bounds
protected tails while preserving tool groups. Closed, unmerged
[#103088](https://github.com/NousResearch/hermes-agent/pull/103088) covers hard
ceiling fallback independently of scheduling. Open
[#125141](https://github.com/NousResearch/hermes-agent/pull/125141) provides bounded
MCP excerpts. None is proof of released equivalent behavior.

## Verification

Use `bash scripts/run_tests.sh`; no direct pytest or machine-slot bypass.
Focused tests: `tests/agent/test_same_session_recovery.py`,
`tests/gateway/test_auto_reset_clean_context.py`,
`tests/agent/test_same_session_recovery_gaps.py`,
`tests/gateway/test_same_session_queue_checkpoint.py`,
`tests/gateway/test_queue_checkpoint_retirement.py` and
`tests/e2e/core/compaction/test_same_session_recovery.py`.
Ordinary FIFO acceptance writes the native private pending spool with file and
directory fsync before memory acknowledgement. Such a spool is not expired or
consumed by restart replay as a naked transcript row; it transfers to the native
transcript only after the matching platform id is durably admitted. A platform id
is admission dedupe, never proof of a completed reply or effect.
Also exercise native preflight, in-place/commit rollback, tail watermark,
compression fallback/attempt lifecycle, pending-input dedupe and isolated
loopback-provider/kill9 compaction suites. Local recording fake-provider traffic
is simulated provider evidence, not authenticated fallback or real Telegram.
The parent owns candidate review, exact merge, native source updater, live
adoption and Telegram inbound/send/readback/restart canary. Source or a healthy
process alone is not end-to-end completion.

## Retirement and rollback

Retire only when a released upstream version meets the full same-session,
lossless admission, bounded complete-request and native-fault contract. Revert
this patch as a source unit, retain all SessionDB rows/archives and recovery
exports, and use the authorized native updater to select a verified previous
release. No persistent schema migration, data deletion or config rollback is
needed. A source rollback restores the old unsafe auto-reset behavior, so pause
oversized-topic traffic until the parent confirms a safe route. Never restart,
deploy or roll back from a source worker.
