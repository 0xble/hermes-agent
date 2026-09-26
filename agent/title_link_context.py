"""Page metadata for the first link in an opening message, used only to inform its session title.

The titler already runs off the critical path in a daemon thread; this adds one bounded, fail-soft
GET there. Only ``<title>``, ``og:title``, ``og:description`` and ``meta description`` are read,
never the body, and the result goes to the auxiliary title model alone (never the main turn).

The fetch is automatic, so it is stricter than the agent's own web tools:

- private, loopback, CGNAT and metadata addresses are refused, before the request and again at
  TCP connect, even when ``security.allow_private_urls`` is on (no model or person chose this
  request);
- the website blocklist applies;
- links that look side-effecting (magic links, resets, verification, unsubscribe, invites) or carry
  credential-named query parameters are never fetched, since a GET can consume them;
- no cookies or credentials are sent, redirects are validated hop by hop, and time, size, redirect
  count and content type are all capped.
"""

from __future__ import annotations

import codecs
import html
import logging
import re
import time
from html.parser import HTMLParser
from typing import Optional
from urllib.parse import unquote, urljoin, urlsplit

logger = logging.getLogger(__name__)

# Total wall-clock budget for the whole lookup (DNS, redirects, body). Kept well under the 10s
# ``wait_for_title_upgrades`` join so a one-shot run never waits on a slow site.
LINK_FETCH_BUDGET_SECONDS = 4.0
# Read stops at ``</head>`` or ``<body>``; the cap only bounds pages whose head ends late (YouTube's
# closes ~1.25 MB in) or never. Bytes stream through the parser, so this bounds work, not memory.
MAX_LINK_BYTES = 2 * 1024 * 1024
MAX_LINK_REDIRECTS = 3
MAX_LINK_CONTEXT_CHARS = 500
_USER_AGENT = "Mozilla/5.0 (compatible; HermesTitle/1.0; +https://hermes-agent.nousresearch.com)"
_ACCEPTED_CONTENT_TYPES = ("text/html", "application/xhtml+xml", "text/plain")

# First http(s) URL in text order. Markdown ``[text](url)`` and ``<url>`` forms are reached because the
# URL itself starts at ``http``; the terminator set stops at whitespace, quotes, angle brackets and
# the closing paren of a Markdown link.
_URL_RE = re.compile(r"https?://[^\s<>\"'`]+", re.IGNORECASE)
_TRAILING_PUNCT = ".,;:!?*_~"
# Path words for links whose GET is itself an action: consuming a login, confirming, resetting,
# unsubscribing, accepting. Matched per path segment, so ``/blog/password-managers`` still fetches.
_SIDE_EFFECT_SEGMENT_RE = re.compile(
    r"(?:^|[-_.])(?:magic|login|signin|sign-in|logout|signout|verify|verification|confirm|confirmation|"
    r"activate|activation|reset|unsubscribe|optout|opt-out|invite|invitation|accept|approve|"
    r"authorize|oauth|callback|redeem|claim|delete|remove|cancel|one-click|oneclick)(?:$|[-_.])",
    re.IGNORECASE,
)
_SIDE_EFFECT_QUERY_KEYS = frozenset({"code", "otp", "nonce", "state", "key", "auth", "sig", "hash", "ticket"})


def first_link(text: str) -> Optional[str]:
    """The first http(s) URL in *text*, trimmed of trailing prose punctuation; None when absent."""
    if not isinstance(text, str) or "http" not in text.lower():
        return None
    match = _URL_RE.search(text)
    if not match:
        return None
    url = match.group(0)
    # Strip unbalanced closing brackets (``(see https://x.y/a)``) and sentence punctuation, but keep
    # a balanced pair that is part of the URL (``https://en.wikipedia.org/wiki/Foo_(bar)``).
    while url:
        last = url[-1]
        if last in _TRAILING_PUNCT:
            url = url[:-1]
        elif last in ")]}" and url.count(last) > url.count({")": "(", "]": "[", "}": "{"}[last]):
            url = url[:-1]
        else:
            break
    try:
        parts = urlsplit(url)
    except ValueError:
        return None
    return url if parts.scheme.lower() in ("http", "https") and parts.hostname else None


