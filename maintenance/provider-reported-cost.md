# Provider-reported cost

**Patch identity:** `provider-reported-cost`.

## Required behavior

- `normalize_usage` keeps plain-attribute usage payloads in `raw_usage`, so provider cost extensions survive.
- `provider_reported_cost` returns a finite, non-negative per-call amount from `cost_details.upstream_inference_cost` (Nous), with a flat `cost` fallback (OpenRouter). A zero nested value never masks a positive flat `cost`.
- `estimate_usage_cost` returns that amount as `status="actual"`, `source="provider_cost_api"`, after subscription-included routes and before pricing-table estimates.
- The main-turn delta keeps its amount in `estimated_cost_usd`, the bucket every existing reader already uses. `update_token_counts` also records it as `actual_cost_usd` on the per-call `session_model_usage` row. The `sessions` row's `actual_cost_usd` changes only when it is already non-NULL (an explicit legacy actual write). There it gets the same billed amount, so `COALESCE(actual_cost_usd, estimated_cost_usd, 0)` keeps showing it.
- MoA turns that fold advisor estimates into the delta persist `cost_status="estimated"`.

## Combine semantics

No reader changes. Rows that never receive a provider amount keep base columns and base totals. A billed amount displays once in each reader: `usage_totals`, the session cost filters, Desktop/project `actual || estimated`, and the Insights per-model cost. A legacy dual-written row (`est=3, actual=3, status=actual`) followed by an estimated $0.50 call still shows $3.00 (base behavior), never $6.50.

## Deferred follow-up

These items stay out of this patch because each rejected head's P1 came from changing how estimated and actual are combined:

- Mixed-status accounting, so a session row can expose its billed and estimated parts separately.
- Reclassifying legacy dual-written rows and NULL-status rows from before the update.
- Review-fork in-memory cost buckets (they persist the combined counter as estimated).
- Auxiliary provider-reported spend. `record_aux_usage` still records estimates only.
- Session-level `actual_cost_usd` and analytics `total_actual_cost` stay 0 for new billed rows. Only the per-model `actual_cost` reflects them.
- Open P2: `hermes_cli/web_server_profiles.py:390` `_aux_usage_rows` does not aggregate `actual_cost_usd`, so auxiliary model cards report `actual_cost=0`.

Evidence: rejected heads `c3cc7a90`, `b5020680`, `d45c3bfe`, and `23c234ef` (PR #436). Their review receipts are in `~/.hermes/review_receipts/<sha>*.json`.

## Focused regression

`scripts/run_tests.sh tests/hermes_state/test_provider_reported_cost.py tests/agent/test_usage_pricing.py`

## Retirement and rollback

Retire this patch when upstream persists provider-reported per-call cost with equivalent exactly-once display. To roll back, revert the `provider-reported-cost` commit.
