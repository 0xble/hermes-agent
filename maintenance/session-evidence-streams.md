# Session Evidence Streams

## Required Behavior And Ownership

Fork patch identity: `session-evidence-streams`. This is an intentional fork-only acquisition surface, not conversational recall or a memory producer. It adds `sessions state inventory` and `sessions state transcript --session-id` beside the bounded inspection commands. Transcript acquisition requires an owner control-coverage manifest and same-snapshot preflight. Their projection/JSONL contract is documented in [CLI reference](../website/docs/reference/cli-commands.md#session-graph-and-audit-streams), with source allowlists in `hermes_state_evidence.py` and encoding/redaction in `hermes_cli/session_evidence.py`.

Load [typed session-state inspection](session-state-inspection.md) when changing shared CLI dispatch. Its native tool keeps the existing no-transcript and size-cap guarantees. Do not route these evidence streams through model-tool caps or ordinary export's model/display loaders. They intentionally preserve raw audit and graph boundaries instead of resolving a conversation.

## Provenance And Disposition

Implementation starts independently from maintained fork base `286bd6320dfd80f896ace6a14abf788abffdf08b`. Upstream design comparison is pinned to `e5f2b15300a3d04978e4c07cc64adc7bcf8e7b1c`. Related [upstream issue #90719](https://github.com/NousResearch/hermes-agent/issues/90719) requests explicit per-message provenance for injected user messages, rather than trusting the user role. [Issue #116651](https://github.com/NousResearch/hermes-agent/issues/116651) reports reasoning leakage from ordinary exports. Neither supplies this capability or a donor implementation. No upstream patch was adopted, and no prior-art authorship is claimed.

Upstream disposition: no contribution submitted, upstream publication is outside this delivery's authority. Fork delivery: an isolated maintained-fork candidate for integration on `main`, with activation owned separately by the existing maintenance integration owner. M0 extends this unit with native occurrence stamps in existing metadata, an edit-invalidation trigger, control-component exclusion and authentication exclusion. No new table, dependency, bank writer or service is introduced.

The narrow snapshot bypasses the general `SessionDB` constructor's FTS `SELECT * ... LIMIT 0` probes, while reusing its tracked `mode=ro` connection factory, URI builder, loop guard and test-isolation guard. Do not replace it with a general constructor/load-history route without proving that unrelated body columns remain unselected. The origin preflight pins the source-owned `(session_id, id)` index. It reads only bounded occurrence identity and the reserved origin namespace. Only verified authored spans may cause body queries. The entire control component is denied before any message-body expression is prepared.

## Update And Verification

Automatic Hindsight retention now runs only on verified-origin turns. Missing verified origin skips retention and emits a debug log.

Contract tests: `tests/hermes_cli/test_session_evidence_stream.py`.

Streaming requires an existing WAL journal mode. Non-WAL stores are refused before output because a stalled consumer could otherwise block writers. The read-only command never changes journal mode. Routing identities (`user_id`, `chat_id`, `session_key`) that need redaction are refused rather than rewritten. Closed output pipes exit unsuccessfully without shutdown flush noise.

For upstream adoption, inspect physical row ordering, origin/display columns, compaction and rewind flags, native index identity and read-only opening semantics. An upstream export or search command is not equivalent merely because it can print conversation text. Keep missing provenance explicitly unknown until a verified source-owned per-turn principal exists, never infer one from role, title, synthetic identity, API label or shared-chat metadata.

`tests/hermes_cli/test_session_evidence_stream.py` owns the fixture contract, real entry-point profile checks, SQLite authorizer projection proof, concurrent WAL snapshot evidence, pre-acquisition huge-field refusal, streaming memory bound and emitted-byte digest checks. Run it alongside `tests/hermes_cli/test_sessions_inspect.py` and `tests/hermes_cli/test_sessions_export_read_only.py` through `scripts/run_tests.sh` with an isolated test scratch root and the worktree interpreter. Only fictional source-owned fixture stores are required. Neither maintenance verification nor publication authorizes querying an operational store, loading real transcripts or writing to a memory system.

## Retirement And Rollback

Retire this patch only when the accepted upstream baseline provides equivalent exhaustive structural inventory and exact-segment audit streams, with projection-before-acquisition, snapshot/byte bounds, no principal inference and independently verifiable terminal evidence. Revert commits carrying `Fork-Patch: session-evidence-streams` to roll back. The additive `messages_source_origin_update` trigger strips a native stamp when content or occurrence identity changes, including writes from an older release. Existing imported metadata is not promoted. Rollback code may leave reserved metadata and this conservative invalidation trigger in place. No table or bank-data rollback is needed.

## M0 Source Acceptance

Task `493240dc-cf81-45ea-84db-cdeae14f31b5` uses the round-2 design with SHA-256 `f0832c52a12d4260f0adba391d07fba85b7eb6b8a8f860ac08bc574475736837`. It requires PR847 and merged PR845. Source qualification is separate from installation, gateway activation and recovery. All acquisition and writer holds remain owner-controlled.

`agent/source_origin.py` owns one strict human/generated/unknown decision. `gateway/source_origin.py` accepts positive person evidence from native Telegram ingress and independently witnessed batching spans. Only the writer expands a process-local receipt into the existing reserved message metadata. Plain dictionaries from imports, clients or restored serialized payloads cannot mint it. Other surfaces and unknown historical rows stay held. Source-only inspection found no historical positive per-event evidence contract that can be safely promoted from inherited session origin fields.

Live messages carry an empty JSON-safe origin anchor. Its exact identity resolves a receipt in a weak-reference side map, keeping the receipt and its captured text outside every serialized message graph. Shallow native handoffs retain the anchor, while JSON imports and deep copies cannot recover its authority. Releasing the last anchor releases the receipt. `tests/tui_gateway/test_source_origin_egress.py` covers history display, the next compute-host turn, stdio, fanout, WebSocket single/batch sends, event replay/publishing and worker/result serialization. `tests/agent/test_source_origin_lifetime.py` covers retention, persistence, import forgery and release.

Worker JSON and durable execution-result copies cannot transfer process-local origin authority. `tests/gateway/test_result_source_origin.py` covers direct and prepared settlement, and `tests/gateway/test_local_supervisor_recovery.py` exercises recovered local check-ins through the real owner path.

`hermes_state_acquisition.py` extends the native evidence boundary with an unfiltered bidirectional graph closure, preserved missing-parent vertices, fixed-point cycle handling, structural digest validation and bounded origin preflight in the same read transaction. The owner must supply complete root coverage, including control sources not already marked by native creation. An owner name or manifest is not proof of census completeness. Unknown coverage fails closed. No operational seed census is performed by source tests.

`agent/authentication_material.py` excludes narrow authentication values independently of origin and known-key masking. The shared memory egress calls it before provider archival. `agent/correction_evidence.py` emits correcting occurrence, speaker, exact supported replacement and unresolved target state. Reviewed fact/source targets are checked for matching occurrence, speaker and replacement. It never invalidates a bank fact or closes a goal. Existing quality/correction owners consume that evidence through the session stream.

Fictional acceptance is in `tests/agent/test_source_origin.py` (native/person/generated/unknown, mixed spans, correction attribution, auth counterexample, retention, import forgery and edit invalidation) and `tests/hermes_state/test_source_acquisition.py` (all structural exclusions, zero content-column reads, origin preflight and drift). `tests/hermes_cli/test_session_evidence_stream.py` preserves the native CLI, selected-home, byte-bound, snapshot and terminal-footer controls under the stricter unknown-body contract. PR845's supported readiness contract is exercised without starting a client or accessing a bank.

Installed acceptance still requires the existing Maintenance promotion route to pin the exact qualified repaired-main release, read back installed SHA/tree/module digests and loaded gateway identity, then run fictional stores through the installed supported paths and both available/missing-prerequisite status cases. Source tests cannot satisfy that boundary, and this unit does not authorize promotion, restart or recovery.
