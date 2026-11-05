# Durable reply-latency evidence

Fork patch identity: `reply-latency-sink`.

## Intent and ownership

Extend [reply-latency metrics](reply-latency-metrics.md) without changing its
receipt clock, genuine-user classification or confirmed-text-send boundary.
The scripts factory scorecard is the concrete consumer. Ordinary gateway.log
rotation must not delete sparse measurement evidence. This is local private
instrumentation, independent of shared-telemetry consent; no content or outbound
telemetry is added. Do not activate the runtime as part of source delivery.

## Contract

Schema 1 JSONL lives at the owning profile's HERMES_HOME under
`logs/reply-latency/YYYY-MM-DD.jsonl` (UTC append date). Directories are 0700 and
files 0600 on POSIX. Retain the current UTC date and the preceding 13 dates;
prune only date-named JSONL files once per day. Existing log records remain.

`inbound_identity` is `[profile, platform, chat_id, thread_id, message_id]`;
`turn_id` remains diagnostic only. A missing message ID remains null; a private
receipt UUID survives native envelope restoration so concurrent ID-less inputs
cannot collide and a restored receipt does not create false unanswered rows. All events carry process UUID,
sequence, observation timestamp and process instrumentation start timestamp.
A bounded 1024-item per-profile daemon writer keeps append/fsync/retention off
intake and delivery. Overflow or failed I/O interrupts coverage, rate-limits a
warning and never blocks delivery. Abrupt exit may lose queued records; FIFO
checkpoints attest only their captured enqueue time, never future/pending data.
`instrumentation_start` is written on first initialization and daily rollover,
including process restarts and idle profiles. Coverage checkpoints run every
30 seconds for served profiles, off-loop. Each checkpoint attests a continuous
interval since the preceding checkpoint; a failed append resets that interval.
Consumers must reject gaps, missing markers, malformed rows and stale tails.
A warning is rate-limited per profile; diagnostic failures never interrupt intake
or confirmed replies. This is best-effort evidence, not a delivery dependency.

## Proof and retirement

Run tests/gateway/test_reply_latency_sink.py with the existing latency log and
follow-up suites. The sink regression covers restart markers, retention,
ordinary-log rotation, failed writes, confirmed reply delivery and authoritative
synthetic origins. Keep this contract until upstream supplies equivalent private
receipt-level durable evidence. No upstream equivalent was found: upstream main
`ed1afb082eb3e46cf2edae9d8f68fb67b09ee4df`, issue #16106 and PR #145500
concern SLO tracing/no-reply counters, not this sink. Remove startup checkpoint
wiring together with the sink when retiring it; never leave an orphan task.
