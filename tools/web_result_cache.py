"""Result caching for web_search / web_extract; both caches TTL-bounded (default 20 min,
``web.cache_ttl_minutes``; disable with ``web.cache_enabled: false``), only successful responses cache.
* **Search memo** — in-memory, single-flighted, and disk-backed under ``cache/web`` so
  concurrent CLI, gateway, cron, and subagent processes share one paid request.
  Limits bucket to 10/20/50/100 so near-identical requests share an entry.
* **Extract cache** — disk-backed under ``cache/web`` (cross-process) with a JSON sidecar index:
  URL digest → (file, fetched_at, title). Hits re-run the normal truncate pipeline.
Lives here, not in tool dispatch, so hits sit *after* every safety check and skip only the vendor call.
"""

import hashlib
import json
import logging
import os
import re
import threading
import time
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import ContextManager, Dict, Optional, Iterator, Tuple
from urllib.parse import urlparse
from utils import atomic_json_write

logger = logging.getLogger(__name__)

# Requested limits round UP to a bucket so cache keys collide on purpose.
_LIMIT_BUCKETS = (10, 20, 50, 100)

DEFAULT_TTL_MINUTES = 20

_INDEX_FILENAME = "extract-index.json"
_INDEX_MAX_ENTRIES = 500  # oldest entries evicted past this
_SEARCH_INDEX_FILENAME = "search-index.json"
_SEARCH_INDEX_MAX_ENTRIES = 1000
_SEARCH_INDEX_LOCK_NAME = "index-update"
_SEARCH_BUDGET_WARNING_LIMITS = (50, 100)


def _web_config() -> dict:
    try:
        from tools.web_tools import _load_web_config
        return _load_web_config()
    except Exception:
        return {}


def cache_enabled() -> bool:
    """Both caches honor ``web.cache_enabled`` (default: on)."""
    return True if (val := _web_config().get("cache_enabled")) is None else bool(val)


def ttl_seconds() -> float:
    """TTL from ``web.cache_ttl_minutes`` (default 20, clamped 1–1440)."""
    raw = _web_config().get("cache_ttl_minutes")
    try:
        minutes = float(raw) if raw is not None else DEFAULT_TTL_MINUTES
    except (TypeError, ValueError):
        minutes = DEFAULT_TTL_MINUTES
    return max(1.0, min(minutes, 1440.0)) * 60.0


def bucket_limit(limit: int) -> int:
    """Round a requested result count up to the nearest bucket."""
    return next((b for b in _LIMIT_BUCKETS if limit <= b), _LIMIT_BUCKETS[-1])


def normalize_query(query: str) -> str:
    """Case-fold and collapse whitespace so trivial variants share an entry."""
    return re.sub(r"\s+", " ", (query or "").strip().lower())


def _host_slug(url: str) -> str:
    """Filesystem-safe hostname slug for cache filenames (``"page"`` when hostless). Shared with
    tools.web_tools_truncate."""
    host = (urlparse(url).hostname or "page").replace(":", "_")
    return re.sub(r"[^A-Za-z0-9._-]", "-", host)[:60].strip("-") or "page"


def _deep_copy(response: dict) -> dict:
    """Defensive copy so callers mutating a hit never corrupt the cached entry."""
    return json.loads(json.dumps(response))


# ---------------------------------------------------------------------------
# Search cache helpers (disk-backed, cross-process)
# ---------------------------------------------------------------------------


def _search_digest(
    provider: str,
    query: str,
    limit: int,
    *,
    endpoint: str = "search",
    search_type: str = "",
    freshness_class: str = "",
) -> str:
    """Hash the complete search identity, including routing and freshness inputs."""
    raw = json.dumps({
        "endpoint": endpoint,
        "provider": provider,
        "query": normalize_query(query),
        "limit": bucket_limit(limit),
        "search_type": search_type or "",
        "freshness_class": freshness_class or "",
    }, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:20]


def _search_index_path() -> Optional[Path]:
    d = _cache_dir()
    return (d / _SEARCH_INDEX_FILENAME) if d else None


