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
    """Return a cached raw mapping when its signature equals ``cache_key``.

    Older callers may have populated the cache before the root-type validation was
    added. Never let such an entry bypass the mapping guard on a cache hit.
    """
    cached = _RAW_CONFIG_CACHE.get(path_key)
    if cached is not None and cached[:len(cache_key)] == cache_key:
        data = cached[len(cache_key)]
        return data if isinstance(data, dict) else None
    return None


def _raw_config_cache_clear(path_key: str) -> None:
    """Remove one raw config entry through the cache module's single binding."""
    _RAW_CONFIG_CACHE.pop(path_key, None)


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
