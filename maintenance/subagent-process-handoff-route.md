# Subagent Process Handoff Route

Load this unit when changing `process_manage(action='handoff')`,
`ProcessRegistry.transfer_ownership`, gateway process-watcher route following, or the
checkpointed route of a background process.

## Required Behavior

- An accepted handoff moves the promised completion together with teardown ownership,
  under the same registry lock that rejects an exited process. The new owner, the
  parent's session-db id (`parent_session_id`) and `notify_on_complete=True` are set
  before the exit path can publish, so a process that exits during the handoff still
  reports to the parent, never to the child.
- The route key stays the chat's gateway key (`HERMES_SESSION_KEY`). Only when no gateway
  key is bound (CLI, TUI) does it fall back to the parent's session id, which is how
  those drains claim events. The parent's session-db id never replaces a gateway key.
- A process the child launched silently still reaches the parent: in a session that
  supports async delivery, the handoff stamps the chat's routing (with the parent's
  session id) when the process has none, and arms one completion watcher when none
  exists (a watcher exists only for a notify launch, so it already delivers to the
  agent). A process that already has a watcher keeps it. The watcher adopts the live
  route on its next tick: a moved session key (topic retirement) or a handed-off
  process whose spawning session id changed. Exactly one watcher delivers, and a
  completion after `/new` is dropped by the existing pre-flight against the parent's
  session id.
- The checkpoint persists the transferred route and the redacted handoff note, so a
  restart-recovered watcher addresses the parent, not the child.
- Watcher and queue completion events carry `handoff_note` and `parent_session_id`, so
  the parent's notice names the purpose the child gave.
- Legacy checkpoints with a bare session id resolve their persisted session-store
  origin before parsing route metadata. If that origin is unavailable, the store may
  supply an unambiguous chat type for the watcher's exact platform, chat and profile.
  A sibling session never supplies a missing thread. Unknown or ambiguous chats remain
  unroutable rather than being guessed. No checkpoint migration is required.
- An unresolvable process source warns once per process per gateway lifetime. Failed
  recovery work has a 30-second cooldown, invalidated by route changes, store replacement
  or routing-generation advancement. The retry cache is capped at 512 entries. No
  checkpoint, output or completion is discarded. Async-delegation diagnostic sources
  retain their existing DEBUG-only behavior.

## Entry Points

`tools/terminal_tool_background.py::handoff_process` validates the live spawn tree and
performs the transfer. `tools/process_registry.py::_handoff_process` is the dispatch
shim. `ProcessRegistry.transfer_ownership` owns the locked field moves, pending-watcher
updates and the checkpoint write. `gateway/run_notifications_ownership.py::_follow_process_route`
is the watcher's route adoption.
`gateway/run_notifications_sources.py` owns synthetic source reconstruction and
per-process unresolvable-source warning suppression.

## Provenance

Fork patch identity: `subagent-process-handoff-route`.

Upstream added the handoff action in `3c0d90e8ef` but transfers only the owner and
writes the parent's session-db id into the gateway route key. Upstream issue
[#131578](https://github.com/NousResearch/hermes-agent/issues/131578) and open
third-party PR [#108483](https://github.com/NousResearch/hermes-agent/pull/108483)
describe the same gap. Recovered from uncommitted work by the 2026-10-26 lost-work audit.

## Verification

```bash
scripts/run_tests.sh tests/tools/test_process_handoff_routing.py \
  tests/tools/test_subagent_process_handoff.py tests/gateway/test_telegram_retire_work.py \
  tests/gateway/test_background_process_notifications.py \
  tests/gateway/test_process_event_source_recovery.py
```

`test_process_handoff_routing.py` drives a real process through real tool dispatch,
child teardown, checkpoint I/O and gateway admission. Against the unpatched code 11 of
its 12 cases fail: the completion keeps the child's session id.
`test_process_event_source_recovery.py` loads a real legacy process checkpoint and a
reloaded routing store, then drives its completion to gateway admission. It also
covers exact-chat fallback, explicit-thread preservation, fail-closed unknown chats,
and warning deduplication across watcher ticks and a fresh completion event. Failed
route scans are bounded while route-write and cooldown-expiry recovery remain intact.

## Retirement And Rollback

Retire when upstream transfers the parent's session id, notify intent and watcher route
with handoff ownership. Roll back by reverting this patch. The added checkpoint key
`handoff_note` is ignored by older code.