def _load_search_index() -> dict:
    path = _search_index_path()
    if path is None or not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else {}
    except (OSError, TypeError, ValueError):  # corrupt/unreadable index == empty cache
        return {}


def _save_search_index(index: dict) -> None:
    path = _search_index_path()
    if path is None:
        return
    try:
        if len(index) > _SEARCH_INDEX_MAX_ENTRIES:
            newest = sorted(index.items(), key=lambda kv: kv[1].get("fetched_at", 0), reverse=True)
            index = dict(newest[:_SEARCH_INDEX_MAX_ENTRIES])
        atomic_json_write(path, index, indent=None, mode=0o600)
    except (OSError, TypeError, ValueError) as exc:  # cache writes are best-effort
        logger.debug("Failed to save web search cache index: %s", exc)


def _search_entry_path(digest: str) -> Optional[Path]:
    d = _cache_dir()
    return (d / f"search-{digest}.json") if d else None


def search_cache_get(
    provider: str,
    query: str,
    limit: int,
    *,
    endpoint: str = "search",
    search_type: str = "",
    freshness_class: str = "",
) -> Optional[dict]:
    """Read a fresh successful search response shared by Hermes processes."""
    if not cache_enabled():
        return None
    digest = _search_digest(provider, query, limit, endpoint=endpoint,
                            search_type=search_type, freshness_class=freshness_class)
    with _index_lock:
        entry = _load_search_index().get(digest)
    if not isinstance(entry, dict):
        return None
    try:
        if (time.time() - float(entry.get("fetched_at", 0))) >= ttl_seconds():
            return None
    except (TypeError, ValueError):
        return None
    try:
        file_path, cache_root = Path(entry["file"]), _cache_dir()
        if cache_root is None or cache_root.resolve() not in file_path.resolve().parents:
            return None
        response = json.loads(file_path.read_text(encoding="utf-8-sig"))
        if not isinstance(response, dict) or not response.get("success"):
            return None
    except (OSError, RuntimeError, TypeError, ValueError, KeyError):  # evicted/tampered file == miss
        return None
    logger.info("web_search disk cache hit: %r via %s", query, provider)
    return _deep_copy(response)


def search_cache_put(
    provider: str,
    query: str,
    limit: int,
    response: dict,
    *,
    endpoint: str = "search",
    search_type: str = "",
    freshness_class: str = "",
) -> None:
    """Persist only a complete, non-rescued successful search response."""
    data = response.get("data") if isinstance(response, dict) else None
    rescued = response.get("rescued_from") if isinstance(response, dict) else None
    rescued = rescued or (isinstance(data, dict) and data.get("rescued_from"))
    if (
        not cache_enabled()
        or not isinstance(response, dict)
        or not response.get("success")
        or rescued
    ):
        return
    digest = _search_digest(provider, query, limit, endpoint=endpoint,
                            search_type=search_type, freshness_class=freshness_class)
    file_path = _search_entry_path(digest)
    if file_path is None:
        return
    try:
        atomic_json_write(file_path, response, indent=None, mode=0o600)
        with search_cache_lock(_SEARCH_INDEX_LOCK_NAME):
            with _index_lock:
                index = _load_search_index()
                index[digest] = {"file": str(file_path), "fetched_at": time.time()}
                _save_search_index(index)
    except (OSError, TypeError, ValueError) as exc:  # cache writes are best-effort
        logger.debug("Failed to cache web search response: %s", exc)


@contextmanager
def search_cache_lock(digest: str) -> Iterator[bool]:
    """Serialize the paid call for one key across CLI, gateway, cron, and children."""
    path = _cache_dir()
    lock_path = path / f"search-{digest}.lock" if path else None
    if lock_path is None:
        yield False
        return
    handle = None
    acquired = False
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = lock_path.open("a+")
        if os.name == "nt":
            import msvcrt
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_LOCK, 1)
        else:
            import fcntl
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        acquired = True
        yield acquired
    finally:
        if handle is not None:
            if acquired:
                try:
                    if os.name == "nt":
                        import msvcrt
                        handle.seek(0)
                        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                    else:
                        import fcntl
                        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
                except OSError:
                    pass
            handle.close()


