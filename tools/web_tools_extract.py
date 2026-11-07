"""web_extract helpers: URL validation, provider resolution, cache-aware dispatch.

Order of controls (each is a gate, never skipped by a cache hit): secret-URL
refusal -> SSRF filter (in web_tools.web_extract_tool) -> provider resolution
(strict selection) -> per-URL website policy -> direct/local route -> disk cache
-> vendor call with one-shot keyless rescue. Logs under the origin (tools.web_tools) logger.
"""

import asyncio
import json
import logging
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

from tools.tool_backend_helpers import selection_error, selection_exists
from tools.url_safety import normalize_url_for_request
from tools.web_tools_rescue import _rescue_eligible, _rescue_extract

logger = logging.getLogger("tools.web_tools")

_NO_RESULT_ERROR = "Extract backend returned no result for this URL"
_DEFAULT_EXTRACT_TIMEOUT_S = 120.0
_EXTRACT_BACKENDS_HINT = "firecrawl, tavily, keenable, exa, or parallel."
_INVALID_ITEM_ERROR = (
    "Invalid URL item at index {}: expected a URL string or an object with a string 'url' or 'href' field"
)


def _web_extract_url(value: Any) -> Optional[str]:
    """URL from a model-supplied extract item (str, or dict with ``url``/``href``); None if unusable.

    Models sometimes forward a whole search result instead of its URL, hence the dict form. Never
    stringify arbitrary objects into misleading fetch targets.
    """
    if isinstance(value, dict):
        value = value.get("url") or value.get("href")
    return (value.strip() or None) if isinstance(value, str) else None


def _disabled_plugin_error(capability: str, disabled_key: str) -> str:
    """Error text when the configured backend's bundled plugin is disabled in config."""
    vendor = disabled_key.split("/", 1)[-1]
    return (
        f"web.{capability}_backend is set to '{vendor}', but its plugin ('{disabled_key}') is disabled "
        f"in config. Re-enable it with `hermes plugins enable {disabled_key}` "
        "(or remove it from plugins.disabled)."
    )


def _no_provider_error(capability: str, fallback: str) -> str:
    """Error when no provider resolved: point at a disabled bundled plugin if that is the real cause."""
    from agent.web_search_registry import _disabled_web_plugin_for
    disabled_key = _disabled_web_plugin_for(capability=capability)
    return _disabled_plugin_error(capability, disabled_key) if disabled_key else fallback


def _strict_selection_error(capability: str, backend: str) -> str:
    """Error for a stored-but-unregistered backend: name the disabled plugin, else the bad selection.
    Strict selection never silently switches to whatever the availability walk finds."""
    failure = f"no registered web {capability} provider has that name"
    return _no_provider_error(capability, selection_error("web", f"'{backend}'", failure))


def _result_entry(url: str, error: Optional[str]) -> dict[str, Any]:
    return {"url": url, "title": "", "content": "", "error": error}


def _extract_error_json(error: str) -> str:
    return json.dumps({"success": False, "error": error}, ensure_ascii=False)


def _refuse_all(error: str):
    """Whole-call refusal tuple for ``_validate_extract_urls`` (exfiltration prevention)."""
    return None, None, None, json.dumps({"success": False, "error": error})


def _extract_identity_key(url: Any) -> Optional[tuple]:
    """Compare provider canonicalizations without changing fetch targets or cache keys."""
    if not isinstance(url, str) or not url:
        return None
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError:
        return (url,)
    if (parts.scheme, port) in (("http", 80), ("https", 443)):
        port = None
    return (parts.scheme.lower(), parts.hostname, port, parts.path.rstrip("/"),
            parts.query, parts.username, parts.password)


def _extract_result_identity(result: dict, requested: set[str], keys: dict) -> Optional[str]:
    metadata = result.get("metadata")
    source = metadata.get("sourceURL") if isinstance(metadata, dict) else None
    # sourceURL owns a redirect even when its final URL is another requested URL.
    for candidate in (source, result.get("url")):
        if isinstance(candidate, str) and candidate in requested:
            return candidate
        identities = keys.get(_extract_identity_key(candidate), set())
        if identities:
            return next(iter(identities)) if len(identities) == 1 else None
    return None


