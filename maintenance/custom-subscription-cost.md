# Custom subscription billing

## Required behavior and prerequisites

An explicitly configured custom subscription route reports zero marginal cost
with `cost_status=included`, rather than inventing per-token subscription prices.
Configuration is authoritative, not the hostname or the absence of provider cost.
The provider-reported-cost unit remains the owner of metered response charges;
read [that unit](provider-reported-cost.md) when changing the priced path.

## Patch record

- Identity: `custom-subscription-cost`.
- Contribution route: fork-only integration of profile configuration with usage
  billing. No donor patch is used.
- Upstream disposition: read-only preflight against upstream
  `79b45d1647f6b5680768d851c4c406e8cc550a1e`; upstream PRs #136076
  (Nous subscription usage) and #130786 (pricing normalization) are related but
  do not provide custom-route subscription declarations. No upstream PR opened.
- Fork delivery: draft fork PR; publication and exact revision live in Git/PR.
- Adaptation: retain `billing_mode` in canonical provider-entry normalization;
  read the same enabled route identity and profile-scoped configuration as other
  provider metadata. Named custom aliases match their own entry and URL. Bare
  `custom` is included only when all enabled entries at that exact URL opt in.
  Billing reads every declaration in `providers` and legacy `custom_providers`,
  without the picker's display-name/URL/model deduplication. Distinct aliases
  with identical display names remain separate billing evidence.
- Review repair: P1 on draft fork PR #478 demonstrated that picker deduplication
  hid an undeclared/metered alias behind an included alias at the same URL. Keep
  the existing normalization and enabled-entry rules, but evaluate unanimity
  before any display deduplication. This is a fork-only correction to the same
  configuration integration; the recorded upstream preflight remains applicable.
- Runtime-identity repair: the second P1 on #478 showed that runtime resolution
  kept `requested_provider=custom:<name>` but dispatched as `provider=custom`.
  Main-turn billing now selects the named identity without changing transport,
  and persists it to both session and per-model usage. CLI `_current_runtime`
  and gateway `_runtime_agent_kwargs` / `_resolve_turn_agent_config` already
  carry the selected identity into `AIAgent`; regressions exercise both paths.
  Empty-response attempt pricing and MoA reference pricing use the same helper.
- Regression proof: `tests/agent/test_custom_subscription_cost.py` exercises both
  config shapes, named/bare routes, same-process profile A/B/A switching, and
  real main-turn SessionDB persistence. Shared display-name regressions cover
  both declaration orders, each config shape and mixed legacy/keyed entries,
  normalized trailing-slash URLs, disabled declarations, and positive reported
  costs. Existing
  `tests/hermes_state/test_provider_reported_cost.py` exercises positive provider
  charges and nonzero main-turn actual cost persistence; nested/flat response
  parsing is covered by `tests/agent/test_usage_pricing.py`.

## Configuration decision

The previous runtime ignores provider-entry `billing_mode`, even when the key
already exists. This patch makes that declaration effective; it does not edit
any live profile or infer billing from loopback addresses.

For the authorized local subscription proxy routes, the parent can set:

```yaml
providers:
  codex-proxy:
    billing_mode: subscription_included
  claude-proxy:
    billing_mode: subscription_included
```

Merge these fields into the existing entries; preserve every credential and
other setting. Legacy `custom_providers` list entries support the same field.
These settings affect future recording only, not historical unknown rows.

An explicit `subscription_included` declaration intentionally takes precedence
over a positive generic `raw_usage.cost`: subscription proxies may forward a
list-price token valuation that is not an additional user charge. The recorded
amount is marginal spend, not that valuation. Use this declaration only for a
route whose requests are covered by the subscription; an actually metered route
must omit it. Ambiguous bare routes do not qualify, so their positive reported
cost remains `actual` / `provider_cost_api`. Provider-profile-specific cost
results retain their existing precedence; this patch does not change them.

## P1 regression evidence

At `d20c7b4b2f276acdfa84f52f7e995e3769bac8c1`, the shared-display-name
regression was RED before changing the implementation. After reading the raw
route declarations, the same selection was GREEN:

