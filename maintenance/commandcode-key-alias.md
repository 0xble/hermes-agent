# Retired CommandCode provider key alias

The maintenance unit identity is `commandcode-key-alias`.

Retired on 2026-11-27. Upstream `hermes_cli/providers.py` has carried the `commandcode` overlay with `extra_env_vars=("COMMANDCODE_API_KEY",)`, the CommandCode endpoint and `COMMANDCODE_BASE_URL` since `v0.23.2`, and still does in `v0.23.4`. A models.dev catalog hit that names `CMD_API_KEY` therefore keeps the plugin's `COMMANDCODE_API_KEY`. That is the complete contract of this unit, and the fork's runtime code now differs from upstream only in a comment.

The fork's 2 test additions in `tests/plugins/model_providers/test_commandcode_profile.py` stay: one keeps `test_resolve_provider_full` independent of the live models.dev catalog, and one covers the catalog-hit merge, which upstream doesn't test. Both exercise upstream code only.

This record stays so published `Fork-Patch: commandcode-key-alias` trailers keep an accountable owner.

Contract tests: `tests/plugins/model_providers/test_commandcode_profile.py`.