def link_fetch_refusal(url: str) -> Optional[str]:
    """Why *url* must not be fetched automatically, or None when it may be. No network I/O."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return "unparseable"
    if parts.scheme.lower() not in ("http", "https") or not parts.hostname:
        return "scheme"
    if parts.username or parts.password:
        return "embedded credentials"
    from tools.url_safety import sensitive_query_param_name
    if sensitive_query_param_name(url):
        return "credential-named query parameter"
    for segment in unquote(parts.path).split("/"):
        if segment and _SIDE_EFFECT_SEGMENT_RE.search(segment):
            return "side-effecting path"
    for pair in parts.query.split("&"):
        key = unquote(pair.split("=", 1)[0]).strip().lower()
        if key in _SIDE_EFFECT_QUERY_KEYS:
            return "side-effecting query parameter"
    return None


def _allowed(url: str) -> bool:
    refusal = link_fetch_refusal(url)
    if refusal:
        logger.debug("Title link context skipped (%s): %s", refusal, _redact(url))
        return False
    from tools.website_policy import check_website_access
    if check_website_access(url) is not None:
        logger.debug("Title link context skipped (website policy): %s", _redact(url))
        return False
    from tools.url_safety import is_safe_url
    if not is_safe_url(url, allow_private=False):
        logger.debug("Title link context skipped (non-public address): %s", _redact(url))
        return False
    return True


def _redact(url: str) -> str:
    """Scheme, host and path only: queries can carry identifiers and belong nowhere in logs."""
    try:
        parts = urlsplit(url)
        return f"{parts.scheme}://{parts.hostname}{parts.path}"
    except ValueError:
        return "<unparseable>"


class _MetaParser(HTMLParser):
    """Collects the page title and description metadata from ``<head>``; stops caring at ``<body>``."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title_parts: list[str] = []
        self.meta: dict[str, str] = {}
        self._in_title = False
        self.done = False

    def handle_starttag(self, tag, attrs):
        if self.done:
            return
        if tag == "body":
            self.done = True
        elif tag == "title" and not self.title_parts:
            self._in_title = True
        elif tag == "meta":
            attr = {k.lower(): (v or "") for k, v in attrs}
            name = (attr.get("property") or attr.get("name") or "").strip().lower()
            if name in ("og:title", "og:description", "description", "twitter:title", "twitter:description"):
                self.meta.setdefault(name, attr.get("content", ""))

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False
        elif tag == "head":
            self.done = True

    def handle_data(self, data):
        if self._in_title and not self.done:
            self.title_parts.append(data)


def _clean(text: str, limit: int) -> str:
    text = " ".join(html.unescape(text or "").split())
    return text[:limit].rstrip()


def _text_metadata(body: str) -> Optional[str]:
    first = next((line.strip() for line in body.splitlines() if any(ch.isalnum() for ch in line)), "")
    return f"Text: {_clean(first, MAX_LINK_CONTEXT_CHARS - 6)}" if first else None


def _parser_metadata(parser: "_MetaParser") -> Optional[str]:
    """Bounded ``Title:``/``Description:`` lines; None when the head carried nothing useful."""
    meta = parser.meta
    title = _clean(meta.get("og:title") or meta.get("twitter:title") or "".join(parser.title_parts), 200)
    description = _clean(meta.get("og:description") or meta.get("description")
                         or meta.get("twitter:description") or "", 280)
    lines = [f"Title: {title}"] if title else []
    if description and description != title:
        lines.append(f"Description: {description}")
    return "\n".join(lines)[:MAX_LINK_CONTEXT_CHARS] or None


