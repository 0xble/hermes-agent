# TTS discovery without installation

Load this unit when changing TTS tool registration, lazy SDK dependencies, or startup tool-schema discovery.

## Required behavior

An unrelated agent turn must not download or install a TTS SDK while constructing tool definitions. Advertise the configured TTS tool when its SDK is already installed or can be installed under the existing lazy-install policy; leave the actual installation on TTS execution. Preserve the opt-out and managed-install restrictions, and keep voice-mode readiness checks independent.

## Provenance and patch

Fork patch identity: `tts-discovery-no-install`.

Independent fork fix after the catalog-matrix gate exposed startup `edge-tts` installation under concurrent load. The current upstream-main preflight found no equivalent fix. This patch changes the TTS registry's capability probe only; it does not change the provider catalog, egress sentinel, or matrix timeout.

## Status

Superseded by upstream. Upstream `398867326d` ("requirement checks answer from state, never
install") made `check_tts_requirements` itself the passive `text_to_speech` `check_fn`: an SDK
that pm can install at first synthesis reads as ready, the probe installs nothing, and the
`security.allow_lazy_installs` opt-out and `pm.extras.extra_supported` platform gate are both
honored. The 2026-10-02 pre-tip checkpoint merge (`f787eedef4`, #283) adopted it, removing the
fork's `check_tts_tool_availability` probe and its `tests/tools/test_tts_startup_availability.py`
regression. The identity stays owned here for its historical trailers.

## Verification

Contract tests: `tests/tools/test_tts_requirements_passive.py`

The upstream regression rejects any install during a requirement check while still reporting an
installable SDK as ready. In a cold Linux environment with no Edge SDK and egress sentinel,
`scripts/run_tests.sh tests/e2e/core/providers/test_catalog_matrix_1.py` must not contact pypi.org
during unrelated startup.

## Retirement and rollback

Retire when a released upstream version separates TTS discovery from lazy SDK installation with equivalent opt-out and managed-install behavior. Roll back the probe and its focused tests together; re-check the matrix under cold-start load before removing this protection.
