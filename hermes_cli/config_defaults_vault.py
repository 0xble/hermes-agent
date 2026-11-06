"""Browser vault defaults; pure data, separate from the CLI config loader."""

DEFAULT_VAULT_CONFIG = {
    "key_storage": "file",  # OS keychain is an explicit opt-in for new vaults.
    # Map an existing login handle or 1Password item ID to extra exact HTTPS origins.
    # Aliases extend fill authorization without editing the password-manager item; malformed,
    # HTTP, and wildcard origins are ignored. Example: {"item-id": ["https://signin.example.com"]}.
    "origin_aliases": {},
    # Alias-only login fills ask for confirmation unless this is false.
    "confirm_alias_fills": True,
    # Login and one-time-code fills tick a safe "Remember me"/"Trust this device" box in the same
    # form. Keyed by browser account alias, with "default" for every account not listed, e.g.
    # {"default": True, "work": False} keeps the "work" identity's logins unremembered.
    "remember_logins": {"default": True},
    "onepassword": {
        # Detected managers are login sources unless the user opts out (vault.<name>.enabled: false).
        "enabled": True,        # `op` CLI: Login items with a website URL become fillable handles.
        "account": "",          # account shorthand for `op --account`; empty = default account.
        "binary_path": "",      # absolute path to op; empty = PATH.
        # Env var holding a service-account token (headless auth, no unlock prompt). Unset = prompt.
        "service_account_token_env": "OP_SERVICE_ACCOUNT_TOKEN",
        # Optional model-blind fields. Each entry binds one op:// reference to a supported semantic and
        # exact HTTPS origins; the model sees only an opaque handle and can fill only that field there.
        "protected_fields": [],
        # Additional accounts: [{alias, account, service_account_token_env, browser_account?}].
        # Each uses only its own service-account token; handles are op@<alias>:<item-id>.
        "accounts": [],
    },
    "bitwarden": {
        "enabled": True,        # `bw` CLI (Password Manager, not Secrets Manager); run `bw login` once first.
        "binary_path": "",      # absolute path to bw; empty = PATH.
    },
}
