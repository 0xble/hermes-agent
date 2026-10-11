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

## Review history and final-stop rule

The maintenance unit has recorded these rejected candidate heads and their primary P1 evidence. Each receipt is available as `~/.hermes/review_receipts/<head>*.json`:

- `35e3a5f383dcf0b30cb85ace55df002f830f85c2`: actual provider spend was written only to `actual_cost_usd`, while existing readers used the estimated bucket.
- `86a6d2e213fdbd4fbeefecdc43bf021ddeb523e5`: provider spend was lost from in-memory session totals and mixed persisted rows undercounted.
- `101193002642ecccaa49e35fc5266f689e6cddc8`: persisted `mixed` status was not sticky across later writes.
- `b8ce8ddc268de1c5fda2c0d88ce2327dda14210d`: `included`/`unknown` writes could replace actual or estimated status and hide spend.
- `94a69a0e80a67893a9bba4378a2e89f789fdfd26`: mixed-bucket writes broke unchanged `COALESCE`/`actual || estimated` readers.
- `3234ffd5bdb29c9068bd76e44f6f014afdf8fecf`: auxiliary provider-reported spend was persisted as an unclassified estimate.
- `c3cc7a90c0238f946f414ddc624cad89fb1a58a2`: Insights preferred estimated spend over provider-reported actual spend on legacy dual-written rows.
- `b5020680a57e2634028bdc75d9f291c0771484bc`: background-review forks lost actual/estimated provenance at the parent persistence boundary.
- `d45c3bfedbc98a1943bae2790995b2a202ab5c84`: legacy auxiliary estimates were hidden after a later provider-reported actual write.
- `23c234ef6e4bc65122f69034a903c368b9b18049`: transitioning legacy dual-written rows to mixed double-counted historical estimated spend.
- `42ed65ec42c2fbbbaf52586151613ef9a6d0f98f`: queue coalescing merged explicit and inferred cost write shapes, hiding billed spend; this bounded candidate fixes that seam with a real SessionDB sequential-vs-coalesced regression.

**Final stop:** after the next exact-head review of this narrowed candidate, any P1 parks PR #436 as a draft for Brian's decision. No further repair cycle is authorized by this maintenance unit.

## Deferred follow-up

These items stay out of this patch because each rejected head's P1 came from changing how estimated and actual are combined:

- Mixed-status accounting, so a session row can expose its billed and estimated parts separately.
- Reclassifying legacy dual-written rows and NULL-status rows from before the update.
- Review-fork in-memory cost buckets (they persist the combined counter as estimated).
- Auxiliary provider-reported spend. `record_aux_usage` still records estimates only.
- Session-level `actual_cost_usd` and analytics `total_actual_cost` stay 0 for new billed rows. Only the per-model `actual_cost` reflects them.
- Open P2: `hermes_cli/web_server_profiles.py:390` `_aux_usage_rows` does not aggregate `actual_cost_usd`, so auxiliary model cards report `actual_cost=0`.

Evidence: the complete rejected-head inventory and receipt naming convention are recorded in **Review history and final-stop rule** above.

## Focused regression

`scripts/run_tests.sh tests/hermes_state/test_provider_reported_cost.py tests/agent/test_usage_pricing.py`

## Retirement and rollback

Retire this patch when upstream persists provider-reported per-call cost with equivalent exactly-once display. To roll back, revert the `provider-reported-cost` commit.
