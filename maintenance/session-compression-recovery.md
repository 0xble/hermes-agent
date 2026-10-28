# Same-Session Compression Recovery

## Implemented Exhaustion Boundary

Compression exhaustion is not `/new`. The gateway retains the current session,
its transcript, topic binding, manual title provenance and conversation-scoped
settings. It neither resets immediately nor schedules a deferred reset after the
active admission settles. Already accepted FIFO input stays on the same owner.
Explicit `/new` and other requested reset operations retain their existing behavior.

A non-deferred messaging failure keeps the agent's failure response and appends
the existing `gateway.compress.hygiene_failed` notice: history was kept, `/compress`
can retry, and `/new` is an explicit alternative. Deferred failures keep their
existing response. API callers keep their existing typed failure and response.
No successful compression, completed answer or guaranteed next-turn recovery is
claimed. The next message retries against the retained context and may fail again
until compression succeeds or the user explicitly changes the context.

The implementation is the gateway exhaustion boundary in
`gateway/run_turn_persistence.py`, ported narrowly from #546's initial
`f7f904558377e` change. It does not install #546's mechanical request projection,
pre-compaction input checkpoint or queue-recovery machinery. In particular, it
does not change the current context-overflow transcript-persistence policy, and
does not promise that an input rejected before durable admission is recoverable.

Load this unit when changing hard-window recovery, preflight input admission,
archive-backed request projection or the gateway exhaustion boundary.

## Deferred Recovery Design

The remainder of this design describes additional lossless recovery, not shipped
behavior or proof that the named files exist on `main`. The implemented boundary
above is the only completed portion of this record.

### Proposed Behavior

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
  consumed event (`notify_input_admitted`); proxy turns retire only when the
  transport returns an explicit successful-handling acknowledgement (terminal
  SSE `[DONE]` or generated response content). Connection refusal, non-200 HTTP,
  empty/error-only streams and pre-admission stream failure retain the checkpoint;
- discarded: `/stop` dropping a parked follow-up, `/new`/reset/resume clearing
  the overflow (`_clear_conversation_scope`), adapter cancel/stale-lock heal
  (`_discard_pending_message`), a pending slash command refused as agent input,
  or a follow-up merged into the head's refreshed checkpoint;
- expired: `QUEUE_CHECKPOINT_MAX_AGE_SECONDS` (7 days) bounds automatic replay.
  Expired input moves to the existing `*.bad` quarantine intact for manual
  recovery, including older drain arrivals with the legacy 24-hour bound.
  Age never deletes the only durable copy of accepted input;
- unauthorized owner at recovery: quarantined to `*.bad`, never replayed. Any
  declined accepted or legacy drain claim stops recovery for that input; it never
  falls through to a naked transcript row.

Quarantine retention follows the lossless-input contract: age alone cannot
 delete a sole copy. Manual cleanup may remove a payload only after verifying a
complete, byte-identical durable replacement and its recoverable session/topic
identity. Profile storage maintenance owns capacity management. Quarantine is
outside every automatic replay glob, so holding a payload does not create a
restart loop or re-execute it.

Every pending-slot content merge (running photos, setup-time input, non-queue
Telegram grace and adapter batching) uses the same copy-then-checkpoint admission
boundary. Refresh succeeds before publishing the new slot; failure preserves the
accepted head and returns rejection to its caller. Shutdown compares the event's
serialized content fingerprint against its checkpoint and durably refreshes a
stale spool rather than treating path existence as proof of current content.
Internal busy/drain callers return the actual admission receipt so rejected
completion notifications can refund their claims. Yuanbao recall replacements
use this boundary too: checkpoint the replacement before publishing the slot or
signalling the active turn, and retire the displaced checkpoint only on success.
A failed-first-delivery deferral must checkpoint a newly synthesized eventless
follow-up before replacing the head; keep the displaced admitted head in FIFO
order and leave it untouched if the new checkpoint fails. Moving an unchanged,
already-admitted event between the head, overflow and local drain references is
not a new content admission and must retain its existing checkpoint.