def _infer_extract_redirect(result: dict, keys: dict) -> bool:
    metadata = result.get("metadata")
    source = metadata.get("sourceURL") if isinstance(metadata, dict) else None
    final = _extract_identity_key(result.get("url"))
    # A canonical collision is not an unknown redirect, even with one slot left.
    return bool(final and final not in keys and (not source or _extract_identity_key(source) == final))


def _match_extract_results(urls: list[str], results: list[dict]) -> list[dict]:
    """Align identities first; infer redirects only for one unambiguous leftover pair."""
    requested = set(urls)
    keys = {}
    for url in requested:
        keys.setdefault(_extract_identity_key(url), set()).add(url)
    matched = {}
    unmatched = []
    for result in results:
        identity = _extract_result_identity(result, requested, keys)
        if identity is None:
            unmatched.append(result)
        elif identity not in matched or (matched[identity].get("error") and not result.get("error")):
            matched[identity] = result
    remaining = requested - matched.keys()
    # Deliberate tradeoff: infer this lone pair as a redirect without source
    # evidence, then cache its content under the requested key for the TTL.
    # Multiple leftovers remain errors rather than risk positional misattribution.
    if len(remaining) == 1 and len(unmatched) == 1:
        result = unmatched[0]
        if _infer_extract_redirect(result, keys):
            matched[remaining.pop()] = result
    ordered = []
    for url in urls:
        result = dict(matched[url]) if url in matched else _result_entry(url, _NO_RESULT_ERROR)
        if not result.get("url"):
            result["url"] = url
        if result["url"] != url:
            result["requested_url"] = url
        ordered.append(result)
    return ordered


def _cache_extract_results(
    provider_name: str, urls: list[str], results: list[dict],
    format: Optional[str], direct_fallbacks: set[str],
) -> None:
    from tools.web_extract_html import MIN_DIRECT_CHARS
    from tools.web_result_cache import extract_cache_put

    for url, fetched in zip(urls, results):
        content = fetched.get("raw_content", "") or fetched.get("content", "")
        metadata = fetched.get("metadata")
        rescued = isinstance(metadata, dict) and metadata.get("rescued_from")
        if not content or fetched.get("error") or rescued:
            continue
        # Direct misses may be walls/previews. Cache the paid replacement only
        # when it supplies substantial text, never the thin direct response.
        if url in direct_fallbacks and len(content.strip()) < MIN_DIRECT_CHARS:
            continue
        cache_fields: dict[str, Any] = {"result_url": fetched["url"]} if fetched.get("url") != url else {}
        if url in direct_fallbacks:
            _attribute_direct_fallbacks([fetched], provider_name)
            cache_fields["provenance"] = {key: fetched[key] for key in ("fallback_from", "extract_backend", "direct_fetch_error")}
        extract_cache_put(url, content, fetched.get("title", ""), format=format, provider=provider_name, **cache_fields)


def _merge_in_order(
    total: int, fixed: dict[int, dict], fetch_positions: list[int], fetch_urls: list[str], results: list[dict]
) -> list[dict]:
    """Merge fixed entries and already identity-aligned dispatch results by request position."""
    merged = dict(fixed)
    for pos, position in enumerate(fetch_positions):
        missing = _result_entry(fetch_urls[pos], _NO_RESULT_ERROR)
        merged[position] = results[pos] if pos < len(results) else missing
    return [merged[i] for i in range(total)]


def _validate_extract_urls(urls: list[Any]):
    """Normalize model-supplied items and block URLs carrying secrets (percent-encoded forms are unquoted
    and checked too). Returns ``(normalized_urls, normalized_indices, invalid_urls, blocked_json)``;
    ``blocked_json`` is a whole-call refusal (exfiltration prevention) or None."""
    from agent.redact import _PREFIX_RE
    from urllib.parse import unquote

    normalized_urls, normalized_indices, invalid_urls = [], [], {}
    for index, item in enumerate(urls):
        _url = _web_extract_url(item)
        if _url is None:
            invalid_urls[index] = _result_entry("", _INVALID_ITEM_ERROR.format(index))
            continue
        normalized_url = normalize_url_for_request(_url)
        if any(_PREFIX_RE.search(c) for c in (_url, unquote(_url), normalized_url, unquote(normalized_url))):
            return _refuse_all(
                "Blocked: URL contains what appears to be an API key or token. "
                "Secrets must not be sent in URLs."
            )
        normalized_urls.append(normalized_url)
        normalized_indices.append(index)
    return normalized_urls, normalized_indices, invalid_urls, None


