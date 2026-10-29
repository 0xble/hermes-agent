"""Direct extraction policy through registered handlers, using fake SDK/HTTP only."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from plugins.web import keyless_mcp
from plugins.web.exa import provider as exa
from tools import web_extract_direct as direct
from tools import web_result_cache as cache
from tools import web_tools as wt


@pytest.fixture
def backend_harness(monkeypatch, tmp_path):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    provider = exa.ExaWebSearchProvider()
    sdk = Mock()
    sdk.get_contents.side_effect = lambda urls, **kw: SimpleNamespace(results=[
        SimpleNamespace(url=url, title="Paid contents", text="Full provider page") for url in urls
    ])
    monkeypatch.setattr(exa, "_get_exa_client", lambda: sdk)
    monkeypatch.setattr(exa, "provider_env", lambda key: "test-key")
    monkeypatch.setattr(exa, "use_keyless", lambda *a: False)
    monkeypatch.setattr("tools.web_tools_rescue._ring_vendor_keyless", lambda name: False)
    monkeypatch.setattr(wt, "_load_web_config", dict)
    monkeypatch.setattr(wt, "_ensure_web_plugins_loaded", lambda: None)
    monkeypatch.setattr(wt, "_get_extract_backend", lambda: "exa")
    monkeypatch.setattr("agent.web_search_registry.get_provider", lambda name: provider)
    monkeypatch.setattr(wt, "async_is_safe_url", AsyncMock(return_value=True))
    monkeypatch.setattr(direct, "async_is_safe_url", AsyncMock(return_value=True))
    monkeypatch.setattr("tools.website_policy.check_website_access", lambda url: None)
    monkeypatch.setattr(cache, "extract_cache_get", lambda *a, **k: None)
    put = Mock()
    monkeypatch.setattr(cache, "extract_cache_put", put)
    return provider, sdk, put

def _transport(monkeypatch, respond):
    monkeypatch.setattr(direct, "create_ssrf_safe_async_client", lambda **kw: httpx.AsyncClient(
        transport=httpx.MockTransport(respond), **kw,
    ))


def _extract(urls):
    entry = wt.registry.get_entry("web_extract")
    assert entry is not None
    handler = entry.handler
    return json.loads(asyncio.run(handler({"urls": urls})))["results"]


_PARAGRAPHS = (
    "Readable page text describing ceramic techniques, temperatures and materials. "
    "The firing schedule depends on the composition of the clay and the thickness of each vessel. "
    "Artists record these observations to compare the outcome of later experiments.",
    "Glaze preparation starts with carefully measured minerals and a stable mixture of water. "
    "Small test tiles reveal how the finished surface responds to different firing temperatures. "
    "A written recipe allows another potter to reproduce the intended finish.",
    "Kiln loading requires space around every piece so heat can circulate through the chamber. "
    "After firing, the pottery must cool slowly before the door is opened to avoid thermal damage. "
    "The completed pieces are inspected for cracks and variations in color.",
)
_BODY = " ".join(_PARAGRAPHS)
_ARTICLE = "".join(f"<p>{paragraph}</p>" for paragraph in _PARAGRAPHS)


def test_known_url_html_is_cleaned_without_paid_contents(backend_harness, monkeypatch):
    _, sdk, put = backend_harness
    html = f"<html><head><title>Ceramics</title></head><body><nav>Navigation</nav><main><h1>Glazing</h1>{_ARTICLE}<button>Accept all</button></main><script>secret script</script></body></html>"
    _transport(monkeypatch, lambda request: httpx.Response(200, headers={"content-type": "text/html"}, text=html))
    result = _extract(["https://site.test/article"])[0]
    assert result["title"] == "Ceramics"
    assert "ceramic techniques" in result["content"]
    assert "Navigation" not in result["content"] and "secret script" not in result["content"]
    assert result["extract_backend"] == "direct_http"
    sdk.get_contents.assert_not_called()
    put.assert_not_called()


@pytest.mark.parametrize("mode", ["failure", "thin", "blocked", "js", "plain-thin", "links-only", "blank-lines",
                                  "cookie-wall", "login-wall", "paywall-teaser"])
def test_direct_misses_fall_back_visibly_without_cache(backend_harness, monkeypatch, mode):
    _, sdk, put = backend_harness

    def respond(request):
        if mode == "failure":
            raise httpx.ConnectError("offline", request=request)
        body = f"<body><main>{_BODY}</main></body>"
        if mode == "thin":
            body = "<body><main>Too little text</main></body>"
        if mode == "js":
            body = f"<body><h1>Enable JavaScript to continue</h1><main>{_BODY}</main></body>"
        if mode == "links-only":
            body = '<main><a href="https://site.test/' + 'x' * 500 + '">Click</a></main>'
        if mode == "blank-lines":
            body = '<main>Start' + '<p></p>' * 500 + 'End</main>'
        if mode == "cookie-wall":
            body = "<main><h1>We value your privacy</h1><p>" + (
                "We and our vendors use cookies to store information on your device and process personal data. "
                "Manage your consent preferences and privacy choices. " * 5
            ) + "</p><button>Accept all</button><button>Preferences</button></main>"
        if mode == "login-wall":
            body = "<main><h1>Log in</h1><p>" + (
                "Log in to your account to access this page. Enter your email and password. " * 6
            ) + "</p><form><label>Email</label><input><button>Log in</button></form></main>"
        if mode == "paywall-teaser":
            body = "<main><h1>Ceramic materials</h1><p>" + (
                "This introduction describes ceramic techniques and materials " * 7
            ) + ".</p><p>Subscribe for full access to this article and continue reading.</p></main>"
        mime = "text/plain" if mode == "plain-thin" else "text/html"
        if mode == "plain-thin":
            body = "tiny response"
        return httpx.Response(403 if mode == "blocked" else 200, headers={"content-type": mime}, text=body)

    _transport(monkeypatch, respond)
    result = _extract(["https://site.test/article"])[0]
    assert result["title"] == "Paid contents" and result["content"] == "Full provider page"
    assert result["fallback_from"] == "direct_http"
    assert result["extract_backend"] == "exa" and result["direct_fetch_error"]
    sdk.get_contents.assert_called_once_with(["https://site.test/article"], text=True)
    put.assert_not_called()


def test_direct_miss_can_use_existing_paid_cache_visibly(backend_harness, monkeypatch):
    _, sdk, _ = backend_harness
    _transport(monkeypatch, lambda request: httpx.Response(403))
    monkeypatch.setattr(cache, "extract_cache_get", lambda url, **kw: {
        "url": url, "title": "Cached", "content": "Cached provider contents", "error": None,
    })
    result = _extract(["https://site.test/article"])[0]
    assert result["fallback_from"] == "direct_http" and result["extract_backend"] == "exa_cache"
    sdk.get_contents.assert_not_called()


def test_canonical_provider_url_keeps_direct_fallback_attribution(backend_harness, monkeypatch):
    _, sdk, _ = backend_harness
    _transport(monkeypatch, lambda request: httpx.Response(403))
    sdk.get_contents.side_effect = lambda urls, **kw: SimpleNamespace(results=[
        SimpleNamespace(url="https://site.test/canonical", title="Canonical", text="Provider content"),
    ])
    result = _extract(["https://site.test/article"])[0]
    assert result["url"] == "https://site.test/canonical"
    assert result["fallback_from"] == "direct_http"


@pytest.mark.parametrize("extra_url", ["https://site.test/missing", "https://site.test/paper.pdf"])
def test_partial_canonical_batch_keeps_direct_fallback_attribution(backend_harness, monkeypatch, extra_url):
    _, sdk, put = backend_harness
    _transport(monkeypatch, lambda request: httpx.Response(403))

    def contents(urls, **kwargs):
        return SimpleNamespace(results=[
            SimpleNamespace(url="https://site.test/canonical", title="Canonical", text="Provider content"),
        ] if "https://site.test/article" in urls else [])

    sdk.get_contents.side_effect = contents
    result = _extract([extra_url, "https://site.test/article"])
    canonical = next(r for r in result if r["url"] == "https://site.test/canonical")
    assert canonical["fallback_from"] == "direct_http"
    assert canonical["extract_backend"] == "exa" and canonical["direct_fetch_error"]
    put.assert_not_called()


def test_redirected_html_links_use_final_url_but_keep_requested_identity(backend_harness, monkeypatch):
    def respond(request):
        if request.url.host == "site.test":
            return httpx.Response(302, headers={"location": "https://canonical.test/docs/article"})
        return httpx.Response(200, headers={"content-type": "text/html"}, text=(
            f'<main>{_ARTICLE}<a href="next">Next page</a></main>'
        ))

    _transport(monkeypatch, respond)
    result = _extract(["https://site.test/original"])[0]
    assert result["url"] == "https://site.test/original"
    assert "https://canonical.test/docs/next" in result["content"]
    assert result["extract_backend"] == "direct_http"
    backend_harness[1].get_contents.assert_not_called()


def test_private_redirect_is_not_fetched_and_original_url_falls_back(backend_harness, monkeypatch):
    _, sdk, _ = backend_harness
    seen = []

    def respond(request):
        seen.append(str(request.url))
        return httpx.Response(302, headers={"location": "http://127.0.0.1/private"})

    _transport(monkeypatch, respond)
    monkeypatch.setattr(direct, "async_is_safe_url", AsyncMock(side_effect=lambda url: "127.0.0.1" not in url))
    assert _extract(["https://site.test/article"])[0]["fallback_from"] == "direct_http"
    assert seen == ["https://site.test/article"]
    sdk.get_contents.assert_called_once_with(["https://site.test/article"], text=True)


def test_private_original_url_is_rejected_without_any_fetch(backend_harness, monkeypatch):
    _, sdk, _ = backend_harness
    monkeypatch.setattr(wt, "async_is_safe_url", AsyncMock(return_value=False))
    _transport(monkeypatch, lambda request: pytest.fail("private URL must never be fetched"))
    assert "Blocked" in _extract(["http://127.0.0.1/private"])[0]["error"]
    sdk.get_contents.assert_not_called()


def test_pdf_stays_on_existing_paid_path(backend_harness, monkeypatch):
    _, sdk, put = backend_harness
    _transport(monkeypatch, lambda request: pytest.fail("PDF must not use direct HTML"))
    result = _extract(["https://site.test/paper.pdf"])[0]
    assert result["title"] == "Paid contents" and "fallback_from" not in result
    sdk.get_contents.assert_called_once()
    put.assert_called_once()


def test_mixed_direct_and_fallback_preserve_order(backend_harness, monkeypatch):
    def respond(request):
        return httpx.Response(200, headers={"content-type": "text/html"}, text=(
            f"<title>Direct</title><main>{_ARTICLE}</main>" if request.url.path == "/full" else "<main>thin</main>"
        ))

    _transport(monkeypatch, respond)
    urls = ["https://site.test/thin", "https://site.test/full"]
    results = _extract(urls)
    assert [r["url"] for r in results] == urls
    assert [r["title"] for r in results] == ["Paid contents", "Direct"]
    backend_harness[1].get_contents.assert_called_once_with(urls[:1], text=True)


def test_direct_failure_then_402_rescue_keeps_both_attributions(backend_harness, monkeypatch):
    _, sdk, put = backend_harness
    _transport(monkeypatch, lambda request: httpx.Response(403))
    request = httpx.Request("POST", "https://api.exa.ai/contents")
    sdk.get_contents.side_effect = httpx.HTTPStatusError("HTTP 402 Payment Required", request=request,
                                                       response=httpx.Response(402, request=request))
    monkeypatch.setattr(keyless_mcp, "extract_with_failover", lambda name, urls: [
        {"url": urls[0], "title": "Rescued", "content": "Rescued content", "error": None},
    ])
    result = _extract(["https://site.test/article"])[0]
    assert result["title"] == "Rescued"
    assert result["fallback_from"] == "direct_http" and result["extract_backend"] == "keyless_rescue"
    assert result["metadata"]["rescued_from"] == "exa"
    assert "402" in result["metadata"]["backend_error"]
    put.assert_not_called()


@pytest.mark.parametrize("route", ["disabled", "keyless", "other-provider", "non-markdown"])
def test_existing_routes_do_not_opt_into_html(backend_harness, monkeypatch, route):
    provider, _, _ = backend_harness
    if route == "disabled":
        monkeypatch.setattr(wt, "_load_web_config", lambda: {"extract_direct": False})
    elif route == "keyless":
        monkeypatch.setattr(exa, "use_keyless", lambda *args: True)
    elif route == "other-provider":
        provider = SimpleNamespace(name="other-provider", supports_extract=lambda: True)
        monkeypatch.setattr("agent.web_search_registry.get_provider", lambda name: provider)
    provider.extract = Mock(return_value=[{"url": "https://site.test/article", "title": "Backend",
                                          "content": "Backend content", "error": None}])
    _transport(monkeypatch, lambda request: pytest.fail("existing route must not do direct HTML"))
    entry = wt.registry.get_entry("web_extract")
    assert entry is not None
    args = {"urls": ["https://site.test/article"]}
    call = wt.web_extract_tool(args["urls"], format="html") if route == "non-markdown" else entry.handler(args)
    result = json.loads(asyncio.run(call))["results"][0]
    assert result["title"] == "Backend" and "fallback_from" not in result
    provider.extract.assert_called_once()


_CMP = (
    "We and our partners use personalised advertising and device identification. "
    "Our vendors rely on legitimate interest for audience insights. "
    "Manage choices about consent and cookie preferences in our privacy policy. "
    "We store and/or access information with partners for personalised advertising. "
) * 3


@pytest.mark.parametrize("prose", [_CMP, ("Our partners require consent. Consent to personalised advertising. " * 16)])
@pytest.mark.parametrize("marker", ["", ' class="cmp-overlay"', ' role="dialog"'])
def test_consent_wall_falls_back_even_without_markers(backend_harness, monkeypatch, marker, prose):
    _, sdk, _ = backend_harness
    html = f'<main{marker}><h1>We value your privacy</h1><p>{prose}</p><button>Accept all</button><button>Reject all</button></main>'
    _transport(monkeypatch, lambda request: httpx.Response(200, headers={"content-type": "text/html"}, text=html))
    result = _extract(["https://site.test/article"])[0]
    assert result["extract_backend"] == "exa"
    sdk.get_contents.assert_called_once()


@pytest.mark.parametrize("marker", ['id="onetrust-banner"', 'class="cookie-consent"', 'role="dialog"', 'role="alertdialog"'])
def test_consent_overlay_does_not_override_the_article(backend_harness, monkeypatch, marker):
    _, sdk, _ = backend_harness
    html = f'<main><div {marker}><p>{_CMP}</p><button>Accept all</button></div><article><h1>Ceramics</h1>{_ARTICLE}</article></main>'
    _transport(monkeypatch, lambda request: httpx.Response(200, headers={"content-type": "text/html"}, text=html))
    result = _extract(["https://site.test/article"])[0]
    assert result["extract_backend"] == "direct_http"
    assert "ceramic techniques" in result["content"] and "advertising" not in result["content"]
    sdk.get_contents.assert_not_called()


@pytest.mark.parametrize("marker", ["", ' id="cookies"', ' class="cookie-tutorial"'])
@pytest.mark.parametrize("title,prose", [
    ("Python Requests cookie tutorial", "Requests stores HTTP cookies in a cookie jar. Use a session to preserve cookies between requests. The cookie jar exposes domain and path attributes for each cookie. Inspect response cookies before sending the next request. "),
    ("How CAPTCHA systems work", "CAPTCHA systems distinguish automated requests from human visitors. A CAPTCHA may use images or interactive puzzles to make this distinction. Engineers must consider accessibility when choosing a challenge. The following examples explain how the verification process works. "),
])
def test_ambiguous_cookie_and_captcha_articles_use_paid_contents(backend_harness, monkeypatch, title, prose, marker):
    _, sdk, _ = backend_harness
    html = f'<html><head><title>{title}</title></head><main><h1>{title}</h1><section{marker}><p>{prose * 12}</p></section></main></html>'
    _transport(monkeypatch, lambda request: httpx.Response(200, headers={"content-type": "text/html"}, text=html))
    result = _extract(["https://site.test/article"])[0]
    assert result["extract_backend"] == "exa"
    assert result["fallback_from"] == "direct_http"
    sdk.get_contents.assert_called_once()


@pytest.mark.parametrize("kind,size", [("cookie", 390), ("paywall", 488), ("javascript", 2111)])
def test_sparse_wall_signals_still_fall_back(backend_harness, monkeypatch, kind, size):
    _, sdk, put = backend_harness
    if kind == "cookie":
        # Exactly one consent signal, surrounded by otherwise neutral boilerplate.
        text = ("We use cookies. " + "Choose the settings for this website before moving on. " * 8)[:size]
    elif kind == "paywall":
        teaser = "Ceramic materials change with temperature and careful preparation. " * 8
        text = teaser[:210] + "\n" + teaser[:210] + "\nSubscribe to continue reading."
        text += " More details are available in the complete edition."
        text = text[:size]
    else:
        text = ("Enable JavaScript to continue. Please turn on JavaScript in your browser to view this page. " * 30)[:size]
    assert len(text) == size
    html = "<main>" + "".join(f"<p>{part}</p>" for part in text.split("\n")) + "</main>"
    _transport(monkeypatch, lambda request: httpx.Response(200, headers={"content-type": "text/html"}, text=html))
    result = _extract(["https://site.test/article"])[0]
    assert result["extract_backend"] == "exa" and result["fallback_from"] == "direct_http"
    assert "challenge" not in result["direct_fetch_error"].lower()
    sdk.get_contents.assert_called_once()
    put.assert_not_called()


@pytest.mark.parametrize("signal", ["Consent", "Login", "Log in", "Sign in", "Subscribe", "Subscription", "Paywall"])
def test_one_short_wall_signal_is_enough(backend_harness, monkeypatch, signal):
    text = f"{signal}. " + "Choose the settings for this website before moving on. " * 8
    assert 300 <= len(text) < 1000
    _transport(monkeypatch, lambda request: httpx.Response(200, headers={"content-type": "text/html"}, text=f"<main><p>{text}</p></main>"))
    assert _extract(["https://site.test/article"])[0]["extract_backend"] == "exa"


@pytest.mark.parametrize("title,prose", [
    ("Implementing CAPTCHA", "A CAPTCHA implementation compares a visitor response with a stored answer. "
     "Render an accessible input and validate it on the server. Avoid storing the expected answer in the page. "
     "Rotate the challenge after each attempt and make the fallback usable without images. "
     "Record failed submissions to diagnose integration problems."),
    ("Testing a web form", "This testing guide describes browser automation for a form that mentions CAPTCHA. "
     "Use deterministic fixtures in the test environment. Assert that validation failures preserve user input. "
     "Cover keyboard navigation as well as pointer interactions. "
     "Keep external services out of the test suite to avoid unpredictable failures."),
])
@pytest.mark.parametrize("repeat", [1, 4])
def test_short_captcha_prose_is_not_a_challenge_error(backend_harness, monkeypatch, title, prose, repeat):
    html = f"<title>{title}</title><main><h1>{title}</h1><p>{prose * repeat}</p></main>"
    _transport(monkeypatch, lambda request: httpx.Response(200, headers={"content-type": "text/html"}, text=html))
    result = _extract(["https://site.test/article"])[0]
    assert result["extract_backend"] == "exa"
    assert "thin" in result["direct_fetch_error"].lower()


@pytest.mark.parametrize("notice", [
    "Enable JavaScript to continue.", "JavaScript is required to view this page.",
    "Please turn on JavaScript to continue.",
])
@pytest.mark.parametrize("noscript", [False, True])
def test_javascript_wall_at_any_length(backend_harness, monkeypatch, notice, noscript):
    text = (notice + " Your browser settings prevent this page from displaying its contents. ") * 35
    wall = f"<noscript>{text}</noscript>" if noscript else f"<p>{text}</p>"
    html = f"<main><p>{_BODY[:400]}</p>{wall}</main>"
    _transport(monkeypatch, lambda request: httpx.Response(200, headers={"content-type": "text/html"}, text=html))
    assert _extract(["https://site.test/article"])[0]["extract_backend"] == "exa"


def test_javascript_tutorial_with_small_noscript_notice_remains_readable(backend_harness, monkeypatch):
    html = f"<main>{_ARTICLE}</main><noscript>Interactive examples require scripting.</noscript>"
    _transport(monkeypatch, lambda request: httpx.Response(200, headers={"content-type": "text/html"}, text=html))
    assert _extract(["https://site.test/article"])[0]["extract_backend"] == "direct_http"
    backend_harness[1].get_contents.assert_not_called()


@pytest.mark.parametrize("marker", ['class="cf-chl-container"', 'src="/cdn-cgi/challenge-platform/abc"',
                                  'class="g-recaptcha"', 'class="h-captcha"'])
@pytest.mark.parametrize("link_padding", [False, True])
def test_challenge_widgets_with_little_prose_fall_back(backend_harness, monkeypatch, marker, link_padding):
    links = '<a href="https://site.test/' + "path/" * 400 + '">Help</a>' if link_padding else ""
    html = f'<main><p>{"Complete the browser verification before proceeding. " * 8}</p>{links}<div {marker}></div></main>'
    _transport(monkeypatch, lambda request: httpx.Response(200, headers={"content-type": "text/html"}, text=html))
    assert _extract(["https://site.test/article"])[0]["extract_backend"] == "exa"


@pytest.mark.parametrize("text", [
    *[("We use cookies. " + "Choose the settings for this website before moving on. " * 30)[:size]
      for size in (999, 1000, 1001)],
    *[(signal + ". " + "Choose the settings for this website before moving on. " * 30)[:1001]
      for signal in ("Subscribe", "Log in")],
    "Enable JavaScript to continue. " + "Your browser settings prevent this page from displaying its contents. " * 30,
    "Complete the browser verification before proceeding. " * 30,
    "The publisher asks visitors to choose their preferred settings before viewing the page. " * 30,
])
def test_reviewer_padded_and_repeated_walls_fall_back(backend_harness, monkeypatch, text):
    _, sdk, put = backend_harness
    html = f'<main><p>{text}</p><div class="g-recaptcha"></div></main>' if text.startswith("Complete") else f"<main><p>{text}</p></main>"
    _transport(monkeypatch, lambda request: httpx.Response(200, headers={"content-type": "text/html"}, text=html))
    result = _extract(["https://site.test/article"])[0]
    assert result["extract_backend"] == "exa" and result["fallback_from"] == "direct_http"
    assert "thin" in result["direct_fetch_error"].lower()
    sdk.get_contents.assert_called_once()
    put.assert_not_called()


@pytest.mark.parametrize("signal", [
    "Accept cookies", "Cookie settings", "We use cookies", "Cookie preferences", "Cookie-preference", "Consent settings",
    "Manage consent preferences", "Sign in to continue", "Log in to continue",
    "Subscribe to continue", "Subscribe for full access", "Paywall",
    "Enable JavaScript", "Turn on JavaScript", "JavaScript must be enabled",
    "Browser verification", "Verify you are human", "Checking your browser", "CAPTCHA",
])
@pytest.mark.parametrize("location", ["title", "body", "outside-main"])
def test_any_kept_wall_signal_overrides_substantive_article(backend_harness, monkeypatch, signal, location):
    html = f"<main>{_ARTICLE}</main>"
    html += f"<title>{signal}</title>" if location == "title" else f"<p>{signal}</p>" if location == "outside-main" else ""
    if location == "body":
        html = f"<main>{_ARTICLE}<p>{signal}</p></main>"
    _transport(monkeypatch, lambda request: httpx.Response(200, headers={"content-type": "text/html"}, text=html))
    assert _extract(["https://site.test/article"])[0]["extract_backend"] == "exa"


@pytest.mark.parametrize("marker", ["cf-chl", "challenge-platform", "g-recaptcha", "h-captcha", "turnstile"])
def test_challenge_marker_anywhere_overrides_full_article(backend_harness, monkeypatch, marker):
    html = f'<main>{_ARTICLE}</main><!-- {marker} -->'
    _transport(monkeypatch, lambda request: httpx.Response(200, headers={"content-type": "text/html"}, text=html))
    assert _extract(["https://site.test/article"])[0]["extract_backend"] == "exa"


@pytest.mark.parametrize("text", [
    f"<p>{(_PARAGRAPHS[0] + chr(32)) * 30}</p>",
    "".join(f"<p>{_PARAGRAPHS[0]}</p>" for _ in range(30)),
    "".join(f"<p>{' '.join(_PARAGRAPHS[index:] + _PARAGRAPHS[:index])}</p>" for index in range(3)),
])
def test_duplicate_blocks_and_sentences_do_not_supply_prose_evidence(backend_harness, monkeypatch, text):
    _transport(monkeypatch, lambda request: httpx.Response(200, headers={"content-type": "text/html"}, text=f"<main>{text}</main>"))
    assert _extract(["https://site.test/article"])[0]["extract_backend"] == "exa"


def test_ordinary_noscript_alternative_does_not_reduce_article_evidence(backend_harness, monkeypatch):
    html = f"<main>{_ARTICLE}<noscript><p>{_BODY * 30}</p></noscript></main>"
    _transport(monkeypatch, lambda request: httpx.Response(200, headers={"content-type": "text/html"}, text=html))
    assert _extract(["https://site.test/article"])[0]["extract_backend"] == "direct_http"


def test_deep_five_mib_html_finishes_within_direct_timeout(backend_harness, monkeypatch):
    from time import perf_counter

    prefix = '<main>' + '<div class="cookie-content">' * 252
    suffix = '</div>' * 252 + _ARTICLE + '</main>'
    html = prefix + 'x' * (5 * 1024 * 1024 - len(prefix) - len(suffix)) + suffix
    assert len(html) == 5 * 1024 * 1024
    _transport(monkeypatch, lambda request: httpx.Response(200, headers={"content-type": "text/html"}, text=html))
    started = perf_counter()
    result = _extract(["https://site.test/article"])[0]
    elapsed = perf_counter() - started
    assert elapsed < direct._TIMEOUT_SECONDS
    assert result["extract_backend"] == "exa" and result["fallback_from"] == "direct_http"
    print(f"deep 5 MiB depth 252: {elapsed:.6f}s (deadline {direct._TIMEOUT_SECONDS}s)")


@pytest.mark.parametrize("scope", ["body", "main", "article"])
@pytest.mark.parametrize("block", ["p", "li", "pre", "blockquote", "h2", "td", "dd"])
def test_substantive_blocks_in_primary_scope_succeed(backend_harness, monkeypatch, scope, block):
    html = f"<{scope}>" + "".join(f"<{block}>{paragraph}</{block}>" for paragraph in _PARAGRAPHS) + f"</{scope}>"
    _transport(monkeypatch, lambda request: httpx.Response(200, headers={"content-type": "text/html"}, text=html))
    assert _extract(["https://site.test/article"])[0]["extract_backend"] == "direct_http"


@pytest.mark.parametrize("blocks", [
    _PARAGRAPHS[:2],
    tuple(paragraph[:100] for paragraph in _PARAGRAPHS),
])
def test_both_substantive_block_count_and_distinct_length_are_required(backend_harness, monkeypatch, blocks):
    html = "<main>" + "".join(f"<p>{paragraph}</p>" for paragraph in blocks) + "</main>"
    _transport(monkeypatch, lambda request: httpx.Response(200, headers={"content-type": "text/html"}, text=html))
    assert _extract(["https://site.test/article"])[0]["extract_backend"] == "exa"


@pytest.mark.parametrize("container", [
    "script", "style", "template", "noscript", "nav", "header", "footer", "aside",
    "form", "button", "label", "select", "dialog", 'div role="dialog"',
    'div role="alertdialog"', 'div role="navigation"', 'div role="banner"',
    'div aria-hidden="true"', 'div class="cmp-overlay"', 'div id="onetrust-banner"',
])
def test_structural_chrome_cannot_supply_content_evidence(backend_harness, monkeypatch, container):
    tag = container.split()[0]
    html = f"<body><{container}>{_ARTICLE}</{tag}></body>"
    _transport(monkeypatch, lambda request: httpx.Response(200, headers={"content-type": "text/html"}, text=html))
    assert _extract(["https://site.test/article"])[0]["extract_backend"] == "exa"


_ACCESS_SIGNALS = (
    "Log in to keep reading.", "Members only.", "Create a free account to continue.",
    "You have reached your article limit.", "Keep reading", "Continue reading", "Read the full article",
    "Read the full story", "Member-only", "Members-only", "Register to continue", "Register to read",
    "Sign up to continue", "Sign up to read", "Free article remaining", "Free articles remaining",
    "You've reached your free article limit", "You've reached your story limit",
    "You’ve reached your free story limit", "Already a subscriber", "Become a member",
    "Unlock this article", "Unlock this story", "Unlock the full article", "Unlock the full story",
    "Subscriber-only", "Subscriber only", "Subscriber-exclusive", "Subscriber exclusive",
    "Start your free trial", "Paywall",
)
_AVAILABILITY_SIGNALS = (
    "Not available in your country", "Not available in your region", "Unavailable in your country",
    "Unavailable in your region", "Page not found", "404", "Page moved", "Page has moved",
    "Under maintenance", "Temporarily unavailable", "Your browser is not supported", "Unsupported browser",
    "Update your browser", "Confirm your age", "You must be 18", "You must be 21",
    "Open in app", "Open in the app", "Continue in app", "Continue in the app",
    "Something went wrong", "An error occurred",
)


def _assert_html_fallback(backend_harness, monkeypatch, html, *, status=200):
    _, sdk, put = backend_harness
    _transport(monkeypatch, lambda request: httpx.Response(status, headers={"content-type": "text/html"}, text=html))
    result = _extract(["https://site.test/article"])[0]
    assert result["title"] == "Paid contents" and result["content"] == "Full provider page"
    assert result["extract_backend"] == "exa" and result["fallback_from"] == "direct_http"
    assert result["direct_fetch_error"]
    sdk.get_contents.assert_called_once_with(["https://site.test/article"], text=True)
    put.assert_not_called()


@pytest.mark.parametrize("signal", _ACCESS_SIGNALS + _AVAILABILITY_SIGNALS)
def test_preview_and_availability_phrases_override_full_article(backend_harness, monkeypatch, signal):
    _assert_html_fallback(backend_harness, monkeypatch, f"<main>{_ARTICLE}<p>{signal}</p></main>")


@pytest.mark.parametrize("container", [
    "form", "button", 'div role="dialog"', "dialog", "aside", "footer", "header", "nav",
    'div aria-hidden="true"', 'div hidden', 'div style="display: none"', 'div class="cmp-overlay"',
    "noscript",
])
def test_removed_containers_do_not_hide_access_wall(backend_harness, monkeypatch, container):
    tag = container.split()[0]
    html = f"<main>{_ARTICLE}</main><{container}>Subscribe to <b>continue</b> reading.</{tag}>"
    _assert_html_fallback(backend_harness, monkeypatch, html)


@pytest.mark.parametrize("heading", [
    "Unsupported browser", "Not available in your region", "Confirm your age", "Page not found",
    "Page has moved", "Under maintenance", "Something went wrong", "Open in the app",
])
def test_distinct_boilerplate_pages_fall_back(backend_harness, monkeypatch, heading):
    # Synthetic long, distinct paragraphs meet the old evidence thresholds without being an article.
    paragraphs = (
        "The website cannot display the requested information at this time. Our service team has prepared "
        "this notice to explain why the destination is not being displayed on your device today.",
        "Please review the information in this message before trying to navigate to another destination. "
        "Your current session has not loaded the requested resource, and no content can be shown here.",
        "The support department can assist with questions about this message and alternative ways to proceed. "
        "You may return to the home screen or choose another destination from the available navigation. "
        "We appreciate your patience while we work to make the website accessible to all our visitors.",
    )
    html = f"<main><h1>{heading}</h1>" + "".join(f"<p>{text}</p>" for text in paragraphs) + "</main>"
    _assert_html_fallback(backend_harness, monkeypatch, html)


@pytest.mark.parametrize("value", [False, "false", "FALSE"])
@pytest.mark.parametrize("key", ["isAccessibleForFree", "ISACCESSIBLEFORFREE"])
def test_jsonld_publisher_paywall_falls_back(backend_harness, monkeypatch, key, value):
    metadata = json.dumps({"@graph": [{"@type": "NewsArticle", key: value}]})
    html = f'<script type="APPLICATION/LD+JSON">{metadata}</script><main>{_ARTICLE}</main>'
    _assert_html_fallback(backend_harness, monkeypatch, html)


@pytest.mark.parametrize("markup", [
    '<meta itemprop="isAccessibleForFree" content="false">',
    '<meta itemprop="ISACCESSIBLEFORFREE" content="FALSE">',
    '<input itemprop="isAccessibleForFree" value="false">',
    '<span itemprop="isAccessibleForFree">False</span>',
    '<span itemprop="isAccessibleForFree">fal<b>s</b>e</span>',
    '<span itemprop="isAccessibleForFree"><span itemprop="isAccessibleForFree">false</span></span>',
])
def test_microdata_publisher_paywall_falls_back(backend_harness, monkeypatch, markup):
    _assert_html_fallback(backend_harness, monkeypatch, f"{markup}<main>{_ARTICLE}</main>")


@pytest.mark.parametrize("metadata", [
    '{"isAccessibleForFree":',
    '{"padding":"' + "x" * (64 * 1024) + '","isAccessibleForFree":false}',
], ids=["malformed", "oversized"])
def test_unreadable_jsonld_falls_back_defensively(backend_harness, monkeypatch, metadata):
    html = f'<script type="application/ld+json">{metadata}</script><main>{_ARTICLE}</main>'
    _assert_html_fallback(backend_harness, monkeypatch, html)


@pytest.mark.parametrize("status", [199, 300, 404, 500])
def test_substantive_non_success_response_falls_back(backend_harness, monkeypatch, status):
    _assert_html_fallback(backend_harness, monkeypatch, f"<main>{_ARTICLE}</main>", status=status)


@pytest.mark.parametrize("tail", ["<footer>Subscribe to continue reading</footer>", "", '<div class="g-recaptcha"></div>'])
def test_over_cap_document_never_accepts_a_partial_article(backend_harness, monkeypatch, tail):
    from tools.web_extract_html import _MAX_HTML_CHARS

    html = f"<main>{_ARTICLE}</main>" + " " * _MAX_HTML_CHARS + tail
    _assert_html_fallback(backend_harness, monkeypatch, html)


@pytest.mark.parametrize("metadata", [
    '<script type="application/ld+json">{"isAccessibleForFree":true}</script>',
    '<script type="application/ld+json">{"isAccessibleForFree":"TRUE"}</script>',
    '<meta itemprop="isAccessibleForFree" content="true">',
    '<script>Subscribe to continue reading</script><style>/* Members only */</style><template>Paywall</template>',
])
def test_real_article_with_newsletter_and_free_metadata_succeeds(backend_harness, monkeypatch, metadata):
    html = (f"<title>Ceramics</title>{metadata}<main>{_ARTICLE}</main>"
            '<footer><form><button>Subscribe to our newsletter</button></form></footer>')
    _transport(monkeypatch, lambda request: httpx.Response(200, headers={"content-type": "text/html"}, text=html))
    result = _extract(["https://site.test/article"])[0]
    assert result["extract_backend"] == "direct_http"
    assert "ceramic techniques" in result["content"] and "newsletter" not in result["content"]
    backend_harness[1].get_contents.assert_not_called()
    backend_harness[2].assert_not_called()


_ONETRUST_BANNER = f'<div id="onetrust-banner-sdk"><p>{_CMP}</p><button>Accept cookies</button></div>'


@pytest.mark.parametrize("metadata", [{"@type": "NewsArticle"}, {"@type": "NewsArticle", "isAccessibleForFree": True}])
@pytest.mark.parametrize("cookie_ui", [
    "", _ONETRUST_BANNER,
    f'<div id="CybotCookiebotDialog" role="dialog"><p>{_CMP}</p><button>Accept cookies</button></div>',
    '<footer><button>Cookie Settings</button></footer>',
    *[f'<{container}><p>{_CMP}</p></{container.split()[0]}>' for container in (
        "form", "button", "nav", "header", "aside", 'div aria-hidden="true"',
    )],
], ids=["none", "onetrust", "cookiebot", "footer-settings", "form", "button", "nav", "header", "aside", "aria-hidden"])
def test_news_article_with_cookie_ui_stays_direct(backend_harness, monkeypatch, metadata, cookie_ui):
    html = (f'<html><head><title>Ceramics report</title><script type="application/ld+json">'
            f'{json.dumps(metadata)}</script></head><body>{cookie_ui}'
            f'<main><article><h1>Ceramics report</h1>{_ARTICLE}</article></main></body></html>')
    _transport(monkeypatch, lambda request: httpx.Response(200, headers={"content-type": "text/html"}, text=html))
    result = _extract(["https://site.test/article"])[0]
    assert result["extract_backend"] == "direct_http"
    assert "ceramic techniques" in result["content"]
    assert "advertising" not in result["content"] and "Cookie Settings" not in result["content"]
    backend_harness[1].get_contents.assert_not_called()
    backend_harness[2].assert_not_called()


def test_paywall_dialog_still_overrides_article_with_cookie_banner(backend_harness, monkeypatch):
    html = (f'<main>{_ARTICLE}</main>{_ONETRUST_BANNER}'
            '<div role="dialog">Subscribe to <b>continue</b> reading.</div>')
    _assert_html_fallback(backend_harness, monkeypatch, html)


def test_related_error_heading_accepts_cheap_false_rejection(backend_harness, monkeypatch):
    html = f"<main>{_ARTICLE}<h2>Related: page not found errors explained</h2></main>"
    _assert_html_fallback(backend_harness, monkeypatch, html)