An already-drained eventless follow-up whose new checkpoint fails retains an
unacknowledged owner in the native FIFO. Startup's synthetic resume event owns
its exact preclaimed turn slot, so it enters that turn rather than queuing its
own empty wake. Unrelated input still queues and requires its normal durable
admission, including other internal completions.

Every FIFO producer must honor its durable-admission boolean, including the
Telegram follow-up grace window. A failed busy queue admission must produce a
failure reply, not a successful queue acknowledgement or a silently consumed
event; an active turn is not proof that its follow-up was checkpointed. Orphan
rescue may replace the incoming turn only after that incoming input is admitted;
on rejection, restore the original orphan FIFO/slot and process the arrival as
the current turn. Do not consume an older orphan while dropping the arrival. Post-turn goal continuations and
leftover eventless `/steer` follow-ups must also branch on the receipt: report a rejected
continuation and retain a rejected steer in-band rather than logging successful enqueue or
silently losing user input.

Goal continuations and heartbeat ticks are not checkpointed: goal state and the
heartbeat poller re-derive them. User `/goal` kickoffs retain the user's platform
message id and are not exempt: the command must propagate a rejected admission,
log it, and return failure text instead of claiming the kickoff started. Older
 goal-continuation checkpoints are dropped
at recovery. Registered built-in commands also terminate through `DROP_PENDING`,
including accepted-queue checkpoints; they must not remain eligible and warn
again on every restart. Internal completions (delegation/watch notifications)
are claim-settled at admission, so their checkpoint is their only durable copy and
stays until consumed. The same holds for human input without a platform id
(leftover `/steer`): retirement is by path and does not need a transport id.
Before provider work, refresh the accepted spool with the actual original session
id. Id-less queued input reuses `_outbox_admission_id` as a private occurrence
receipt, persisted through the native user-row platform-id field and restored
on spool recovery. Failed unlink cannot replay an admitted occurrence into a
rotated successor. Real transport ids still require matching content. A failed
binding refresh retains input and starts no provider work. Retirement callbacks
remain retryable on `False` or exception, but are cleared at the turn boundary.
Recovered follow-ups use the gateway's existing strict key/session metadata.
Unauthorized legacy drain arrivals and accepted checkpoints keep their full
payload in native quarantine. A declined future-turn claim never becomes a
transcript instruction, including when quarantine storage is unavailable.
The normal dispatch resolver checks that original owner again after recovery
claims the event, and refuses changed or stopped owners before provider work.
An unused event attribute is not a routing fence.

After the configured native auxiliary summarizer/fallback route cannot fit a
hard-window request, a no-network mechanical candidate may excerpt oversized
tool observations and discard prior-turn thinking. Preserve human instructions,
assistant visible receipts, tool call arguments and active-turn reasoning
verbatim. Every excerpt identifies its retained original session/message row;
use `session_search(session_id=..., around_message_id=...)` to locate it. Full
content remains in SessionDB and the native session exporter. Do not replay
completed effects, grant a summary authority, or silently clip giant mandatory
user/system instructions. Requests include system/tools/media estimates,
the transport's effective output cap and estimator/transport safety headroom.
The effective cap is resolved by the builders' own helpers (Anthropic native
ceiling, window clamp and thinking floor; profile default and Gemini thinking
raise), so an unset cap is never priced as zero. It is reserved where the
endpoint rejects input + cap over the window; Claude 4.5+ stops generation at
 the window instead, so only an explicitly selected cap is reserved there. A
