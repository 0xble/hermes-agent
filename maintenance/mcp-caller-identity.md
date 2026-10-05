# MCP caller identity

Load this unit when changing MCP tool-call dispatch (`tools/mcp_tool_handlers.py`),
per-server opt-in bookkeeping in `tools/mcp_tool_discovery.py`, the identity capture in
`tools/mcp_tool_caller.py`, or `gateway/session_context.py::bound_session_env`.

**Patch identity:** `mcp-caller-identity`.

## Required behavior

- `mcp_servers.<name>.caller_identity` defaults to `false` and is parsed like
  `supports_parallel_tool_calls` (`_parse_boolish`: unrecognized values warn and count
  as `false`). The opt-in is the calling profile's policy, keyed by its own server key,
  so one profile's opt-in never sends another profile's identity over a shared
  connection.
- When opted in, every `tools/call` carries request
  `_meta["hermes/caller"] = {"profile", "session_id", "topic_session_id"}`.
  `session_id` is the calling turn's ContextVar-bound `HERMES_SESSION_ID`.
  `topic_session_id` is the nearest session in the `parent_session_id` chain whose
  `source` is not `subagent` (the session itself when it is not one), read from the
  profile's `state.db` read-only. Missing rows, cycles or an unreadable database
  give `null`. `profile` is the bound `HERMES_SESSION_PROFILE`, else the profile
  derived from the bound Hermes home.
- Identity is captured in the calling thread inside the registry handler, before the
  call is scheduled onto the MCP loop, and closed over by the coroutine (retries reuse
  it).
- Once any session has been bound in the process, an unbound ContextVar means no
  session: the `os.environ` mirror is never read, because it holds whichever
  concurrent turn wrote last. A process that never bound a session (plain CLI) uses
  its own single-session mirror, matching the subprocess env bridge.
- No session id means no `_meta` at all. A tool argument named `_meta` is an ordinary
  argument and is never merged into request `_meta`. A server that did not opt in
  gets `call_tool(name, arguments=...)` exactly as before, with no `meta` keyword.

## Provenance

Requested by the relay fleet design (2026-10-05, decision R1): relay's MCP surface
attributes each `send` to a session from request `_meta` only and fails closed when it
is missing, replacing the hand-written `relay_session` plugin's per-call
`build_subprocess_env()` identity. The ancestor walk is that plugin's `_root_session`
moved into core.

Upstream search (2026-10-05; MCP `_meta`, caller or session identity,
`HERMES_SESSION_ID` with MCP) found one related item:
[NousResearch/hermes-agent#91427](https://github.com/NousResearch/hermes-agent/pull/91427),
open and unreviewed since 2026-08-21. It uses the same mechanism (snapshot before the
loop, `call_tool(..., meta=...)`) but sends the gateway *user* id to every server by
default under a configurable key, and bundles unrelated Feishu/WeCom changes. It
carries no session or topic identity and no opt-in, so it does not replace this
patch. If it lands, fold this payload into its request-`_meta` path rather than keep
two. Retire this patch if upstream ships per-call session identity for MCP servers
with an equivalent opt-in and the same model-cannot-influence guarantee.

On CPython 3.11, `run_coroutine_threadsafe` copies the calling thread's context through
`call_soon_threadsafe`, so the loop task does see the caller's ContextVars today.
Capture stays in the calling thread so the identity does not depend on that scheduling
detail.

## Verification

`scripts/run_tests.sh tests/tools/test_mcp_caller_identity.py` covers opt-in versus
default (no `meta` keyword), two concurrent sessions with a third id in `os.environ`,
a delegated grandchild mapping to its non-subagent ancestor across a compression
parent, unresolvable chains (`null`), unbound sessions (no `_meta`), config parsing,
and a real stdio MCP server that reads `hermes/caller` from the request `_meta` it
received while a model-supplied `_meta` argument does not displace it.

## Rollback

Revert the `Fork-Patch: mcp-caller-identity` commit. It touches only the files listed
above, the MCP docs (user guide, config reference, multi-profile note), this unit, and
its index row. Servers configured with `caller_identity: true` then receive plain
calls, and a server that requires the identity (relay) refuses them.
