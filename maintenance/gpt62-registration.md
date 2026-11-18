# GPT-6.2 Sol and Astra registration

Narrow model-registration carry for `gpt-6.2-sol` and `gpt-6.2-astra`. Load it
when touching OpenAI/Codex model catalogs, reasoning-effort ladders, context
windows, Ultrafast eligibility or OpenAI usage pricing, or when a release sync
conflicts in those files. The root `MAINTENANCE.md` owns the shared adoption
policy.

## Preserve

- Both IDs are recognized as exact slugs: no `-pro`, `-mini`, `-900k` or dated
  variants are invented, and predecessors (`gpt-6-sol`, `gpt-6.1-sol`) keep
  their ladders.
- Both take the Astra-style ladder `low, medium, high, xhigh, max`, with no
  disable (`none`). Whether `minimal` is rejected on the wire is unverified here;
  Hermes simply never offers it.
- Codex OAuth advertises them only after verified account discovery. A stale
  cache, `config.toml` default or curated fallback never confers entitlement.
- `DEFAULT_CODEX_MODELS` keeps `gpt-6-sol` first so the implicit default works on
  plans without GPT-6.2.
- Fast and Ultrafast are priced from the tier the response reports it was served
  at. The launch Ultrafast multiple changes at 2027-03-01 00:00 Pacific
  (08:00 UTC).
- Regional OpenAI hosts follow `hermes_cli/models_fast_regions.py`: both IDs get
  Fast and Ultrafast on `us.api.openai.com`, and neither tier on
  `eu.api.openai.com`, because OpenAI documents GPT-6.2 Ultrafast as US-only and
  one row per host cannot express EU Fast without EU Ultrafast.

## Provenance and patches

- **Identity and status:** `gpt62-registration`, active, adapted.
- **Source:** upstream PR NousResearch/hermes-agent#157141 (open at carry time),
  head `293b2e0caf84b0177b827e38e3494585dcdbc19e`, ten commits by Vortex Openclaw
  from `8b4a6d4bf23` through `293b2e0caf8`; plus `510857dbbac` (pricing) and
  `d362ab0558c` (Ultrafast), both by BearHuddleston on
  `upstream/fix/gpt62-ultrafast`.
- **Surfaces:** `agent/model_metadata.py`, `agent/models_dev.py`,
  `agent/reasoning_effort.py`, `agent/transports/codex.py`,
  `agent/usage_pricing.py`, `hermes_cli/codex_models.py`, `hermes_cli/models.py`,
  `hermes_cli/models_catalog_static.py`.
- **Adaptations:** the Desktop, ACP and TUI surfaces from #157141 and its
  documentation page are not carried. Everything else is the upstream text,
  including the `models_fast_regions.py` docstring and regional test from
  `d362ab0558c`.
- **Declared, not verified:** context 1,050,000 overall, 922,000 input and
  128,000 output (from the upstream PR); Codex OAuth fallback windows 272,000
  Astra and 500,000 Sol (from the upstream PR). Pricing follows OpenAI's
  published model pages as cited in `510857dbbac`. Live inference, long-context,
  tool-use and image behavior are unverified. The cache-read and replay gaps in
  upstream issues #157129 and #157124 are not addressed.
- **Upstream disposition:** unmerged at carry time.

## Update

On a release sync, compare against the upstream release before keeping any of
this. If upstream already registers both IDs, take upstream's text in every
surface above and drop the matching hunks here. Where upstream lands only part
of it, keep only the missing parts.

Retire this unit once an upstream release carries equivalent registration,
ladder, context, Ultrafast and pricing support for both IDs.

## Verify and recover

Contract tests: `tests/agent/test_gpt62_support.py`,
  `tests/hermes_cli/test_openai_cost_safe_defaults.py`,
  `tests/hermes_cli/test_regional_fast_mode.py`.

Related suites: `tests/agent/test_openai_ultrafast.py`,
`tests/agent/test_usage_pricing.py`, `tests/hermes_cli/test_codex_models.py`,
`tests/hermes_cli/test_model_cache_swr.py`.

To roll back, revert the commit carrying `Fork-Patch: gpt62-registration`.
