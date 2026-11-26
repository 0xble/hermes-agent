# TTS discovery without installation

Load this unit when changing TTS tool registration, lazy SDK dependencies, or startup tool-schema discovery.

## Required behavior

An unrelated agent turn must not download or install a TTS SDK while constructing tool definitions. Advertise the configured TTS tool when its SDK is already installed or can be installed under the existing lazy-install policy; leave the actual installation on TTS execution. Preserve the opt-out and managed-install restrictions, and keep voice-mode readiness checks independent.

## Provenance and patch

Fork patch identity: `tts-discovery-no-install`.

Independent fork fix after the catalog-matrix gate exposed startup `edge-tts` installation under concurrent load. The current upstream-main preflight found no equivalent fix. This patch changes the TTS registry's capability probe only; it does not change the provider catalog, egress sentinel, or matrix timeout.

## Verification

Run `scripts/run_tests.sh tests/tools/test_tts_*.py` and `scripts/run_tests.sh tests/e2e/core/providers/test_catalog_matrix_1.py`. The capability regression at `tests/tools/test_tts_startup_availability.py` must reject any SDK installation during discovery while still allowing installation on actual use. In a cold Linux environment with no Edge SDK and egress sentinel, verify the matrix no longer contacts pypi.org during unrelated startup.

## Retirement and rollback

Retire when a released upstream version separates TTS discovery from lazy SDK installation with equivalent opt-out and managed-install behavior. Roll back the probe and its focused tests together; re-check the matrix under cold-start load before removing this protection.
