# Delegation service tier

Load this unit when changing how delegated children inherit the parent's
Fast/normal preference, `delegation.inherit_service_tier`, or the child
request-override resolution in `tools/delegate_tool_config.py`.

## Required behavior

- `delegation.inherit_service_tier` defaults to `false`. When it is false, a
  child keeps the pre-patch behavior: it inherits unrelated parent request
  overrides, but the parent's transient `service_tier` or `speed` does not
  leak into it.
- When it is true, a child of a Fast parent inherits the Fast preference. The
  wire fields are re-derived for the child's own provider and model route. The
  parent's field is never copied verbatim, because a Fast field valid for the
  parent's provider can be wrong for the child's.
- A normal parent never passes on a `priority` override.
- An explicit child override always wins over the inherited preference.
- A child routed to a different provider or base URL does not inherit the
  parent's route-bound overrides.
- The live personal profile sets `inherit_service_tier: true`.

## Provenance

Fork patch identity: `delegation-service-tier`.

Fork-Patch-Backfill: 893c9262c9beacb9296f7c03f365790b2be2929e; delegation-service-tier

Fork-only. Commit `8a7b4a857e0c` (2026-09-23) landed without a `Fork-Patch`
trailer. The backfill line above records its stable patch ID, so `main` is not
rewritten. Upstream has no equivalent option. Contribute it upstream only if a
delegation-speed issue asks for it there.

## Verification

Run `scripts/run_tests.sh tests/tools/test_delegate_service_tier_inheritance.py`.
It covers the disabled default, re-derivation for the child's wire field, the
normal-parent guard and explicit-override precedence.

## Retirement and rollback

Retire it if upstream ships an equivalent inheritance option, or if the
profile stops using Fast mode for parents. To roll back, revert `8a7b4a857e0c`
and remove `inherit_service_tier` from the live config.
