# Delegation service tier and reasoning inheritance

Load this unit when changing how delegated children inherit the parent's
Fast/normal mode or reasoning level: `delegation.inherit_service_tier`,
`delegation.reasoning_effort`, `agent.reasoning_override`, or the child
runtime and request-override resolution in `tools/delegate_tool_config.py`.

## Required behavior

### Fast mode

- `delegation.inherit_service_tier` defaults to `false`. When it is false, a
  child keeps the pre-patch behavior: it inherits unrelated parent request
  overrides, but the parent's transient `service_tier` or `speed` does not
  leak into it.
- When it is true, the parent's Fast *mode* (`priority`, `auto` or `cold`)
  crosses to the child as the child's `service_tier`. This applies whether the
  child inherits the parent's route or `delegation.provider`/`base_url` pins
  another one.
- The parent's wire fields are never copied. A static `priority` mode is
  re-derived for the child's own provider, model and base URL
  (`resolve_fast_mode_overrides`). A route with no fast mode, such as a local
  proxy, gets no fast field.
- Bounded modes stay bounded. `auto`/`cold` open the child's own
  `agent.fast_auto_seconds` window at its first turn and are never pinned into
  static request fields.
- A normal parent never passes on a `priority` override.
- An explicit `service_tier`/`speed` in `delegation.request_overrides` wins, and
  the child then inherits no mode.
- A child routed to a different provider or base URL does not inherit the
  parent's route-bound overrides.

### Reasoning level

- The child's level is resolved in this order:
  1. The parent's explicit session pick.
  2. `delegation.reasoning_effort`.
  3. The parent's configured level.
- An explicit pick is `agent.reasoning_override`, which surfaces set from:
  - gateway `/reasoning` (live and per turn),
  - CLI `--reasoning`, `/reasoning` and `/model --reasoning`,
  - TUI `/reasoning`, `create_reasoning_override` and `/model --reasoning`.
  A `--global` save or a reset is not a session pick. It becomes the
  configured default and clears the marker.
- `explicit_parent_reasoning` honors the marker only while it equals the live
  `reasoning_config`. A model switch or fallback that re-resolves from config
  therefore retires it, without every re-resolution path having to clear it.
- Transports clamp the inherited level to what the child's route supports.
- The live personal profile sets `inherit_service_tier: true` and
  `reasoning_effort: medium`. Children therefore run at medium unless the user
  explicitly picks a level for the session.

## Provenance

Fork patch identities: `delegation-service-tier`, `delegation-explicit-inheritance`.

Fork-Patch-Backfill: 893c9262c9beacb9296f7c03f365790b2be2929e; delegation-service-tier

Fork-only. Commit `8a7b4a857e0c` (2026-09-23) landed without a `Fork-Patch`
trailer. The backfill line above records its stable patch ID, so `main` is not
rewritten. Its route gate (`inherit_parent_route`) made pinned-provider
children skip inheritance entirely.

The follow-up patch `delegation-explicit-inheritance` changes two things:
- The Fast mode now crosses independently of the route.
- The reasoning level resolves to the explicit pick first, then the delegation
  default.

Upstream has no equivalent option. Contribute it upstream only if a
delegation-speed issue asks for it there.

## Verification

Run:

```
scripts/run_tests.sh tests/tools/test_delegate_service_tier_inheritance.py \
  tests/tools/test_delegate_explicit_inheritance.py \
  tests/gateway/test_running_agent_session_toggles.py
```

These cover:
- the disabled default,
- pinned-route inheritance and wire-field re-derivation,
- no fast fields on a route without a fast mode,
- bounded modes,
- the normal-parent guard,
- explicit-override precedence,
- the reasoning precedence table, including stale markers and an explicit
  `none`,
- surface marking and reset.

## Retirement and rollback

Retire it if upstream ships an equivalent inheritance option, or if the
profile stops delegating with Fast or reasoning picks. To roll back, revert
the `delegation-explicit-inheritance` commit first. Then revert `8a7b4a857e0c`
and remove `inherit_service_tier` from the live config.
