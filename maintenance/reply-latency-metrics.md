# First-Reply Latency Metrics

## Provenance And Adoption

Patch identity: `reply-latency-metrics`. The local responsiveness reader consumes
per-message receipt and confirmed-send records, not the opt-in shared-telemetry
histogram. Upstream `NousResearch/hermes-agent` main at
`1ae3c55e4bb16f495ebf0536a5672782d903e15e` has `start_reply_clock`,
`stop_reply_clock`, and `stops_reply_clock` in
[`shared_metrics_gateway.py`](../hermes_cli/observability/shared_metrics_gateway.py),
but its execution-start, chat-keyed clock does not include pre-execution wait.

Contribution route: keep this local log contract until upstream offers an
unconditional receipt-to-confirmed-text event pair with message-level identity.
Upstream disposition: not submitted. Fork delivery: source patch only, runtime
activation remains a separate decision. The gateway now owns a local JSONL
reader, `python -m gateway.reply_latency_report --since 7d`. The earlier
responsiveness reader in `0xble/scripts` remains a separate consumer.

## Update Boundary

Load [attended model switching](attended-model-switching.md) before changing
receipt classification, and [stream preview crash ownership](stream-crash-visible-persist.md)
before moving transport acknowledgements. Keep the classification, original
receipt timestamp and successful-send meaning owned by their existing helpers.
Do not replace a confirmed send with the optimistic pre-send journal, an outbox
admission timestamp, an empty native seed frame or an inherited chat clock.

The immutable inbound key is platform, chat, thread and inbound message ID.
`turn_id` remains diagnostic, because rehydration changes it from the transport
identity to the durable admission identity. The reader folds append-only
observations into 1 record per inbound. Missing transport identities stay nullable.
The profile-owned writer appends receipt, durable admission, agent start, first
visible delivery kind and final-delivery confirmations to the existing daily
`logs/reply-latency/` JSONL files. Progress sends, status cards and edits count only
after transport success. Deferred outbox acceptance is not delivery. Telegram
split sends stamp each acknowledged chunk before outbox/bookkeeping waits.
Adapter confirmations require an explicit delivery scope and matching chat/thread
identity. An inherited turn receipt alone never authorizes attribution. Deferred
outbox sweeps clear the scheduling turn's delivery scope, including when an older
row targets the same chat and thread. Log directory creation is cached per path,
while each observation remains a small single append write.
The local logging must stay independent of the shared-telemetry opt-in. Stamp
arrival before buffering, but emit `received` only after authorization. Diagnostic
identity or log failures must not interrupt intake. Recursive queued follow-ups
bind their own receipt and restore the head's context on exit. Their terminal
receipt, including an explicit absence for event-less input, owns the outer final
send without replacing the head's original receipt.

## Proof And Retirement

[`test_reply_latency_logs.py`](../tests/gateway/test_reply_latency_logs.py)
exercises the confirmed transport boundaries and cross-event isolation.
[`test_reply_latency_followups.py`](../tests/gateway/test_reply_latency_followups.py)
drives the real in-band drain through queued, interrupting and nested turns,
including streaming, final-send attribution, failure and cancellation. It also
covers unauthorized DMs and diagnostic failures during admission. The delivery
tests also cover progress/status sends and edits, admission rehydration, skipped
and deferred sends, split Telegram confirmations, fresh streamed finals and
fallback continuation chunks. Run these
alongside [`test_reply_latency_attribution.py`](../tests/gateway/test_reply_latency_attribution.py),
which covers copied worker context, unrelated destinations, inherited outbox
sweeps and matching scoped replies across Telegram send and overflow paths. Run them
with the existing shared-metrics, stream-consumer and attended-activity tests
when these lifecycles move. A native-frame or adapter-internal split cannot
claim delivery before its actual transport acknowledgement.

Retire this patch and its companion reader contract only after the upstream
replacement proves original-receipt coverage and confirmed-send semantics for
queued messages and simultaneous threads. Remove the log wiring together with
its helper if rolling back, leaving attended activity and shared telemetry intact.
