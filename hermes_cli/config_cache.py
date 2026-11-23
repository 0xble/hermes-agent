"""Small shared caches used by the configuration reader.

This module owns the raw YAML cache so config.py can remain the compatibility
surface while lock-free readers and worker snapshots stay isolated from the
larger configuration implementation.
"""

from typing import Any, Optional


# path -> (cache signature..., raw YAML mapping). Published by whole-tuple
# replacement so lock-free readers see either a complete old or new entry.
_RAW_CONFIG_CACHE: dict[str, tuple[Any, ...]] = {}


def _raw_config_cache_hit(path_key: str, cache_key: tuple[Any, ...]) -> Optional[dict[str, Any]]:
    """Return the cached raw config when its signature equals ``cache_key``."""
    cached = _RAW_CONFIG_CACHE.get(path_key)
    if cached is not None and cached[:len(cache_key)] == cache_key:
        return cached[len(cache_key)]
    return None


def _raw_config_cache_store(path_key: str, cache_key: tuple[Any, ...], data: dict[str, Any]) -> None:
    """Publish a caller-owned raw config copy as one complete cache entry."""
    _RAW_CONFIG_CACHE[path_key] = (*cache_key, data)


def _worker_config_snapshot() -> Optional[dict[str, Any]]:
    """Return a safe worker's frozen config, or None when no worker package exists."""
    try:
        from agent.safe_worker_policy import worker_config_snapshot
    except ModuleNotFoundError as exc:
        if exc.name != "agent" and not "agent".startswith(f"{exc.name}."):
            raise
        return None
    return worker_config_snapshot()
