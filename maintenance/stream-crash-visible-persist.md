# Stream preview crash ownership

Patch identity: `stream-crash-visible-persist`.

## Required behavior

A real streamed preview survives a gateway crash as the same editable platform
message when its ACK supplied a message ID. Recovery is fenced by the session
key, session ID and exact resume-turn token; reset and successor turns never
inherit a predecessor's preview. An edit whose ACK is lost retains the already
known ID, so recovery reconciles by edit without a possible-duplicate marker.

A provider stream is not complete until its terminal event. Never persist or
adopt a potentially partial journaled preview as a finished assistant answer.
The first-send-ACK-lost case cannot recover an editable ID: the platform may
already hold that copy. The owner's option B keeps auto-resume, persists the
real re-answer, and prefixes its delivery with the delivery ledger's existing
`RECOVERED_MARKER`. Only this exact crash-recovery state is marked and buffered
until completion, so its new preview cannot silently become another copy.
Ordinary typed turns retain their streaming cadence and progress configuration.

## Resume-turn and policy gate

Adoption and marking belong to the synthetic auto-resume turn only, never to the
session-level `resume_pending` flag: that flag outlives admission when the
restart-loop breaker trips, the marker is stale, the adapter is offline or auth
fails, so a real user inbound can run while it is set. `restore_preview`
requires the current turn's event to be `internal` and to carry
`_outbox_admission_id == "resume:<resume_turn_id>"`, the identity
`_schedule_resume_pending_sessions` stamps on the empty-text resume event.
It also requires `restart_resume_policy: continue`. Under `ask` (the default)
or any other value the resume turn only asks one line, which is not the
interrupted answer: the partial preview stays visible and untouched, a new
message is sent, and nothing is marked. A restored row is re-recorded under the
resumed turn's own active token so a second crash still finds the message.

## Source and implementation

`gateway/stream_recovery.py` journals exact wire text before real send/edit I/O
and the message ID after ACK, in the existing profile-scoped `state.db`. It
restores only the active resume token's known ID, or flags an uncertain first
send. `TurnRunner` marks only a completed recovery result's delivery text;
model history and normal persistence retain the actual re-answer without the
transport marker. Failed/interrupted results are not fabricated completions.
Ephemeral native draft frames are not journaled. Journal writes are
best-effort: a failure is logged and never changes delivery. Every path that
replaces or deletes the on-screen preview re-records or discards first:
fresh-final re-records the new message ID; silence-marker, empty-fallback and
full-resend fallback deletes discard. Journal I/O stays off the gateway loop.

## Upstream contract note

The governing docstring in upstream `b477dd96`'s
`test_crash_between_completion_and_send` explicitly allows any second copy the
platform may hold when it is marked as a possible duplicate. Its helper's
unconditional original-versus-resume rejection was stricter than that contract.
The docstring contract governs: for `stream_accepted_unpersisted` only, allow at
most one unmarked complete original, require one complete marked re-answer,
require the persisted assistant text to equal that re-answer without the marker,
and require one persisted user row. Two model calls are legitimate because the
first never completed. This Director tracks the recovery call separately from
`turns`; the scoped assertion permits `turns == 2` as well as that representation.
Every assertion for the other crash points is unchanged. The cell's runtime
xfail and its gap/signature constants are removed.

Current upstream guidance was checked at
`e06df9c2eae6901f4c88f464e0ae6e753c0428b8`: extend the existing delivery owner,
preserve feature behavior, and prove real I/O/recovery contracts. Fresh searches
for stream-preview crashes found no matching PR; the ACK-lost issue search found
only unrelated cron handoff issue #109243. No upstream writes are authorized.

## Verification

The regression was reproduced at `3bbbc3f96897`: `5 passed, 2 failed` in
`test_crash_between_completion_and_send`, with two unmarked complete replies in
both stream cells. The real child harness built `GatewayConfig` directly and
never forwarded the `restart_resume_policy: continue` written by `fresh_gw`, so
those resume turns resolved to `ask`; the journal row and resume token were
correct. The harness now carries the global policy and per-platform `extra`
through its child config.

Current verification on `d71da74fe3d9`:

- E2E crash matrix: `7 passed, 0 failed` (`161.9s`):
  `HERMES_PYTHON=.venv/bin/python bash scripts/run_tests.sh tests/e2e/core/delivery/test_messaging_exactly_once.py -k test_crash_between_completion_and_send`
- Stream recovery and consumer units: `105 passed, 0 failed` (`26.1s`):
  `HERMES_PYTHON=.venv/bin/python bash scripts/run_tests.sh tests/gateway/test_stream_recovery.py tests/gateway/test_stream_consumer.py tests/gateway/test_stream_consumer_transport.py`
- Python compile check: passed for all changed gateway and harness/test modules.

Preview journal connections use a 250 ms busy timeout so state-db contention
cannot hold a stream edit behind the delivery ledger's 10 s timeout. Successful
stream finalization discards the durable preview row.

## Retirement

Retire after upstream implements the complete same-message crash contract,
first-send-ACK-lost honest marking, ownership fencing and visible/transcript
alignment, with RED/GREEN proof through the real gateway crash harness.

Stack fit: reused the stream consumer, resume identity, SQLite delivery ledger,
`RECOVERED_MARKER` and existing pytest crash harness; added no dependencies.
