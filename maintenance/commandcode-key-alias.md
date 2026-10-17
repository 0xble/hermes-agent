# CommandCode Provider Key Alias

## Patch Identity

The maintenance unit identity is `commandcode-key-alias`.

## Required Behavior

The CommandCode provider must accept the user-facing `COMMANDCODE_API_KEY`
credential even when the models.dev catalog names its key `CMD_API_KEY`. The
provider keeps its configured CommandCode endpoint and the plugin's canonical
credential name remains available to the model picker and runtime resolver.

## Provenance And Verification

models.dev currently advertises the provider with `CMD_API_KEY`, while the
CommandCode plugin and user-facing configuration use `COMMANDCODE_API_KEY`.
No released upstream equivalent was identified for this fork-specific
catalog-to-plugin mismatch. The fork delivery is the `commandcode` entry in
`hermes_cli/providers.py`, which appends the plugin key without dropping the
catalog key and keeps the CommandCode endpoint and base-URL override.

Verify with
`tests/plugins/model_providers/test_commandcode_profile.py`, including the
synthetic models.dev catalog-hit merge test, and the repository
provider-resolution checks.

Retire this unit when models.dev and the provider plugin use the same canonical
credential name and the focused contract passes without the overlay.