def _resolve_extract_provider(backend: str):
    """Resolve the extract provider for *backend*; returns ``(provider, error_json)``.

    A registered search-only backend is a typed error (never a silent switch). An unregistered name with
    a stored web selection is a strict-selection error; with no selection, fall through to the walk.
    """
    from agent.web_search_registry import get_active_extract_provider, get_provider as _wsp_get_provider
    provider = _wsp_get_provider(backend) if backend else None
    if provider is not None and provider.supports_extract():
        return provider, None
    if provider is not None:
        return None, _extract_error_json(
            f"{provider.display_name} is a search-only backend and cannot extract URL content. "
            "Set web.extract_backend to " + _EXTRACT_BACKENDS_HINT
        )
    if backend and selection_exists("web"):
        return None, _extract_error_json(_strict_selection_error("extract", backend))
    provider = get_active_extract_provider()
    if provider is None:
        fallback = "No web extract provider configured. Set web.extract_backend to " + _EXTRACT_BACKENDS_HINT
        return None, _extract_error_json(_no_provider_error("extract", fallback))
    return provider, None


def _extract_timeout_seconds() -> float:
    """Wall-clock cap for one provider ``extract()`` dispatch (``web.extract_timeout``, default 120s).

    A hanging backend (server keeps the response open without finishing) otherwise stalls the
    tool call indefinitely. 0 or a negative value disables the cap.
    """
    from tools.web_tools import _load_web_config
    try:
        return float(_load_web_config().get("extract_timeout", _DEFAULT_EXTRACT_TIMEOUT_S))
    except (TypeError, ValueError):
        return _DEFAULT_EXTRACT_TIMEOUT_S


async def _dispatch_extract(provider, fetch_urls: list[str], format: Optional[str], *, direct_fallbacks: Optional[set[str]] = None) -> list[dict]:
    """Call ``provider.extract`` (async or sync-in-thread), with one-shot keyless rescue.

    Rescue fires on a raised exception — including a dispatch timeout — or when the WHOLE batch
    failed (backend outage, not per-page problems). Rescued batches are never cached.
    """
    import inspect
    timeout = _extract_timeout_seconds()
    try:
        if inspect.iscoroutinefunction(provider.extract):
            coro = provider.extract(fetch_urls, format=format)
        else:  # sync extract() runs in a thread so network I/O never blocks the loop
            coro = asyncio.to_thread(provider.extract, fetch_urls, format=format)
        if timeout > 0:
            results = await asyncio.wait_for(coro, timeout=timeout)
        else:
            results = await coro
    except TimeoutError:  # hanging backend — bounded, never a stalled tool call
        logger.warning("web_extract provider '%s' timed out after %.0fs for %d URL(s)",
                       provider.name, timeout, len(fetch_urls))
        failed = [_result_entry(u, f"Extract timed out after {timeout:.0f}s via {provider.name}")
                  for u in fetch_urls]
        if not _rescue_eligible(provider):
            return failed
        return _match_extract_results(fetch_urls, await asyncio.to_thread(_rescue_extract, provider.name, fetch_urls, failed))
    except Exception as exc:
        logger.exception("web_extract provider %s failed", provider.name)
        failed = [_result_entry(u, str(exc)) for u in fetch_urls]
        if not _rescue_eligible(provider):
            return failed
        return _match_extract_results(fetch_urls, await asyncio.to_thread(_rescue_extract, provider.name, fetch_urls, failed))
    if results and all(r.get("error") for r in results) and _rescue_eligible(provider):
        return _match_extract_results(fetch_urls, await asyncio.to_thread(_rescue_extract, provider.name, fetch_urls, results))

    results = _match_extract_results(fetch_urls, results)
    _cache_extract_results(provider.name, fetch_urls, results, format, direct_fallbacks or set())
    return results


def _direct_html_enabled(provider) -> bool:
    # Resolve the paid route here without changing Exa's search plugin or schema.
    from plugins.web.exa.provider import ExaWebSearchProvider, provider_env, use_keyless

    return isinstance(provider, ExaWebSearchProvider) and not use_keyless("exa", provider_env("EXA_API_KEY"))


