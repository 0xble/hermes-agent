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
activation remains a separate decision. The companion reader belongs to
[`0xble/scripts`](https://github.com/0xble/scripts/pull/452), not this repository.

## Update Boundary

Load [attended model switching](attended-model-switching.md) before changing
receipt classification, and [stream preview crash ownership](stream-crash-visible-persist.md)
before moving transport acknowledgements. Keep the classification, original
receipt timestamp and successful-send meaning owned by their existing helpers.
Do not replace a confirmed send with the optimistic pre-send journal, an outbox
admission timestamp, an empty native seed frame or an inherited chat clock.

The record's `turn_id` is the existing `outbox.transport_id(event)` known before
admission, not the later randomly allocated outbox admission UUID. Preserve
this distinction when adopting upstream event identities, or migrate the
reader and producer together. Missing transport identities stay nullable.
The local logging must stay independent of the shared-telemetry opt-in.

## Proof And Retirement

[`test_reply_latency_logs.py`](../tests/gateway/test_reply_latency_logs.py)
exercises the confirmed transport boundaries and cross-event isolation. Run it
with the existing shared-metrics, stream-consumer and attended-activity tests
when these lifecycles move. A native-frame or adapter-internal split cannot
claim a delivery instant earlier than the acknowledgement the caller observes.

Retire this patch and its companion reader contract only after the upstream
replacement proves original-receipt coverage and confirmed-send semantics for
queued messages and simultaneous threads. Remove the log wiring together with
its helper if rolling back, leaving attended activity and shared telemetry intact.
