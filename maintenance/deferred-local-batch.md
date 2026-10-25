# Deferred local tool_call batching

Fork patch identity: `deferred-local-batch`.

## Required behavior

A multi-entry deferred `tool_call` containing local tools is not rejected merely
for batching. The agent turn splitter remains the owner of per-tool scheduling,
inline tools, persistence, and concurrency. Direct bridge calls dispatch entries
sequentially through the ordinary schema, session-scope, hook, middleware, and
approval pipeline, and return every entry's result in call order.

Every entry is scope- and schema-checked before any entry runs. Batches above
`MAX_CALLS_PER_DISPATCH` fail with a cap error before any entry runs, in both the
direct dispatcher and the executor unwrap. A `/stop` between entries leaves the
unstarted entries unrun and reports them as `INTERRUPTED`. The internal
`local__execute` sentinel never reaches hooks, guardrails, or the activity feed.
The wrapper's skip flags never reach an entry: each entry runs its own
`pre_tool_call` hook and request/execution middleware under a unique child id
(`<parent>:<index>`, or a random parent prefix when the wrapper has no id), and
entry errors are normalized to `{code, message}`. An entry is an error only when
it carries a truthy `error` and does not claim `success: true`, so results such as
`{"success": true, "error": null}` pass through unchanged; the direct local and
connector dispatchers share this predicate (`batch_entry_error`), and every cap
check, the agent splitter's included, goes through `batch_cap_error`. For direct
bridge callers only, a batch mixing local and `connectors__` entries fails before
any entry runs with an error telling the model to split them into separate
`tool_call` batches; the agent-loop splitter instead expands a mixed batch into
single-entry calls and runs each entry as a singleton. Direct-batch scope and
schema failures name the failing entry (`calls[<index>]: ...`), and an entry naming
a directly-listed core tool gets the same `not_deferrable_error` guidance as the
single-entry path, not a `tool_search` instruction.

## Provenance and proof

Upstream commit `2ce704550b5f` added the agent-side local batch splitter and changed
the schema to promise batching, but the direct bridge resolver retained its
single-entry rejection. This patch closes that remaining entry point, leaving
the agent-side scheduling contract unchanged.

Run:

```sh
scripts/run_tests.sh tests/tools/test_connector_local_batches.py tests/tools/test_connector_bridge_wiring.py tests/agent/test_local_tool_batches.py
```

## Retirement and rollback

Retire when the accepted upstream release supports lossless, bounded,
interruptible direct local bridge batches and these behavior tests pass without
this patch. Revert commits carrying this identity to roll back; no
persisted-state migration is involved.