async def _try_direct(url: str, allow_html: bool) -> Optional[dict]:
    from tools.web_extract_direct import extract_direct
    from tools.web_extract_html import MIN_DIRECT_CHARS
    try:
        direct = await extract_direct(url, allow_html=True) if allow_html else await extract_direct(url)
    except Exception as exc:  # health: allow BLE001 -- direct extraction is optional; any failure uses the configured provider
        logger.debug("Direct extract miss for %s: %s", url, exc)
        return None
    if direct is None or direct.get("error"):
        return None
    if allow_html and len(direct.get("content", "").strip()) < MIN_DIRECT_CHARS:
        return None
    if allow_html:
        direct["extract_backend"] = "direct_http"
    return direct


def _attribute_direct_fallbacks(results: list[dict], provider_name: str) -> None:
    for result in results:
        metadata = result.get("metadata")
        rescued = isinstance(metadata, dict) and metadata.get("rescued_from")
        result.update(
            fallback_from="direct_http",
            extract_backend="keyless_rescue" if rescued else provider_name,
            direct_fetch_error="Direct fetch failed, was blocked, unsupported, or returned thin/JS-only content. Used the configured extract backend.",
        )


async def _dispatch_extract_groups(provider, fetch_urls: list[str], format: Optional[str], fallbacks: set[str]) -> list[dict]:
    from itertools import groupby

    results = []
    # Keep fallback provenance attached to the dispatch, not guessed from result
    # positions: providers may omit failures and canonicalize every returned URL.
    for is_fallback, group in groupby(fetch_urls, key=fallbacks.__contains__):
        urls = list(group)
        fetched = await _dispatch_extract(provider, urls, format, direct_fallbacks=fallbacks)
        if is_fallback:
            _attribute_direct_fallbacks(fetched, provider.name)
        results.extend(fetched)
    return results


async def _extract_safe_urls(provider, safe_urls: list[str], format: Optional[str]) -> list[dict]:
    """Serve cache hits, fetch the rest, and merge back in ``safe_urls`` order.

    The disk cache (tools/web_result_cache.py) sits AFTER the secret-URL gate, SSRF gate, and provider
    resolution, and is gated per-URL on the website policy — a hit skips only the vendor call, never a
    control; policy-blocked URLs are cache misses. Keys include provider and format, so switching either
    within the TTL never serves the other's content."""
    from tools.web_result_cache import extract_cache_get
    from tools.web_extract_direct import is_html_candidate
    from tools.website_policy import check_website_access as _check_site
    from tools.web_tools import _load_web_config
    from utils import is_truthy_value

    direct_enabled = is_truthy_value(_load_web_config().get("extract_direct"), default=True)
    html_enabled = direct_enabled and _direct_html_enabled(provider) and format in (None, "markdown")
    direct_fallbacks: set[str] = set()
    fixed, fetch_urls, fetch_positions = {}, [], []
    for position, url in enumerate(safe_urls):
        try:
            policy_block = _check_site(url)
        except Exception:
            policy_block = None
        allow_html = html_enabled and is_html_candidate(url)
        if policy_block is None and direct_enabled:
            direct = await _try_direct(url, allow_html)
            if direct is not None:
                fixed[position] = direct
                continue
            if allow_html:
                direct_fallbacks.add(url)
        hit = extract_cache_get(url, format=format, provider=provider.name) if policy_block is None else None
        if hit is not None:
            if url in direct_fallbacks or hit.get("fallback_from") == "direct_http":
                _attribute_direct_fallbacks([hit], provider.name + "_cache")
            fixed[position] = hit
        else:
            fetch_urls.append(url)
            fetch_positions.append(position)

    if not fetch_urls:
        return [fixed[i] for i in range(len(safe_urls))]
    logger.info("Web extract via %s: %d URL(s)", provider.name, len(fetch_urls))
    if direct_fallbacks:
        results = await _dispatch_extract_groups(provider, fetch_urls, format, direct_fallbacks)
    else:
        results = await _dispatch_extract(provider, fetch_urls, format)
    return _merge_in_order(len(safe_urls), fixed, fetch_positions, fetch_urls, results)
