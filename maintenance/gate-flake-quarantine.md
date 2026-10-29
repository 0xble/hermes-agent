# Gate flake quarantine

## Required behavior

The PR gate must remain deterministic and blocking. A test is listed here only when recent exact-SHA gate runs failed on a host/process race, the unchanged test passed on a later equivalent run, and no quick root-cause repair was available for this delivery. The nightly lane runs every listed path on every scheduled run and retains its failure output as repair evidence.

## Provenance and disposition

Fork patch identity: `ci-gate-flake-quarantine`.

Confirmed evidence from Repository gate runs:

- `tests/gateway/test_generation_transfer_runner.py`: run `36627699252` failed two drain-cap race assertions; runs `36953199648` and `36955974020` failed the overlap-draining assertion. The unchanged file passed in run `37164377815` on the current equivalent main line. The failures are scheduling-sensitive transfer/drain races, so the file moves to the existing `nightly-only-e2e` path lane until the lifecycle synchronization is deterministic.

Not quarantined because the failures were real regressions or had a quick repair:

- `tests/hermes_cli/test_doctor.py`, `tests/tools/test_voice_mode.py`, and `tests/hermes_cli/test_immutable_releases.py` failed with deterministic assertion, fixture, or API-shape regressions; they remain blocking coverage.
- `tests/scripts/test_fresh_source_install.py`, `tests/pm/test_cold_runtime_e2e.py`, and `tests/e2e/core/mcp_plugins/test_plugin_activation.py` failed because the hosted environment lacked the expected `uvx`/PM toolchain; they remain blocking until the environment contract is repaired rather than being hidden in nightly.
- `tests/gateway/test_telegram_final_delivery.py` failed on a deterministic floating-point string assertion. That is a correctness regression, not a scheduling flake, so it remains blocking.
- The `rcedit` "Unable to commit changes" lines are deliberate mocked retry cases inside a passing test, not a production failure.

## Verification and retirement

Run the quarantined file on every nightly with `scripts/run_tests.sh --file-retries 0`. Remove it from `NIGHTLY_ONLY_TESTS` and this record only after the transfer/drain race has a deterministic fix and repeated exact-SHA gate evidence passes.
