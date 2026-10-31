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

## Named Terminal, File And Process Consumers

`tools/node_execution.py` binds the resolver's immutable node for a call, with
profile/session/connection-qualified mutable state and a per-scope lock. Raw
`task_id` remains the hook, process-owner and cleanup identity. Named SSH uses
`sync_files=False`, never a temporary mutation of profile terminal policy.
`tools/file_backend_state.py` uses the existing SSH file backend for target-side
realpaths, metadata and hashes. Keep read stamps, patch baselines, compaction
reset, scope locks and cleanup aligned with that same backend identity.
Controller twins must not satisfy remote existence or overwrite authorization.
Mutation locks span sessions within one controller for the same configured SSH
alias, platform and resolved path. Distinct aliases for one host and separate
controllers do not share these locks. Read authority remains session-scoped.

The existing process record carries `SSHProcessBinding`, not another registry.
Its immutable alias, originating profile home and target-side incarnation UUID
survive checkpoint and completion receipt serialization. The UUID is a run
identity, not a new service credential. Profile-filtered list and release sweeps
must filter before probing remote handles. `tools/process_registry_ssh.py` uses a per-command POSIX
supervisor holding its unreaped child's identity and a token-scoped receipt. It
is not a resident service. Stop requests go to that supervisor, never to a bare
remote PID or local kill helper. Persist the binding before launch, never replay
on lost acknowledgement, and retain unknown/unavailable state when proof is
missing. Old SSH checkpoints without the binding remain unverified and cannot
be turned into local process identities.

Named macOS/Linux execution requires target Bash and Python 3. Remote PTY and
stdin interaction refuse. ACP named edit proposals refuse before controller
diff construction. Rich controller document extraction and browser adapters are
not supplied by this consumer. Preserve legacy behavior with no selected node.

## Proof And Retirement

Run `scripts/run_tests.sh tests/tools/test_computer_use_nodes.py` with isolated
`HERMES_HOME` and `HERMES_RUNTIME_DIR`. Its public-dispatch integration uses real
MCP stdio with only SSH substituted, including mode-race, profile interleaving,
image blocks, failure deadlines, approval denial, and child cleanup.

Also run the existing computer-use lifecycle, provider, capture, CLI, profile-cache,
and MCP-image suites. Real remote desktop and channel image delivery remain
promotion-time acceptance checks, not claims inferred from synthetic SSH tests.

Also repeat `tests/tools/test_named_node_execution.py` and
`tests/tools/test_node_process.py`, plus the existing terminal-scope, SSH,
file-baseline, process-checkpoint and ACP regressions. The integration fixture
exercises `model_tools.handle_function_call`, real caches and backend/process/file
layers on the actual controller OS, replacing only SSH transport with separate
fixture-owned peer filesystems and processes. This proves source invariants,
not live VPS/Telegram execution. Exact activation, real Mac command/read/edit
proof and channel/browser acceptance stay with the migration and Fork Patches
owners.

Retire this divergence only when upstream provides the same shared-node,
no-fallback, persistent-session, node-bound file/process and native-image contracts
without requiring a new service, and exact-target integration checks still pass.
