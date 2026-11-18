"""Structural JSON redaction using the active profile's shared text and vault policy."""

def redact_sensitive_json(value, **kwargs):
    """Redact parsed JSON leaves and secret-named fields without touching JSON syntax.

    Every string leaf goes through :func:`redact.redact_sensitive_text` first, so the registered vault
    boundary (and the redact_secrets preference, flags and vault tab/origin) apply exactly as
    they do to serialized text; the secret-named field mask is applied only to that output.
    """
    from agent import redact

    if isinstance(value, str):
        return redact.redact_sensitive_text(value, **kwargs)
    if isinstance(value, dict):
        # Mirror redact.redact_sensitive_text: effective (routed-profile) policy, and secret_file wins.
        key_rule = ((kwargs.get("force") or redact._redact_enabled())
                    and (kwargs.get("secret_file")
                         or not (kwargs.get("code_file") or kwargs.get("file_read"))))
        mask = redact._mask_token_nonreusable if kwargs.get("file_read") else redact._mask_token
        out = {}
        for key, leaf in value.items():
            # Numeric keys serialize as text too, so they get the same registered-value scrub as leaves.
            new_key = (redact.redact_sensitive_text(key, **kwargs) if isinstance(key, str)
                       else redact_sensitive_json(key, **kwargs))
            new_leaf = redact_sensitive_json(leaf, **kwargs)
            if (key_rule and isinstance(key, str) and isinstance(new_leaf, str) and new_leaf
                    and redact._JSON_KEY_FULL_RE.fullmatch(key) and not redact._ENV_LOOKUP_VALUE_RE.match(new_leaf)):
                new_leaf = mask(new_leaf)
            out[new_key] = new_leaf
        return out
    if isinstance(value, (list, tuple)):
        return [redact_sensitive_json(leaf, **kwargs) for leaf in value]
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        scrubbed = redact.redact_registered_vault_number(
            value, tab=kwargs.get("vault_tab"), origin=kwargs.get("vault_origin", ""))
        if isinstance(scrubbed, str):
            return scrubbed
        # Serialized dumps scrubbed registered numeric values too; keep that boundary structurally.
        text = str(value)
        scrubbed = redact.redact_registered_vault_values(
            text, tab=kwargs.get("vault_tab"), origin=kwargs.get("vault_origin", ""))
        return value if scrubbed == text else scrubbed
    return value
