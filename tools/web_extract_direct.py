"""Free, bounded extraction of plain files and checked-in Hermes documentation.

Call only after web_extract's secret-URL, SSRF, provider and website-policy gates.
A miss returns None, leaving the original URL for the configured provider.
"""

from pathlib import Path
from typing import Any, Optional
from urllib.parse import unquote, urljoin, urlsplit

from tools import website_policy
from tools.url_safety import async_is_safe_url, create_ssrf_safe_async_client

_MAX_BYTES = 5 * 1024 * 1024
_TIMEOUT_SECONDS = 15
_MAX_REDIRECTS = 5
_PLAIN_SUFFIXES = frozenset({".md", ".txt", ".json", ".yaml", ".yml", ".csv", ".xml", ".toml"})
_PLAIN_TYPES = frozenset({
    "application/json", "application/xml", "application/yaml", "application/x-yaml",
    "application/toml", "application/x-toml", "application/markdown", "application/x-ndjson", "application/csv",
    "application/vnd.github.raw+json", "application/vnd.api+json",
})


def _is_plain_type(content_type: str) -> bool:
    mime = content_type.split(";", 1)[0].strip().lower()
    return (mime.startswith("text/") and mime not in {"text/html", "text/x-html"}
            or mime in _PLAIN_TYPES or mime.endswith(("+json", "+xml", "+yaml")))


def _is_plain_url(url: str) -> bool:
    parsed = urlsplit(url)
    host = (parsed.hostname or "").lower().rstrip(".")
    return (host in {"raw.githubusercontent.com", "api.github.com", "gist.githubusercontent.com"}
            or Path(unquote(parsed.path)).suffix.lower() in _PLAIN_SUFFIXES)


def _local_docs(url: str) -> Optional[dict[str, Any]]:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or (parsed.hostname or "").lower().rstrip(".") != "hermes-agent.nousresearch.com":
        return None
    if parsed.port not in (None, 443):
        return None
    raw_path = unquote(parsed.path)
    if not raw_path.startswith("/docs/"):
        return None
    relative = raw_path[len("/docs/"):]
    # Decode once more to reject double-encoded traversal; resolve() also contains symlinks.
    if any(part in ("..", ".") for part in unquote(relative).split("/")) or "\\" in relative:
        return None
    if "\x00" in relative:
        return None
    # Any path or filesystem error (odd names, permissions, loops) is a miss: the provider still runs.
    try:
        docs = (Path(__file__).resolve().parent.parent / "website" / "docs").resolve()
        stem = docs / relative
        candidates = ([stem] if stem.suffix.lower() in {".md", ".mdx"} else
                      [stem.with_suffix(".md"), stem.with_suffix(".mdx"), stem / "index.md", stem / "index.mdx"])
        for candidate in candidates:
            resolved = candidate.resolve()
            if resolved.is_relative_to(docs) and resolved.is_file():
                return {"url": url, "title": f"{resolved.stem} (local Hermes docs checkout)",
                        "content": resolved.read_text(encoding="utf-8-sig"), "error": None}
    except (OSError, ValueError, RuntimeError, UnicodeError):
        return None
    return None


async def _direct_download(url: str) -> Optional[dict[str, Any]]:
    import asyncio
    import httpx
    from agent.redact import _PREFIX_RE

    try:
        async with asyncio.timeout(_TIMEOUT_SECONDS):
            async with create_ssrf_safe_async_client(timeout=_TIMEOUT_SECONDS, follow_redirects=False) as client:
                target = url
                for hop in range(_MAX_REDIRECTS + 1):
                    if not await async_is_safe_url(target):
                        return None
                    async with client.stream("GET", target) as response:
                        if response.is_redirect:
                            location = response.headers.get("location")
                            if not location or hop == _MAX_REDIRECTS:
                                return None
                            next_url = urljoin(target, location)
                            # Never send model-supplied or redirect-supplied secrets to another host.
                            if _PREFIX_RE.search(unquote(next_url)):
                                return None
                            # The dispatcher checked policy only for the original URL; a redirect must not
                            # reach a blocked site. Policy errors fail closed here (the provider still runs).
                            try:
                                if website_policy.check_website_access(next_url) is not None:
                                    return None
                            except Exception:  # noqa: BLE001
                                return None
                            target = next_url
                            continue
                        if not 200 <= response.status_code < 300 or not _is_plain_type(response.headers.get("content-type", "")):
                            return None
                        data = bytearray()
                        async for chunk in response.aiter_bytes():
                            if len(data) + len(chunk) > _MAX_BYTES:
                                return None
                            data.extend(chunk)
                        encoding = response.encoding or "utf-8"
                        try:
                            content = data.decode(encoding)
                        except (UnicodeError, LookupError):
                            return None
                        return {"url": url, "title": Path(urlsplit(target).path).name,
                                "content": content, "error": None}
    except (httpx.HTTPError, OSError, TimeoutError, ValueError, UnicodeError):
        return None
    return None


async def extract_direct(url: str) -> Optional[dict[str, Any]]:
    """Return a provider-shaped entry or None; direct results deliberately skip the vendor cache."""
    docs = _local_docs(url)
    if docs is not None:
        return docs
    parsed = urlsplit(url)
    if (parsed.hostname or "").lower().rstrip(".") == "hermes-agent.nousresearch.com" and parsed.path.startswith("/docs/"):
        return None  # Missing docs (including llms.txt) use the provider, not direct HTTP.
    if _is_plain_url(url):
        return await _direct_download(url)
    return None