def _decoder(response):
    try:
        return codecs.getincrementaldecoder(getattr(response, "charset_encoding", None) or "utf-8")(errors="replace")
    except LookupError:
        return codecs.getincrementaldecoder("utf-8")(errors="replace")


def _read_metadata(response, content_type: str, deadline: float) -> Optional[str]:
    """Stream the body into the head parser, stopping at the end of ``<head>``, the byte cap, or the deadline."""
    decoder, size = _decoder(response), 0
    plain = content_type == "text/plain"
    text: list[str] = []
    parser = _MetaParser()
    try:
        for chunk in response.iter_bytes():
            piece = decoder.decode(chunk[: MAX_LINK_BYTES - size])
            if not size:
                piece = piece.lstrip("\ufeff")
            size += len(chunk)
            if plain:
                text.append(piece)
                if "\n" in piece and _text_metadata("".join(text).rsplit("\n", 1)[0]):
                    break
            else:
                parser.feed(piece)
                if parser.done:
                    break
            if size >= MAX_LINK_BYTES or time.monotonic() > deadline:
                break
    except Exception:
        logger.debug("Title link metadata parse failed", exc_info=True)
    return _text_metadata("".join(text)) if plain else _parser_metadata(parser)


def fetch_link_metadata(url: str, *, budget: float = LINK_FETCH_BUDGET_SECONDS, transport=None) -> Optional[str]:
    """Metadata lines for *url*, or None on any refusal or failure. Never raises.

    ``transport`` replaces the network for tests (an ``httpx.MockTransport``); every hop still passes
    the same pre-flight refusal, policy and address checks. Production uses the strict SSRF-guarded
    client, which re-validates the resolved address at TCP connect.
    """
    try:
        import httpx
        from tools.url_safety import create_ssrf_safe_client, normalize_url_for_request
    except Exception:
        logger.debug("Title link context unavailable (httpx or url_safety import failed)", exc_info=True)
        return None
    deadline = time.monotonic() + budget
    current = normalize_url_for_request(url)
    headers = {"User-Agent": _USER_AGENT, "Accept": "text/html,application/xhtml+xml;q=0.9,text/plain;q=0.5"}
    try:
        # trust_env=False: no proxy from the environment (a proxy would resolve the target itself and
        # skip the connect-time check) and no .netrc credentials.
        client = (httpx.Client(transport=transport, follow_redirects=False, trust_env=False, headers=headers)
                  if transport is not None else
                  create_ssrf_safe_client(allow_private=False, follow_redirects=False, trust_env=False, headers=headers))
    except Exception:
        logger.debug("Title link context client construction failed", exc_info=True)
        return None
    try:
        with client:
            for _hop in range(MAX_LINK_REDIRECTS + 1):
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not _allowed(current):
                    return None
                timeout = httpx.Timeout(remaining, connect=min(remaining, 2.0))
                with client.stream("GET", current, timeout=timeout) as response:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            return None
                        current = normalize_url_for_request(urljoin(str(response.url), location))
                        continue
                    if response.status_code != 200:
                        logger.debug("Title link context: HTTP %s from %s", response.status_code, _redact(current))
                        return None
                    content_type = (response.headers.get("content-type") or "").split(";")[0].strip().lower()
                    if content_type not in _ACCEPTED_CONTENT_TYPES:
                        logger.debug("Title link context: unsupported content type %r", content_type)
                        return None
                    return _read_metadata(response, content_type, deadline)
            logger.debug("Title link context: too many redirects from %s", _redact(url))
            return None
    except Exception as exc:
        logger.debug("Title link context fetch failed for %s: %s", _redact(url), exc)
        return None


def link_context_for_title(user_text: str) -> Optional[str]:
    """Untrusted page context for the first link in *user_text*, ready for the title input; else None."""
    url = first_link(user_text)
    if not url:
        return None
    metadata = fetch_link_metadata(url)
    if not metadata:
        return None
    try:
        host = urlsplit(url).hostname or ""
    except ValueError:
        host = ""
    return f"Source: {host}\n{metadata}" if host else metadata
