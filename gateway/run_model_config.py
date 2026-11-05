"""Model-route config expansion and request override merging for gateway turns."""
from typing import Optional

def _load_gateway_runtime_config(config: dict | None = None) -> dict:
    """Expand an explicitly supplied route; disk reads use the effective user-config seam."""
    if config is None:
        from gateway.run import _load_gateway_config
        return _load_gateway_config()
    from hermes_cli.config import _expand_env_vars
    from hermes_cli.model_presets import expand_model_presets
    return expand_model_presets(_expand_env_vars(config))


def _deep_merge_request_overrides(base: Optional[dict], override: Optional[dict]) -> dict:
    """Merge request_overrides dicts, deep-merging nested dictionaries."""
    from hermes_cli.config import _deep_merge
    base_dict = dict(base or {})
    override_dict = dict(override or {})
    if not base_dict:
        return override_dict
    if not override_dict:
        return base_dict
    return _deep_merge(base_dict, override_dict)
