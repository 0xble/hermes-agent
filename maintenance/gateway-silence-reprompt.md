# Gateway human-turn silence re-prompt

This unit owns one gateway-level recovery execution when a locally executed human-addressed turn ends on a bare silence marker.

## Preserve

- Evaluate `silence_allowed(display_kind, reply_expected)` **after execution**, from the live gateway `TurnContext`. The real busy fold in `gateway/run_busy.py` changes a relay's `reply_expected=False` to `None` when a genuine typed request is absorbed. Relay-only, machinery, failed, interrupted, and stale executions must not start this retry.
- After the first stream's existing end-of-turn silence filter finishes, call the gateway's recursive `_run_agent` continuation once. Keep session/source/run generation, latest reply anchor, typed history, tool results, and the outer persistence offset. Do not re-run the original request or completed tools.
- The retry's own result is terminal: tools, stop gates, compression, or another folded message cannot renew its budget. A second bare marker reaches the unchanged human-turn visible fallback. A separate queued or subsequent genuine human turn gets its own retry.
- Recovery guidance uses the existing API-only ephemeral system prompt, not an assistant/user nudge pair. The continuation's empty runtime row carries `internal_notification` provenance; it cannot replace the latest genuine user ask, session title, or inbound delivery anchor. The note is absent from live/result message history, SQLite content and sidecars, trajectories, compression input, and subsequent queued turns. No agent transcript scaffolding or compressor-specific flags belong to this patch.
- A queued terminal turn owns its own silence verdict; absent a genuine successor, the original human verdict and inbound ID must survive the internal retry. Never allow the retry's internal display kind to legalize a second marker.
- Fresh execution means a fresh stream consumer. Bare-marker segment previews must be held back on edit, native, and stream-is-the-message draft transports. Cumulative adapters keep earlier tool preambles while removing only the rejected final segment; neither the first marker nor its retry may appear in sent, edited, or native/draft frame payloads. Close an open native stream with the retained preamble through the existing boundary finalizer before recovery starts, without claiming turn-final delivery.
- Proxy turns do not get this retry; the existing human-turn warning remains. The API requires non-empty final conversation content and cannot represent an empty internal continuation: a system-only/empty-row request returns 400, while omitting the empty row makes the preceding assistant marker new user input. Do not change API-server validation or invent a persisted user nudge to work around that contract.

## Provenance and patches

- **Identity and status:** `gateway-silence-reprompt`; active fork adaptation.
- **Source / fork refs:** maintained fork `origin/main` at the implementation base; fork delivery is the commit carrying `Fork-Patch: gateway-silence-reprompt`.
- **Surfaces:** `gateway/run_turn.py`, `gateway/run_turn_runner.py`, `gateway/turn_context.py`, `gateway/stream_consumer.py`, `gateway/stream_consumer_fallback.py`, and `tests/gateway/test_folded_steer_silence_reprompt.py`.
- **Contribution route and rationale:** narrow core patch at the existing gateway recursive follow-up and stream-finalization boundaries. Plugins cannot recover before delivery while preserving the native human silence guard and queued-turn provenance.
- **Links:** related upstream merged PR [NousResearch/hermes-agent#111624](https://github.com/NousResearch/hermes-agent/pull/111624), which supplies the visible fallback but does not perform this gateway retry; maintained fork PR [0xble/hermes-agent#429](https://github.com/0xble/hermes-agent/pull/429).
- **Upstream disposition:** related behavior is released upstream as a fallback; no upstream equivalent of this gateway one-shot recovery was found.
- **Fork delivery:** exact-head gate and independent review precede protected landing. This unit does not activate or restart the runtime.

## Update

Compare upstream gateway execution/follow-up and stream-finalization paths. Preserve queued-terminal metadata, the existing synthetic-prompt classification, and API-only ephemeral prompt ownership. Do not move recovery back into the agent final-text/tool/compression loop.

## Verify and recover

Run `scripts/run_tests.sh tests/gateway/test_folded_steer_silence_reprompt.py` first, then affected gateway silence, queued-followup, streaming, and agent final-response suites. The integration regressions use real `GatewayRunner._run_agent`, `TurnRunner`, `AIAgent.run_conversation`, busy-fold, stream consumers, and compression/SQLite boundaries, with model/tool/transport I/O replaced by deterministic fakes. Cover both marker spellings, folded typed input, exactly one recovery, second-marker fallback, relay-only/machinery controls, retry tools and a new human turn's separate budget, all transport payloads, cumulative preamble retention, and genuine-user compaction/durability without the retry note. Run Ruff and source-only `scripts/check_fork_patches.py`; queue the full gate for the exact published head. Rollback is the focused revert of commits carrying `Fork-Patch: gateway-silence-reprompt`; the existing downstream gateway fallback remains intact.
