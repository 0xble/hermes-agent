# Gate flake quarantine

## Required behavior

The PR gate must remain deterministic and blocking. A test is listed here only when recent exact-SHA gate runs failed on a host/process race, the unchanged test passed on a later equivalent run, and no quick root-cause repair was available for this delivery. The nightly lane runs every listed path on every scheduled run and retains its failure output as repair evidence.

## Provenance and disposition

Fork patch identity: `ci-gate-flake-quarantine`.

Confirmed evidence from Repository gate runs:

- The former `tests/gateway/test_generation_transfer_runner.py` quarantine followed transfer/drain race failures in runs `36627699252`, `36953199648` and `36955974020`. Commit `e26135fcb0a154741163f9027771453fd6141a28` withdrew that overlap/forward-only implementation and its tests. The stale nightly selector survived until the 2026-11-09 nightly failed with `no such test path`. Remove that selector, not current restart coverage.

Current transfer coverage stays in the ordinary blocking Python shards: `tests/gateway/test_zero_cut_handover.py` covers polling-stop/lease ordering and successor-lock ownership, `tests/plugins/test_received_handover.py` covers buffered text/media and held-input execution across transfer, and `tests/gateway/test_shutdown_unstarted_turn_replay.py` covers replay by the successor under a new admission generation. These exercise the maintained implementations rather than the withdrawn overlap runner. `scripts/ci/tests/test_portable.py` verifies that every nightly selector discovers real tests and that ordinary coverage is partitioned exactly once.

Not quarantined because the failures were real regressions or had a quick repair:

- `tests/hermes_cli/test_doctor.py`, `tests/tools/test_voice_mode.py`, and `tests/hermes_cli/test_immutable_releases.py` failed with deterministic assertion, fixture, or API-shape regressions; they remain blocking coverage.
- `tests/scripts/test_fresh_source_install.py`, `tests/pm/test_cold_runtime_e2e.py`, and `tests/e2e/core/mcp_plugins/test_plugin_activation.py` failed because the hosted environment lacked the expected `uvx`/PM toolchain; they remain blocking until the environment contract is repaired rather than being hidden in nightly.
- `tests/gateway/test_telegram_final_delivery.py` failed on a deterministic floating-point string assertion. That is a correctness regression, not a scheduling flake, so it remains blocking.
- The `rcedit` "Unable to commit changes" lines are deliberate mocked retry cases inside a passing test, not a production failure.

## Verification and retirement

Run active quarantined files on every nightly with `scripts/run_tests.sh --file-retries 0`. A withdrawn implementation retires its selector with an explicit coverage disposition. A maintained implementation leaves quarantine only after its race has a deterministic fix and exact-SHA evidence passes.

Contract tests: `tests/gateway/test_zero_cut_handover.py` `tests/plugins/test_received_handover.py` `tests/gateway/test_shutdown_unstarted_turn_replay.py`