class SearchBudget:
    """Warn-only per-session counter for remote search attempts."""

    def __init__(self) -> None:
        self._counts: dict[str, int] = {}
        self._warned: dict[str, set[int]] = {}
        self._lock = threading.Lock()

    def record(self, session_id: Optional[str], provider: str) -> Optional[str]:
        if not session_id:
            return None
        with self._lock:
            count = self._counts.get(session_id, 0) + 1
            self._counts[session_id] = count
            warned = self._warned.setdefault(session_id, set())
            threshold = next((limit for limit in _SEARCH_BUDGET_WARNING_LIMITS
                              if count >= limit and limit not in warned), None)
            if threshold is None:
                return None
            warned.add(threshold)
        warning = (
            f"Web search usage warning: session has made {count} remote {provider} search attempts. "
            f"This is a warn-only soft budget at {threshold} attempts; work will continue."
        )
        logger.warning(warning)
        return warning

    def clear(self) -> None:
        with self._lock:
            self._counts.clear()
            self._warned.clear()


search_budget = SearchBudget()


# ---------------------------------------------------------------------------
# Search memo (in-memory, single-flight)
# ---------------------------------------------------------------------------


class SearchMemo:
    """TTL memo + single-flight coalescer for search responses. Thread-safe: the parallel tool-dispatch pool
    and subagents share this process, so identical queries genuinely race; per-key locks make the losers
    wait for (and share) the winner's response."""

    def __init__(self) -> None:
        self._store: dict[tuple, tuple[float, dict]] = {}  # key -> (expires_at, response)
        self._store_lock = threading.Lock()
        self._key_locks: dict[tuple, threading.Lock] = {}

    @staticmethod
    def _key(
        provider: str,
        query: str,
        limit: int,
        *,
        endpoint: str = "search",
        search_type: str = "",
        freshness_class: str = "",
    ) -> tuple:
        return (
            endpoint,
            provider,
            normalize_query(query),
            bucket_limit(limit),
            search_type or "",
            freshness_class or "",
        )

    def lookup(
        self,
        provider: str,
        query: str,
        limit: int,
        *,
        endpoint: str = "search",
        search_type: str = "",
        freshness_class: str = "",
    ) -> Optional[dict]:
        if not cache_enabled():
            return None
        key = self._key(provider, query, limit, endpoint=endpoint,
                        search_type=search_type, freshness_class=freshness_class)
        with self._store_lock:
            hit = self._store.get(key)
            if hit is None or time.monotonic() >= hit[0]:
                self._store.pop(key, None)
                hit = None
        if hit is not None:
            logger.info("web_search cache hit: %r via %s", query, provider)
            return _deep_copy(hit[1])
        return search_cache_get(
            provider, query, limit, endpoint=endpoint, search_type=search_type,
            freshness_class=freshness_class,
        )

    def store(
        self,
        provider: str,
        query: str,
        limit: int,
        response: dict,
        *,
        endpoint: str = "search",
        search_type: str = "",
        freshness_class: str = "",
    ) -> None:
        """Cache a successful, non-rescued response for the bucketed key."""
        data = response.get("data") if isinstance(response, dict) else None
        rescued = response.get("rescued_from") if isinstance(response, dict) else None
        rescued = rescued or (isinstance(data, dict) and data.get("rescued_from"))
        if (
            not cache_enabled()
            or not isinstance(response, dict)
            or not response.get("success")
            or rescued
        ):
            return
        key = self._key(provider, query, limit, endpoint=endpoint,
                        search_type=search_type, freshness_class=freshness_class)
        with self._store_lock:
            now = time.monotonic()  # opportunistic expiry sweep bounds memory
            for k in [k for k, (exp, _) in self._store.items() if now >= exp]:
                del self._store[k]
            self._store[key] = (now + ttl_seconds(), _deep_copy(response))
        search_cache_put(
            provider, query, limit, response, endpoint=endpoint, search_type=search_type,
            freshness_class=freshness_class,
        )

    def flight_lock(
        self,
        provider: str,
        query: str,
        limit: int,
        *,
        endpoint: str = "search",
        search_type: str = "",
        freshness_class: str = "",
    ) -> threading.Lock:
        """Per-key lock held around lookup-miss → paid request → store."""
        key = self._key(provider, query, limit, endpoint=endpoint,
                        search_type=search_type, freshness_class=freshness_class)
        with self._store_lock:
            lock = self._key_locks.get(key)
            if lock is None:
                # Bound the lock table, but never evict a HELD lock: dropping one lets a concurrent
                # identical request mint a fresh lock and issue a duplicate paid call. locked() is a
                # safe snapshot under _store_lock because holders already have their reference.
                # See #94618.
                if len(self._key_locks) > 256:
                    self._key_locks = {k: v for k, v in self._key_locks.items() if v.locked()}
                lock = self._key_locks[key] = threading.Lock()
            return lock

    def process_lock(
        self,
        provider: str,
        query: str,
        limit: int,
        *,
        endpoint: str = "search",
        search_type: str = "",
        freshness_class: str = "",
    ) -> ContextManager[bool]:
        return search_cache_lock(_search_digest(
            provider, query, limit, endpoint=endpoint, search_type=search_type,
            freshness_class=freshness_class,
        ))

    def clear(self) -> None:
        """Drop all cached entries (tests; config changes)."""
        with self._store_lock:
            self._store.clear()
            self._key_locks.clear()
        search_budget.clear()
        path = _cache_dir()
        index_path = _search_index_path()
        # Nothing on disk means nothing to unlink. Skipping resolve() here keeps the common
        # empty-cache clear free of filesystem path resolution.
        if path is None or index_path is None or not index_path.exists():
            return
        with _index_lock:
            cache_root = path.resolve()
            index = _load_search_index()
            for entry in index.values():
                with suppress(OSError, RuntimeError, TypeError, KeyError):
                    file_path = Path(entry["file"]).resolve()
                    if cache_root in file_path.parents:
                        file_path.unlink()
            with suppress(OSError):
                index_path = _search_index_path()
                if index_path is not None:
                    index_path.unlink()