one-shot ephemeral
cap replaces the configured cap, including when lower; fit estimation peeks
without consuming it and shares selection with request builders. The auxiliary
summarizer cap is not a main-request cap. Native Responses fit uses the same
route-aware checkpoint-pruned wire projection as preflight pressure, not the
unpruned durable transcript or a stale provider usage anchor. Count system text
and the actual request tool schemas alongside that projection.

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
compression`, then the no-progress guard's `352,895 vs 272,000` wording under
the shared timeout exception. There was no before/after summary at this boundary.
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
protected tails while preserving tool groups. Open [#103088](https://github.com/NousResearch/hermes-agent/pull/103088) covers hard
ceiling fallback independently of scheduling. Open
[#125141](https://github.com/NousResearch/hermes-agent/pull/125141) provides bounded
MCP excerpts. None is proof of released equivalent behavior.

## Verification

The implemented exhaustion boundary is exercised by
`tests/gateway/test_compression_exhaustion_keeps_context.py`,
`tests/gateway/test_native_fifo_context_exhaustion.py`,
`tests/gateway/test_api_session_compression_exhaustion.py` and the compression
role-retention case in `tests/gateway/test_model_role_lifecycle.py`.
`tests/gateway/test_auto_reset_clean_context.py` continues to verify that an
explicit reset creates an empty context without destroying the old transcript.

The focused tests below are acceptance targets for the deferred recovery design,
not current proof of lossless input admission or mechanical same-session recovery.
Do not claim these additional files or mechanisms exist on `main`.

Use `bash scripts/run_tests.sh`; no direct pytest or machine-slot bypass.
Focused tests: `tests/agent/test_same_session_recovery.py`,
`tests/gateway/test_auto_reset_clean_context.py`,
`tests/agent/test_same_session_recovery_gaps.py`,
`tests/gateway/test_same_session_queue_checkpoint.py`,
`tests/gateway/test_queue_checkpoint_retirement.py`,
`tests/gateway/test_proxy_queue_checkpoint.py`,
`tests/agent/test_native_preflight_estimate.py`,
`tests/e2e/core/compaction/test_same_session_recovery.py` and
`tests/e2e/core/compaction/test_recovery_gateway_crash.py`.
Ordinary FIFO acceptance writes the native private pending spool with file and
directory fsync before memory acknowledgement. Such a spool is not expired or
consumed by restart replay as a naked transcript row; it transfers to the native
transcript only after the matching platform id is durably admitted. A platform id
is admission dedupe, never proof of a completed reply or effect. A turn reuses a
historical trigger row only for an identical retry (same id and content); new
content under a reused id is new input. Gateway persistence accesses this lookup
through `AsyncSessionStore`, so its content-aware result must be awaited; the
underlying SessionDB query is synchronous. A truthy unawaited coroutine is not
an admission receipt.
Durable checkpoint refreshes stage under a non-eligible name and preserve the
previous accepted payload until the replacement's directory entry is fsynced. A
failed admission leaves no eligible replay file; a failed refresh leaves the
prior accepted checkpoint intact. Use the shared `_same_trigger_content`
normalization helper for every restart-admission comparison; never dedupe by
platform id alone.
Also exercise native preflight, in-place/commit rollback, tail watermark,
compression fallback/attempt lifecycle, pending-input dedupe and isolated
loopback-provider/kill9 compaction suites. Local recording fake-provider traffic
is simulated provider evidence, not authenticated fallback or real Telegram.
The combined gateway crash test kills after a giant observation is archived and
the answer is persisted, but before platform acceptance. It queues an input
before that kill and verifies full native archive retrieval, in-place session
identity, one reply per input in the original topic, unchanged manual title/icon
and model settings, consumed goal budgets, a stopped control lane and one
completed effect.
The parent owns candidate review, exact merge, native source updater, live
adoption and Telegram inbound/send/readback/restart canary. Source or a healthy
process alone is not end-to-end completion.
The same process harness also makes the pending publish location unavailable
after a real `/queue` admission. It verifies no queued provider work starts,
the input survives, and one queued reply arrives after storage recovery and
restart. Expiry regressions verify byte-for-byte preservation outside automatic
replay for accepted checkpoints and legacy drain payloads.

## Retirement and rollback

Retire only when a released upstream version meets the full same-session,
lossless admission, bounded complete-request and native-fault contract. Revert
this patch as a source unit, retain all SessionDB rows/archives and recovery
exports, and use the authorized native updater to select a verified previous
release. No persistent schema migration, data deletion or config rollback is
needed. A source rollback restores the old unsafe auto-reset behavior, so pause
oversized-topic traffic until the parent confirms a safe route. Never restart,
deploy or roll back from a source worker.
