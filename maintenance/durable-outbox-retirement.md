# Durable outbox retirement

Patch identity: `durable-outbox-retirement`.

The fork's durable outbox (`gateway/outbox.py`, opt-in through `gateway.durable_outbox.enabled`)
admitted Telegram updates and receipted the Telegram adapter's egress. It never covered any other
platform. Once the [Telegram stack](telegram-stack-retirement.md) was removed it covered nothing, so
it was deleted rather than migrated.

Load this unit before adding any second durable store for inbound admission or outbound delivery.

## Required behavior

- Inbound admission and per-session ordering stay with upstream's `session_authority` FIFO, and
  final-reply crash recovery stays with upstream's `delivery_ledger`, both unchanged.
- No gateway path imports `gateway.outbox`. The 3 unconditional call sites (the inline command
  reply and the message-processing task in `gateway/platforms/base.py`, the proxy stream task in
  `gateway/run_turn.py`) and the boot and standby-promotion replays (`gateway/run_startup.py`,
  `gateway/zero_cut_standby.py`) are gone. The Telegram admission and redelivery checks in
  `gateway/run_inbound.py`, `gateway/platforms/base.py` and `gateway/session_ingress.py` went with them.
- `_record_delivery_obligation` ledgers every eligible final. No final is skipped as outbox-covered.
- `gateway.durable_outbox` still parses (default `enabled: false`) so existing configs load. A gateway
  started with `enabled: true` logs one warning that the setting has no effect.
- The `resume:` admission ids on restart replay (`_outbox_admission_id`) belong to the fork's restart
  replay (roadmap step 7), not to this module, and are unchanged.

## Retired identities

- Retired identities, outbox: `outbox-coalesced-sweep`, `telegram-outbound-class`.

Commits of `seamless-restart`, `seamless-restart-s2`, `telegram-delivery`, `telegram-chat-budget`,
`zero-cut-handover` and `hindsight-memory` also touched the outbox. Their other code stays, so their
existing units keep ownership.

## State left behind

`gateway-outbox.db` and `gateway-outbox-media/` in each profile home are no longer opened. Nothing
deletes them. Remove them by hand once a restart has run without the outbox.

## Verify

`scripts/run_tests.sh tests/gateway/test_compression_retry_lifecycle.py tests/gateway/test_zero_cut_busy_routing.py
tests/gateway/test_zero_cut_standby_promote.py tests/gateway/test_delivery_ledger.py tests/providers/test_header_resolution.py`. Live proof, in a restart window: with `durable_outbox.enabled: false`, a real
Slack message sent across a gateway restart is answered once, and a crash during the final reply is
recovered by the delivery ledger.

## Rollback

Revert this change. The left-behind database lets the restored outbox resume once
`gateway.durable_outbox.enabled` is set back to `true`.
