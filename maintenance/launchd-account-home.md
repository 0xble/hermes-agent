# Launchd Account Home

Patch identity: `launchd-account-home`.

Launchd label ownership and plist placement share the real account home from `pwd`. A profile or sandbox may override process `HOME`, but that cannot claim the account's bare `ai.hermes.gateway` label. The native default retains its bare label, while an unrelated temporary Hermes home receives a distinct suffix. The existing service-conflict guard remains active.

Reproduction: the real Telegram contract harness sets both `HOME` and `HERMES_HOME` to a temporary profile. Native service homes previously used process `HOME`, while `get_launchd_plist_path()` used the account home. That split treated the live account gateway as the temporary profile's service and refused its start. The same name mismatch could route lifecycle operations to a foreign service.

Proof surfaces: `tests/hermes_cli/test_gateway_service.py::test_launchd_account_identity_survives_profile_home_override`, plus `tests/e2e/core/platforms/test_telegram_contract.py` on macOS with a local stand-in token. Retire when current upstream uses one account authority for launchd label and plist identity and these invariants pass without the fork implementation.

The preexisting systemd identity class is Linux-marked. Its synthetic account-home fixture tests systemd names and sudo unit adoption, not launchd ownership. The separate native macOS invariant verifies both the isolated home and the real account default.
