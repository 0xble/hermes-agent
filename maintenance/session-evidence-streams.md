# Session Evidence Streams

## Required Behavior And Ownership

Fork patch identity: `session-evidence-streams`. This is an intentional fork-only acquisition surface, not conversational recall or a memory producer. It adds `sessions state inventory` and `sessions state transcript --session-id` beside the bounded inspection commands. Their independent projection/JSONL contract is documented in [CLI reference](../website/docs/reference/cli-commands.md#session-graph-and-audit-streams), with source allowlists in `hermes_state_evidence.py` and encoding/redaction in `hermes_cli/session_evidence.py`.

Load [typed session-state inspection](session-state-inspection.md) when changing shared CLI dispatch. Its native tool keeps the existing no-transcript and size-cap guarantees. Do not route these evidence streams through model-tool caps or ordinary export's model/display loaders. They intentionally preserve raw audit and graph boundaries instead of resolving a conversation.

## Provenance And Disposition

Implementation starts independently from maintained fork base `286bd6320dfd80f896ace6a14abf788abffdf08b`. Upstream design comparison is pinned to `e5f2b15300a3d04978e4c07cc64adc7bcf8e7b1c`. Related [upstream issue #90719](https://github.com/NousResearch/hermes-agent/issues/90719) requests explicit per-message provenance for injected user messages, rather than trusting the user role. [Issue #116651](https://github.com/NousResearch/hermes-agent/issues/116651) reports reasoning leakage from ordinary exports. Neither supplies this capability or a donor implementation. No upstream patch was adopted, and no prior-art authorship is claimed.

Upstream disposition: no contribution submitted, upstream publication is outside this delivery's authority. Fork delivery: an isolated maintained-fork candidate for integration on `main`, with activation owned separately by the existing maintenance integration owner. No schema, dependency, policy or producer change is part of this unit.

The narrow snapshot bypasses the general `SessionDB` constructor's FTS `SELECT * ... LIMIT 0` probes, while reusing its tracked `mode=ro` connection factory, URI builder, loop guard and test-isolation guard. Do not replace it with a general constructor/load-history route without proving that unrelated body columns remain unselected. The audit query pins the source-owned `(session_id, id)` index so stale planner statistics cannot materialize and sort an entire segment.

## Update And Verification

For upstream adoption, inspect physical row ordering, origin/display columns, compaction and rewind flags, native index identity and read-only opening semantics. An upstream export or search command is not equivalent merely because it can print conversation text. Keep missing provenance explicitly unknown until a verified source-owned per-turn principal exists, never infer one from role, title, synthetic identity, API label or shared-chat metadata.

`tests/hermes_cli/test_session_evidence_stream.py` owns the fixture contract, real entry-point profile checks, SQLite authorizer projection proof, concurrent WAL snapshot evidence, pre-acquisition huge-field refusal, streaming memory bound and emitted-byte digest checks. Run it alongside `tests/hermes_cli/test_sessions_inspect.py` and `tests/hermes_cli/test_sessions_export_read_only.py` through `scripts/run_tests.sh` with an isolated test scratch root and the worktree interpreter. Only fictional source-owned fixture stores are required. Neither maintenance verification nor publication authorizes querying an operational store, loading real transcripts or writing to a memory system.

## Retirement And Rollback

Retire this patch only when the accepted upstream baseline provides equivalent exhaustive structural inventory and exact-segment audit streams, with projection-before-acquisition, snapshot/byte bounds, no principal inference and independently verifiable terminal evidence. Revert commits carrying `Fork-Patch: session-evidence-streams` to roll back. No persisted migration or data rollback is needed.