```text
RED:   Summary: 1 files, 1 tests passed, 5 failed, 1 file FAILED (100% complete) in 1.7s (14 workers)
GREEN: Summary: 1 files, 6 tests passed, 0 failed (100% complete) in 1.3s (14 workers)
```

Both used `scripts/run_tests.sh tests/agent/test_custom_subscription_cost.py -k
shared_display_names` through the machine-wide heavy-slot gate and the private
worktree `.venv`. The full focused run (subscription, usage pricing,
provider-reported persistence, config, and non-mutating normalization) passed
274 tests with 0 failures and 4 Windows-only skips on macOS. Ruff and
`scripts/check_fork_patches.py` passed.

## Runtime-identity P1 regression evidence

At `3a2314dfe81219ccfacd5b0f7760b3fce44bb65a`, the runtime-selected-route
regression was RED: both CLI and gateway priced the explicitly selected included
alias as `actual` / $0.25, and both named routes persisted `custom` instead of
their declaration identity. Bare custom remained correctly metered. The same
selection was GREEN after carrying billing identity to pricing and persistence:

```text
RED:   Summary: 1 files, 2 tests passed, 4 failed, 1 file FAILED (100% complete) in 9.5s (14 workers)
GREEN: Summary: 1 files, 6 tests passed, 0 failed (100% complete) in 3.6s (14 workers)
```

Both used `scripts/run_tests.sh tests/agent/test_custom_subscription_cost.py -k
runtime_selected_route` through `heavy-slot --label io-cost-p1b` and the private
worktree `.venv`. The final wider run includes named/bare runtime paths,
empty-response and MoA reference pricing, native-transport isolation, usage
pricing, provider-reported persistence, runtime resolution/late binding, and
custom provider switching/normalization:

```text
Summary: 9 files, 270 tests passed, 0 failed (100% complete) in 8.4s (14 workers)
```

Ruff, `git diff --check`, and `scripts/check_fork_patches.py` passed. No live
configuration, runtime installation, or activation changed.

## MoA main-turn P1 and recording-site audit

At `005522a7c4ef5bb196cd38363062cf8923db97f7`, the real `AIAgent` / MoA facade
priced a `custom:included` aggregator as `actual` / $0.25 and persisted the
virtual `moa` / `moa://local` route. The normalized preset slot has no resolved
endpoint. Preserve the resolved aggregator billing coordinates on the facade
before dispatch (including streaming and prepared requests), then replace them
with the successful response's route if recovery moved the call. Main-turn
pricing and both SQLite usage tables consume that same snapshot. Transport
routing stays `moa` / the preset; no credentials enter the billing snapshot.

The self-audit covered every production `estimate_usage_cost`,
`billing_provider` / `billing_base_url`, and token/auxiliary usage writer. The
sibling repairs below belong to the same `custom-subscription-cost` identity:

