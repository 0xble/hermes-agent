# State write-lock hold indexes

Fork patch identity: `state-write-lock-hold-indexes`.

## Required behavior

Reduce state.db writer-lock holds without changing transaction boundaries or adding
caps, queues, pauses, or chunking:

- Local receipt handoff scans use the declarative `idx_state_meta_local_policy_route`
  index and an SQL `json_each` target filter when a repoint supplies target ids.
  `_epoch` and `hand_off_conversations` remain in the same write transaction.
- Boot's local-receipt scan (`gateway/session_local_recovery.py` `local_receipt_ids`, 3 calls
  per boot) reads the same covering index. Its literal `GLOB` is the index predicate, because a bound
  `LIKE` cannot imply it and the planner would scan every row and parse every receipt's JSON. On a
  70k-receipt clone (2026-11-28) the scan dropped from 462 s to 17.9 s cold (0.3 s warm), and the
  404-row result was identical. A missing index falls back to the unforced query.
- Held-media collection uses the partial `idx_session_admissions_held` index. The
  exact predicate is shared by the schema DDL and production query, with no
  `json_valid` guard.
- Session-row prompt cleanup deletes only the newly stored hash when it is not
  referenced by either session prompt or tool-name column. The upsert's
  `COALESCE(sessions.system_prompt_hash, excluded.system_prompt_hash)` means an
  existing session hash is never replaced, so the targeted delete is equivalent to
  the previous unbounded cleanup for the hash this call stores. Other orphaned prompts are
  no longer reclaimed here; the maintenance pass and session deletion still sweep them.
- Prompt and tool-pin updates, including worker receipts, collect only the previous
  hash when its binding changes. Both reference columns are checked in the same
  transaction, so one binding cannot remove a prompt still shared by another.

The declarative indexes are created on the first writable open of an existing store
(`CREATE INDEX IF NOT EXISTS`). The receipt index build parses every
`gateway.local_policy.v1:*` value with `json_valid`/`json_extract`, which on a cron-heavy
store means reading every receipt's JSON once (about 2 GB at 40k receipts), comparable to a
single pre-index boot heal. The held-media index build scans `session_admissions` once. If
the receipt index is missing, `moved_receipts` falls back to the unforced query, so a store
without it is slower but never fails the boot heal or an explicit repoint. No
schema-version bump is required.

## Hold measurements

The receipt path previously reached a maximum 32.6 s with zero handoffs in 16
runs. Held-media collection reached 21.2 s. The prompt-cleanup event reached 18.3
s, but that event was a Python GC pause rather than SQL work. For prompt replacement,
a disposable 10,000-row database confirmed that the old delete scanned the prompt
table and the targeted delete used its hash index. This establishes the amount of SQL
work removed. Live latency also depends on storage and process scheduling.

## Source surfaces

`hermes_state_keys.py`, `hermes_state_common.py`, `hermes_state_local.py`,
`gateway/session_ingress_media.py`, `hermes_state.py`, `hermes_state_sessions.py`,
`hermes_state_worker_context.py`, `hermes_state_worker_lifecycle.py`, and
`tests/hermes_state/test_lock_hold_indexes.py`.

## Verification

**Contract tests:** `tests/hermes_state/test_lock_hold_indexes.py`.

The tests record SQL and bound parameters at the production function boundary,
assert the receipt plan with and without target ids and the held-media partial
index, compare indexed results with `NOT INDEXED` references, exercise malformed
and missing receipts plus status/media transitions, verify targeted prompt
behavior, and pin the no-index falsifier plans. Run the focused file followed by
the existing receipt, media, prompt, and runtime-schema suites listed in the PR.

## Retirement and rollback

Retire when upstream retains equivalent receipt and held-media plan contracts and
an equivalent targeted prompt cleanup. Revert the commit carrying
`Fork-Patch: state-write-lock-hold-indexes`.
