# Oneshot plugin-hook discovery

Fork patch identity: `oneshot-plugin-hook-discovery-barrier`.

## Contract

A CLI one-shot turn must not invoke lifecycle hooks against a partially loaded
plugin registry. Startup may launch plugin discovery in a daemon thread, and
`PluginManager.discover_and_load()` marks its registry discovered before loading
plugin modules to prevent recursive discovery. Hook delivery must therefore
join an in-flight discovery worker before reading the registry.

## Failure and fix

The oneshot entrypoint starts background plugin discovery and then builds the
first turn. The delivery path treated `_discovered=True` as equivalent to
"discovery complete" and skipped `_join_background_discovery()`. Under startup
contention, `pre_llm_call` observed an empty hook registry, so the turn still
completed but the plugin marker and injected canary were absent.

`_delivery_manager()` joins the background worker before checking readiness. The
recursion guard (`_discovered`) is not readiness (`_discovery_complete`): a timeout
or re-entry during the sweep refuses partial policy delivery. A sibling foreground
sweep is also awaited through the existing discovery lock. The barrier helpers live in
`hermes_cli/plugins_dispatch.py` and are re-exported by the facade. Policy readers
(`invoke_hook`, `has_hook`, middleware and `iter_hook_callbacks`) use this barrier.
Synchronous observer readers on a running loop instead take a current callback
snapshot without discovery or sweep-lock acquisition; a closed allowlist in the
dispatch owner keeps unknown hooks and all policy/transform hooks strict.

Async hook delivery snapshots a complete registry under a nonblocking sweep-lock
probe, then releases the lock before callbacks. This gives ready registries a no-yield
fast path and prevents a concurrent forced reload from exposing an empty/partial
callback list between the readiness probe and dispatch. A ready registry can still
be locked by deferred platform loading, activation or single-plugin unload; that
contention waits through the existing bounded foreground-lock barrier off-loop,
then re-probes readiness. It must not spin through worker hops just because
`_delivery_manager()` can already return the ready manager. Timeout or lock-wait
failure refuses policy delivery with `PluginDiscoveryIncomplete`. Cold startup and forced
reload await discovery off-loop with `asyncio.to_thread`, then await
callbacks on the caller's event loop. `pre_gateway_dispatch` is an ingress policy
gate, not a fail-open observer: the first message waits for its skip/rewrite policy,
and discovery timeout raises `PluginDiscoveryIncomplete` rather than authorizing
uninspected text. Callback exception handling is unchanged. Gateway post-admission and platform-event probes
also run off-loop; platform-event sync delivery runs there too. Synchronous delivery
called directly on a running loop refuses unready **policy** discovery without joining
or acquiring the lock. The `pre_tool_call` directive path turns that refusal into a
block, never an empty policy decision. Observer delivery uses the available
callback snapshot instead; observer returns cannot authorize or rewrite work.

Plain `discover_plugins()` listing/completion callers log a warning and return the
available registry on a background join timeout, without starting a second sweep. The
strict timeout exception remains in hook delivery, not in skill command completion.
Gateway startup explicitly requests `require_complete=True` off-loop and lets a
not-ready error abort startup before relay registration or adapter connection.
Strict startup waits for the in-flight sweep to finish, logging progress every ten
seconds; per-plugin load deadlines remain the upper bound. A worker failure or
premature exit still raises rather than serving a partial registry.

Synchronous hook calls on an event-loop thread must not wait on discovery.
Observer-only hooks (`on_session_reset`, `agent_loop_stopped`, `pre_command`) use
the current callback snapshot, while policy hooks remain fail-closed and are
delivered through `ainvoke_hook` or an off-loop handoff. Discord and Telegram
`gateway_platform_event` probes use the same non-waiting snapshot, so reloads do
not drop an update or fail its preparation.

Bare-runner dispatch harnesses complete actual discovery during setup to mirror
this startup prerequisite; their short settle/spin windows do not stand in for
plugin readiness.

Regression workers are event-gated rather than sleep-gated. Concurrent independent
tool deliveries carry distinct `tool_call_id` values so this readiness proof does
not collide with the bounded callback gate. A same-call variant deliberately
reuses the identity and verifies that in-flight duplicate suppression remains in
place; production deduplication is unchanged.

## Synchronous call-site inventory

The production AST inventory of `invoke_hook`, `has_hook`, `iter_hook_callbacks`
and `fire_pre_command_hook` has these dispositions:

| Owner / sites | Decision |
| --- | --- |
| `gateway/slash_commands_session.py` reset; `gateway/run_agent_cache.py` interrupt | On-loop observer snapshot; never delay `/new` or `/stop` for a reload. |
| `gateway/run_inbound.py`; `cli.py` command dispatch | `pre_command` is explicitly observer-only in the hook guide and facade; snapshot current callbacks, ignore block/rewrite-shaped returns. `command:<name>` interceptors are a different API. |
| Discord `_platform_events_subscribed`; Telegram `_on_platform_update` | `gateway_platform_event` observer probe snapshots on-loop; a reload alone cannot fail Telegram update preparation. |
| `tui_gateway/session_lifecycle.py` boundary, finalize, interrupt; `hermes_cli/cli_session_mixin.py`, `cli_shutdown.py` | Session observers snapshot if reached on-loop; worker-thread callers retain complete-discovery delivery. |
| `hermes_cli/kanban_db.py` lifecycle, consumed probes, dispatch tick | Every registered Kanban observer name is in the on-loop snapshot allowlist. |
| `agent/api_request_hooks.py`, `turn_response_intake.py`, `auxiliary_hooks.py`, `conversation_loop.py`, `turn_finalizer.py`, `plugin_stream_hooks.py` | API-error/post-request, auxiliary, session, finalization and stream observers snapshot on-loop, including callback iteration; normal agent worker delivery stays strict. |
| `tools/approval_context.py`, `human_input_hooks.py`, `skill_usage.py`, `delegate_tool.py`, `delegate_tool_results.py`; `model_tools.py` post-tool | Approval/input/skill/subagent/post-tool observers snapshot if called from an event loop. |
| `agent/turn_api_request.py`, `turn_context.py`, `turn_stop_gates.py`; `hermes_cli/plugins.py` directive/verification/error-classification helpers | Rewrite/gating hooks remain strict. These are synchronous agent-worker paths, not gateway-loop handoffs. `pre_tool_call` converts discovery failure into a block. |
| `model_tools.py` transform; `tools/terminal_tool_result.py`, `transcription_command.py`, `delegate_tool_progress.py` | Transform/transcription/suppression hooks are not observers and remain strict on their existing synchronous tool/agent worker paths. |
| `hermes_cli/lifecycle.py`, facade and dispatch mixin internals | Shared observer allowlist enforces the distinction. Unknown hooks remain strict; the callback tuple is passed explicitly so a reload cannot alter iteration after selection. |

`pre_gateway_dispatch` already uses async delivery and awaits discovery off-loop.
No policy hook is added to the observer allowlist.

## Verification and retirement

Verify `tests/hermes_cli/test_plugin_delivery_discovery.py` and the plugin
provider discovery tests. The parity one-shot probe must report both the plugin
marker and injected canary under constrained Linux startup. Retire this patch
when the fork's upstream baseline guarantees the same discovery barrier.
