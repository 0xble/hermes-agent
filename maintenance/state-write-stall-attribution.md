---
targets:
  - ../hermes_state_transactions.py
  - ../hermes_state_write_stalls.py
  - ../hermes_state_runtime.py
  - ../hermes_state.py
  - ../tests/hermes_state/test_commit_tail_attribution.py
  - ../tests/hermes_state/test_write_stall_sampling.py
  - ../tests/hermes_state/test_write_stall_native.py
maintenance:
  mode: local
---

# State write commit-tail attribution

Fork patch identity: `state-write-stall-attribution`.

## Required behavior

Slow-write warnings distinguish commit wall time from thread CPU and include
native TID, the existing operation identity (worker receipt operation when
available), and commit-end WAL bytes from stat only. Neither CPU nor the Python
commit envelope proves how long SQLite's native writer lock remained held.
No SQL tracing, schema changes, or checkpoint/durability policy changes.

An in-flight callback or commit older than ten seconds warrants one bounded
same-user native stack sample, at most once per process per ten minutes, with
at most twenty owned sample files per profile. Diagnostics must not acquire a
writer/database lock or fail a transaction. Unsupported platforms do nothing.

## Upstream design preflight

Guidance inspected at upstream `c419cf7c864ac0ee511dbd9f7091a8bba0b567ee`:
root `AGENTS.md` and `CONTRIBUTING.md`; both were rechecked unchanged at
`9a76ac8e4f612c081e12008923893077f10f2ec7` before GREEN. The related issues and
PR were also rechecked open/unmerged. The narrow extension belongs beside the
existing transaction settlement/metrics owner, not a new tool or plugin API.
Independent proposal: extend the warning and arm a cheap in-flight timestamp;
use the OS native sampler rather than SQL tracing or private SQLite pointers.

Related issues (not PRs): [#139959](https://github.com/NousResearch/hermes-agent/issues/139959)
remains open and documents long cross-process writer holds. Its linked
[#139984](https://github.com/NousResearch/hermes-agent/pull/139984) is still open
(not merged) and moves turn-lease renewal onto a separate writer connection;
that is not native stall capture and is not adopted here.
[#145421](https://github.com/NousResearch/hermes-agent/issues/145421) is also open:
it reports a CPU-heavy SQLite housekeeping thread and a manually attached macOS
`sample`, with no implementation or comments. Neither issue provides landed
method names or a diagnostic format to reuse. A current symbol search found no
`commit_cpu_s` or equivalent native stall sampler. Keep this observation-only
addition fork-local pending an upstream equivalent.

A Python watchdog thread alone cannot trigger under a GIL-holding extension.
The required GIL-independent trigger therefore needs an external helper with a
read-only view of preallocated shared phase stamps. Start it from a tiny daemon
thread before acquiring any writer lock; the helper owns the single sleep loop
and invokes `/usr/bin/sample`. This is a necessary refinement of the requested
thread-only sketch, not a database policy change. The writer does no diagnostic
file write or sampler subprocess work inside the transaction.

## Implementation boundaries

One process-owned helper observes 64 preallocated slots in an unlinked shared
mapping. Phase publication is bounded memory writes only. Startup and slot
reservation happen before the writer guard; the startup thread waits for the
external interpreter outside the guard so a subsequent GIL-holding C call
cannot prevent launch. Slots are cleared in callback/commit `finally` blocks,
including failed attempts. Each phase has a unique sequence; failed captures
also consume the ten-minute process cooldown. No work is sampled during writer
acquisition, retries/backoff, rollback, or idle time.

Warnings and WAL `stat` are deferred until immediately after releasing the
writer guard and before existing checkpoint/optimization maintenance. WAL bytes
are consequently a best-effort post-commit observation (another process can
change the file), not a lock-held measurement or a SQLite query. Missing WAL is
zero; inaccessible WAL is `unknown`. The native helper can read stamps without
the target interpreter's GIL, and a parent-owned pipe bounds its lifetime.
Sample files have mode 0600 under the active profile's `logs/write-stalls`;
only the `write-stall-*.txt` namespace participates in twenty-file retention.
The sampler has a five-second timeout and records only exception type on
failure, never payload/error text. Capture warnings identify PID, native TID,
phase, operation and the sample path.

## Inline auto-checkpoint finding

At base `3e149c0d9412394a1b8b744e542d0fcd68f6e187`,
`hermes_state.py:853-879` opens the writer and calls `apply_database_pragmas`.
`hermes_state_wal.py:644-666` only changes `wal_autocheckpoint` when explicitly
configured; absent that key it leaves SQLite's connection default enabled
(normally 1000 pages). Nothing disables it merely because maintenance also runs.
`hermes_state.py:1623-1646` runs a separate PASSIVE checkpoint, using the dedicated
connection from `hermes_state.py:1661-1677`, outside the process writer guard.
Thus inline auto-checkpoint and background/dedicated PASSIVE checkpoint can
coexist. This is a source finding, not a read of the live profile's connection:
its actual configured page threshold is intentionally not queried here.
No checkpoint, synchronous, or fullfsync setting is changed by this patch.

## Verification and retirement

Contract tests: `tests/hermes_state/test_commit_tail_attribution.py`,
  `tests/hermes_state/test_write_stall_sampling.py`,
  `tests/hermes_state/test_write_stall_native.py`,
  `tests/hermes_state/test_transcript_write_diagnostics.py`.

Regression files above cover warning fields, worker receipt identity, stalled
callback/commit capture, quiet idle/finished writes, cross-write throttling,
sampler failure, and retention. The native-host regression blocks a disposable
interpreter in a GIL-holding C read and checks that an actual native sample is
produced before that read is released. All SQLite work uses isolated temporary homes;
epoch-dependent setup uses `tests/fakes/runtime_owner.py` reservations.
Retire once upstream provides equivalent attribution and a GIL-independent,
bounded native capture. Roll back commits carrying the patch identity above;
retained diagnostic samples are not database state.

Stack fit: reused Python stdlib, existing transaction settlement and real-owner
test helpers; no dependencies or services added.
