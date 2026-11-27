# Retired launchd account home

Patch identity: `launchd-account-home`.

Retired on 2026-11-27. Upstream `v0.23.4` resolves native service homes through `hermes_constants.account_default_hermes_home()` (commit `9248d847d09`), the account home from `pwd`, not process `HOME`. `hermes_cli/gateway.py::_native_service_homes` uses it, so a profile or sandbox that overrides `HOME` can't claim the account's bare `ai.hermes.gateway` label. That is the complete contract of this unit. A release sync already replaced the fork's `pwd` lookup with the upstream function, so no private implementation remains.

The fork's regression, `tests/hermes_cli/test_gateway_service.py::test_launchd_account_identity_survives_profile_home_override`, stays because upstream has no equivalent macOS test. It exercises upstream code only.

This record stays so published `Fork-Patch: launchd-account-home` trailers keep an accountable owner.

Contract tests: `tests/hermes_cli/test_gateway_service.py`.
