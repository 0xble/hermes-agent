# MCP Reconnect Manifest

Fork patch identity: `mcp-reconnect-manifest`.

## Provenance and Adoption

The discovery early return in fork base `c65b15b8c0464ea2db885c5d45abf714afd42bc6`
also exists in upstream main `7ee6d99c4f16df08cd6ca0b2c34abb81014db2ba`.
Existing MCP maintenance units cover process reaping, shutdown and caller
identity, not tool-manifest publication.

Related upstream proposal: [PR #127124](https://github.com/NousResearch/hermes-agent/pull/127124),
open at inspection. This fork change independently reproduces stale descriptions
through the real registry and memoized `get_tool_definitions()` path, then
shares the existing notification reconciliation with reconnect discovery.
Upstream disposition and fork delivery remain separate: this is a fork patch,
not a merged upstream fix.

## Update and Retirement

Keep the fetched-manifest publication shared by reconnect and list_changed.
Do not re-list tools during publication or change the live-agent prefix policy
in `tools/mcp_tool_agent.py`. Adopt an upstream equivalent when it preserves
removed/ambiguous-name cleanup and fresh-agent schema invalidation. Retire this
unit and patch when the accepted upstream release passes
`tests/tools/test_mcp_reconnect_manifest.py` unchanged.

## Proof and Rollback

Run the reconnect-manifest regression alongside dynamic discovery, capability
gating, schema-cache and registry-collision tests through the canonical runner.
The regression must fail on the fork base with the old description, then pass
with updated descriptions and parameters, no removed tool, one tools/list call,
and an unchanged previously returned snapshot. Roll back the shared helper,
reconnect call site and regression together if publication must be reverted.
