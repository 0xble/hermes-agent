# Local Receipt Config Dedupe

**Patch identity:** `local-receipt-config-dedupe`.

A local creation receipt (`gateway.local_policy.v1:<id>` in `state_meta`) freezes the
whole profile config in `policy.config_json`, so restore runs the session under the
config it was created with. That copy is about 51 KB of a typical 64 KB receipt. On
the default store in November 2026, 70.6k receipts held 4.55 GB, 98% of them cron
fires. They held only 372 distinct configs, 17.6 MB stored once against 3.3 GB inline.
The differences were real config edits and a few per-job overrides, not volatile
fields, and 2 to 30 new configs appeared a day.

The shared form stores the config once under `gateway.local_policy_config.v1:<sha256>`
and keeps `policy.config_ref` in the receipt (`hermes_state_local.encode_receipt`).
`decode_receipt` restores `config_json`, so every reader still sees the inline policy.

## Rollout in two steps

A release without the decoder cannot read a shared-form receipt, and the session it
belongs to becomes unopenable. Releases are rolled back by pointer, and 7 are retained.
So readers ship first and writers follow:

1. **Decoder.** Readers whose policy is consumed, or compared with a decoded copy,
   decode: `local_receipt`, `local_snapshot`, the creation retry check in
   `commit_local_session` and `adopt_untracked_compression`. Every writer still stores
   the inline form, so this step changes nothing on its own.
2. **Encoder.** `commit_local_session`, `_commit_policy`
   (`hermes_state_mutation_prepared.py`) and `_commit_local_target`
   (`hermes_state_local_lineage.py`) store the shared form. Land it only once every
   retained release carries the decoder.

Either way:

- Raw read-modify-write paths that never touch the policy (lineage advance, reset,
  branch, profile-rename rekey) keep a stored ref unchanged: `encode_receipt` passes a
  ref-only policy through.
- Inline receipts read unchanged and age out with their sessions. There is no data
  migration and no schema change.
- A ref whose config row is missing refuses as `storage_unavailable`, the existing
  code for an unreadable receipt.

Known gap: config rows are never deleted. They grow by one row per distinct frozen
config, about 50 KB each, which tracks config edits rather than session volume: about
1 MB a day at the November 2026 rate.

Contract tests: `tests/hermes_state/test_local_policy_config_dedupe.py`.

Retirement: remove when upstream stores frozen session config without a full copy
per receipt.

Rollback: before the encoder lands, revert the commit carrying
`Fork-Patch: local-receipt-config-dedupe`. After it lands, revert the encoder only and
keep the decoder, which must stay while any shared-form receipt exists.