search_memo = SearchMemo()


def slice_search_response(response: dict, limit: int) -> dict:
    """Trim a bucketed response's result list down to the caller's limit."""
    try:
        web = response.get("data", {}).get("web")
        if isinstance(web, list) and len(web) > limit:
            out = _deep_copy(response)
            out["data"]["web"] = out["data"]["web"][:limit]
            return out
    except Exception:
        pass
    return response


# ─── Extract cache (disk-backed, reuses cache/web) ────────────────────────────

_index_lock = threading.Lock()


def _cache_dir() -> Optional[Path]:
    try:
        from hermes_constants import get_hermes_dir
        d = get_hermes_dir("cache/web", "web_cache")
        d.mkdir(parents=True, exist_ok=True)
        return d
    except Exception:
        return None


def _load_index() -> dict:
    try:
        data = json.loads((_cache_dir() / _INDEX_FILENAME).read_text(encoding="utf-8-sig"))
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _save_index(index: dict) -> None:
    if (d := _cache_dir()) is None:
        return
    path = d / _INDEX_FILENAME
    try:
        if len(index) > _INDEX_MAX_ENTRIES:
            newest = sorted(index.items(), key=lambda kv: kv[1].get("fetched_at", 0), reverse=True)
            index = dict(newest[:_INDEX_MAX_ENTRIES])
        # CLI, gateway, cron, and subagents all write this index; the replace is atomic, so the worst case
        # under concurrent writers is a lost insert, never a truncated index.
        atomic_json_write(path, index, indent=None)
    except Exception as exc:
        logger.debug("Failed to save web extract cache index: %s", exc)


def _url_digest(url: str, format: Optional[str], provider: str = "") -> str:
    # format AND provider are part of the key: html != markdown, and one backend's rendering is not another's.
    raw = f"{url}\n{format or 'markdown'}\n{provider or ''}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _entry_file_path(url: str, format: Optional[str], provider: str) -> Optional[Path]:
    """Dedicated cache file per (url, format, provider) — deliberately NOT the truncate-store file
    (keyed on URL alone), which html/markdown or two providers' copies of one URL would overwrite.

    The truncate-store file keeps its role for read_file paging; these files exist only for cache reuse and
    carry the full key in their name. See #94618.
    """
    if (d := _cache_dir()) is None:
        return None
    slug = "page"
    with suppress(Exception):
        slug = _host_slug(url)
    return d / f"{slug}-{_url_digest(url, format, provider)}.cache.md"


