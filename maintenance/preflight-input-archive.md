# Rejected Preflight Input Archive

Fork patch identity: `preflight-input-archive`.

Load when changing turn-start compaction rejection, `PreflightCompressionTimedOut`, or archived message appends.

## Required Behavior

A turn rejected before its first provider request retains the original user input in display/search history, but never adds it to model replay. Repeated rejected asks must not grow the already oversized prompt (#7100). The failure remains explicit and fail-closed, with the previous model history in its result.

`SessionDB.append_message(archived=True)` inserts and archives the row in one transaction under the unchanged transcript generation, turn-lease and compression-lock guards. Preserve message identity, timestamp, platform id and display metadata. Do not archive the prior history or fabricate a successful compaction.

## Evidence And Verification

The deterministic seed-23 provider-overflow case failed identically at fork main `907525949f2fa` and sync candidate `0408203171be`: turn 9's new input `U23x9xfa0dbc` never reached persistence because preflight rejected before the normal turn-start flush. No previously persisted message was deleted. This is inherited rejected-input loss, not an upstream compaction deletion regression.

Run `tests/e2e/core/compaction/`, `tests/agent/test_preflight_compression_timeout_fail_closed.py`, `tests/agent/test_export_current_turn_boundary.py`, and the SessionDB message/lease tests. The focused rejection test repeats the same ask twice and asserts recoverable archived rows, empty model replay, unchanged failure envelopes, and zero provider calls.

Retire when upstream retains rejected input durably without increasing model history and passes these tests with the write guards unchanged.
