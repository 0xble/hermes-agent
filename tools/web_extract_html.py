"""Accept substantive direct HTML, ignoring removed consent UI but not access walls."""

import json
import re
from collections import defaultdict
from html.parser import HTMLParser
from urllib.parse import urljoin

MIN_DIRECT_CHARS = 600
_MAX_HTML_CHARS = 2 * 1024 * 1024
_MAX_JSONLD_CHARS = 64 * 1024
_NON_VISIBLE = frozenset({"script", "style", "template"})
_BLOCKED = frozenset({
    "script", "style", "template", "noscript", "nav", "header", "footer", "aside",
    "form", "button", "label", "select", "dialog", "svg", "canvas",
})
_ROLES = frozenset({"dialog", "alertdialog", "navigation", "banner"})
_VOID = frozenset({"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "param", "source", "track", "wbr"})
_BLOCKS = frozenset({"p", "li", "pre", "blockquote", "h1", "h2", "h3", "h4", "h5", "h6", "td", "dd"})
_SCOPES = frozenset({"main", "article", "body"})
_WALL_BREAKS = _BLOCKS | _SCOPES | _BLOCKED | {"div", "section", "br"}
_CHALLENGE_MARKER = re.compile(r"cf-chl|challenge-platform|g-recaptcha|h-captcha|turnstile", re.IGNORECASE)
_CONTAINER = re.compile(
    r"cookie|consent|gdpr|onetrust|(?:^|[^a-z])cmp(?:[^a-z]|$)|didomi|usercentrics|"
    r"trustarc|quantcast|sp_message|cookiebot|osano|truste|privacy-banner", re.IGNORECASE,
)
_CONSENT = re.compile(
    r"\b(?:accept (?:all )?cookies|cookies?[- ](?:settings|preferences?)|we (?:and our \w+ )?use cookies|"
    r"consent (?:preferences|settings)|manage (?:your )?consent|consent to|personalised advertising|personalized advertising)\b",
    re.IGNORECASE,
)
_WALL = re.compile(
    r"\b(?:(?:log[ -]?in|sign[ -]?in) (?:to|for) (?:continue|read|access|view)|"
    r"subscribe (?:to|for) (?:continue|read|access|view|full access)|"
    r"keep reading|continue reading|read the full (?:article|story)|members?[- ]only|"
    r"create a free account|register to (?:continue|read)|sign up to (?:continue|read)|"
    r"article limit|free articles? remaining|you(?:['’]ve| have) reached your (?:free )?(?:article|story) limit|"
    r"already a subscriber|become a member|unlock (?:this|the full) (?:article|story)|"
    r"subscriber[- ](?:only|exclusive)|start your free trial|paywall|"
    r"(?:not available|unavailable) in your (?:country|region)|page not found|404|page (?:has )?moved|"
    r"under maintenance|temporarily unavailable|browser is not supported|unsupported browser|update your browser|"
    r"confirm your age|you must be (?:18|21)|(?:open|continue) in (?:the )?app|something went wrong|an error occurred|"
    r"enable javascript|turn on javascript|javascript (?:is required|must be enabled)|"
    r"browser verification|verify (?:that )?you are human|checking your browser|captcha)\b",
    re.IGNORECASE,
)
_CHALLENGE_TITLE = re.compile(r"^(?:just a moment|attention required|access denied|verify (?:that )?you are human)\b", re.IGNORECASE)


def _normalize(text: str) -> str:
    return " ".join(text.split())


def _is_false(value: object) -> bool:
    return value is False or isinstance(value, str) and value.strip().casefold() == "false"


def _jsonld_requires_provider(text: str) -> bool:
    try:
        pending = [json.loads(text)]
    except (ValueError, RecursionError):
        # Unreadable publisher metadata cannot establish that the article is free.
        return True
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            for key, child in value.items():
                if key.casefold() == "isaccessibleforfree" and _is_false(child):
                    return True
                if isinstance(child, (dict, list)):
                    pending.append(child)
        elif isinstance(value, list):
            pending.extend(value)
    return False


class _ReadableHTML(HTMLParser):
    def __init__(self, url: str):
        super().__init__(convert_charrefs=True)
        self.url = url
        self.stack: list[tuple[str, bool]] = []
        self.positions: dict[str, list[int]] = defaultdict(list)
        self.depth: dict[str, int] = defaultdict(int)
        self.blocked = 0
        self.block_depth = 0
        self.has_main = False
        self.has_body = False
        self.wall_text: list[str] = []
        self.consent_text: list[str] = []
        self.title: list[str] = []
        self.publisher_wall = False
        self.jsonld: list[str] | None = None
        self.jsonld_size = 0
        # Only literal false matters. The 6-character sentinel bounds nested value copies.
        self.free_values: list[tuple[int, str]] = []
        self.chunks: list[str] = []
        self.links: list[tuple[bool, bool, str]] = []
        self.blocks: list[tuple[bool, bool, str]] = []

    def _flush(self) -> None:
        if self.chunks:
            self.blocks.append((bool(self.depth["main"] or self.depth["article"]),
                                bool(self.depth["body"]), _normalize("".join(self.chunks))))
        self.chunks = []

    def _finish_jsonld(self) -> None:
        if self.jsonld is not None:
            self.publisher_wall |= _jsonld_requires_provider("".join(self.jsonld))
            self.jsonld = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if len(self.stack) >= 512:
            raise ValueError("HTML nesting exceeds direct extraction limit")
        attributes = dict(attrs)
        if tag == "script" and (attributes.get("type") or "").split(";", 1)[0].strip().casefold() == "application/ld+json":
            self.jsonld, self.jsonld_size = [], 0
        free_property = "isaccessibleforfree" in (attributes.get("itemprop") or "").casefold().split()
        if free_property:
            self.publisher_wall |= any(_is_false(attributes.get(key)) for key in ("content", "value"))
            if tag not in _VOID:
                self.free_values.append((len(self.stack), ""))
        if tag in _WALL_BREAKS and not any(self.depth[name] for name in _NON_VISIBLE):
            self.wall_text.append(" ")
            self.consent_text.append(" ")
        marker = " ".join(attributes.get(key) or "" for key in ("id", "class"))
        hidden = ("hidden" in attributes or (attributes.get("aria-hidden") or "").lower() == "true"
                  or "display:none" in (attributes.get("style") or "").replace(" ", "").lower())
        blocked = tag in _BLOCKED or hidden or (attributes.get("role") or "").lower() in _ROLES or bool(_CONTAINER.search(marker))
        if tag in _BLOCKS | _SCOPES and not self.blocked:
            self._flush()
        if tag not in _VOID:
            self.positions[tag].append(len(self.stack))
            self.stack.append((tag, blocked))
            self.depth[tag] += 1
            self.block_depth += tag in _BLOCKS
            self.blocked += blocked
        self.has_main |= tag in {"main", "article"}
        self.has_body |= tag == "body"
        if not self.blocked:
            if tag == "a" and attributes.get("href"):
                href = urljoin(self.url, attributes["href"])
                if href.startswith(("https://", "http://")):
                    self.links.append((bool(self.depth["main"] or self.depth["article"]),
                                       bool(self.depth["body"]), href))
            if tag == "br":
                self.handle_data(" ")

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in _VOID:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        positions = self.positions[tag]
        if not positions:
            return
        if tag == "script":
            self._finish_jsonld()
        if tag in _WALL_BREAKS and not any(self.depth[name] for name in _NON_VISIBLE):
            self.wall_text.append(" ")
            self.consent_text.append(" ")
        if tag in _BLOCKS | _SCOPES and not self.blocked:
            self._flush()
        # Indexed matching plus amortized pops avoids scanning/reparsing nested subtrees.
        index = positions[-1]
        while len(self.stack) > index:
            if self.free_values and self.free_values[-1][0] == len(self.stack) - 1:
                _, value = self.free_values.pop()
                self.publisher_wall |= _is_false(value)
                if self.free_values:
                    position, parent = self.free_values[-1]
                    self.free_values[-1] = (position, (parent + value)[:6])
            name, blocked = self.stack.pop()
            self.positions[name].pop()
            self.depth[name] -= 1
            self.block_depth -= name in _BLOCKS
            self.blocked -= blocked

    def handle_data(self, data: str) -> None:
        if self.jsonld is not None and self.depth["script"]:
            self.jsonld_size += len(data)
            if self.jsonld_size > _MAX_JSONLD_CHARS:
                self.publisher_wall = True
                self.jsonld = None
            else:
                self.jsonld.append(data)
        if any(self.depth[name] for name in _NON_VISIBLE):
            return
        # Removed cookie UI is benign, but chrome must never hide an access wall.
        self.wall_text.append(data)
        if self.free_values:
            position, value = self.free_values[-1]
            self.free_values[-1] = (position, (value + data.strip())[:6])
        if self.depth["title"]:
            self.title.append(data)
        if self.blocked:
            return
        if not self.depth["head"] and not self.depth["title"]:
            self.consent_text.append(data)
        if not self.depth["title"] and not self.depth["head"] and self.block_depth:
            self.chunks.append(data)


def _distinct_blocks(parser: _ReadableHTML) -> list[str]:
    blocks, seen_blocks, seen_sentences = [], set(), set()
    for main, body, text in parser.blocks:
        if (parser.has_main and not main) or (not parser.has_main and parser.has_body and not body):
            continue
        key = text.casefold()
        if not text or key in seen_blocks:
            continue
        seen_blocks.add(key)
        sentences = []
        # Link destinations never count as prose evidence.
        for sentence in re.split(r"(?<=[.!?])\s+", re.sub(r"https?://[^\s)]+", "", text)):
            sentence = sentence.strip()
            key = sentence.casefold()
            if sentence and key not in seen_sentences:
                seen_sentences.add(key)
                sentences.append(sentence)
        prose = " ".join(sentences)
        if prose:
            blocks.append(prose)
    return blocks


def readable_html(html: str, url: str) -> dict | None:
    # Never accept a truncated preview. Over-cap documents use the existing provider path.
    if len(html) > _MAX_HTML_CHARS or _CHALLENGE_MARKER.search(html):
        return None
    parser = _ReadableHTML(url)
    parser.feed(html)
    parser.close()
    parser._finish_jsonld()
    parser._flush()
    title = _normalize("".join(parser.title))
    if (parser.publisher_wall or any(_is_false(value) for _, value in parser.free_values)
            or _CHALLENGE_TITLE.search(title) or _WALL.search(_normalize("".join(parser.wall_text)))
            or _CONSENT.search(title) or _CONSENT.search(_normalize("".join(parser.consent_text)))):
        return None
    blocks = _distinct_blocks(parser)
    if sum(len(prose) >= 80 for prose in blocks) < 3 or sum(map(len, blocks)) < MIN_DIRECT_CHARS:
        return None
    links = [href for main, body, href in parser.links
             if main or not parser.has_main and (body or not parser.has_body)]
    content = "\n\n".join(blocks + list(dict.fromkeys(links)))
    return {"url": url, "title": title, "content": content, "error": None}
