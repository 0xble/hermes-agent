# CI slow-file timeouts

Fork patch identity: `ci-slow-file-timeouts`.

## Required behavior

Hosted Python/E2E shards must give a known-slow test file a per-file bound that fits its measured hosted runtime, the same way on every run, without raising the 300-second default for any other file and without skipping or shortening the file.

`scripts/ci/portable.py` keeps a committed `FILE_TIMEOUTS` map from repository-relative test path to seconds. `python_shard` and the ordinary part of `e2e_tests` split their file list by that map. Unlisted files run in one `scripts/run_tests.sh` invocation under the runner default. Each listed bound runs as its own invocation with `HERMES_TEST_FILE_TIMEOUT` set, through the same path the upgrade suite already uses. A failure in one group does not stop the other group, and the first failure is still raised.

## Why not the runner's duration cache

`scripts/run_tests_parallel.py` already raises a file's bound to 3x its last clean duration, but it reads `test_durations.json`, which is gitignored and runner-written. A fresh hosted checkout has no cache, so every file gets the flat 300 s. Committing that file would not work as intended: the runner rewrites it after each local run, so it would churn in every diff and pick up host-specific timings. Upstream solves the same problem by restoring the cache with `actions/cache` and running E2E with a 900 s bound in a separate job. The fork's shard layout and its rule that runner-local cache never affects CI (see [portable-ci.md](portable-ci.md)) make a small reviewed map the closer fit.

## Entries

- `tests/e2e/core/delivery/test_cron_virtual_clock_soak.py`: 600 s. Hosted shard 3 timings were 179.1 s (run 37707869254), 283.1 s (run 37709685397) and 294.8 s (run 37705343675) against a 300 s bound, leaving at most 5 s of headroom. 600 s is about 2x the worst observed passing run, so a real hang is still killed.

Add an entry only with hosted timing evidence. Remove one when its file is consistently well under 300 s.

## Upstream disposition

Upstream has no `scripts/ci/portable.py` and no fork-style shards. Its `tests.yml` restores `test_durations.json` from `actions/cache` and sets `HERMES_TEST_FILE_TIMEOUT=900` for the E2E job. There is nothing to contribute upstream. The patch can be retired if the fork adopts upstream's job layout or its cache restore.

## Verification

`scripts/ci/tests/test_portable.py` checks that the soak file's shard runs it alone under its listed bound while every other file in that shard, and every file in other shards, runs with no override. It also checks that ordinary E2E files keep their mapped bound and the upgrade suite keeps 900 s.
