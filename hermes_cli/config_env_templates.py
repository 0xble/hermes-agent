"""Preserve authored environment references when saving expanded configuration."""

def _items_by_unique_name(items):
    """Return a name-indexed dict only when all items have unique string names."""
    if not isinstance(items, list):
        return None
    indexed = {}
    for item in items:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str):
            return None
        name = item["name"]
        if name in indexed:
            return None
        indexed[name] = item
    return indexed


def _preserve_env_ref_templates(current, raw, loaded_expanded=None):
    """Restore raw ``${VAR}`` templates where the value is otherwise unchanged, so persisting a
    loaded (expanded) config never writes the plaintext secret back to ``config.yaml``."""
    from hermes_cli.config import _ENV_REF_RE, _expand_env_vars
    if isinstance(current, str) and isinstance(raw, str) and _ENV_REF_RE.search(raw):
        if current in (raw, loaded_expanded) or _expand_env_vars(raw) == current:
            return raw
        return current

    if isinstance(current, dict) and isinstance(raw, dict):
        return {
            key: _preserve_env_ref_templates(
                value, raw.get(key),
                loaded_expanded.get(key) if isinstance(loaded_expanded, dict) else None)
            for key, value in current.items()}

    if isinstance(current, list) and isinstance(raw, list):
        # Match named objects (e.g. custom_providers) by name so reordering keeps templates;
        # with duplicate names fall back to positional matching rather than shadowing an entry.
        current_by_name = _items_by_unique_name(current)
        raw_by_name = _items_by_unique_name(raw)
        loaded_by_name = _items_by_unique_name(loaded_expanded)
        if current_by_name is not None and raw_by_name is not None:
            return [
                _preserve_env_ref_templates(
                    item, raw_by_name.get(item.get("name")),
                    loaded_by_name.get(item.get("name")) if loaded_by_name is not None else None)
                for item in current]
        return [
            _preserve_env_ref_templates(
                item,
                raw[index] if index < len(raw) else None,
                loaded_expanded[index]
                if isinstance(loaded_expanded, list) and index < len(loaded_expanded)
                else None)
            for index, item in enumerate(current)]

    return current
