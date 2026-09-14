# Working heartbeat delivery

The periodic `⏳ Working — N min — activity` notification keeps its existing
180-second default cadence and display controls. It is mutable progress, not a
turn-final message. Its send uses the existing `expect_edits` metadata and
`StatusDelivery` receipt owner.

## Invariants

- One initial send per turn. Only a definitive missing-message edit failure can
  authorize one replacement; 429, network, permission, and ambiguous failures
  never authorize another bubble. An edit retry-after delays future edits.
- A cancelled or ambiguous initial/replacement send is not blindly retried.
  Shielded receipt tasks retain the turn's exact adapter/session ownership;
  a late success participates in cleanup even after final delivery.
- Cleanup uses the adapter that returned the receipt. A newer turn or topic
  cannot lend its identity or temporary-message IDs to the old turn.
- With `cleanup_progress: false`, status messages remain intentionally. With
  cleanup enabled, final-delivery failure preserves breadcrumbs. Always-run
  post-delivery release hooks still run; a task-local delivery outcome gates only
  temporary-bubble deletion.
- Telegram can defer expendable deletes before making any Bot API request.
  Cleanup distinguishes this from remote failure, retries deferral at most three
  times within ten seconds, and logs unresolved IDs rather than claiming them
  deleted. It neither bypasses Telegram's shared scheduler nor retries a 429 on
  its own schedule. Long outages may still leave visible breadcrumbs.

The six-ordinary-message delegation reanchor and its delete-first ordering are
unchanged. This is not a new status manager or a historical-message sweeper.

## Investigation limits

The reported screenshot's four Working receipts were present in its final
cleanup list; their delete calls returned false. The separately cited missing
edit anchors belonged to other tool-progress messages. Thus cleanup omission
was not established for that screenshot. The old heartbeat did resend after
**any** failed edit, and its receipts bypassed the status owner's transport and
late-receipt tracking. Telegram's boolean delete wrapper also erased local
scheduler deferral, while final cleanup had no final-delivery success signal.

Two bounded, new-message live probes in the affected topic successfully edited
and deleted their own receipts, with remote absence readback. One used raw rich
API calls and one the Telegram adapter. Rich-message incompatibility and the
precise historical remote-error cause were **not** reproduced; neither is
claimed as the root cause. The new `expect_edits` marker is the existing mutable
message contract, not a workaround based on that unproven hypothesis.

## Verification boundary

`tests/gateway/test_working_heartbeat.py` executes the actual heartbeat
coroutine, Telegram adapter and turn-final cleanup with only the Bot API wire
and heartbeat clock substituted. It covers multiple ticks, bounded not-found
replacement, transient/429 failures, ambiguous sends, late cancellation
receipts, session/topic and adapter isolation, final-delivery failure, new
turn generations, and locally deferred cleanup. Fake-wire tests prove code
behavior, not Telegram delivery. Live verification must separately record its
loaded revision and exact newly-created receipts; never delete arbitrary
historical messages to manufacture a clean screenshot.