def _host_matches_pattern(host: str, pattern: str) -> bool:
    """Case-insensitive: exact, ``*.wildcard``, or bare-domain suffix
    (``mysite.dev`` also matches ``preview.mysite.dev``)."""
    host = host.lower().strip(".")
    pattern = (pattern or "").lower().strip().strip(".").removeprefix("*.")
    return bool(pattern) and (host == pattern or host.endswith("." + pattern))


def _is_cache_exempt_host(url: str) -> bool:
    """True when the host matches ``web.cache_exempt_hosts`` — sites the user develops over public DNS
    (staging, tunnels, previews) that must fetch live."""
    try:
        patterns = _web_config().get("cache_exempt_hosts") or []
        host = (urlparse(url).hostname or "").strip("[]")
        if not isinstance(patterns, (list, tuple)) or not host:
            return False
        return any(_host_matches_pattern(host, str(p)) for p in patterns)
    except Exception:
        return False


def _is_local_dev_url(url: str) -> bool:
    """True for loopback/private/LAN URLs — never cached: they are the user's own fast-changing dev servers.
    Hostname heuristics only, no DNS: this is a freshness decision, not a security boundary (SSRF enforcement
    lives in tools/url_safety.py, which blocks these by default anyway)."""
    try:
        host = (urlparse(url).hostname or "").strip("[]").lower()
        # Unparseable → don't cache; single-label (no "." / ":") == LAN name, not public DNS.
        if not host or host == "localhost" or host.endswith((".localhost", ".local")):
            return True
        if "." not in host and ":" not in host:
            return True
        import ipaddress
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            return False  # public DNS name
        return ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_unspecified
    except Exception:
        return True


def _cacheable(url: str) -> bool:
    """Extract-cache gate: enabled, not a local-dev host, not user-exempted."""
    return cache_enabled() and not (_is_local_dev_url(url) or _is_cache_exempt_host(url))


def extract_cache_get(url: str, format: Optional[str] = None, provider: str = "") -> Optional[dict]:
    """Return {'url','title','content'} for a fresh cached page, else None."""
    if not _cacheable(url):
        return None
    with _index_lock:
        entry = _load_index().get(_url_digest(url, format, provider))
    if not entry or (time.time() - float(entry.get("fetched_at", 0))) >= ttl_seconds():
        return None
    try:
        file_path, cache_root = Path(entry["file"]), _cache_dir()
        # The index is plain JSON on disk; never let a tampered entry read outside cache/web.
        if cache_root.resolve() not in file_path.resolve().parents:
            return None
        content = file_path.read_text(encoding="utf-8-sig")
    except Exception:
        return None
    logger.info("web_extract cache hit: %s", url)
    return {"url": url, "title": entry.get("title", ""), "content": content, "error": None, "cached": True}


def extract_cache_put(
    url: str, content: str, title: str = "", format: Optional[str] = None, provider: str = ""
) -> None:
    """Store one successful extraction's full clean text for TTL reuse; pages over the truncate-store
    ceiling are not cached (serving a capped copy back as if whole would silently lose the tail)."""
    if not content or not _cacheable(url):
        return
    try:
        from tools.web_tools_truncate import MAX_STORED_TEXT_CHARS
        file_path = _entry_file_path(url, format, provider)
        if len(content) > MAX_STORED_TEXT_CHARS or file_path is None:
            return
        from tools.spill_safety import write_text_exclusive
        write_text_exclusive(file_path, content, private=False, overwrite=True)
        with _index_lock:
            index = _load_index()
            index[_url_digest(url, format, provider)] = {
                "url": url, "file": str(file_path), "title": title or "", "fetched_at": time.time(),
            }
            _save_index(index)
    except Exception as exc:
        logger.debug("Failed to cache web extract for %s: %s", url, exc)
