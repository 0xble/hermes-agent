# Security guidance plugin

Load this unit when changing `plugins/security-guidance/`, its pattern data,
plugin wiring, or focused tests.

## Required behavior

- Keep the pattern scanner local, bounded, and warn-by-default because pattern
  matches can be false positives.
- Preserve `SECURITY_GUIDANCE_BLOCK=1` as an optional supplemental refusal mode,
  never as the sole control for a critical security invariant.
- Scan only the registered write tools and preserve path-aware language filters.
- Do not add LLM review or source-upload behavior without an explicit privacy,
  cost, lifecycle, and authorization design for Hermes.

## Provenance and patches

- Fork patch identity: `security-guidance`.
- Pattern provenance: Anthropic's `claude-plugins-official`, Apache-2.0,
  synchronized from the recorded upstream commit in `plugins/security-guidance/NOTICE`.
- Hermes-side glue, documentation, and tests are maintained under the Hermes
  project license.

## Verification

Run:

```text
uv run pytest -q tests/plugins/test_security_guidance_plugin.py
uv run ruff check plugins/security-guidance tests/plugins/test_security_guidance_plugin.py
git diff --check
```

Also verify the plugin's real discovery path when changing registration or
configuration. Broader plugin-suite failures must name their unrelated missing
dependencies rather than being converted into a pass.

## Retirement and rollback

Retire the local pattern synchronization when Hermes can consume an equivalent
released upstream implementation without losing path filtering, warning/block
semantics, attribution, and tests. Roll back by reverting the logical patch and
removing any separately installed profile copy only when the active profile no
longer depends on it.
