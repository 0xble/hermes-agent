# Reaction Tool Name

Fork patch identity: `react-tool-name`.

## Contract

Advertise and dispatch the reaction tool as `react`, with `👍` as its progress icon. Preserve its opt-in gate, reaction payloads and persistence behavior. Saved `react_to_message` calls remain silent in desktop transcripts, but the legacy name is not registered as a callable alias. The existing retired-name metrics set preserves previously saved rows while current recording uses `react`.

## Upstream Findings

Upstream inspected at `e5f2b15300a3d04978e4c07cc64adc7bcf8e7b1c` still registers the longer name and yellow-heart icon. Related closed [#133210](https://github.com/NousResearch/hermes-agent/pull/133210) broadens gateway reaction availability, not this naming preference. No equivalent rename was found. This fork intentionally adopts Brian's shorter identifier and thumbs-up icon through the existing registry, not a plugin override.

## Verification

Run `scripts/run_tests.sh tests/gateway/test_session_policy.py tests/tools/test_react_tool.py tests/tools/test_reaction_session_identity.py tests/tools/test_display_toggles.py tests/tui_gateway/test_gui_surface_toolsets.py tests/hermes_cli/test_shared_metrics_signals.py tests/hermes_cli/test_shared_metrics_harness.py tests/hermes_cli/test_shared_metrics_efficiency.py`. Run the desktop turn-activity and render-weight tests for current and historical tool names. Regenerate gateway contracts and prompt-surface snapshots with the repository generators.

## Rollback and Retirement

Revert the rename as one unit, including generated artifacts. No reaction storage or configuration migration. Retire this divergence when upstream's registration, progress icon and consumers satisfy the contract.
