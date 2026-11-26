# Bounded request-debug artifacts

Load when changing the request-dump writer, JSON sizing, or sessions-directory
artifact retention. Fork patch identity: `bounded-request-dumps`.

## Required divergence

Unconditional provider-error dumps must not grow the profile's sessions directory
without a bound. Keep valid diagnostic JSON, recognizable request/error context,
and explicit omission markers; do not truncate the actual provider request.
The limits are local defaults, not another configuration surface.

Retention is write-triggered and best-effort. Scan the whole directory by name,
stat only dump-shaped entries, and only unlink regular files matching the writer's
timestamped dump names. A partial scan budget would let unrelated entries starve
pruning forever. Preserve transcripts, unrelated JSON, temporary files,
directories, and symlinks. A filesystem race may defer pruning until a later write.
Size accounting includes text-mode newline expansion so Windows dumps keep the cap.

## Provenance and adoption

Fork base: `61757685cacdb64132cc554327c8019ce8773b10`.
Upstream design preflight: `NousResearch/hermes-agent` main
`79b45d1647f6b5680768d851c4c406e8cc550a1e`; root/subsystem guidance and
`CONTRIBUTING.md` read at that revision. Its request-dump writer still writes the
entire redacted payload without size/count retention.

Selected route: fork-only delivery for the requested offline integrity slice;
no upstream contribution is authorized. Searches of open/closed upstream issues
and PRs for request-dump size/retention found no matching implementation.
Related, not adopted: [owner-only dump permissions PR #125795](https://github.com/NousResearch/hermes-agent/pull/125795)
and [JSON-leaf redaction PR #123688](https://github.com/NousResearch/hermes-agent/pull/123688),
both open at preflight. Their permission/redaction changes remain separate work.

The existing writer remains the sole persistence owner. Preserve its forced
redaction, safe session filenames, endpoint selection, and atomic writes when
adopting upstream changes. Pruning errors must not turn a successfully written
dump into a provider failure.

## Proof and retirement

`tests/agent/test_request_dump_bounds.py` drives the real request-dump entry point
and inspects on-disk byte size, parseable diagnostics, request immutability,
newest-file retention, and non-dump preservation. Existing endpoint and session
cleanup tests protect surrounding behavior.

Retire when released upstream satisfies the full size, valid-JSON, write-time
retention, full-scan progress, and non-dump-preservation contract. Roll back by reverting
the patch; previously pruned diagnostics cannot be recovered. No profile config
or runtime activation belongs to this unit.
