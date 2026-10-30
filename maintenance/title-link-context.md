# Link-informed session titles

## Required behavior

When the opening message contains a link, the background title upgrade starts immediately unless the shared self-hosted endpoint requires deferral. It reads the first `http(s)` link's page metadata and gives it to the auxiliary title model as labeled, untrusted input. The turn-start predicate never fetches metadata. If the model cannot support a title, the opening turn retries once after its reply with a bounded reply excerpt. The instant derived title and the main turn are unchanged. The page text never reaches the main agent, the system prompt, or conversation history.

- **First link only:** the first URL in text order, after control wrappers, the gateway `[Replying to: "..."]` pointer, and the @-reference attached-context footer are removed. Trailing prose punctuation and unbalanced brackets are trimmed. A Desktop paste preview takes precedence and suppresses the fetch.
- **What is read:** `og:title`, `twitter:title` or `<title>`, plus `og:description`, `description` or `twitter:description`, or the first meaningful line of `text/plain`. Parsing streams and stops at `</head>` or `<body>`. The result is capped at 500 characters.
- **Limits:** a 4-second total budget, at most 3 redirects, a 2 MB streamed read ceiling, HTTP 200 only, and HTML or plain text content types only.
- **Safety:** refuses private, loopback, link-local, CGNAT, reserved, and metadata addresses before the request and again at TCP connect, even when `security.allow_private_urls` is true. RFC 2544 `198.18.0.0/15` stays blocked unless explicitly declared in `security.fake_ip_ranges`. On a fake-IP TUN host, configure `security.fake_ip_ranges: ['198.18.0.0/15']` before using this path. Declared ranges are dialable as DNS answers, since the proxy resolves the real target. A literal URL host in any declared range stays blocked, as do loopback, RFC 1918, link-local, CGNAT, and metadata. It does not follow environment proxies or `.netrc` credentials and sends no cookies. It honors `security.website_blocklist`. It skips links with embedded credentials, credential-named query parameters, side-effecting path segments (login, magic, reset, verify, unsubscribe, invite, oauth, and similar), or action query keys (`code`, `otp`, `state`, and similar). A declared `security.fake_ip_ranges` block remains dialable, as it is for every other SSRF-guarded client.
- **Failure:** every refusal, error, or timeout titles from the typed text alone. Logs are debug-level and redact query strings.
- **Configuration:** `auxiliary.title_generation.link_context` (default `true`).
- **Prompt:** the link rule appears only when link context is present, so prompts without links are byte-identical to before.

## Provenance and ownership

- **Identity and status:** active `title-link-context` fork patch.
- **Source surfaces:** `agent/title_link_context.py`, `agent/title_generator.py` (`first_link_text`, `_title_link_context`, `build_title_input`, prompt link rule), `tools/url_safety.py` (optional per-call `allow_private` on `is_safe_url` and `create_ssrf_safe_client`, threaded to the connect-time backend), `hermes_cli/config_defaults.py`, and `website/docs/user-guide/configuration.md`.
- **Upstream:** Searched `NousResearch/hermes-agent` issues and PRs on 2026-09-26 for link- or URL-informed session titles. The audit subsequently created proposal [#124560](https://github.com/NousResearch/hermes-agent/issues/124560) for URL-informed titles under equivalent refusal rules. This is a proposal, not an accepted implementation. The `allow_private` override on `url_safety` is a small general primitive the contribution could reuse.
- **Coupling:** shares `agent/title_generator.py` with [Session titles](session-titles.md). Load both when changing title input or prompt assembly.

## Verification and retirement

Run `scripts/run_tests.sh tests/agent/test_title_link_context.py tests/agent/test_title_generator.py tests/tools/test_url_safety.py -q`. Sample the configured title model on a few real links with opaque URLs, such as a YouTube watch URL or an arXiv abstract. On a host behind a fake-IP TUN proxy, a resolved `198.18.0.0/15` answer is dialable only after `security.fake_ip_ranges: ['198.18.0.0/15']` is configured. Without that declaration both validation and TCP connect reject the answer.

Retire this patch when a released upstream baseline informs automatic titles from linked page metadata under equivalent refusal rules. Roll back by reverting the `title_link_context` module, the title-generator wiring, and the config default together. The `url_safety` `allow_private` parameter defaults to the previous behavior, so it can stay or be removed independently.
