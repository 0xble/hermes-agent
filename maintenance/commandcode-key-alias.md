# CommandCode Provider Key Alias

## Patch Identity

The maintenance unit identity is `commandcode-key-alias`.

## Required Behavior

The CommandCode provider must accept the user-facing `COMMANDCODE_API_KEY`
credential even when the models.dev catalog names its key `CMD_API_KEY`. The
provider keeps its configured CommandCode endpoint and the plugin's canonical
credential name remains available to the model picker and runtime resolver.

## Provenance And Verification

This fork overlay preserves the upstream CommandCode plugin contract while
bridging the catalog's legacy key name. Verify with
`tests/plugins/model_providers/test_commandcode_profile.py` and the repository
provider-resolution checks.

Retire this unit when models.dev and the provider plugin use the same canonical
credential name and the focused contract passes without the overlay.
