# Named Remote Computer Use

Fork-Patch identity: `remote-computer-use`.

## Divergence And Provenance

The fork routes the existing `computer_use` tool through a profile-owned shared
`nodes` registry and the existing CuaDriver provider. Node capabilities remain
under each shared identity so later adapters do not duplicate addressing.

The transport follows CuaDriver's documented SSH stdio MCP route, checked against
[cua-driver v0.32.0](https://github.com/trycua/cua/tree/58aba84b5b83d77e7e2b0f006547699eb594e50d/libs/cua-driver/rust)
and its [remote guide](https://cua.ai/docs/cua-driver/guides/vms-and-remote).
[OpenClaw's node interface](https://docs.openclaw.ai/nodes/computer-use) supplied
interface inspiration only. No external implementation was copied.

Upstream proposals [#103653](https://github.com/NousResearch/hermes-agent/pull/103653)
and [#104080](https://github.com/NousResearch/hermes-agent/pull/104080), and
[issue #103932](https://github.com/NousResearch/hermes-agent/issues/103932), were
open at design preflight. The larger HTTP bridge is not adopted: existing SSH
identity and MCP transport avoid another server, port, pairing secret, or service.

## Update Rules

Recheck upstream remote routing before modifying the registry or provider seam.
Preserve local behavior without configured nodes. Never substitute the controller
or a sibling after selecting a node. Keep profile, session, and node in cache
identity, with per-node locks and persistent MCP lifetimes.

CuaDriver's MCP proxy can auto-start a standard public daemon. Its logical
session label does not survive transport closure. Retain preflight and
post-connect daemon-mode observation, capture invalidation after recovery, and
outcome-unknown failures without write replay. Remote startup policy belongs to
the node's public-daemon owner, not this adapter or the controller's private
daemon. Controller approvals are a separate boundary.

## Proof And Retirement

Run `scripts/run_tests.sh tests/tools/test_computer_use_nodes.py` with isolated
`HERMES_HOME` and `HERMES_RUNTIME_DIR`. Its public-dispatch integration uses real
MCP stdio with only SSH substituted, including mode-race, profile interleaving,
image blocks, failure deadlines, approval denial, and child cleanup.

Also run the existing computer-use lifecycle, provider, capture, CLI, profile-cache,
and MCP-image suites. Real remote desktop and channel image delivery remain
promotion-time acceptance checks, not claims inferred from synthetic SSH tests.

Retire this divergence only when upstream provides the same shared-node,
no-fallback, persistent-session and native-image contracts without requiring a
new service, and exact-target integration checks still pass.