| Pricing or recording site | Billing route source |
|---|---|
| `turn_usage.record_response_usage` (ordinary turns) | Active provider plus selected `requested_provider`; active endpoint and model |
| `turn_usage.record_response_usage` (MoA) | Facade's resolved `last_aggregator_billing_route`, refined by the successful response; not the preset or virtual endpoint |
| `moa_loop._price_reference_response` | Each reference's own resolved runtime, refined by successful response metadata; priced reference spend retains the existing main-turn fold |
| `empty_response_guard._estimate_attempt_cost` | Same active custom identity, or the MoA aggregator's resolved billing snapshot |
| `codex_runtime._record_codex_app_server_usage` | Selected custom identity plus active endpoint/model; usage-less calls resolve billing mode rather than assuming every route is included |
| `auxiliary_client` primary sync/async calls | Successful client's provider identity and endpoint; named declarations survive transport normalization |
| `auxiliary_client` retry/model-downgrade/credential-refresh calls | Retried client's effective provider and endpoint, not an empty route or stale original declaration |
| `auxiliary_client` provider fallback | Successful fallback destination's provider and endpoint, in both directions between included and metered routes |
| `aux_accounting.record_aux_usage` (titles, compression, vision, summarization, etc.) | Successful route from response validation; persist included mode and cost status/source without moving cost between legacy display buckets |
| Built-in compressor | Live main runtime's selected identity, inherited only when model/provider/endpoint match compressor state; actual auxiliary destination owns recording |
| Background-review fork and parent aggregate | Preserve selected identity in fork construction; snapshot the fork's actual identity (resolved aggregator snapshot for MoA) and cost provenance |
| Delegation session-cost rollup | Child's already-priced main-turn counters; no parent-route repricing or separate session-cost write |
| `SessionDB` queued token writes / `update_token_counts` | Producer-supplied resolved billing coordinates; first accounted turn repairs the initial virtual/requested session route |
| `SessionDB.record_auxiliary_usage` / `_record_model_usage` | Producer-supplied successful route plus included mode/status/source; no main-route fallback for auxiliary rows |
| Insights fallback estimates (read-only) | Persisted `billing_provider` / `billing_base_url` and model; no session-cost write |
| Langfuse export (external observability) | Hook-provided route coordinates; its explicit subscription policy exports no authoritative marginal cost and lets Langfuse estimate nominal value; no SessionDB or in-memory session-cost write, unchanged |

The billing identity helper also refuses to overwrite a different concrete
`custom:<name>` destination with a stale selected alias. The auxiliary auto-route
cache now distinguishes selected aliases sharing an endpoint. Cost amounts stay
in their existing session and auxiliary display buckets; this repair does not
reclassify history or change the provider-reported-cost unit's combine semantics.

The regression uses the real facade, runtime resolver, OpenAI client, turn
accounting, and SessionDB; only `Completions.create` is replaced. Included and
metered aliases share an endpoint and forward positive `usage.cost=0.25`. All
prepared/non-prepared and streamed/non-streamed combinations check both SQLite
tables and empty-response attempt pricing. Additional real-routing regressions
cover sync/async auxiliary calls, retries, alias cache isolation, both fallback
directions, live compressor alias changes, background review, and custom
Responses accounting.

```text
RED:   Summary: 2 files, 36 tests passed, 22 failed, 2 files FAILED (100% complete) in 34.8s (14 workers)
GREEN: Summary: 2 files, 58 tests passed, 0 failed (100% complete) in 20.6s (14 workers)
WIDER: Summary: 98 files, 1257 tests passed, 0 failed (100% complete) in 41.6s (14 workers)
```

RED used baseline production code at the recorded head with the new regressions.
GREEN and WIDER used the repaired production code. All ran in the private
worktree `.venv`, through `heavy-slot --label io-cost-p1c -- scripts/run_tests.sh`
in background processes whose exit was collected. GREEN selects both custom
subscription files; WIDER adds usage pricing, provider-reported and auxiliary
persistence, every MoA suite, auxiliary routing/recovery, compressor,
background-review, Codex, empty-guard, and delegation suites. Two Nous stale-auth
regression mocks now accept the validator's billing-coordinate keyword arguments.
Ruff, `git diff --check`, and `scripts/check_fork_patches.py` pass. No live
configuration, runtime installation, or activation changed.

### Auxiliary transport and auto-vision review repair

The follow-up review of #478 found that choosing the billing declaration before
text-client resolution replaced a normalized custom runtime's live endpoint and
credential with its configured alias endpoint. Resolve the main transport first,
then retain the selected custom alias only in the successful route's billing
identity. Auto-vision follows the same order and carries that identity only when
its main-runtime candidate succeeds; fallback destinations retain their own
billing identity. The route regressions cover a live/declaration endpoint
mismatch and sync/async auto-vision included-cost persistence versus a different
fallback destination.

## Update and retirement

Preserve explicit opt-in, disabled-entry filtering, exact route identity, and
profile scoping. Retire this divergence once upstream exposes equivalent
configuration through the real usage path; preserve the metered persistence
regressions while adopting it. Mixed included/metered aliases at one URL must
continue to leave a bare `custom` route unknown.
