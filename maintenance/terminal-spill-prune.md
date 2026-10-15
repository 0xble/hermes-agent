# Terminal spill prune throttle

**Patch identity:** `terminal-spill-prune`.

Load when changing `tools/environments/base_output.py` spill creation or pruning,
`_SPILL_MAX_AGE_S`, or when an upstream sync touches `_new_output_collector()`.

## Preserve

- A bounded terminal call never scans the spill directory on its own thread and
  never waits for a scan. It at most starts one daemon `terminal-spill-prune`
  thread.
- Single flight per process: a non-blocking lock is held for the entire scan.
  Callers that lose the lock skip.
- At most one scan per spill directory per `_SPILL_PRUNE_INTERVAL_S` (60 s). The
  interval is keyed by directory because one gateway serves several
  `HERMES_HOME` profiles. It is reserved before the scan starts, so a slow or
  failing scan cannot cause a burst of scans.
- Retention is unchanged: only `out-*.log` entries older than
  `_SPILL_MAX_AGE_S` are deleted, and errors stay silent. Spills last 7 days, so
  a deletion delayed by up to a minute is not observable. A new home's first
  prune runs on its first bounded call.

## Provenance

Live gateway PID 70725 (2026-10-13) had 15 concurrent stacks in
`_new_output_collector()` statting a 6,434-entry spill directory. Terminal
threads accounted for about 65% of a 131% CPU sample. On a synthetic 6,500-file
directory, collector creation took a median of 48–53 ms per call on main and
0.008 ms with this patch. Upstream `main` (`d97007c32d63`) still globs and stats
on every call. Open upstream PRs #126504 and #126365 change spill retention, not
scan frequency.

## Proof

`scripts/run_tests.sh tests/tools/test_base_environment.py` (`TestSpillPruning`:
20 concurrent collectors do not wait and start one scan; a second call within
the interval does not scan; a call after the interval scans again; stale spills
are deleted while fresh and unrelated files remain) and
`tests/tools/test_terminal_truncation_spill.py`.

## Retire

Retire when a selected upstream release removes the per-call scan or moves it to
a rate-limited background job, and `TestSpillPruning` passes without this patch.
If upstream lengthens spill retention, keep the interval well below it. Roll
back by reverting the commit carrying `Fork-Patch: terminal-spill-prune`.
