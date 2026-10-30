# Telegram stack retirement

Patch identity: `telegram-stack-retirement`.

The personal profile moved from Telegram to Slack, and no other profile uses the fork's
Telegram features: Meridian runs Slack only, and Everguide uses plain Telegram DMs with an
allowlist and no topic toolset. The fork therefore carries no Telegram-specific patches.
`plugins/platforms/telegram/` is byte-identical to the accepted release baseline, so an
upstream sync takes upstream's Telegram plugin as-is.

Load this unit when an upstream sync touches the Telegram plugin, before adding any
Telegram-specific behavior back, and when a retired identity below shows up in history.

## Required behavior

- `plugins/platforms/telegram/`, `hermes_state_telegram.py`, `gateway/run_topics.py` and
  `tools/send_message_senders.py` match the release baseline. Upstream's own Telegram
  features (DM topic mode, `/topic`, topic bindings, rich messages, approval buttons for
  commands) keep working unchanged.
- No fork-only Telegram module remains: topic retirement (`gateway/telegram_retire*.py`),
  the `telegram_topic` tool and toolset, topic sessions and owner routing, topic icons,
  lineage aliases, explicit title receipts, `/topic edit`, `/retire`, the per-chat and daily
  call budgets, durable flood state, rich rendering and inbound Rich Messages, typing refresh,
  first-poll health, non-blocking ingress, reconnect and disconnect handover, deleted-topic
  final recovery, and session-control Approve/Deny cards.
- Shared code keeps no hook that only those modules reached. Where shared code called one, it
  now follows its non-Telegram path:
  - Session-control requests that need approval fail as `target_unapprovable`
    (`_APPROVABLE_SOURCES` is empty). See [Session controls](session-controls.md).
  - Goal and loop wakes no longer probe for a deleted Telegram topic before running.
  - Titles keep the stock Telegram topic rename lane, without icons or lineage aliases.
    See [Session titles](session-titles.md).
  - Zero-cut handover refuses with `refused: telegram` while a Telegram adapter is served,
    because nothing transfers Telegram polling to the standby anymore. Without Telegram it
    behaves as before. See [Zero-cut gateway handover](zero-cut-handover.md).
  - Cron jobs' `telegram_topic_owner_key` and `telegram_topic_allow_parallel` fields are
    ignored. Delivery uses upstream's named-topic route.
- Telegram-conditional code that other families still own stays in place:
  - Reply anchors, the delivery ledger, outbound-class labels and cron live-lane flood
    handling: [Telegram delivery remnants](telegram-delivery.md).
  - Zero-cut standby's deferred Telegram start: [Zero-cut gateway handover](zero-cut-handover.md).
  - `/bg` and `/btw` recovered-topic routing, adopted from upstream PR #123797:
    [Background topic recovery](background-topic-recovery.md).
  - Reviewed cron delivery resolves review subjects from upstream's topic bindings
    (`cron/scheduler_review.py`); no job uses it.
  - The versioned shared-metrics schemas (v3, v4) still list `telegram_topic`. Published schema
    versions stay append-only so older events still validate.

## Retired identities

These identities stay owned here so history keeps passing `scripts/check_fork_patches.py`.
Their code is gone. Any new commit that reuses one is a regression.

- Retired identities, topics and titles: `gateway-telegram-topic-tool`, `slice-9-telegram-topic-edit`,
  `slice-16-topic-icons`, `slice-17-topic-lineage`, `telegram-title-receipts`,
  `telegram-topic-owner-routing`, `telegram-topic-retire`.
- Retired identities, delivery and flood: `slice-10-flood-coherence`, `slice-10-telegram-delivery`,
  `slice-10-telegram-delivery-followup`, `telegram-replacement-adapter-egress`,
  `telegram-retry-after-egress`, `telegram-rich-delivery-recovery`, `telegram-send-teardown`,
  `telegram-standalone-chunk-indicator`, `telegram-album-flood-contract`,
  `telegram-media-flood-under-lock`, `telegram-stale-final-delivery`,
  `telegram-internal-delivery-recovery`.
- Retired identities, rendering: `telegram-rendering`, `telegram-rich-modes`, `telegram-standalone-rich`,
  `telegram-footnote-refs`, `telegram-link-targets`, `telegram-literal-hash`,
  `telegram-ordered-list-separation`, `telegram-paragraph-spacing`, `telegram-progress-literal`,
  `telegram-rich-currency`, `telegram-table-boundaries`, `telegram-inbound-rich-messages`.
- Retired identities, transport: `telegram-ingress-nonblocking`, `telegram-first-poll-health`,
  `telegram-typing-refresh-4500ms`.

The two published commits of `telegram-topic-owner-routing` (PR #638) carry no trailer; their
stable patch IDs keep them classified:

Fork-Patch-Backfill: b91a64164821e8c1a3669a3b5e153f871370e86c; telegram-topic-owner-routing
Fork-Patch-Backfill: 3d381f0050b51c28e63a482435e87a0b8380d59a; telegram-topic-owner-routing

`format_progress_literal` and `typing_refresh_interval` remain on `BasePlatformAdapter` as
identity defaults. No adapter overrides them now.

## State left behind

Nothing migrates or deletes data. The fork's extra state.db tables
(`telegram_dm_topic_owners`, `telegram_topic_icon_state`, `telegram_topic_icon_history`), the
profile's `telegram-flood-state.db`, retirement records in `state_meta`, and the cron job fields
above stay where they are and are no longer read. Stock code rewrites
`telegram_dm_topic_schema_version` to `3` and creates its own tables with `CREATE TABLE IF NOT
EXISTS`, so a rollback that restores the fork code migrates forward again without loss.

## Verify

`scripts/run_tests.sh tests/gateway/test_telegram_*.py tests/plugins/test_telegram_*.py
tests/plugins/platforms/telegram tests/e2e/core/platforms/test_telegram_contract.py
tests/gateway/test_zero_cut_handover.py tests/gateway/test_zero_cut_handover_verb.py
tests/hermes_cli/test_session_controls.py tests/agent/test_title_generator.py`, then
`git diff --exit-code <baseline> -- plugins/platforms/telegram hermes_state_telegram.py gateway/run_topics.py tools/send_message_senders.py`
to confirm the stock files are still stock.

## Rollback

Revert the merge commit of the removal PR as one unit. The left-behind state above lets the
restored code resume. Re-enable Telegram on the personal profile only after that revert is
installed.
